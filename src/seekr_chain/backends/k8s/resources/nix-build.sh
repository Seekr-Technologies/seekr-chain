#!/bin/sh
# nix-build: realize a closure inside the cluster + push it to the binary
# cache. Invoked by the auto-injected build step (nix_resolution._make_build_step).
#
# Reads these env vars (set on the build step's container):
#   SEEKR_CHAIN_NIX_STORE        URI of the binary cache to push to
#   SEEKR_CHAIN_NIX_CLOSURE      expected /nix/store path (sanity check vs build output)
#   SEEKR_CHAIN_NIX_WORKSPACE    copied nix source tree inside /seekr-chain
#   SEEKR_CHAIN_NIX_EXPRESSION   path inside that nix workspace to the flake
#   SEEKR_CHAIN_NIX_SYSTEM       e.g. x86_64-linux
#   SEEKR_CHAIN_NIX_ATTR         attribute path within the flake (default: "default")
#   SEEKR_CHAIN_NIX_COMPRESSION  zstd|xz|none|bzip2|gzip — applied to NARs we upload
#
# Plain POSIX sh, deliberately: run_teeing_stderr() below replaces bash's
# `2> >(tee -a "$LOG" >&2)` process substitution (which plain /bin/sh, i.e.
# busybox ash in the runner image, can't parse at all) with a named-pipe
# equivalent that works identically under ash, dash, or bash.
set -e

START_TIME=$(date +%s)
# stderr from nix build + nix copy is tee'd here so the final summary can
# parse stats out of it. /tmp is the build pod's ephemeral scratch.
LOG=/tmp/nix-build.log

# POSIX-sh substitute for bash's `2> >(tee -a "$LOG" >&2)`: a named pipe
# fed into a backgrounded `tee`, started before the wrapped command so
# it's already waiting when that command opens its end. Once the command
# (the sole writer) exits, tee sees EOF and finishes on its own — reaped
# below via `wait "$tee_pid"`.
run_teeing_stderr() {
    fifo="/tmp/nix-build-stderr.fifo.$$"
    rm -f "$fifo"
    mkfifo "$fifo"
    tee -a "$LOG" >&2 < "$fifo" &
    tee_pid=$!
    "$@" 2>"$fifo"
    rc=$?
    wait "$tee_pid"
    rm -f "$fifo"
    return $rc
}

# aws-sdk-cpp timeouts (s3 transport). 10 min per HTTP request gives
# ~3 MB/s threshold on multi-GB NARs — anything slower is stuck, not
# merely slow. 10s on connect catches DNS / TCP setup failures fast.
# nix's own stalled-download-timeout doesn't apply to s3 transport.
export AWS_REQUEST_TIMEOUT=600000
export AWS_CONNECT_TIMEOUT=10000

# This pod mounts the shared hostPath (or emptyDir) volume directly at
# /nix, same as a consumer pod — see jobset.py's _select_role_runtime.
# That replaces whatever the runner image itself had at /nix, so bootstrap
# it from /nix-baked (the image's own preserved toolchain) before running
# any real nix command. Shared with chain-nix-init.sh — see that script
# for the full rationale.
sh /seekr-chain/resources/nix-bootstrap.sh

# $SEEKR_CHAIN_NIX_STORE is the durable cache; cache.nixos.org backs the
# nix toolchain's own transitive deps. No local?root= substituter needed:
# now that /nix is bootstrapped and real (not chroot), a warm path from a
# previous build/run pod on this node is already valid in nix's own store
# DB — nix finds it without substituting at all.
{
  echo 'experimental-features = nix-command flakes'
  echo 'sandbox = false'
  echo 'filter-syscalls = false'
  echo "substituters = $SEEKR_CHAIN_NIX_STORE https://cache.nixos.org"
  # $SEEKR_CHAIN_NIX_STORE has no signing key; unlike `nix copy` there's
  # no per-command --no-check-sigs equivalent for implicit substitution
  # during `nix build`, so this has to be set globally.
  echo 'require-sigs = false'
  # Default 10 lines is too few to debug failed builds. 200 captures most
  # autoconf/configure failures with their actual error context.
  echo 'log-lines = 200'
} >> /etc/nix/nix.conf

cd "${SEEKR_CHAIN_NIX_WORKSPACE:-/seekr-chain/workspace}"

# No --store flag: nix build runs against the real, bootstrapped /nix
# directly. An earlier design passed --store local?root=/nix-shared here
# and broke — flake source imports split between the eval store and the
# chroot store inconsistently, failing validation with "path is not in
# the Nix store". That failure was specific to redirecting nix build's
# own store via --store; it doesn't reappear here since no --store flag
# is passed at all. Each derivation lands in /nix/store the moment it's
# realized, so the *resulting closure* is already on the node's shared
# hostPath by construction — no separate copy-out step needed.
FLAKE_REF="path:$SEEKR_CHAIN_NIX_EXPRESSION#packages.$SEEKR_CHAIN_NIX_SYSTEM.$SEEKR_CHAIN_NIX_ATTR"
echo "=== nix build $FLAKE_REF ==="
# -L (--print-build-logs) streams each derivation's stderr into our log,
# prefixed with the derivation name. Without it, nix suppresses build
# output when stdout/stderr aren't a TTY — so chain pod logs would show
# only "building '<drv>'" headers and you'd have no way to tell whether
# a 10-minute "build" is making progress or stuck.
BUILT=$(run_teeing_stderr nix build --print-out-paths --no-link -L "$FLAKE_REF")
echo "built closure: $BUILT"

