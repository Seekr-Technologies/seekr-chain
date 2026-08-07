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
#
# Do NOT add `pipefail`. The cosmetic `du`/`path-info | awk` stats below
# have right-hand sides that always succeed, so pipefail is the only
# thing that would propagate a left-hand failure -- and a `du` over a
# shared /nix goes non-zero whenever a peer pod mutates it mid-walk. That
# kills the init container over a number printed in a banner. Nothing
# here needs pipefail: `nix copy`, the one command whose failure matters,
# is backgrounded and reaped via `wait $nix_pid`, not piped.
set -eu

# Overridable so tests can point the script at throwaway paths instead of
# the real mounts; unset in production. Same idea as nix-gc.sh's and
# nix-bootstrap.sh's SEEKR_CHAIN_NIX_ROOT.
NIX_ROOT="${SEEKR_CHAIN_NIX_ROOT:-/nix}"
NIX_CONF="${SEEKR_CHAIN_NIX_CONF:-/etc/nix/nix.conf}"
RESOURCE_DIR="${SEEKR_CHAIN_RESOURCE_DIR:-/seekr-chain/resources}"

# Millisecond clock. `date +%s` is too coarse to attribute phases that
# differ by a few hundred ms, so probe once for something finer and stick
# with the result:
#
#   ns    `date +%s%N` (GNU coreutils) -- true millisecond resolution.
#   csec  /proc/uptime -- always present on Linux, 10ms resolution.
#   sec   `date +%s` -- last resort; every phase reads as a whole second.
#
# The runner image lands on csec: it's wolfi-base, whose `date` is busybox
# with no `%N` (glibc's strftime doesn't implement it), and coreutils is
# deliberately not baked into /nix -- see docker/Dockerfile. /proc/uptime
# is capped at 10ms because the kernel renders it "%lu.%02lu", two decimal
# places, so that's the file's format rather than anything lost here.
#
# CLOCK_DECIMALS keeps the printed precision honest: report exactly as
# many decimals as the chosen clock can actually resolve, never more.
_probe=$(date +%s%N 2>/dev/null || echo x)
case "$_probe" in
  # Non-digits mean %N came back literal or empty; a short string means it
  # expanded to nothing. Either way there are no nanoseconds here.
  *[!0-9]*) CLOCK_MODE=none ;;
  *) if [ "${#_probe}" -ge 19 ]; then CLOCK_MODE=ns; else CLOCK_MODE=none; fi ;;
esac
CLOCK_NOTE=""
CLOCK_DECIMALS=3
if [ "$CLOCK_MODE" = none ]; then
  if [ -r /proc/uptime ]; then
    CLOCK_MODE=csec
    CLOCK_DECIMALS=2
    CLOCK_NOTE="  (clock: /proc/uptime, 10ms resolution)"
  else
    CLOCK_MODE=sec
    CLOCK_DECIMALS=0
    CLOCK_NOTE="  (clock: whole seconds only)"
  fi
fi
unset _probe

now_ms() {
  case "$CLOCK_MODE" in
    ns)
      # Trim 6 digits rather than dividing: keeps the value inside the
      # 13-digit range every shell can do arithmetic on.
      _n=$(date +%s%N)
      echo "${_n%??????}"
      ;;
    csec)
      # "103466.72 993576.54" -> 103466720 ms. Strip one leading zero off
      # the fraction: `$((08))` is an invalid octal constant, not 8.
      read -r _up _rest < /proc/uptime
      _frac=${_up#*.}
      _frac=${_frac#0}
      echo "$(( ${_up%.*} * 1000 + ${_frac:-0} * 10 ))"
      ;;
    *)
      echo "$(( $(date +%s) * 1000 ))"
      ;;
  esac
}

# Milliseconds -> seconds, at the clock's real precision. Display only.
fmt_ms() {
  awk -v ms="$1" -v dec="$CLOCK_DECIMALS" \
    'BEGIN { printf "%." dec "f", ms / 1000 }'
}

# Wall-clock accounting: the pull's own "Duration" summary below only
# brackets run_copy() -- it doesn't cover the bootstrap copy that runs
# before it, the store-size walks, or nix-gc.sh that runs after, all of
# which count toward this container's real lifetime as seen by kubelet.
# The per-phase breakdown printed at the end attributes every millisecond,
# so a gap between kubelet's container-start/stop timestamps and the
# pull's own reported Duration is never a mystery.
SCRIPT_START_MS=$(now_ms)

