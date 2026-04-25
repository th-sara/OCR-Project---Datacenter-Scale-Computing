# Scalable Document OCR Processing Service with Asynchronous Job Queue and Cloud Storage

**Author:** Sara Daneshvar  
**Course:** Datacenter Scale Computing  
**Project Type:** Individual Project

---

## 1. Project Goals

The goal of this project was to build a self-hosted, cloud-native OCR service that accepts image files and PDF documents as input and returns extracted text asynchronously. The service is designed as a realistic alternative to proprietary APIs such as Google Cloud Vision and Amazon Textract, using only open-source components deployed on Google Cloud Platform.

### What Was Accomplished

The final system is a fully operational, containerized OCR pipeline that:

- Accepts image uploads (PNG, JPEG, TIFF, BMP, GIF) and multi-page PDFs via a REST API
- Stores raw files in MinIO S3-compatible object storage
- Queues processing jobs asynchronously through RabbitMQ so the API never blocks
- Runs stateless OCR workers using Tesseract with Pillow image preprocessing
- Persists job metadata and results in PostgreSQL with Redis caching for repeated polling
- Deploys entirely via a single `docker compose up --build` command on a GCP Compute Engine VM
- Achieves < 10% character error rate on clean scanned documents
- Successfully handles 10+ concurrent upload requests without dropping jobs
- Demonstrates fault tolerance: if a worker dies mid-job, RabbitMQ requeues the message and another worker completes it

---

## 2. Software and Hardware Components

### Hardware Platform
**Google Cloud Platform — Compute Engine e2-standard-2**  
2 vCPUs, 8 GB RAM, 30 GB boot disk, Debian 12. This instance type provides enough memory to run all six service containers simultaneously while leaving headroom for concurrent OCR processing.

### Software Components

#### 2.1 REST API — Flask (Python)

**Purpose:** The user-facing interface. Exposes two primary endpoints:
- `POST /upload` — accepts a multipart file, stores it in MinIO, inserts a job record in PostgreSQL, publishes a message to RabbitMQ, and returns a job ID with HTTP 202 immediately.
- `GET /result/<job_id>` — queries Redis first, then PostgreSQL, and returns the current job status, extracted text, and Tesseract confidence score.

**Why Flask:** Lightweight, well-documented, and straightforward to containerize. The alternative was FastAPI, which offers async I/O but adds complexity without material benefit here since the API's work per request is minimal (storage write + queue publish + DB insert).

**Advantages:** Simple, battle-tested, easy to debug with standard HTTP tools. Minimal boilerplate.  
**Disadvantages:** Synchronous by default; not ideal for very high-throughput HTTP workloads. For this project's scale, it is more than sufficient.

**Interactions:** Writes files to MinIO via boto3, inserts/queries rows in PostgreSQL via psycopg2, publishes messages to RabbitMQ via pika, reads/writes to Redis via the redis-py client.

---

#### 2.2 Message Queue — RabbitMQ 3.13

**Purpose:** Decouples the upload API from the OCR workers. When a file is uploaded, the API publishes a lightweight JSON message (job ID + MinIO file path, ~200 bytes) to a durable queue. Workers consume one message at a time using `basic_qos(prefetch_count=1)`.

**Why RabbitMQ:** Mature, well-documented, native Docker image available, and supports exactly the delivery guarantees needed: durable queues, persistent messages, and consumer acknowledgments. The management UI (port 15672) makes it easy to inspect queue depth live during load tests.

**Advantages:** Proven reliability, automatic message requeue on worker crash, management UI for demos.  
**Disadvantages:** Adds infrastructure complexity. For very simple use cases, a database-backed queue (e.g., PostgreSQL LISTEN/NOTIFY) would suffice, but RabbitMQ handles backpressure and redelivery more gracefully under load.

**Interactions:** Receives messages from the Flask API. Delivers messages to OCR workers. Requeues messages if a worker connection closes before acknowledgment (primary fault tolerance mechanism).

---

#### 2.3 OCR Workers — Python + Tesseract + Pillow + PyMuPDF

**Purpose:** Stateless Docker containers that consume one job at a time, download the file from MinIO, preprocess it, run Tesseract, and write results back to PostgreSQL.

**Image Preprocessing (Pillow):** Before OCR, each image is converted to grayscale and contrast-enhanced (2× multiplier using `ImageEnhance.Contrast`). This step was found to meaningfully reduce character error rate on faded or low-contrast scans, at negligible added latency.

**PDF Handling (PyMuPDF / fitz):** Multi-page PDFs are rasterized at 200 DPI per page. Each page is OCR'd independently and results are concatenated with page-break markers.

**Why Tesseract:** Mature, well-documented, no training or fine-tuning required, supports 100+ languages, and produces a per-word confidence score accessible via `image_to_data`.

**Advantages:** Fully open-source, easy to install via apt, confidence scoring is built in, scales horizontally by adding worker replicas.  
**Disadvantages:** Tesseract struggles with handwriting and very low-quality scans. Cloud Vision or Textract would outperform it on degraded documents, but the preprocessing pipeline closes much of that gap for typical office documents.

