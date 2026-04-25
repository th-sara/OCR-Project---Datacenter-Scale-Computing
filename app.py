import os
import uuid
import json
import logging
import time

import boto3
import pika
import psycopg2
import redis
from flask import Flask, request, jsonify
from botocore.client import Config

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MINIO_ENDPOINT  = os.getenv("MINIO_ENDPOINT",  "http://minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET    = os.getenv("MINIO_BUCKET",    "documents")

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
QUEUE_NAME   = "ocr_jobs"

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_NAME = os.getenv("DB_NAME", "ocrdb")
DB_USER = os.getenv("DB_USER", "ocruser")
DB_PASS = os.getenv("DB_PASS", "ocrpass")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "tiff", "pdf"}


def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def get_db():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


def get_mq_channel():
    params = pika.URLParameters(RABBITMQ_URL)
    conn = pika.BlockingConnection(params)
    ch = conn.channel()
    ch.queue_declare(queue=QUEUE_NAME, durable=True)
    return conn, ch


def ensure_bucket():
    s3 = get_s3()
    try:
        s3.head_bucket(Bucket=MINIO_BUCKET)
    except Exception:
        s3.create_bucket(Bucket=MINIO_BUCKET)


def allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file field"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    if not allowed(f.filename):
        return jsonify({"error": f"File type not supported. Use: {ALLOWED_EXTENSIONS}"}), 400

    job_id  = str(uuid.uuid4())
    ext     = f.filename.rsplit(".", 1)[1].lower()
    fpath   = f"{job_id}.{ext}"

    # 1. Store file in MinIO
    try:
        ensure_bucket()
        get_s3().upload_fileobj(f, MINIO_BUCKET, fpath)
        logger.info(f"[{job_id}] Stored in MinIO: {fpath}")
    except Exception as e:
        logger.error(f"[{job_id}] MinIO error: {e}")
        return jsonify({"error": "Storage failed"}), 500

    # 2. Insert job record
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO jobs (id, file_path, status) VALUES (%s, %s, 'pending')",
            (job_id, fpath),
        )
        conn.commit(); cur.close(); conn.close()
        logger.info(f"[{job_id}] DB record created")
    except Exception as e:
        logger.error(f"[{job_id}] DB insert error: {e}")
        return jsonify({"error": "DB insert failed"}), 500

    # 3. Publish to queue
    try:
        mq_conn, ch = get_mq_channel()
        ch.basic_publish(
            exchange="",
            routing_key=QUEUE_NAME,
            body=json.dumps({"job_id": job_id, "file_path": fpath}),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        mq_conn.close()
        logger.info(f"[{job_id}] Published to RabbitMQ")
    except Exception as e:
        logger.error(f"[{job_id}] RabbitMQ error: {e}")
        return jsonify({"error": "Queue publish failed"}), 500

    return jsonify({"job_id": job_id, "status": "pending"}), 202


@app.route("/result/<job_id>")
def result(job_id):
    # Redis cache
    try:
        cached = get_redis().get(f"result:{job_id}")
        if cached:
            return jsonify(json.loads(cached))
    except Exception:
        pass

    # DB lookup
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, file_path, status, extracted_text, confidence, submitted_at "
            "FROM jobs WHERE id = %s",
            (job_id,),
        )
        row = cur.fetchone(); cur.close(); conn.close()
    except Exception as e:
        logger.error(f"[{job_id}] DB query error: {e}")
        return jsonify({"error": "DB query failed"}), 500

    if not row:
        return jsonify({"error": "Job not found"}), 404

    resp = {
        "job_id":         row[0],
        "file_path":      row[1],
        "status":         row[2],
        "extracted_text": row[3],
        "confidence":     row[4],
        "submitted_at":   str(row[5]),
    }

    # Cache completed results for 1 hour
    if resp["status"] == "done":
        try:
            get_redis().setex(f"result:{job_id}", 3600, json.dumps(resp))
        except Exception:
            pass

    return jsonify(resp)


@app.route("/jobs")
def list_jobs():
    """List recent jobs — useful for the demo."""
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, file_path, status, confidence, submitted_at "
            "FROM jobs ORDER BY submitted_at DESC LIMIT 20"
        )
        rows = cur.fetchall(); cur.close(); conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify([
        {"job_id": r[0], "file_path": r[1], "status": r[2],
         "confidence": r[3], "submitted_at": str(r[4])}
        for r in rows
    ])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
