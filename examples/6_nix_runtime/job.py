#!/usr/bin/env python3
"""Demonstrate that the nix closure is doing real work.

The script exercises:
  - python itself (interpreter from the closure)
  - `requests` (a third-party library bundled in the closure via flake.nix)

If you bump or add packages in flake.nix, the closure hash changes; on next
submit seekr-chain re-evaluates, sees the new hash isn't in the binary
cache, and triggers a build step automatically.
"""

import sys

import requests


def main() -> None:
    print("=" * 60)
    print(f"python  exe : {sys.executable}")
    print(f"python  ver : {sys.version.split()[0]}")
    print(f"requests ver: {requests.__version__}")
    print("=" * 60)

    # Do something visible: fetch a tiny piece of JSON and print one field.
    # httpbin is a no-auth test endpoint that returns an "origin" IP. If the
    # pod has egress, this confirms the runtime closure can do real network
    # work.
    print("\nFetching https://httpbin.org/ip ...")
    try:
        r = requests.get("https://httpbin.org/ip", timeout=5)
        r.raise_for_status()
        print(f"  origin: {r.json()['origin']}")
    except requests.exceptions.RequestException as e:
        # Egress-restricted clusters: still exit 0 — we just wanted to prove
        # the import path worked.
        print(f"  (network unreachable: {type(e).__name__})")

    print("\nDone.")


if __name__ == "__main__":
    main()