**Interactions:** Connects to RabbitMQ (consumes jobs), MinIO (downloads files), PostgreSQL (writes results). Has no direct communication with the API or other workers.

---

#### 2.4 Object Storage — MinIO (S3-Compatible)

**Purpose:** Stores the raw uploaded files outside of the queue and outside worker memory. Workers retrieve files by path using the standard S3 API.

**Why MinIO:** Drop-in S3-compatible API means the same boto3 code works locally and would work against AWS S3 in production. Runs as a single Docker container with no configuration beyond credentials. The console UI (port 9001) provides a file browser useful for demos.

**Advantages:** S3-compatible (portable), persistent Docker volume, web console for inspection.  
**Disadvantages:** Single-node MinIO has no built-in replication. For production, you would use multi-node MinIO or actual S3.

**Interactions:** Receives file uploads from the Flask API. Serves file downloads to OCR workers. Files are referenced by path in RabbitMQ messages.

---

#### 2.5 Metadata Database — PostgreSQL 16

**Purpose:** The only component that maintains persistent state visible to both the API and the workers. Stores one row per job with: job ID (primary key), file path, status (pending / processing / done / failed), extracted text, Tesseract confidence score, and submission timestamp.

**Why PostgreSQL:** The rubric requires a relational database and PostgreSQL is the obvious choice — well-understood, reliable, and trivial to run in Docker. The schema is a single table with simple primary-key lookups, so there is no risk of lock contention at this scale.

**Advantages:** ACID guarantees, straightforward schema, no sharding needed for project-scale load.  
**Disadvantages:** Not suited for storing very large text blobs (millions of rows of full document text) without partitioning. At the scale of this project this is not a concern.

**Interactions:** Written to by both the API (job creation) and workers (result update). Read by the API on `GET /result`. Both access patterns are primary-key lookups.

---

#### 2.6 Result Cache — Redis 7

**Purpose:** Caches `GET /result` responses for completed jobs for 1 hour, so repeated polling by a user does not hit PostgreSQL every time. Particularly useful during demos where the same job is polled many times.

**Why Redis:** In-memory key-value store, trivially added as a Docker service, and the `setex` command makes TTL-based caching a two-line addition.

**Advantages:** Near-zero latency for cache hits, reduces PostgreSQL read load under concurrent polling.  
**Disadvantages:** Cache invalidation edge cases if a job is manually corrected in the DB. Acceptable for this project since jobs are write-once once completed.

**Interactions:** Written by the API when a job transitions to `done`. Read by the API on every `GET /result` request before touching the database.

---

#### 2.7 Deployment — Docker Compose on GCP Compute Engine

**Purpose:** All six services run as Docker containers orchestrated by a single `docker-compose.yml`. The VM is a GCP e2-standard-2 instance running Debian 12.

**Why Docker Compose:** Satisfies the single-command deployment requirement. Each service is independently restartable. Worker replicas can be added with `--scale worker=N` without touching any configuration.

**Advantages:** Fully reproducible, portable, easy to scale workers, all services share a private Docker network with no exposed ports except 5000, 15672, and 9001.  
**Disadvantages:** Not a production orchestrator (no auto-healing, no rolling updates). Kubernetes would be appropriate at larger scale, but is out of scope for a one-semester project.

---

## 3. Architectural Diagram

```
┌────────────────────────────────────────────────────────────────────┐
│  GCP Compute Engine VM (Docker Compose network)                    │
│                                                                    │
│  ┌──────────┐  (1) POST /upload     ┌──────────┐                  │
│  │  User    │──────────────────────►│ Flask    │                  │
│  │          │  (9) GET /result      │  API     │                  │
│  └──────────┘◄──────────────────────└────┬─────┘                  │
│                                          │ (2) upload file        │
│                                     ┌────▼─────┐                  │
│                                     │  MinIO   │◄──(6) download   │
│                                     │ (S3)     │                  │
│                                     └──────────┘                  │
│                                          │ (3) publish job_id     │
│                                     ┌────▼─────┐                  │
│                                     │ RabbitMQ │                  │
│                                     │  Queue   │                  │
│                                     └────┬─────┘                  │
│                                          │ (5) consume message    │
│                                     ┌────▼─────┐                  │
│                                     │  OCR     │──(7)──►Tesseract │
│                                     │  Worker  │                  │
│                                     └────┬─────┘                  │
│                                          │ (8) write result       │
│                                     ┌────▼─────┐                  │
│                  (4) insert job ───►│ Postgres │                  │
│                  (10) query result  └────┬─────┘                  │
│                                          │                        │
│                                     ┌────▼─────┐                  │
│                                     │  Redis   │ (cache layer)    │
│                                     └──────────┘                  │
└────────────────────────────────────────────────────────────────────┘
```

---

## 4. Component Interactions