if [ "$BUILT" != "$SEEKR_CHAIN_NIX_CLOSURE" ]; then
    echo "FATAL: nix build produced $BUILT but submit-time eval expected $SEEKR_CHAIN_NIX_CLOSURE" >&2
    echo "this usually means the source tree drifted between submit and build" >&2
    exit 1
fi

# Durable copy: push to the configured binary cache. Compression scheme is
# configurable via SEEKR_CHAIN_NIX_COMPRESSION (default zstd). xz is ~5x
# slower and single-threaded; on a multi-GB closure that turns a 30s
# compress into 7min. The narinfo records each NAR's scheme, so consumers'
# `nix copy --from` decompresses correctly regardless of what we picked at
# upload time. Only NEW paths are affected — existing paths in the cache
# stay as they were written (mixed-scheme caches are fine in nix).
case "$SEEKR_CHAIN_NIX_STORE" in
  *\?*) COPY_URI="$SEEKR_CHAIN_NIX_STORE&compression=$SEEKR_CHAIN_NIX_COMPRESSION" ;;
  *)    COPY_URI="$SEEKR_CHAIN_NIX_STORE?compression=$SEEKR_CHAIN_NIX_COMPRESSION" ;;
esac
echo "=== nix copy --to $COPY_URI ==="
run_teeing_stderr nix copy --to "$COPY_URI" "$BUILT"
echo "pushed $SEEKR_CHAIN_NIX_CLOSURE to $SEEKR_CHAIN_NIX_STORE"

# Build summary — parsed from the captured log. The numbers here are what
# tells a docker-mode user "oh, this is the architectural win": how many
# paths reused from cache vs built from source, where they came from, and
# how much incremental data we actually shipped.
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

# `grep -c` outputs "0" *and* exits 1 when there are no matches; `|| echo 0`
# would append a second "0", giving multi-line output. Use `|| true` + `${:-0}`.
#
# No SUBST_LOCAL counter: with /nix now bootstrapped and real (see above),
# a path already warm on this node from a prior build/run pod is simply
# already-valid in nix's local store DB — nix doesn't substitute it from
# anywhere, so it never appears in the build log at all. Those paths are
# invisible to this log-scraping approach; SUBST_TOTAL + BUILT_FROM_SRC
# below can legitimately sum to less than CLOSURE_PATHS on a warm node.
# Recovering that number would need the same before/after `path-info
# --recursive` diffing chain-nix-init.sh's own summary already uses,
# which is a bigger change than this alignment — not done here.
SUBST_S3=$(grep -c    "from 's3://"                   "$LOG" 2>/dev/null || true)
SUBST_NIXOS=$(grep -c "from 'https://cache.nixos.org" "$LOG" 2>/dev/null || true)
SUBST_S3=${SUBST_S3:-0}; SUBST_NIXOS=${SUBST_NIXOS:-0}
SUBST_TOTAL=$((SUBST_S3 + SUBST_NIXOS))

BUILT_FROM_SRC=$(grep -oE "these [0-9]+ derivations? will be built" "$LOG" 2>/dev/null \
                 | grep -oE '[0-9]+' | head -1)
BUILT_FROM_SRC=${BUILT_FROM_SRC:-0}

UPLOAD_PATHS=$(grep -c "^uploaded 's3://"    "$LOG" 2>/dev/null || true)
UPLOAD_PATHS=${UPLOAD_PATHS:-0}
UPLOAD_BYTES=$(grep -oE "uploaded '[^']+' \([0-9]+ bytes\)" "$LOG" 2>/dev/null \
               | grep -oE '[0-9]+ bytes' | grep -oE '[0-9]+' \
               | awk 'BEGIN {s=0} {s+=$1} END {print s}')
UPLOAD_BYTES=${UPLOAD_BYTES:-0}

CLOSURE_SIZE_BYTES=$(nix path-info --closure-size "$BUILT" 2>/dev/null \
                     | awk '{print $2+0}')
CLOSURE_SIZE_BYTES=${CLOSURE_SIZE_BYTES:-0}
CLOSURE_PATHS=$(nix path-info --recursive "$BUILT" 2>/dev/null | wc -l)
CLOSURE_PATHS=${CLOSURE_PATHS:-0}

fmt_bytes() {
  awk -v b="$1" 'BEGIN {
    if (b >= 1073741824) printf "%.2f GB", b/1073741824
    else if (b >= 1048576) printf "%.2f MB", b/1048576
    else if (b >= 1024) printf "%.2f KB", b/1024
    else printf "%d B", b
  }'
}

if [ "$((SUBST_TOTAL + BUILT_FROM_SRC))" -gt 0 ]; then
  HIT_PCT=$(awk "BEGIN { printf \"%.1f\", 100 * $SUBST_TOTAL / ($SUBST_TOTAL + $BUILT_FROM_SRC) }")
else
  HIT_PCT="n/a"
fi

cat <<EOF

===================================================================
  Nix build summary
===================================================================
  Total time:              ${DURATION}s
  Closure:                 $SEEKR_CHAIN_NIX_CLOSURE
  Closure size:            $(fmt_bytes "$CLOSURE_SIZE_BYTES")  ($CLOSURE_PATHS paths)

  Cache hits:              $SUBST_TOTAL paths  ($HIT_PCT%)
    from $SEEKR_CHAIN_NIX_STORE:    $SUBST_S3
    from cache.nixos.org:  $SUBST_NIXOS
  Cache misses (built):    $BUILT_FROM_SRC paths
  (paths already warm on this node from a prior build/run are invisible
   to this log-scraping summary — see comment above SUBST_S3)

  Uploaded to cache:       $UPLOAD_PATHS paths,  $(fmt_bytes "$UPLOAD_BYTES")
===================================================================
EOF
