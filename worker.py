import os
import io
import json
import logging
import time

import boto3
import pika
import psycopg2
import pytesseract
from PIL import Image, ImageEnhance
from botocore.client import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MINIO_ENDPOINT   = os.getenv("MINIO_ENDPOINT",   "http://minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY",  "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY",  "minioadmin")
MINIO_BUCKET     = os.getenv("MINIO_BUCKET",      "documents")

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
QUEUE_NAME   = "ocr_jobs"

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_NAME = os.getenv("DB_NAME", "ocrdb")
DB_USER = os.getenv("DB_USER", "ocruser")
DB_PASS = os.getenv("DB_PASS", "ocrpass")


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


def set_status(job_id, status):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE jobs SET status=%s WHERE id=%s", (status, job_id))
    conn.commit(); cur.close(); conn.close()


def preprocess(img: Image.Image) -> Image.Image:
    """Grayscale + contrast boost — meaningfully improves Tesseract accuracy."""
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    return img


def ocr_image(img: Image.Image):
    processed = preprocess(img)
    text = pytesseract.image_to_string(processed)
    data = pytesseract.image_to_data(processed, output_type=pytesseract.Output.DICT)
    confs = [c for c in data["conf"] if c != -1]
    confidence = sum(confs) / len(confs) if confs else 0.0
    return text, confidence


def process_job(job_id: str, file_path: str):
    logger.info(f"[{job_id}] Processing started")
    set_status(job_id, "processing")

    # Download from MinIO
    buf = io.BytesIO()
    get_s3().download_fileobj(MINIO_BUCKET, file_path, buf)
    buf.seek(0)
    logger.info(f"[{job_id}] File downloaded from MinIO ({buf.getbuffer().nbytes} bytes)")

    ext = file_path.rsplit(".", 1)[-1].lower()
    pages = []

    if ext == "pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(stream=buf.read(), filetype="pdf")
        for i in range(len(doc)):
            pix = doc.load_page(i).get_pixmap(dpi=200)
            pages.append(Image.open(io.BytesIO(pix.tobytes("png"))))
        logger.info(f"[{job_id}] PDF → {len(pages)} page(s)")
    else:
        pages.append(Image.open(buf))

    texts, confidences = [], []
    for i, img in enumerate(pages):
        t, c = ocr_image(img)
        texts.append(t)
        confidences.append(c)
        logger.info(f"[{job_id}] Page {i+1}: confidence={c:.1f}%")

    full_text    = "\n\n--- PAGE BREAK ---\n\n".join(texts)
    avg_conf     = sum(confidences) / len(confidences)

    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE jobs SET status='done', extracted_text=%s, confidence=%s WHERE id=%s",
        (full_text, avg_conf, job_id),
    )
    conn.commit(); cur.close(); conn.close()
    logger.info(f"[{job_id}] Done. avg_confidence={avg_conf:.1f}%")


def on_message(channel, method, _props, body):
    msg    = None
    job_id = "unknown"
    try:
        msg    = json.loads(body)
        job_id = msg["job_id"]
        process_job(job_id, msg["file_path"])
        channel.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        logger.error(f"[{job_id}] Job failed: {e}")
        if msg:
            try:
                conn = get_db()
                cur  = conn.cursor()
                cur.execute("UPDATE jobs SET status='failed' WHERE id=%s", (job_id,))
                conn.commit(); cur.close(); conn.close()
            except Exception:
                pass
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def connect_rabbitmq(retries=15, delay=5):
    for attempt in range(retries):
        try:
            params = pika.URLParameters(RABBITMQ_URL)
            conn   = pika.BlockingConnection(params)
            ch     = conn.channel()
            ch.queue_declare(queue=QUEUE_NAME, durable=True)
            ch.basic_qos(prefetch_count=1)
            logger.info("Connected to RabbitMQ ✓")
            return conn, ch
        except Exception as e:
            logger.warning(f"RabbitMQ not ready ({attempt+1}/{retries}): {e}")
            time.sleep(delay)
    raise RuntimeError("Could not connect to RabbitMQ")


if __name__ == "__main__":
    _, channel = connect_rabbitmq()
    channel.basic_consume(queue=QUEUE_NAME, on_message_callback=on_message)
    logger.info("Worker ready — waiting for jobs...")
    channel.start_consuming()
