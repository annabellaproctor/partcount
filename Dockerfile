FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# cache pip layer separately from app code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ensure static dirs exist even if empty
RUN mkdir -p /app/app/static/css /app/app/static/js /app/app/static/img /app/images

# --proxy-headers: trust the reverse proxy's X-Forwarded-Proto so redirects are
# built as https, not http (which a browser blocks as mixed content).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--proxy-headers", "--forwarded-allow-ips=*"]
