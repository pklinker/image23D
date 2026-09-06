#!/usr/bin/env python3
"""End-to-end browser check: upload a photo, watch a job, inspect what loaded.

PHASE3.md verified the viewer by hand in a headless Chromium session. This makes
that repeatable, and adds the assertion PLAN-BUGFIX.md item 5 turns on: each GLB
must be fetched **once**, not once per progress tick. That bug is invisible to
every other kind of test -- the app looks and behaves correctly either way, it
just re-downloads and re-parses several megabytes repeatedly.

    IMAGE23D_API_KEY=i23d_... .venv-dev/bin/python scripts/browser_check.py

Requires the containerised stack up and `playwright install chromium-headless-shell`.
"""
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWER_URL = os.environ.get("VIEWER_URL", "http://localhost:5173")
API_KEY = os.environ.get("IMAGE23D_API_KEY")
IMAGE = os.environ.get(
    "IMAGE23D_TEST_IMAGE",
    "data/input/Screenshot 2026-09-04 at 13-28-35 Usain Bolt Biography Speed Height Medals & Facts Britannica.png",
)
JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", "240"))

# Strip the presigned query string: the signature changes on every read, so the
# path is what identifies the object.
def _object_path(url: str) -> str:
    return url.split("?", 1)[0]


def main() -> int:
    if not API_KEY:
        print("IMAGE23D_API_KEY is required", file=sys.stderr)
        return 2
    image = Path(IMAGE)
    if not image.exists():
        print(f"test image not found: {image}", file=sys.stderr)
        return 2

    glb_requests: Counter[str] = Counter()
    console_errors: list[str] = []
    stages_seen: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on(
            "request",
            lambda r: glb_requests.update([_object_path(r.url)]) if ".glb" in r.url else None,
        )

        page.goto(VIEWER_URL)

        # The key is entered once and kept in localStorage.
        if page.locator('input[type="password"]').count():
            page.fill('input[type="password"]', API_KEY)
            page.click("text=Save key")

        page.set_input_files('input[type="file"]', str(image))
        print(f"uploaded {image.name}, waiting for the job...")

        deadline = time.time() + JOB_TIMEOUT_SECONDS
        status = ""
        while time.time() < deadline:
            body = page.locator(".progress").inner_text() if page.locator(".progress").count() else ""
            match = re.search(r"Status:\s*(\w+)", body)
            status = match.group(1) if match else ""
            for label in ("Segmenting", "coarse structure", "Upsampling", "texture", "Remeshing"):
                if label in body and label not in stages_seen:
                    stages_seen.append(label)
            if status in ("succeeded", "failed"):
                break
            page.wait_for_timeout(1000)

        page.wait_for_timeout(3000)  # let the final model finish loading
        canvas = page.locator("canvas").count()
        page.screenshot(path="/tmp/browser_check.png")
        browser.close()

    print()
    print(f"final status : {status}")
    print(f"stages seen  : {len(stages_seen)} -> {stages_seen}")
    print(f"canvas       : {canvas}")
    print("GLB requests :")
    for url, count in sorted(glb_requests.items()):
        print(f"   {count:3d}x  {url.rsplit('/', 2)[-2]}/{url.rsplit('/', 1)[-1]}")
    if console_errors:
        print("console errors:")
        for err in console_errors[:10]:
            print("   ", err[:160])

    failures = []
    if status != "succeeded":
        failures.append(f"job did not succeed (status={status!r})")
    if not canvas:
        failures.append("no canvas rendered")
    if len(stages_seen) < 3:
        failures.append(f"expected several stage labels, saw {stages_seen}")
    repeated = {u: c for u, c in glb_requests.items() if c > 1}
    if repeated:
        failures.append(f"GLB re-downloaded (item 5 regression): { {u.rsplit('/',1)[-1]: c for u, c in repeated.items()} }")
    if not glb_requests:
        failures.append("no GLB was ever fetched")
    if console_errors:
        failures.append(f"{len(console_errors)} console error(s)")

    print()
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("PASS: job succeeded, model rendered, every GLB fetched exactly once")
    print("screenshot: /tmp/browser_check.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
