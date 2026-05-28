FROM python:3.11-slim

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl cron rsyslog tesseract-ocr \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Python 依赖
RUN pip install --no-cache-dir fastapi uvicorn jinja2 aiofiles itsdangerous python-multipart Pillow -q

WORKDIR /app

# 复制项目文件
COPY heute_api.py .
COPY heute_db.py .
COPY heute_sdk.py .
COPY heute_express_sync.py .
COPY sync_to_dashboard.py .
COPY heute_cli.py .
COPY scan_anomalies.py .
COPY batch_track_hybrid.py .
COPY heute_track_api.py .
COPY dashboard/app.py ./dashboard/app.py
COPY dashboard/idcard_upload.py ./dashboard/idcard_upload.py
COPY dashboard/templates/index.html ./dashboard/templates/

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8890

ENTRYPOINT ["/entrypoint.sh"]
