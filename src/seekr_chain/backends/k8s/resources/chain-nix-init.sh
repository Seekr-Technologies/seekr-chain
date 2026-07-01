#!/bin/sh
# chain-nix-init: fetch a nix closure into the node's shared /nix volume
# before main starts. Reads SEEKR_CHAIN_NIX_STORE + SEEKR_CHAIN_NIX_CLOSURE
# from its env (set by _resolve_nix_role).
#
# Mount layout: this container mounts the shared volume at /nix-shared (NOT
# /nix), so the image's own /nix — which contains the nix binary plus its
# transitive deps — stays usable for the duration of this script.
#
# `nix --store local?root=/nix-shared` treats /nix-shared as a chroot prefix:
# a logical store path /nix/store/<hash> is physically written to
# /nix-shared/nix/store/<hash>. Main mounts the same volume at /nix with
# subPath="nix", so /nix-shared/nix/store/<hash> surfaces in main as
# /nix/store/<hash> — exactly where the closure's binaries' RPATHs expect.
#
# `--no-check-sigs` is a v1 shortcut; production should configure signing.
set -e

{
  echo 'experimental-features = nix-command flakes'
  echo 'sandbox = false'
  echo 'filter-syscalls = false'
  echo 'download-attempts = 8'
  # stalled-download-timeout only applies to HTTP/libcurl, not to s3
  # transport. Set it anyway in case the store URI is changed to http.
  echo 'stalled-download-timeout = 60'
} >> /etc/nix/nix.conf

# aws-sdk-cpp timeouts. These ARE honored on s3:// substituters (where
# nix's internal stalled-download-timeout doesn't reach). 10 min per
# request gives ~3 MB/s threshold for a 1.65 GB NAR — anything slower
# is "stuck", not "slow". 10s on connect catches DNS / TCP setup
# failures fast.
export AWS_REQUEST_TIMEOUT=600000
export AWS_CONNECT_TIMEOUT=10000

LOG=/tmp/nix-init.log
SIZE_BEFORE=$(du -sk /nix-shared 2>/dev/null | awk 'BEGIN{kb=0} {kb=$1+0} END{print kb*1024}')
START_TIME=$(date +%s)

# Watchdog: monitors /nix-shared size growth. If size doesn't change
# for STALL_S consecutive seconds, kill the nix process. This is the
# primary mechanism for detecting hung downloads — measures actual
# progress, not elapsed wall time, so it doesn't false-alarm on slow-
# but-progressing pulls.
#
# Also enforces an overall MAX_S budget per attempt as a final
# backstop in case nix gets into a state where it keeps writing but
# never finishes (unlikely but bounded).
WATCHDOG_STALL_S=120
WATCHDOG_MAX_S=1800
COPY_ATTEMPTS=3

# Set to 1 by run_copy when the closure is already fully present locally
# (and we skip the s3 fetch). Used by the summary block to short-circuit
# expensive nix path-info calls that we don't need on a no-op pull.
FAST_PATH=0

run_copy() {
  # Fast path: if the closure root + every transitive dep already lives in
  # /nix-shared, we don't need to call out to s3 at all. `nix path-info
  # --recursive` is a local-store DB query (no network), and exits non-zero
  # the moment any path in the closure graph is missing — which means we
  # can use a successful exit as proof of full presence.
  #
  # This matters because nix copy --from, even when nothing needs to copy,
  # still fetches every narinfo in the closure graph from the remote cache
  # to compute the dep tree. That's ~50-100ms × N paths of serial s3
  # roundtrips → ~10s on a 200-path closure even when everything is local.
  if nix --store "local?root=/nix-shared" path-info --recursive \
       "$SEEKR_CHAIN_NIX_CLOSURE" >/dev/null 2>&1; then
    echo "Closure already fully present on node — skipping s3 fetch."
    FAST_PATH=1
    return 0
  fi

  nix --store "local?root=/nix-shared" copy \
      --from "$SEEKR_CHAIN_NIX_STORE" \
      --no-check-sigs \
      "$SEEKR_CHAIN_NIX_CLOSURE" 2> >(tee -a "$LOG" >&2) &
  local nix_pid=$!

  (
    local start=$(date +%s)
    local last_size=$(du -sk /nix-shared 2>/dev/null | awk 'BEGIN{kb=0} {kb=$1+0} END{print kb*1024}')
    local stall_at=$start
    while kill -0 $nix_pid 2>/dev/null; do
      sleep 30
      local now=$(date +%s)
      local cur_size=$(du -sk /nix-shared 2>/dev/null | awk 'BEGIN{kb=0} {kb=$1+0} END{print kb*1024}')
      if [ "$cur_size" != "$last_size" ]; then
        stall_at=$now
        last_size=$cur_size
      fi
      local stall_dur=$((now - stall_at))
      local elapsed=$((now - start))
      if [ "$stall_dur" -ge "$WATCHDOG_STALL_S" ]; then
        echo "[watchdog] no progress for ${stall_dur}s, killing nix (pid=$nix_pid)" >&2
        kill -KILL $nix_pid 2>/dev/null
        return
      fi
      if [ "$elapsed" -ge "$WATCHDOG_MAX_S" ]; then
        echo "[watchdog] ${elapsed}s exceeded ${WATCHDOG_MAX_S}s budget, killing nix" >&2
        kill -KILL $nix_pid 2>/dev/null
        return
      fi
    done
  ) &
  local watch_pid=$!

  wait $nix_pid
  local rc=$?
  kill $watch_pid 2>/dev/null
  wait $watch_pid 2>/dev/null
  return $rc
}

