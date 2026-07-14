#!/bin/sh
# chain-nix-init: fetch a nix closure into the node's shared /nix volume
# before main starts. Reads SEEKR_CHAIN_NIX_STORE + SEEKR_CHAIN_NIX_CLOSURE
# from its env (set by _resolve_nix_role).
#
# Mount layout: this container mounts the shared volume directly at /nix
# with subPath=nix (see _nix_init_container.yaml.j2) — the SAME mount
# main uses, so both containers see an identical, unified store. That
# mount replaces whatever the runner image itself had at /nix, so the
# very first thing this script does is bootstrap /nix from /nix-baked
# via nix-bootstrap.sh (shared with nix-build.sh, the build pod's main
# container, which mounts /nix identically) so a working nix toolchain
# exists before any real nix command runs.
#
# Earlier design used `nix --store "local?root=/nix-shared"` to redirect
# writes into a separate, non-conflicting mount instead of bootstrapping
# — abandoned because `nix-store --restore`/`--register-validity
# --hash-given` were found to silently ignore a chroot store's root= for
# path resolution (only --add/--check-validity respect it), making that
# approach a dead end for anything but real nix's own unified `nix
# copy`/`nix build`.
#
# `--no-check-sigs` is a v1 shortcut; production should configure signing.
#
# Plain POSIX sh, deliberately: `command: ["/bin/sh"]` in
# _nix_init_container.yaml.j2 resolves to busybox ash in the
# nix-runner image, which cannot parse bash's process substitution
# (`2> >(tee ...)`) at all -- syntax error, script never runs. This
# script never uses that; run_copy() below instead tees `nix copy`'s
# stderr through a named pipe (mkfifo), which is fully POSIX and
# works identically under ash, dash, or bash. `local` (used throughout
# run_copy() and its watchdog subshell) is a de facto standard
# extension supported by ash/dash/bash alike despite not being
# POSIX-specified -- verified directly against dash.
set -euo pipefail

# Wall-clock accounting: the pull's own "Duration" summary below only
# brackets run_copy() -- it doesn't cover the bootstrap copy that runs
# before it or nix-gc.sh that runs after, both of which count toward
# this container's real lifetime as seen by kubelet. Print an explicit
# total at the end so a gap between kubelet's container-start/stop
# timestamps and the pull's own reported Duration is never a mystery.
SCRIPT_START_TIME=$(date +%s)

sh /seekr-chain/resources/nix-bootstrap.sh

{
  echo 'experimental-features = nix-command flakes'
  echo 'sandbox = false'
  echo 'filter-syscalls = false'
  echo 'download-attempts = 8'
  # stalled-download-timeout only applies to HTTP/libcurl, not to s3
  # transport. Set it anyway in case the store URI is changed to http.
  echo 'stalled-download-timeout = 60'
  # This role never substitutes from anywhere: the actual closure
  # transfer goes through `nix copy --from "$SEEKR_CHAIN_NIX_STORE"`
  # below (an explicit --from, not a substituter), and every other
  # `nix path-info` call in this script is meant to be a pure local-DB
  # presence check. Without this, those checks fall through to
  # whatever substituters the runner image's own nix.conf configures
  # for its own purposes and pay a real (multi-second, per call) tax
  # probing each one before concluding "not present" -- same fix
  # nix-build.sh already applies for its own role by setting its own
  # substituters list.
  #
  # This relies on nix.conf's last-write-wins semantics for repeated
  # keys: appending `substituters =` here only clears the runner
  # image's own earlier `substituters = ...` line because nix reads
  # the LAST occurrence of a key, not the first or a merge of both --
  # verified empirically (nix 2.34.7): a nix.conf with
  # `substituters = https://cache.nixos.org` followed later by a bare
  # `substituters =` resolves via `nix show-config` to an empty
  # substituters list, not the cache.nixos.org value. If this were
  # instead first-write-wins or additive, this line would silently do
  # nothing and every path-info call above would keep paying the
  # substituter-probe tax this exists to avoid.
  echo 'substituters ='
} >> /etc/nix/nix.conf