# `set -e` exits silently, which in kubectl looks identical to a dozen
# other failure modes. This trap names the stage instead -- `$LINENO`
# would be more precise but isn't dependable under busybox ash, which
# `command: ["/bin/sh"]` resolves to in the runner image.
#
# Advance phases with `stage <name>`, never by assigning STAGE directly:
# that keeps the trap's label and the timing breakdown from drifting
# apart. Assign STAGE on its own only to add detail *within* a phase
# (the pull loop does this per attempt); the phase is still recorded
# under the name its `stage` call gave it.
STAGE="startup"
STAGE_LABEL="startup"
STAGE_START=$SCRIPT_START_MS
PHASE_TIMES=""

stage() {
  local now
  now=$(now_ms)
  # One "label|milliseconds" record per line, consumed by awk at the end.
  PHASE_TIMES="${PHASE_TIMES}${STAGE_LABEL}|$((now - STAGE_START))
"
  STAGE="$1"
  STAGE_LABEL="$1"
  STAGE_START=$now
}

trap 'rc=$?; if [ "$rc" -ne 0 ]; then
  echo "chain-nix-init: FAILED (exit $rc) during stage: $STAGE" >&2
fi' EXIT

stage "bootstrap"
sh "$RESOURCE_DIR/nix-bootstrap.sh"

stage "nix.conf setup"
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
} >> "$NIX_CONF"

# aws-sdk-cpp timeouts. These ARE honored on s3:// substituters (where
# nix's internal stalled-download-timeout doesn't reach). 10 min per
# request gives ~3 MB/s threshold for a 1.65 GB NAR — anything slower
# is "stuck", not "slow". 10s on connect catches DNS / TCP setup
# failures fast.
export AWS_REQUEST_TIMEOUT=600000
export AWS_CONNECT_TIMEOUT=10000

LOG=/tmp/nix-init.log

# On-disk size of $NIX_ROOT in bytes, best-effort. Callers use this for
# the summary banner and the stall watchdog, never for a correctness
# decision, so a failed measurement degrades to "0" rather than aborting.
#
# `du`'s exit status is discarded on purpose: on a shared hostPath /nix
# it goes non-zero whenever a peer pod renames or deletes an entry out
# from under the walk, and busybox's du is much less forgiving about that
# than GNU's. It says nothing about whether our closure is fine. Keep the
# `{ ...; } | awk` shape -- it makes the substitution's status awk's, so
# this survives someone reintroducing pipefail.
#
# stderr goes to $LOG, not /dev/null: when this does fail, the reason
# needs to be recoverable.
dir_size_bytes() {
  { du -sk "$NIX_ROOT" 2>>"$LOG" || true; } \
    | awk 'BEGIN{kb=0} {kb=$1+0} END{print kb*1024}'
}

# `nix path-info` wrapper for the summary stats. Same never-fatal
# contract as dir_size_bytes, but NOT silent: a non-zero exit here means
# the closure isn't fully registered in the local DB, which is a real
# problem. Tolerated so it can't kill a pod whose workload would run
# fine; warned about so it can't hide.
#
# "$@" is the path-info argument list; the caller pipes our stdout into
# whatever reducer it wants (awk, wc -l).
closure_stat() {
  if ! nix path-info "$@" 2>>"$LOG"; then
    echo "[chain-nix-init] warning: 'nix path-info $*' failed;" \
         "summary numbers below are incomplete. This can mean the" \
         "closure is not fully registered in the local nix DB --" \
         "see $LOG." >&2
  fi
}

stage "pre-pull store size (du)"
SIZE_BEFORE=$(dir_size_bytes)
START_MS=$(now_ms)

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
# (and we skip the s3 fetch). Used by the summary block to skip the
# post-pull `du` over the whole store: on a no-op pull the size cannot
# have changed, so re-walking a 100+ GiB tree to learn that would be
# pure waste (and, on a shared node, pure I/O contention with peers).
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
    local last_size=$(dir_size_bytes)
    local stall_at=$start
    while kill -0 $nix_pid 2>/dev/null; do
      sleep 30
      local now=$(date +%s)
      local cur_size=$(dir_size_bytes)
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

stage "closure pull"
i=0
while [ $i -lt $COPY_ATTEMPTS ]; do
  i=$((i + 1))
  # Detail within the pull phase, so assign STAGE rather than calling
  # stage() -- retries should not each open a new timing bucket.
  STAGE="closure pull, attempt $i/$COPY_ATTEMPTS"
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
#
# Everything from here to the banner is presentation only. The closure is
# already on the node and the pod's workload can run regardless of what
# these numbers say -- so nothing below may abort the script. That is the
# whole reason dir_size_bytes/closure_stat exist.
stage "summary stats (nix path-info)"
END_MS=$(now_ms)
DURATION_MS=$((END_MS - START_MS))