### 4.1 Upload Flow (Steps 1–4)
When a user POSTs a file to `/upload`, the API immediately: (1) writes the file to MinIO, (2) inserts a `pending` job record in PostgreSQL, and (3) publishes a JSON message to RabbitMQ. The API then returns HTTP 202 with the job ID. The entire upload-side path completes in under one second for typical documents regardless of OCR workload.

### 4.2 Processing Flow (Steps 5–8)
An available worker picks up the next message from RabbitMQ (`basic_qos prefetch_count=1` ensures one job per worker at a time). It downloads the file from MinIO, preprocesses with Pillow, runs Tesseract, and writes the extracted text and confidence score to PostgreSQL. It then sends a RabbitMQ acknowledgment. If the worker crashes before acknowledging, RabbitMQ requeues the message automatically.

### 4.3 Result Polling (Steps 9–10)
The user polls `GET /result/<job_id>`. The API checks Redis first; on a cache miss it queries PostgreSQL. Completed results are cached in Redis for one hour. No computation happens at query time.

### 4.4 Why Files Don't Go Through the Queue
RabbitMQ is designed for small messages. Passing a 10 MB PDF through the broker would consume significant broker memory and slow routing for all jobs. By passing only a file path (< 200 bytes), queue messages remain tiny regardless of document size. This also means retried jobs can reuse the already-stored file.

---

## 5. Debugging and Testing

### 5.1 Unit Testing
Each component was tested independently before integration:
- The Flask API was tested with `curl` to verify correct status codes for valid uploads, invalid file types, missing file fields, and empty filenames.
- RabbitMQ routing was verified by publishing synthetic messages manually and confirming worker consumption and DB updates.
- MinIO storage was tested using the MinIO Python SDK to confirm upload/download round-trips survive container restarts.

### 5.2 OCR Accuracy
A benchmark set of 20 scanned documents with known ground-truth text was processed and character error rate (CER) was computed using the `python-Levenshtein` library. Documents ranged from clean printer output to low-contrast faxes. Preprocessing (grayscale + contrast boost) reduced CER from ~15% to ~7% on degraded documents. All clean scans achieved < 3% CER.

### 5.3 Load Testing
A Python script (`load_test/load_test.py`) submits N concurrent uploads using `ThreadPoolExecutor`, then polls until all jobs complete. Tests were run with 1, 2, and 3 worker replicas against 10 and 20 concurrent uploads. Throughput scaled approximately linearly with worker count, confirming the horizontal scaling design.

### 5.4 Fault Tolerance Testing
A running job was interrupted by `docker compose stop worker` mid-processing. The job remained in `processing` state in the database. On `docker compose start worker`, the worker reconnected to RabbitMQ, which redelivered the unacknowledged message, and the job completed successfully. This confirmed the primary fault-tolerance mechanism works correctly.

### 5.5 Structured Logging
All containers emit structured log lines including job IDs. Any job can be traced end-to-end across all services by running:
```bash
docker compose logs | grep "JOB_ID"
```

---

## 6. Capabilities and Limits

### What the System Can Handle
- Any image format supported by Pillow (PNG, JPEG, TIFF, BMP, GIF)
- Multi-page PDFs (each page OCR'd independently)
- 10+ concurrent uploads without errors or dropped jobs
- Horizontal scaling: adding `--scale worker=N` increases throughput approximately linearly
- Worker crashes without data loss (RabbitMQ requeues unacknowledged messages)
- Container restarts without data loss (PostgreSQL and MinIO use persistent Docker volumes)

### Potential Bottlenecks
1. **OCR Worker CPU:** Tesseract is CPU-bound. Each worker uses one CPU core during OCR. Adding workers requires adding VM cores or additional VMs.
2. **PostgreSQL under high poll volume:** Mitigated by Redis caching, but a very large number of concurrent pollers would eventually stress the DB. Solved by adding read replicas at larger scale.
3. **MinIO disk I/O:** On a single VM with a standard boot disk, MinIO becomes a bottleneck above ~50 MB/s throughput. Use a separate persistent disk or GCS/S3 for production.
4. **Single RabbitMQ node:** No replication in this setup. A RabbitMQ cluster would add availability at the cost of configuration complexity.

### Accuracy Limits
Tesseract performs well on clean printed text but degrades on: handwriting, documents with complex layouts (multi-column), very small fonts (< 8pt), and severely degraded scans. The Pillow preprocessing pipeline mitigates quality issues but cannot recover heavily corrupted images.

---

## 7. Cloud Technology Categories Used

| Category          | Technology          | Justification                                           |
|-------------------|---------------------|---------------------------------------------------------|
| RPC / API         | Flask REST API      | User interface; without it there is no service entry point |
| Message Queues    | RabbitMQ            | Decouples upload from OCR; provides automatic requeue on failure |
| Databases         | PostgreSQL          | Persists job state across restarts; enables user polling |
| Storage Services  | MinIO (S3)          | Stores files outside the queue; enables retry without re-upload |
| Containers / VMs  | Docker on GCP CE    | Reproducible deployment; enables horizontal worker scaling |
| Key-Value Stores  | Redis               | Caches GET /result responses; reduces PostgreSQL read load |
