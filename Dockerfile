FROM python:3.11-slim

# Cài thư viện hệ thống cần thiết cho psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements trước để tận dụng Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ source code
COPY . .

# PORT mặc định nếu không có env var
ENV PORT=8000

EXPOSE $PORT

CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
