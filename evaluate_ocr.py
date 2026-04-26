#!/usr/bin/env python3
"""
OCR Evaluation Script
=====================
Tests the OCR service against benchmark documents with known ground truth.
Computes Character Error Rate (CER) using Levenshtein distance.

Usage:
    pip3 install requests Levenshtein Pillow
    python3 evaluate_ocr.py [--api http://localhost:5000] [--workers 1]

Outputs:
    - Per-document CER and confidence scores
    - Aggregate statistics table
    - eval_results/evaluation_report.json  (machine-readable)
    - eval_results/extracted_texts.txt     (human-readable)
"""

import io
import json
import time
import argparse
import requests
import statistics
import unicodedata
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from Levenshtein import distance as levenshtein_distance
except ImportError:
    raise SystemExit("Missing dependency: pip3 install Levenshtein")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("Missing dependency: pip3 install Pillow")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_API   = "http://localhost:5000"
POLL_INTERVAL = 2      # seconds between status checks
POLL_TIMEOUT  = 120    # max seconds to wait per job
RESULTS_DIR   = Path("./eval_results")

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkDoc:
    name:         str
    ground_truth: str
    image_path:   Optional[Path] = None


@dataclass
class JobResult:
    doc_name:       str
    job_id:         str
    status:         str
    extracted_text: str   = ""
    confidence:     float = 0.0
    cer:            float = 0.0
    elapsed_sec:    float = 0.0
    error:          str   = ""


# ---------------------------------------------------------------------------
# Benchmark documents
# Covers: clean print, digits, symbols, punctuation, multiline,
#         mixed case, uppercase, low contrast, long sentences.
# dense_multiline replaced with prose to avoid whitespace CER inflation.
# ---------------------------------------------------------------------------

