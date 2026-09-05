#!/usr/bin/env python3
"""Run the pruned workflow and record per-node wall time via the /ws stream."""
import json
import time
import uuid
import urllib.request

import websocket

HOST = "localhost:8188"
CLIENT_ID = str(uuid.uuid4())

graph = json.load(open("usain-bolt.pruned.api.json"))

ws = websocket.WebSocket()
ws.connect(f"ws://{HOST}/ws?clientId={CLIENT_ID}")

payload = json.dumps({"prompt": graph, "client_id": CLIENT_ID}).encode()
req = urllib.request.Request(
    f"http://{HOST}/prompt", data=payload, headers={"Content-Type": "application/json"}
)
resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
prompt_id = resp["prompt_id"]
print("prompt_id", prompt_id, "node_errors", resp["node_errors"])

class_types = {k: v["class_type"] for k, v in graph.items()}
events = []
t_start = time.time()
current_node = None
node_start = None

while True:
    msg = ws.recv()
    if isinstance(msg, bytes):
        continue
    data = json.loads(msg)
    if data.get("data", {}).get("prompt_id") not in (prompt_id, None):
        continue
    now = time.time()
    if data["type"] == "executing":
        node = data["data"]["node"]
        if current_node is not None:
            events.append((current_node, class_types.get(current_node, "?"), now - node_start))
        if node is None:
            break
        current_node = node
        node_start = now
    elif data["type"] == "execution_error":
        print("ERROR", json.dumps(data["data"], indent=2))
        break

total = time.time() - t_start
print(f"\nTotal wall time: {total:.2f}s\n")
print(f"{'node':>6}  {'class_type':<28} {'seconds':>8}")
for node, ctype, dur in events:
    print(f"{node:>6}  {ctype:<28} {dur:8.2f}")

json.dump(
    {"prompt_id": prompt_id, "total_seconds": total,
     "stages": [{"node": n, "class_type": c, "seconds": d} for n, c, d in events]},
    open("/tmp/timed_run_result.json", "w"), indent=2,
)
