FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent SQLite cache lives in /data when running in Docker.
ENV TRACKING_DB_PATH=/data/tracking.db
RUN mkdir -p /data

EXPOSE 5400

CMD ["gunicorn", "--bind", "0.0.0.0:5400", "--timeout", "600", \
     "--worker-class", "gthread", "--workers", "1", "--threads", "16", \
     "app:app"]
