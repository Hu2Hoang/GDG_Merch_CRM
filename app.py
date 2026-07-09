from flask import Flask, render_template, request, jsonify
import psycopg2
import psycopg2.extras
from datetime import datetime
import os
import uuid
import boto3
from botocore.config import Config

app = Flask(__name__)

# ─── R2 CONFIG (đọc từ env vars) ─────────────────────────────────────────────
R2_ACCOUNT_ID     = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY     = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY     = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET         = os.environ.get("R2_BUCKET", "gdg-cdn")
R2_PUBLIC_URL     = os.environ.get("R2_PUBLIC_URL", "https://r2.cdn.gdghanoi.com")  # custom domain
R2_UPLOAD_PREFIX  = "cdn/merch/io26/upload"

def get_r2_client():
    endpoint = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

DB_CONFIG = {
    "host": "161.118.238.235",
    "port": 5432,
    "database": "sit_merch",
    "user": "casaos",
    "password": "casaos"
}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


# ─── UPLOAD R2 ───────────────────────────────────────────────────────────────

ALLOWED_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "avif"}

@app.route("/api/upload", methods=["POST"])
def upload_image():
    """Upload ảnh lên Cloudflare R2, trả về public URL."""
    if not R2_ACCOUNT_ID or not R2_ACCESS_KEY or not R2_SECRET_KEY:
        return jsonify({"error": "R2 chưa được cấu hình (thiếu env vars)"}), 500

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Không có file"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"Định dạng không hỗ trợ: {ext}"}), 400

    # Tạo tên file unique để tránh trùng
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    key = f"{R2_UPLOAD_PREFIX}/{unique_name}"

    content_type_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp",
        "gif": "image/gif", "avif": "image/avif",
    }
    content_type = content_type_map.get(ext, "application/octet-stream")

    try:
        s3 = get_r2_client()
        s3.upload_fileobj(
            file,
            R2_BUCKET,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        public_url = f"{R2_PUBLIC_URL.rstrip('/')}/{key}"
        return jsonify({"url": public_url, "key": key})
    except Exception as e:
        app.logger.error(f"R2 upload error: {e}")
        return jsonify({"error": str(e)}), 500


# ─── PRODUCTS ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/products", methods=["GET"])
def list_products():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT p."Id", p."Name", p."Title", p."Type", p."Description",
               p."BasePrice", p."Currency", p."DiscountPercentage",
               p."IsActive", p."CampaignId", p."CreationTime", p."IsDeleted",
               (SELECT COUNT(*) FROM "ProductOptions" o WHERE o."ProductId" = p."Id") AS option_count,
               (SELECT COUNT(*) FROM "ProductImages"  i WHERE i."ProductId" = p."Id") AS image_count,
               (SELECT "ImageUrl" FROM "ProductImages" i WHERE i."ProductId" = p."Id" ORDER BY i."DisplayOrder" LIMIT 1) AS thumb
        FROM "Products" p
        WHERE p."IsDeleted" = false
        ORDER BY p."Id"
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/products/<int:pid>", methods=["GET"])
def get_product(pid):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute('SELECT * FROM "Products" WHERE "Id" = %s', (pid,))
    product = dict(cur.fetchone())

    cur.execute('SELECT * FROM "ProductOptions" WHERE "ProductId" = %s ORDER BY "DisplayOrder"', (pid,))
    product["options"] = [dict(r) for r in cur.fetchall()]

    cur.execute('SELECT * FROM "ProductImages" WHERE "ProductId" = %s ORDER BY "DisplayOrder"', (pid,))
    product["images"] = [dict(r) for r in cur.fetchall()]

    cur.close(); conn.close()
    return jsonify(product)


@app.route("/api/products", methods=["POST"])
def create_product():
    data = request.json
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO "Products" (
            "CampaignId","Name","Title","Type","Description",
            "BasePrice","Currency","DiscountPercentage","IsActive",
            "CreationTime","IsDeleted"
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false) RETURNING "Id"
    """, (
        data.get("CampaignId", 1), data["Name"], data.get("Title",""),
        data.get("Type",""), data.get("Description",""),
        data.get("BasePrice", 0), data.get("Currency","VND"),
        data.get("DiscountPercentage", 0), data.get("IsActive", True),
        datetime.utcnow()
    ))
    new_id = cur.fetchone()[0]

    # Insert options
    for idx, opt in enumerate(data.get("options", []), start=1):
        cur.execute("""
            INSERT INTO "ProductOptions"
            ("ProductId","OptionType","OptionValue","AdditionalPrice",
             "StockQuantity","MinOrderQuantity","MaxOrderQuantity","IsAvailable","DisplayOrder")
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (new_id, opt["OptionType"], opt["OptionValue"],
              opt.get("AdditionalPrice", 0), opt.get("StockQuantity", 1000),
              opt.get("MinOrderQuantity", 1), opt.get("MaxOrderQuantity", 10),
              opt.get("IsAvailable", True), idx))

    # Insert images
    for idx, img in enumerate(data.get("images", []), start=1):
        cur.execute("""
            INSERT INTO "ProductImages" ("ProductId","ImageUrl","ImageType","DisplayOrder")
            VALUES (%s,%s,%s,%s)
        """, (new_id, img["ImageUrl"], img.get("ImageType","gallery"), idx))

    conn.commit(); cur.close(); conn.close()
    return jsonify({"id": new_id}), 201