# aws-sdk-cpp timeouts. These ARE honored on s3:// substituters (where
# nix's internal stalled-download-timeout doesn't reach). 10 min per
# request gives ~3 MB/s threshold for a 1.65 GB NAR — anything slower
# is "stuck", not "slow". 10s on connect catches DNS / TCP setup
# failures fast.
export AWS_REQUEST_TIMEOUT=600000
export AWS_CONNECT_TIMEOUT=10000

LOG=/tmp/nix-init.log
SIZE_BEFORE=$(du -sk /nix 2>/dev/null | awk 'BEGIN{kb=0} {kb=$1+0} END{print kb*1024}')
START_TIME=$(date +%s)

# Watchdog: monitors /nix size growth. If size doesn't change
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
  # /nix, we don't need to call out to s3 at all. `nix path-info
  # --recursive` is a local-store DB query (no network), and exits non-zero
  # the moment any path in the closure graph is missing — which means we
  # can use a successful exit as proof of full presence.
  #
  # This matters because nix copy --from, even when nothing needs to copy,
  # still fetches every narinfo in the closure graph from the remote cache
  # to compute the dep tree. That's ~50-100ms × N paths of serial s3
  # roundtrips → ~10s on a 200-path closure even when everything is local.
  if nix path-info --recursive \
       "$SEEKR_CHAIN_NIX_CLOSURE" >/dev/null 2>&1; then
    echo "Closure already fully present on node — skipping s3 fetch."
    FAST_PATH=1
    return 0
  fi

  # POSIX-sh substitute for bash's `2> >(tee -a "$LOG" >&2)`: a named
  # pipe fed into a backgrounded `tee`, started before the writer so it's
  # already waiting when nix opens its end. Once nix (the sole writer)
  # exits, tee sees EOF and finishes on its own -- reaped below via
  # `wait $tee_pid` alongside the watchdog.
  local copy_fifo=/tmp/nix-copy-stderr.fifo
  rm -f "$copy_fifo"
  mkfifo "$copy_fifo"
  tee -a "$LOG" >&2 < "$copy_fifo" &
  local tee_pid=$!

  nix copy \
      --from "$SEEKR_CHAIN_NIX_STORE" \
      --no-check-sigs \
      "$SEEKR_CHAIN_NIX_CLOSURE" 2>"$copy_fifo" &
  local nix_pid=$!

  (
    local start=$(date +%s)
    local last_size=$(du -sk /nix 2>/dev/null | awk 'BEGIN{kb=0} {kb=$1+0} END{print kb*1024}')
    local stall_at=$start
    while kill -0 $nix_pid 2>/dev/null; do
      sleep 30
      local now=$(date +%s)
      local cur_size=$(du -sk /nix 2>/dev/null | awk 'BEGIN{kb=0} {kb=$1+0} END{print kb*1024}')
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
  wait $tee_pid 2>/dev/null
  rm -f "$copy_fifo"
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
  CLOSURE_PATHS=$(nix path-info \
                    --recursive "$SEEKR_CHAIN_NIX_CLOSURE" 2>/dev/null | wc -l)
  CLOSURE_PATHS=${CLOSURE_PATHS:-0}
  CLOSURE_SIZE=$(nix path-info \
                   --closure-size "$SEEKR_CHAIN_NIX_CLOSURE" 2>/dev/null \
                   | awk '{print $2+0}')
  CLOSURE_SIZE=${CLOSURE_SIZE:-0}
else
  SIZE_AFTER=$(du -sk /nix 2>/dev/null | awk 'BEGIN{kb=0} {kb=$1+0} END{print kb*1024}')
  BYTES_PULLED=$((SIZE_AFTER - SIZE_BEFORE))
  if [ "$BYTES_PULLED" -lt 0 ]; then BYTES_PULLED=0; fi

  CLOSURE_PATHS=$(nix path-info \
                    --recursive "$SEEKR_CHAIN_NIX_CLOSURE" 2>/dev/null | wc -l)
  CLOSURE_PATHS=${CLOSURE_PATHS:-0}
  CLOSURE_SIZE=$(nix path-info \
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

echo "chain-nix-init total wall time: $(($(date +%s) - SCRIPT_START_TIME))s (bootstrap + pull + gc)"
