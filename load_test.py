#!/usr/bin/env python3
"""
Load test: sends N concurrent uploads, polls until all jobs finish,
and prints throughput + per-job latency.

Usage:
    python load_test.py --file test.png --n 10 --workers 3
"""

import argparse
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "http://localhost:5000"
POLL_INTERVAL = 1   # seconds between status polls
MAX_WAIT      = 120 # seconds before giving up on a job


def upload(filepath: str, index: int) -> dict:
    t0 = time.time()
    with open(filepath, "rb") as fh:
        resp = requests.post(
            f"{API_BASE}/upload",
            files={"file": (filepath.split("/")[-1], fh)},
            timeout=30,
        )
    resp.raise_for_status()
    data = resp.json()
    data["upload_time"] = time.time() - t0
    data["index"]       = index
    print(f"  [{index}] Uploaded → job_id={data['job_id']}  ({data['upload_time']:.2f}s)")
    return data


def poll_until_done(job_id: str, index: int) -> dict:
    deadline = time.time() + MAX_WAIT
    while time.time() < deadline:
        r = requests.get(f"{API_BASE}/result/{job_id}", timeout=10)
        r.raise_for_status()
        d = r.json()
        if d["status"] in ("done", "failed"):
            return d
        time.sleep(POLL_INTERVAL)
    return {"status": "timeout", "job_id": job_id}


def run(filepath: str, n: int, workers: int):
    print(f"\n=== Load Test: {n} uploads, {workers} concurrent ===\n")
    t_start = time.time()

    # --- concurrent uploads ---
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(upload, filepath, i): i for i in range(n)}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                print(f"  Upload error: {e}")

    upload_elapsed = time.time() - t_start
    print(f"\nAll {len(results)} uploads done in {upload_elapsed:.2f}s\n")
    print("Polling for results...\n")

    # --- poll all jobs ---
    job_results = []
    t_poll_start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(poll_until_done, r["job_id"], r["index"]): r
            for r in results
        }
        for fut in as_completed(futures):
            jr = fut.result()
            job_results.append(jr)
            status = jr.get("status")
            conf   = jr.get("confidence")
            idx    = futures[fut]["index"]
            conf_str = f"  confidence={conf:.1f}%" if conf is not None else ""
            print(f"  [{idx}] {status}{conf_str}")

    total_elapsed = time.time() - t_start
    done    = sum(1 for r in job_results if r.get("status") == "done")
    failed  = sum(1 for r in job_results if r.get("status") == "failed")
    timeout = sum(1 for r in job_results if r.get("status") == "timeout")

    print(f"""
=== Results ===
  Total jobs  : {n}
  Done        : {done}
  Failed      : {failed}
  Timeout     : {timeout}
  Total time  : {total_elapsed:.2f}s
  Throughput  : {done / total_elapsed:.2f} docs/sec
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",    default="test.png", help="Path to test image or PDF")
    parser.add_argument("--n",       type=int, default=10, help="Number of jobs to submit")
    parser.add_argument("--workers", type=int, default=5,  help="Concurrent upload threads")
    args = parser.parse_args()
    run(args.file, args.n, args.workers)