@app.route("/api/products/<int:pid>", methods=["PUT"])
def update_product(pid):
    data = request.json
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE "Products" SET
            "CampaignId"=%s,"Name"=%s,"Title"=%s,"Type"=%s,"Description"=%s,
            "BasePrice"=%s,"Currency"=%s,"DiscountPercentage"=%s,"IsActive"=%s,
            "LastModificationTime"=%s
        WHERE "Id"=%s
    """, (
        data.get("CampaignId", 1), data["Name"], data.get("Title",""),
        data.get("Type",""), data.get("Description",""),
        data.get("BasePrice",0), data.get("Currency","VND"),
        data.get("DiscountPercentage",0), data.get("IsActive",True),
        datetime.utcnow(), pid
    ))

    # Rebuild options
    # Bước 1: Nullify CartItems đang tham chiếu đến các option của product này
    #         để tránh lỗi FK constraint khi xóa
    cur.execute("""
        UPDATE "CartItems" SET "ProductOptionId" = NULL
        WHERE "ProductOptionId" IN (
            SELECT "Id" FROM "ProductOptions" WHERE "ProductId" = %s
        )
    """, (pid,))

    # Bước 2: Xóa options cũ rồi insert lại
    cur.execute('DELETE FROM "ProductOptions" WHERE "ProductId"=%s', (pid,))
    for idx, opt in enumerate(data.get("options", []), start=1):
        cur.execute("""
            INSERT INTO "ProductOptions"
            ("ProductId","OptionType","OptionValue","AdditionalPrice",
             "StockQuantity","MinOrderQuantity","MaxOrderQuantity","IsAvailable","DisplayOrder")
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (pid, opt["OptionType"], opt["OptionValue"],
              opt.get("AdditionalPrice", 0), opt.get("StockQuantity", 1000),
              opt.get("MinOrderQuantity", 1), opt.get("MaxOrderQuantity", 10),
              opt.get("IsAvailable", True), idx))

    # Rebuild images
    cur.execute('DELETE FROM "ProductImages" WHERE "ProductId"=%s', (pid,))
    for idx, img in enumerate(data.get("images", []), start=1):
        cur.execute("""
            INSERT INTO "ProductImages" ("ProductId","ImageUrl","ImageType","DisplayOrder")
            VALUES (%s,%s,%s,%s)
        """, (pid, img["ImageUrl"], img.get("ImageType", "gallery"), idx))

    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/products/<int:pid>", methods=["DELETE"])
def delete_product(pid):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE "Products" SET "IsDeleted"=true, "DeletionTime"=%s
        WHERE "Id"=%s
    """, (datetime.utcnow(), pid))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5050)
