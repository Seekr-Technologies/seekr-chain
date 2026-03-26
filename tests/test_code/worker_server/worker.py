import json
import os
import sys
import time
import urllib.request

# Read the peermap
peermap_path = os.environ.get("PEERMAP")
if not peermap_path:
    raise ValueError("")
with open(peermap_path, "r") as f:
    peermap = json.load(f)

url = f"http://{peermap['server'][0]}:8000"

print(f"target URL: {url}", flush=True)

attempts = 120
# Simple retry loop while server comes up
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
