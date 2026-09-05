#!/usr/bin/env python3
"""End-to-end Phase 2 smoke test: presigned upload -> job -> poll to completion."""
import sys
import time

import requests

API = "http://localhost:8000"
IMAGE_PATH = sys.argv[1] if len(sys.argv) > 1 else "data/input/Screenshot 2026-09-04 at 13-28-35 Usain Bolt Biography Speed Height Medals & Facts Britannica.png"

r = requests.post(f"{API}/v1/uploads", json={"filename": "usain-bolt.png", "content_type": "image/png"})
r.raise_for_status()
upload = r.json()
print("upload target:", upload["object_key"])

with open(IMAGE_PATH, "rb") as f:
    put = requests.put(upload["upload_url"], data=f, headers={"Content-Type": "image/png"})
put.raise_for_status()
print("uploaded to MinIO, status", put.status_code)

r = requests.post(f"{API}/v1/jobs", json={"object_key": upload["object_key"]})
r.raise_for_status()
job = r.json()
job_id = job["job_id"]
print("job created:", job_id)

start = time.time()
while True:
    r = requests.get(f"{API}/v1/jobs/{job_id}")
    r.raise_for_status()
    status = r.json()
    elapsed = time.time() - start
    print(f"[{elapsed:6.1f}s] status={status['status']} stage={status['stage']}")
    if status["status"] in ("succeeded", "failed"):
        break
    time.sleep(3)

print()
print("final:", status)