# `|| true` not `|| echo 0`: grep -c outputs "0" *and* exits 1 on no matches;
# `|| echo 0` would give multi-line output and break later arithmetic.
PATHS_PULLED=$(grep -c "^copying path '/nix/store/" "$LOG" 2>/dev/null || true)
PATHS_PULLED=${PATHS_PULLED:-0}

if [ "$FAST_PATH" = "1" ]; then
  # Closure was fully present: by construction PATHS_PULLED=0, no bytes
  # transferred, no disk delta. Skip the post-pull du — it could only
  # report the number we already have.
  SIZE_AFTER=$SIZE_BEFORE
  BYTES_PULLED=0
else
  SIZE_AFTER=$(dir_size_bytes)
  BYTES_PULLED=$((SIZE_AFTER - SIZE_BEFORE))
  if [ "$BYTES_PULLED" -lt 0 ]; then BYTES_PULLED=0; fi
fi

# Identical in both branches (on the fast path, path-info --recursive over
# a fully-local closure is ~1s -- cheap relative to the s3 fetch we just
# skipped), so it lives outside the if rather than being duplicated.
CLOSURE_PATHS=$(closure_stat --recursive "$SEEKR_CHAIN_NIX_CLOSURE" | wc -l)
CLOSURE_PATHS=${CLOSURE_PATHS:-0}
CLOSURE_SIZE=$(closure_stat --closure-size "$SEEKR_CHAIN_NIX_CLOSURE" \
                 | awk '{print $2+0}')
CLOSURE_SIZE=${CLOSURE_SIZE:-0}

PATHS_HIT=$((CLOSURE_PATHS - PATHS_PULLED))
if [ "$PATHS_HIT" -lt 0 ]; then PATHS_HIT=0; fi
BYTES_SAVED=$((CLOSURE_SIZE - BYTES_PULLED))
if [ "$BYTES_SAVED" -lt 0 ]; then BYTES_SAVED=0; fi

if [ "$CLOSURE_PATHS" -gt 0 ]; then
  HIT_PCT=$(awk "BEGIN { printf \"%.1f\", 100 * $PATHS_HIT / $CLOSURE_PATHS }")
else
  HIT_PCT="n/a"
fi
if [ "$DURATION_MS" -gt 0 ] && [ "$BYTES_PULLED" -gt 0 ]; then
  SPEED=$(awk "BEGIN { printf \"%.2f MB/s\", $BYTES_PULLED / ($DURATION_MS / 1000) / 1048576 }")
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
  Duration:                $(fmt_ms "$DURATION_MS")s
  Effective speed:         $SPEED
===================================================================
EOF

# Size-bounded GC of the hostPath warm cache. No-op when under budget.
# Pass SIZE_AFTER through so the GC script skips its own `du -sk`
# (which would otherwise walk the entire 14+ GB store again).
# `|| true`: GC failures shouldn't fail the pod — pulling the closure
# succeeded, the user's pod should run. Worst case is the store stays
# oversize until the next pod cleans it.
stage "nix-gc"
export SEEKR_CHAIN_NIX_STORE_CURRENT_BYTES=$SIZE_AFTER
sh "$RESOURCE_DIR/nix-gc.sh" || true

stage "done"

# Per-phase breakdown. The pull's own "Duration" above covers only
# run_copy(); this covers everything kubelet sees, so the two can differ
# by a lot. When they do, the culprit is usually one of the whole-store
# `du` walks or the post-pull `nix path-info` over the closure graph --
# both scale with the store, not with how much was actually transferred,
# and both get slower as peer pods contend for the same disk.
#
# Resolution depends on which clock the probe at the top settled on;
# CLOCK_NOTE says so when it's coarser than a millisecond.
TOTAL_MS=$(( $(now_ms) - SCRIPT_START_MS ))
echo
echo "chain-nix-init phase timing${CLOCK_NOTE}"
# PHASE_TIMES already ends in a newline, so the total is just one more
# record -- same awk, same column widths, no chance of the summary row
# formatting differently from the phases above it.
#
# Build the format in the shell rather than using awk's `%*.*f`: dynamic
# field width is not something busybox awk can be relied on for.
printf '%sTOTAL (kubelet-visible)|%s\n' "$PHASE_TIMES" "$TOTAL_MS" \
  | awk -F'|' -v fmt="  %-32s %8.${CLOCK_DECIMALS}fs\n" \
      '{ printf fmt, $1, $2 / 1000 }'
