FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent DB volume mounted at /data
ENV EMCS_DB_PATH=/data/emcs.db
RUN mkdir -p /data

EXPOSE 5500

CMD ["gunicorn", "--bind", "0.0.0.0:5500", "--timeout", "120", \
     "--worker-class", "gthread", "--workers", "1", "--threads", "8", \
     "app:app"]
