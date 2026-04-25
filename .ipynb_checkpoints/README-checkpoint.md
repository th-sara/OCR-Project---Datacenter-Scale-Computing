# Scalable Document OCR Processing Service

**Author:** Sara Daneshvar  
**Course:** Datacenter Scale Computing

A cloud-native OCR service that accepts image/PDF uploads via REST API and returns
extracted text asynchronously, using RabbitMQ, MinIO, PostgreSQL, Redis, and Tesseract.
all containerized and deployed on Google Cloud Platform.

---

## Architecture

```
User
 │
 ▼
Flask API ──► MinIO (file storage)
     │
     ▼
 RabbitMQ ──► OCR Worker ──► Tesseract
                    │
                    ▼
               PostgreSQL ◄── Redis (cache)
                    ▲
                    │
               Flask API (GET /result)
```

---

## Quick Start (GCP — Full Setup from Zero)

### Step 1 — Create GCP VM

```bash
# In Google Cloud Shell or your local gcloud CLI

gcloud compute instances create ocr-service \
  --project=YOUR_PROJECT_ID \
  --zone=us-central1-a \
  --machine-type=e2-standard-2 \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=30GB \
  --tags=http-server,https-server

# Open port 5000 for the API
gcloud compute firewall-rules create allow-ocr-api \
  --allow tcp:5000 \
  --target-tags=http-server \
  --description="OCR Service API"

# Also open management UIs (optional, for demo)
gcloud compute firewall-rules create allow-ocr-mgmt \
  --allow tcp:15672,tcp:9001 \
  --target-tags=http-server \
  --description="RabbitMQ + MinIO consoles"
```

### Step 2 — SSH into VM and install Docker

```bash
# SSH in
gcloud compute ssh ocr-service --zone=us-central1-a

# Install Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker

# Install Docker Compose plugin
sudo apt-get install -y docker-compose-plugin

# Verify
docker --version
docker compose version
```

### Step 3 — Clone repo and configure

```bash
git clone https://github.com/YOUR_USERNAME/ocr-service.git
cd ocr-service

# Create .env from example
cp .env.example .env
# (optional) edit passwords:
# nano .env
```

### Step 4 — Build and launch everything

```bash
docker compose up --build -d

# Watch logs (all services)
docker compose logs -f

# Watch just the worker
docker compose logs -f worker
```

First build takes ~3–5 minutes (Tesseract install). Subsequent starts are instant.

### Step 5 — Verify it's running

```bash
# Health check
curl http://localhost:5000/health

# From outside (replace with your VM's external IP)
EXTERNAL_IP=$(curl -s ifconfig.me)
curl http://$EXTERNAL_IP:5000/health
```

---

## API Usage

### Upload a document

```bash
# Upload an image
curl -X POST http://localhost:5000/upload \
  -F "file=@/path/to/your/document.png"

# Upload a PDF
curl -X POST http://localhost:5000/upload \
  -F "file=@/path/to/your/document.pdf"
```

Response:
```json
{"job_id": "3f2a1b...", "status": "pending"}
```

### Poll for result

```bash
curl http://localhost:5000/result/3f2a1b...
```

Response (when done):
```json
{
  "job_id": "3f2a1b...",
  "status": "done",
  "extracted_text": "Hello World...",
  "confidence": 91.4,
  "submitted_at": "2025-04-24 21:00:00+00:00"
}
```

### List recent jobs

```bash
curl http://localhost:5000/jobs
```

---

## Load Testing

```bash
# Install dependencies
pip install requests pillow

# Generate a test image
python load_test/make_test_image.py

# Run load test: 10 concurrent uploads
python load_test/load_test.py --file load_test/test.png --n 10 --workers 5

# Scale to 3 OCR workers and re-test
docker compose up --scale worker=3 -d
python load_test/load_test.py --file load_test/test.png --n 20 --workers 10
```

---

## Fault Tolerance Demo

```bash
# Start a job, kill the worker mid-process, watch RabbitMQ requeue it
JOB=$(curl -s -X POST http://localhost:5000/upload -F "file=@load_test/test.png" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "Job: $JOB"

# Kill the worker container
docker compose stop worker

# Check job is still pending/processing (not lost)
curl http://localhost:5000/result/$JOB

# Restart worker — it will pick up the requeued message
docker compose start worker
curl http://localhost:5000/result/$JOB
```

---

## Scale Workers

```bash
# Run 3 parallel OCR workers
docker compose up --scale worker=3 -d

# Check all containers
docker compose ps
```

---

## Management UIs

| Service    | URL                          | Login              |
|------------|------------------------------|--------------------|
| RabbitMQ   | http://VM_IP:15672           | ocruser / ocrpass123 |
| MinIO      | http://VM_IP:9001            | minioadmin / minioadmin123 |

---

## View Logs

```bash
# All services
docker compose logs -f

# Single service
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f rabbitmq

# Search for a specific job ID across all logs
docker compose logs | grep "YOUR_JOB_ID"
```

---

## Stop / Cleanup

```bash
# Stop all containers (data preserved)
docker compose down

# Stop and delete all volumes (full reset)
docker compose down -v
```

---

## Components

| Component  | Technology            | Port  |
|------------|-----------------------|-------|
| REST API   | Flask (Python)        | 5000  |
| Queue      | RabbitMQ 3.13         | 5672  |
| Storage    | MinIO (S3-compatible) | 9000  |
| Database   | PostgreSQL 16         | 5432  |
| Cache      | Redis 7               | 6379  |
| OCR Engine | Tesseract + PyMuPDF   | —     |

---


