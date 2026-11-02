#!/bin/sh
# Approved cacert.pem hash from the hash-verified bootstrap requirements.
# Refresh deliberately with: uv run inv refresh-certificates
# Runs in Python-free final images too; a missing/empty/stale bundle fails closed.
set -eu
bundle="${1:-/etc/ssl/certs/ca-certificates.crt}"
# OpenSSL/Go can also load individual certificates from this directory.
# Managed images must not leave stale hashed links alongside the approved file.
if [ "$bundle" = /etc/ssl/certs/ca-certificates.crt ]; then
  for entry in /etc/ssl/certs/* /etc/ssl/certs/.[!.]*; do
    if [ "$entry" != "$bundle" ] && { [ -e "$entry" ] || [ -L "$entry" ]; }; then
      echo "Certificate gate failed: unexpected CA directory entry: $entry" >&2
      exit 1
    fi
  done
fi
EXPECTED_SHA256="2ef52ad56b4803e264045c915e55c236cbfa045b84b81efefc218a19eb44bd2b"
if ! printf '%s  %s\n' "$EXPECTED_SHA256" "$bundle" | sha256sum -c -; then
  echo "Certificate gate failed: bundle differs from the reviewed bootstrap bundle." >&2
  echo "After reviewing bootstrap pins, run: uv run inv refresh-certificates" >&2
  exit 1
fi
