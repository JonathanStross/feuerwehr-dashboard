FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Berlin \
    DATA_DIR=/data \
    WEB_DIR=/app/web

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

VOLUME /data
# Das Kommando wird je Service in docker-compose.yml gesetzt (dashboard: Port 8080, maintenance: Port 8081)
CMD ["uvicorn", "app.dashboard.main:app", "--host", "0.0.0.0", "--port", "8080"]
