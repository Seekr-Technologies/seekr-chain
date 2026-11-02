#!/bin/sh
# Run gates in the final image, not in the CI host interpreter.
set -eu
image=${1:?usage: docker/test-certificates.sh IMAGE controller|init|nix-runner}
kind=${2:?image kind is required}
gates=$(CDPATH= cd -- "$(dirname -- "$0")/build_gates" && pwd)
docker run --rm --entrypoint /bin/sh \
  -v "$gates:/certificate-gates:ro" "$image" \
  /certificate-gates/gate_system_bundle.sh
case "$kind" in
  controller)
    docker run --rm --entrypoint python \
      -v "$gates:/certificate-gates:ro" "$image" \
      /certificate-gates/gate_certificates.py
    ;;
  nix-runner)
    docker run --rm --entrypoint /bin/sh "$image" -ec \
      'test "$NIX_SSL_CERT_FILE" = /etc/ssl/certs/ca-certificates.crt
       test "$SSL_CERT_FILE" = "$NIX_SSL_CERT_FILE"
       test "$CURL_CA_BUNDLE" = "$NIX_SSL_CERT_FILE"
       test "$SSL_CERT_DIR" = /etc/ssl/certs
       grep -qx "ssl-cert-file = $NIX_SSL_CERT_FILE" /etc/nix/nix.conf'
    ;;
  init) ;;
  *) echo "Unknown image kind: $kind" >&2; exit 1 ;;
esac
