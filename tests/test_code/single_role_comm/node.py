import json
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# Regression workload: a single-role, multi-node step where rank 1 connects to
# rank 0 purely via the PEERMAP-resolved DNS name. Proves MASTER_ADDR/PEERMAP
# actually resolve to a live pod, not just that the strings match.
with open(os.environ["PEERMAP"], "r") as f:
    peermap = json.load(f)

node_rank = int(os.environ["NODE_RANK"])
# Single-role peermap.json is a flat list of node addresses; index 0 is rank 0.
target = peermap[0]

if node_rank == 0:

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"hello from rank0\n")

    server = HTTPServer(("0.0.0.0", 8000), Handler)
    print("server: waiting for one request", flush=True)
    server.handle_request()
    print("server: done", flush=True)
else:
    url = f"http://{target}:8000"
    print(f"target URL: {url}", flush=True)

    attempts = 120
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                body = r.read().decode("utf-8", errors="replace")
            print("OK:", body.strip(), flush=True)
            sys.exit(0)
        except Exception as e:
            print(f"attempt {attempt + 1}/{attempts} failed: {e}", flush=True)
            time.sleep(1)

    print("giving up", flush=True)
    sys.exit(1)
