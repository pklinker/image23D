#!/usr/bin/env python3
"""End-to-end smoke test: presigned upload -> job -> SSE -> artifacts.

Exercises the API the way a service integration would, over HTTP only. The
browser path is covered separately by scripts/browser_check.py.

    IMAGE23D_API_KEY=i23d_... python3 scripts/e2e_smoke_test.py [image] [--bbox x0 y0 x1 y1]

Every /v1 route has required an API key since Phase 4; this script had not sent
one since, so it had been failing with 401 the whole time.
"""
import argparse
import json
import os
import sys
import time

import requests

API = os.environ.get("IMAGE23D_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("IMAGE23D_API_KEY")
DEFAULT_IMAGE = (
    "data/input/Screenshot 2026-09-04 at 13-28-35 Usain Bolt Biography "
    "Speed Height Medals & Facts Britannica.png"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?", default=DEFAULT_IMAGE)
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("X0", "Y0", "X1", "Y1"),
        help="where the athlete is, normalised 0-1; omit to segment the whole frame",
    )
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    if not API_KEY:
        print("IMAGE23D_API_KEY is required", file=sys.stderr)
        return 2

    auth = {"Authorization": f"Bearer {API_KEY}"}

    r = requests.post(
        f"{API}/v1/uploads",
        headers=auth,
        json={"filename": os.path.basename(args.image), "content_type": "image/png"},
    )
    r.raise_for_status()
    upload = r.json()
    print("upload target:", upload["object_key"])

    with open(args.image, "rb") as f:
        put = requests.put(upload["upload_url"], data=f, headers={"Content-Type": "image/png"})
    put.raise_for_status()
    print("uploaded to object storage, status", put.status_code)

    params = {}
    if args.bbox:
        params["bbox"] = list(args.bbox)
    r = requests.post(
        f"{API}/v1/jobs", headers=auth, json={"object_key": upload["object_key"], "params": params}
    )
    r.raise_for_status()
    job_id = r.json()["job_id"]
    print("job created:", job_id, "params:", params or "(defaults)")

    # Follow the SSE stream rather than polling: it is the path the viewer uses,
    # and a stream that never closes is a bug this is meant to catch.
    # EventSource cannot set headers, so that one route also takes ?api_key=.
    # Note it therefore lands in access logs -- fine on a private box, worth
    # replacing with fetch + ReadableStream before this is exposed further.
    print("following SSE...")
    start = time.time()
    status = None
    stages = []
    with requests.get(
        f"{API}/v1/jobs/{job_id}/events",
        params={"api_key": API_KEY},
        stream=True,
        timeout=args.timeout,
    ) as stream:
        stream.raise_for_status()
        for raw in stream.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue  # heartbeat comment or blank separator
            event = json.loads(raw[6:])
            status = event["status"]
            stage = event.get("stage")
            if stage and stage not in stages:
                stages.append(stage)
            print(f"  [{time.time() - start:6.1f}s] {status:9s} {stage or ''}")
            if status in ("succeeded", "failed"):
                break

    final = requests.get(f"{API}/v1/jobs/{job_id}", headers=auth).json()

    print()
    print("status        :", final["status"])
    print("error         :", final["error"])
    print("effective para:", final["params"])
    print("total_seconds :", final["total_seconds"], "| gpu_peak_mb:", final["gpu_peak_mb"])
    print("stage timings :")
    for t in final["stage_timings"]:
        print(f"   {t['stage']:24s} {t['seconds']:7.2f}s")

    failures = []
    if final["status"] != "succeeded":
        failures.append(f"job {final['status']}: {final['error']}")
    if len(stages) < 3:
        failures.append(f"expected several stage transitions over SSE, saw {stages}")
    for key in ("coarse_glb_url", "final_glb_url", "final_glb_compressed_url"):
        if not final[key]:
            failures.append(f"missing {key}")
    total = sum(t["seconds"] for t in final["stage_timings"])
    if final["total_seconds"] and abs(total - final["total_seconds"]) > 1.0:
        failures.append(f"stage seconds ({total:.2f}) do not add up to total ({final['total_seconds']})")

    print()
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("PASS: upload -> job -> SSE -> all three artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