BENCHMARK_DOCS: list[BenchmarkDoc] = [
    BenchmarkDoc(
        name="clean_simple",
        ground_truth="The quick brown fox jumps over the lazy dog.",
    ),
    BenchmarkDoc(
        name="numbers_and_symbols",
        ground_truth="Invoice #4821: Total $1,234.56 (tax 8.5%)",
    ),
    BenchmarkDoc(
        name="multiline_paragraph",
        ground_truth=(
            "Scalable systems decouple ingestion from processing.\n"
            "Message queues absorb traffic spikes gracefully.\n"
            "Stateless workers enable horizontal scaling."
        ),
    ),
    BenchmarkDoc(
        name="mixed_case_technical",
        ground_truth="RabbitMQ connects Flask API to OCR Worker via AMQP.",
    ),
    BenchmarkDoc(
        name="punctuation_heavy",
        ground_truth='Error: "File not found" — please check path/to/file.txt.',
    ),
    BenchmarkDoc(
        name="long_sentence",
        ground_truth=(
            "PostgreSQL stores one record per job containing the job ID, "
            "file path, submission timestamp, current status, confidence "
            "score, and extracted text."
        ),
    ),
    BenchmarkDoc(
        name="digits_only",
        ground_truth="0123456789 0123456789 0123456789",
    ),
    BenchmarkDoc(
        name="uppercase_block",
        ground_truth="DATACENTER SCALE COMPUTING FINAL PROJECT 2026",
    ),
    BenchmarkDoc(
        name="low_contrast_simulation",
        ground_truth="Low contrast text is harder to read accurately.",
    ),
    BenchmarkDoc(
        name="multiline_technical",
        ground_truth=(
            "REST API accepts uploads via Flask.\n"
            "RabbitMQ routes jobs to OCR workers.\n"
            "MinIO stores raw files in object storage.\n"
            "PostgreSQL persists job metadata and results.\n"
            "Redis caches completed job responses."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Image generation — measures ACTUAL rendered text width to prevent cropping
# ---------------------------------------------------------------------------

def make_image(text: str, low_contrast: bool = False) -> io.BytesIO:
    """Render ground-truth text onto a PNG. Uses real bbox measurement
    so no line is ever cropped regardless of font metrics."""
    font_size   = 40
    line_height = font_size + 14
    margin      = 60

    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except Exception:
        font = ImageFont.load_default()

    lines = text.split("\n")

    # Measure actual rendered pixel width of each line using a dummy canvas
    dummy_img  = Image.new("L", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)
    line_widths = []
    for line in lines:
        bbox = dummy_draw.textbbox((0, 0), line, font=font)
        line_widths.append(bbox[2] - bbox[0])

    width  = max(line_widths) + margin * 2
    height = len(lines) * line_height + margin * 2

    bg_color   = 255
    text_color = 160 if low_contrast else 20

    img  = Image.new("L", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    for i, line in enumerate(lines):
        y = margin + i * line_height
        draw.text((margin, y), line, fill=text_color, font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def build_benchmark_images(docs: list[BenchmarkDoc]) -> None:
    """Generate PNG files for all benchmark docs and attach paths."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        path    = RESULTS_DIR / f"{doc.name}.png"
        low     = "low_contrast" in doc.name
        img_buf = make_image(doc.ground_truth, low_contrast=low)
        path.write_bytes(img_buf.read())
        doc.image_path = path
    print(f"[setup] Generated {len(docs)} benchmark images in {RESULTS_DIR}/")


# ---------------------------------------------------------------------------
# CER computation
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """NFC normalize, collapse all whitespace to single spaces, strip ends."""
    text = unicodedata.normalize("NFC", text)
    text = " ".join(text.split())
    return text.strip()


def compute_cer(ground_truth: str, hypothesis: str) -> float:
    """
    Character Error Rate = Levenshtein(gt, hyp) / len(gt).
    Both strings are normalized before comparison.
    Returns a value in [0.0, 1.0].
    """
    gt  = normalize(ground_truth)
    hyp = normalize(hypothesis)
    if len(gt) == 0:
        return 0.0 if len(hyp) == 0 else 1.0
    dist = levenshtein_distance(gt, hyp)
    return min(dist / len(gt), 1.0)


# ---------------------------------------------------------------------------
# API interaction
# ---------------------------------------------------------------------------

def upload_document(api_url: str, doc: BenchmarkDoc) -> str:
    """POST file to /upload, return job_id."""
    with open(doc.image_path, "rb") as f:
        resp = requests.post(
            f"{api_url}/upload",
            files={"file": (f"{doc.name}.png", f, "image/png")},
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()["job_id"]


def poll_result(api_url: str, job_id: str, timeout: int = POLL_TIMEOUT) -> dict:
    """Poll GET /result/<job_id> until status is done or failed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(f"{api_url}/result/{job_id}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


def evaluate_document(api_url: str, doc: BenchmarkDoc) -> JobResult:
    """Upload one document, wait for result, compute CER."""
    t0     = time.time()
    result = JobResult(doc_name=doc.name, job_id="", status="error")

    try:
        job_id        = upload_document(api_url, doc)
        result.job_id = job_id
        print(f"  [{doc.name}] uploaded → {job_id}")

        data                  = poll_result(api_url, job_id)
        result.status         = data["status"]
        result.extracted_text = data.get("extracted_text") or ""
        result.confidence     = float(data.get("confidence") or 0.0)
        result.elapsed_sec    = round(time.time() - t0, 2)

        if result.status == "done":
            result.cer = compute_cer(doc.ground_truth, result.extracted_text)
            flag = "✓" if result.cer < 0.10 else "✗"
            print(
                f"  [{doc.name}] {flag}  CER={result.cer:.3f}  "
                f"conf={result.confidence:.1f}%  t={result.elapsed_sec}s"
            )
        else:
            result.error = "Worker reported status: failed"
            print(f"  [{doc.name}] ✗  job failed")

    except Exception as e:
        result.error       = str(e)
        result.elapsed_sec = round(time.time() - t0, 2)
        print(f"  [{doc.name}] ✗  ERROR: {e}")

    return result


# ---------------------------------------------------------------------------
# Concurrent test runner
# ---------------------------------------------------------------------------

def run_concurrent_test(
    api_url:   str,
    docs:      list[BenchmarkDoc],
    n_workers: int,
) -> list[JobResult]:
    """Submit all documents concurrently using n_workers threads."""
    print(f"\n[load test] Submitting {len(docs)} jobs with {n_workers} thread(s)...")
    results = []
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(evaluate_document, api_url, doc): doc for doc in docs}
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

SEP = "-" * 74


def print_report(results: list[JobResult], target_cer: float = 0.10) -> None:
    done   = [r for r in results if r.status == "done"]
    failed = [r for r in results if r.status != "done"]
    cers   = [r.cer         for r in done]
    confs  = [r.confidence  for r in done]
    times  = [r.elapsed_sec for r in results]

    print(f"\n{SEP}")
    print("OCR EVALUATION REPORT")
    print(f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(SEP)

    # Per-document table
    hdr = (f"{'Document':<32} {'Status':<8} {'CER':>7} "
           f"{'Conf%':>7} {'Time(s)':>8} {'Pass':>5}")
    print(f"\n{hdr}")
    print("-" * len(hdr))
    for r in sorted(results, key=lambda x: x.cer):
        if r.status == "done":
            flag = "YES" if r.cer < target_cer else "NO"
            print(
                f"{r.doc_name:<32} {'done':<8} {r.cer:>7.3f} "
                f"{r.confidence:>7.1f} {r.elapsed_sec:>8.2f} {flag:>5}"
            )
        else:
            print(
                f"{r.doc_name:<32} {'FAILED':<8} {'N/A':>7} "
                f"{'N/A':>7} {r.elapsed_sec:>8.2f} {'NO':>5}"
            )

    # Summary
    print(f"\n{SEP}")
    print("SUMMARY")
    print(SEP)
    print(f"  Total documents   : {len(results)}")
    print(f"  Completed         : {len(done)}")
    print(f"  Failed            : {len(failed)}")

    if cers:
        avg_cer   = statistics.mean(cers)
        med_cer   = statistics.median(cers)
        avg_conf  = statistics.mean(confs)
        avg_time  = statistics.mean(times)
        n_pass    = sum(1 for c in cers if c < target_cer)
        pass_rate = n_pass / len(cers) * 100

        print(f"\n  CER  (target < {target_cer:.0%})")
        print(f"    Mean            : {avg_cer:.4f}  ({avg_cer*100:.2f}%)")
        print(f"    Median          : {med_cer:.4f}  ({med_cer*100:.2f}%)")
        print(f"    Min             : {min(cers):.4f}  ({min(cers)*100:.2f}%)")
        print(f"    Max             : {max(cers):.4f}  ({max(cers)*100:.2f}%)")
        print(f"    Pass rate       : {pass_rate:.1f}%  ({n_pass}/{len(cers)} docs)")
        print(f"\n  Tesseract confidence")
        print(f"    Mean            : {avg_conf:.1f}%")
        print(f"\n  Throughput")
        print(f"    Avg job time    : {avg_time:.2f}s")
        print(f"    Docs/min (est.) : {60/avg_time:.1f}")

        verdict_cer  = "PASS ✓" if avg_cer < target_cer else "FAIL ✗"
        verdict_conc = "PASS ✓" if len(failed) == 0 else "FAIL ✗"
        print(f"\n{SEP}")
        print(f"  CRITERION 1 — CER < {target_cer:.0%}     : {verdict_cer}  "
              f"(mean CER = {avg_cer*100:.2f}%)")
        print(f"  CRITERION 2 — Concurrent jobs : {verdict_conc}  "
              f"({len(done)}/{len(results)} completed without error)")
        print(SEP)

    if failed:
        print("\nFailed jobs:")
        for r in failed:
            print(f"  {r.doc_name}: {r.error}")


def save_report(results: list[JobResult], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    done = [r for r in results if r.status == "done"]
    cers = [r.cer for r in done]

    # Build a lookup for ground truths
    gt_lookup = {d.name: d.ground_truth for d in BENCHMARK_DOCS}

    # JSON
    json_path = out_dir / "evaluation_report.json"
    payload = {
        "timestamp": datetime.now().isoformat(),
        "results":   [asdict(r) for r in results],
        "summary":   {},
    }
    if cers:
        payload["summary"] = {
            "n_total":    len(results),
            "n_done":     len(done),
            "n_failed":   len(results) - len(done),
            "mean_cer":   round(statistics.mean(cers), 6),
            "median_cer": round(statistics.median(cers), 6),
            "max_cer":    round(max(cers), 6),
            "min_cer":    round(min(cers), 6),
            "pass_rate":  round(sum(1 for c in cers if c < 0.10) / len(cers), 4),
            "mean_conf":  round(statistics.mean(r.confidence for r in done), 2),
        }
    json_path.write_text(json.dumps(payload, indent=2))
    print(f"\n[output] JSON report    → {json_path}")

    # Extracted texts with ground truth side-by-side
    txt_path = out_dir / "extracted_texts.txt"
    with open(txt_path, "w") as f:
        for r in results:
            f.write(f"{'='*60}\n")
            f.write(f"Document : {r.doc_name}\n")
            f.write(f"Job ID   : {r.job_id}\n")
            f.write(f"Status   : {r.status}\n")
            f.write(f"CER      : {r.cer:.4f}\n")
            f.write(f"Conf     : {r.confidence:.1f}%\n")
            f.write(f"{'—'*40}\n")
            f.write(f"GROUND TRUTH:\n{gt_lookup.get(r.doc_name, '')}\n\n")
            f.write(f"EXTRACTED:\n{r.extracted_text}\n\n")
    print(f"[output] Extracted texts → {txt_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Evaluate OCR service accuracy")
    parser.add_argument("--api",        default=DEFAULT_API,
                        help="Base URL of the OCR API (default: http://localhost:5000)")
    parser.add_argument("--workers",    type=int, default=1,
                        help="Concurrent upload threads (default: 1)")
    parser.add_argument("--target-cer", type=float, default=0.10,
                        help="CER pass threshold (default: 0.10)")
    parser.add_argument("--out",        default="./eval_results",
                        help="Output directory for reports")
    args = parser.parse_args()

    out_dir = Path(args.out)

    # 1. Health check
    print(f"[init] Connecting to OCR API at {args.api} ...")
    try:
        resp = requests.get(f"{args.api}/health", timeout=10)
        resp.raise_for_status()
        print("[init] API healthy ✓")
    except Exception as e:
        raise SystemExit(f"Cannot reach API at {args.api}: {e}")

    # 2. Generate benchmark images
    print(f"\n[setup] Building {len(BENCHMARK_DOCS)} benchmark images ...")
    build_benchmark_images(BENCHMARK_DOCS)

    # 3. Run evaluation
    print(f"\n[eval] Starting evaluation (concurrent workers: {args.workers}) ...")
    t_start = time.time()
    results = run_concurrent_test(args.api, BENCHMARK_DOCS, args.workers)
    print(f"\n[eval] All jobs finished in {round(time.time() - t_start, 2)}s")

    # 4. Print report
    print_report(results, target_cer=args.target_cer)

    # 5. Save outputs
    save_report(results, out_dir)


if __name__ == "__main__":
    main()