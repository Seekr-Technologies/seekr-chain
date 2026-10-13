#!/bin/sh
# Exercise the runner as it is used in a pod: /nix starts as an empty shared
# volume and is initialized by nix-bootstrap.sh. This requires only the image
# being tested; the raw derivations have no nixpkgs inputs or network fetches.
set -eu

IMAGE=${1:?usage: docker/test-nix-runner.sh IMAGE}
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VOLUME="seekr-chain-nix-runner-smoke-$$"
RESOURCE_DIR="$ROOT/src/seekr_chain/backends/k8s/resources"

cleanup() {
  docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

docker volume create "$VOLUME" >/dev/null

bootstrap() {
  docker run --rm \
    -v "$VOLUME:/nix" \
    -v "$RESOURCE_DIR:/seekr-chain/resources:ro" \
    "$IMAGE" /bin/sh /seekr-chain/resources/nix-bootstrap.sh
}

assert_store_permissions() {
  docker run --rm -v "$VOLUME:/nix" "$IMAGE" /bin/sh -ec \
    'test "$(stat -c "%u:%g:%a" /nix/store)" = "0:30000:1775"'
}

identity_expr='derivation {
  name = "builder-identity";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "set -eu; export PATH=/usr/bin:/bin; uid=$(id -u); echo uid=$uid home=$HOME >&2; case $uid in 3000[1-9]|300[12][0-9]|3003[0-2]) ;; *) exit 1 ;; esac; mkdir -p $out; echo ok > $out/result" ];
}'

post_failure_identity_expr='derivation {
  name = "builder-identity-after-home-failure";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "set -eu; export PATH=/usr/bin:/bin; uid=$(id -u); echo uid=$uid home=$HOME >&2; case $uid in 3000[1-9]|300[12][0-9]|3003[0-2]) ;; *) exit 1 ;; esac; mkdir -p $out; echo ok > $out/result" ];
}'

home_failure_expr='derivation {
  name = "homeless-shelter-must-remain-unwritable";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "set -eu; export PATH=/usr/bin:/bin; mkdir -p \"$HOME\"" ];
}'

run_identity() {
  expr=$1
  output=$(docker run --rm -v "$VOLUME:/nix" "$IMAGE" nix --option substituters '' build -L --no-link --print-out-paths --impure --expr "$expr" 2>&1)
  printf '%s\n' "$output"
  printf '%s\n' "$output" | grep -Eq 'uid=(3000[1-9]|300[12][0-9]|3003[0-2]) home=/homeless-shelter'
}

# Fresh-volume initialization must produce the fixed shared-store mode before
# the first derivation asks Nix to select a build user.
bootstrap
assert_store_permissions
run_identity "$identity_expr"

# A builder must not be able to create /homeless-shelter. Its failed attempt
# must also not affect a later independent derivation.
if home_failure_output=$(docker run --rm -v "$VOLUME:/nix" "$IMAGE" nix --option substituters '' build -L --no-link --print-out-paths --impure --expr "$home_failure_expr" 2>&1); then
  echo 'expected the /homeless-shelter reproducer to fail' >&2
  exit 1
fi
printf '%s\n' "$home_failure_output"
printf '%s\n' "$home_failure_output" | grep -F '/homeless-shelter'
printf '%s\n' "$home_failure_output" | grep -i 'permission denied'
run_identity "$post_failure_identity_expr"

# hostPath volumes outlive images. Deliberately simulate the root:root/0755
# metadata left by an old runner, then require the marker-fast path to repair
# it without recopying or recursively changing immutable store paths.
docker run --rm -v "$VOLUME:/nix" "$IMAGE" /bin/sh -ec \
  'chown 0:0 /nix/store; chmod 0755 /nix/store'
bootstrap
assert_store_permissions

echo "nix runner smoke test passed for $IMAGE"
