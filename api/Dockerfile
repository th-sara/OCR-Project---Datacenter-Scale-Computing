FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    flask \
    boto3 \
    pika \
    psycopg2-binary \
    redis

COPY app.py .

EXPOSE 5000

CMD ["python", "-u", "app.py"]