i=0
while [ $i -lt $COPY_ATTEMPTS ]; do
  i=$((i + 1))
  echo "Attempt $i/$COPY_ATTEMPTS: pulling closure $SEEKR_CHAIN_NIX_CLOSURE from $SEEKR_CHAIN_NIX_STORE..."
  if run_copy; then
    break
  fi
  if [ $i -ge $COPY_ATTEMPTS ]; then
    echo "Closure pull failed after $COPY_ATTEMPTS attempts. Exiting so k8s can reschedule."
    exit 1
  fi
  echo "Attempt $i failed (stall, timeout, or nix error). Retrying..."
  sleep 5
done

# Pull summary. Distinguishes "had it already" (hostPath warm cache) from
# "pulled fresh from s3" so the wow moment of "5 GB closure, 0.4s startup"
# is visible directly in the log.
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

# `|| true` not `|| echo 0`: grep -c outputs "0" *and* exits 1 on no matches;
# `|| echo 0` would give multi-line output and break later arithmetic.
PATHS_PULLED=$(grep -c "^copying path '/nix/store/" "$LOG" 2>/dev/null || true)
PATHS_PULLED=${PATHS_PULLED:-0}

if [ "$FAST_PATH" = "1" ]; then
  # Closure was fully present: by construction PATHS_PULLED=0, no bytes
  # transferred, no disk delta. Skip the post-pull du AND the path-info
  # walk over the closure graph — neither informs the summary in a way
  # the user can't already see from the "fully present" log line.
  SIZE_AFTER=$SIZE_BEFORE
  BYTES_PULLED=0
  # Track the count for the summary header. path-info --recursive on a
  # fully-local closure is ~1s; cheap relative to what we just saved.
  CLOSURE_PATHS=$(nix --store "local?root=/nix-shared" path-info \
                    --recursive "$SEEKR_CHAIN_NIX_CLOSURE" 2>/dev/null | wc -l)
  CLOSURE_PATHS=${CLOSURE_PATHS:-0}
  CLOSURE_SIZE=$(nix --store "local?root=/nix-shared" path-info \
                   --closure-size "$SEEKR_CHAIN_NIX_CLOSURE" 2>/dev/null \
                   | awk '{print $2+0}')
  CLOSURE_SIZE=${CLOSURE_SIZE:-0}
else
  SIZE_AFTER=$(du -sk /nix-shared 2>/dev/null | awk 'BEGIN{kb=0} {kb=$1+0} END{print kb*1024}')
  BYTES_PULLED=$((SIZE_AFTER - SIZE_BEFORE))
  if [ "$BYTES_PULLED" -lt 0 ]; then BYTES_PULLED=0; fi

  CLOSURE_PATHS=$(nix --store "local?root=/nix-shared" path-info \
                    --recursive "$SEEKR_CHAIN_NIX_CLOSURE" 2>/dev/null | wc -l)
  CLOSURE_PATHS=${CLOSURE_PATHS:-0}
  CLOSURE_SIZE=$(nix --store "local?root=/nix-shared" path-info \
                   --closure-size "$SEEKR_CHAIN_NIX_CLOSURE" 2>/dev/null \
                   | awk '{print $2+0}')
  CLOSURE_SIZE=${CLOSURE_SIZE:-0}
fi

PATHS_HIT=$((CLOSURE_PATHS - PATHS_PULLED))
if [ "$PATHS_HIT" -lt 0 ]; then PATHS_HIT=0; fi
BYTES_SAVED=$((CLOSURE_SIZE - BYTES_PULLED))
if [ "$BYTES_SAVED" -lt 0 ]; then BYTES_SAVED=0; fi

if [ "$CLOSURE_PATHS" -gt 0 ]; then
  HIT_PCT=$(awk "BEGIN { printf \"%.1f\", 100 * $PATHS_HIT / $CLOSURE_PATHS }")
else
  HIT_PCT="n/a"
fi
if [ "$DURATION" -gt 0 ] && [ "$BYTES_PULLED" -gt 0 ]; then
  SPEED=$(awk "BEGIN { printf \"%.2f MB/s\", $BYTES_PULLED / $DURATION / 1048576 }")
else
  SPEED="—"
fi

fmt_bytes() {
  awk -v b="$1" 'BEGIN {
    if (b >= 1073741824) printf "%.2f GB", b/1073741824
    else if (b >= 1048576) printf "%.2f MB", b/1048576
    else if (b >= 1024) printf "%.2f KB", b/1024
    else printf "%d B", b
  }'
}

cat <<EOF

===================================================================
  chain-nix-init summary
===================================================================
  Closure:                 $SEEKR_CHAIN_NIX_CLOSURE
  Total closure size:      $(fmt_bytes "$CLOSURE_SIZE")  ($CLOSURE_PATHS paths)

  Already on node:         $PATHS_HIT paths  ($HIT_PCT% hit) — saved $(fmt_bytes "$BYTES_SAVED")
  Pulled from cache:       $PATHS_PULLED paths,  $(fmt_bytes "$BYTES_PULLED")
  Duration:                ${DURATION}s
  Effective speed:         $SPEED
===================================================================
EOF

# Size-bounded GC of the hostPath warm cache. No-op when under budget.
# Pass SIZE_AFTER through so the GC script skips its own `du -sk`
# (which would otherwise walk the entire 14+ GB store again).
# `|| true`: GC failures shouldn't fail the pod — pulling the closure
# succeeded, the user's pod should run. Worst case is the store stays
# oversize until the next pod cleans it.
export SEEKR_CHAIN_NIX_STORE_CURRENT_BYTES=$SIZE_AFTER
sh /seekr-chain/resources/nix-gc.sh || true
