FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DB_PATH=/data/hotel.db
CMD ["sh","-c","mkdir -p /data/uploads && rm -rf uploads && ln -s /data/uploads uploads && uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
