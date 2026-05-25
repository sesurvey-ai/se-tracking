FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DEBT_DB_PATH=/data/debt.db
RUN mkdir -p /data

EXPOSE 5600

CMD ["gunicorn", "--bind", "0.0.0.0:5600", "--timeout", "120", \
     "--worker-class", "gthread", "--workers", "1", "--threads", "8", \
     "app:app"]
