FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    boto3 \
    pika \
    psycopg2-binary \
    pytesseract \
    pillow \
    pymupdf \
    numpy

COPY worker.py .

CMD ["python", "-u", "worker.py"]
