#!/bin/sh
# nix-gc: size-bounded cleanup for the warm-node hostPath store.
#
# Invoked at the end of chain-nix-init, AFTER the closure pull succeeded.
# If on-disk size of /nix exceeds SEEKR_CHAIN_NIX_STORE_MAX_BYTES,
# delegates to nix's own GC, which deletes paths not reachable from any
# gcroot. We add a gcroot pointing at the closure we just pulled so it
# AND its transitive deps are protected.
#
# Best-effort: GC failures (lock contention, etc.) don't fail the pod —
# pulling succeeded, the user's workload should run. Worst case is the
# store stays oversize until the next pod runs the script.
#
# Env vars consumed:
#   SEEKR_CHAIN_NIX_CLOSURE             closure we just fetched (gcroot target)
#   SEEKR_CHAIN_NIX_STORE_MAX_BYTES     size budget in bytes (default 50 GiB)
#   SEEKR_CHAIN_NIX_STORE_CURRENT_BYTES current store size (optional; chain-nix-init
#                                       computes this for its own summary and
#                                       passes it through to avoid a redundant
#                                       du -sk over the 50+ GB tree).
set -e

# Overridable so tests can point the script at a throwaway tree instead of
# the real /nix; unset in production, where it's always the real mount.
NIX_ROOT="${SEEKR_CHAIN_NIX_ROOT:-/nix}"

MAX_BYTES="${SEEKR_CHAIN_NIX_STORE_MAX_BYTES:-53687091200}"  # 50 GiB

CURRENT_BYTES="${SEEKR_CHAIN_NIX_STORE_CURRENT_BYTES:-}"
if [ -z "$CURRENT_BYTES" ]; then
  # `|| true` plus the `${:-0}` below: du goes non-zero when a peer pod
  # renames or deletes an entry out from under the walk on a shared
  # hostPath store. That must not abort GC — and must stay safe if this
  # script ever gains `pipefail`. Same reason chain-nix-init.sh won't.
  CURRENT_BYTES=$({ du -sk "$NIX_ROOT" 2>/dev/null || true; } | awk '{print $1 * 1024}')
fi
CURRENT_BYTES=${CURRENT_BYTES:-0}

if [ "$CURRENT_BYTES" -le "$MAX_BYTES" ]; then
  echo "[nix-gc] store size $(($CURRENT_BYTES / 1048576)) MiB / $(($MAX_BYTES / 1048576)) MiB budget — no GC needed"
  exit 0
fi

echo "[nix-gc] store size $(($CURRENT_BYTES / 1048576)) MiB exceeds $(($MAX_BYTES / 1048576)) MiB budget — collecting"

# Serialize the gcroot-symlink + GC sequence below across pods racing on
# this node's shared hostPath /nix. `nix store gc` itself holds its own
# internal DB lock, so two concurrent invocations can't corrupt the
# store -- but the `ln -sfn ".../active"` gcroot below is a single
# shared path with no such protection: two pods updating it
# concurrently could interleave such that neither pod's closure ends
# up actually protected for the GC that follows. flock only serializes
# our OWN gcroot-then-gc sequence (best-effort; if flock isn't
# available, proceed anyway -- see the comment below).
GC_LOCK="$NIX_ROOT/.seekr-chain-nix-gc.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$GC_LOCK"
  if ! flock -w 60 9; then
    echo "[nix-gc] could not acquire GC lock within 60s (another pod is GC'ing on this node) — skipping this round" >&2
    exit 0
  fi
else
  # No flock on this image. Proceeding without cross-pod serialization
  # is the same behavior this script has always had (racing failures
  # here are silent-tolerated, same as the "|| true" on `nix store gc`
  # below and the older-pull-gcroot caveat above) -- not a regression,
  # just not actively guarded on an image without flock.
  echo "[nix-gc] flock not available on this image — proceeding without cross-pod GC serialization" >&2
fi

# Protect the closure we just pulled (plus all its transitive deps) via a
# gcroot symlink. The `ln -sfn` overwrites any previous symlink at this
# path, so only the MOST RECENT pull is rooted from seekr-chain's side.
# Older pulls' closures become unreachable, hence "dead", hence eligible
# for nix's GC to free — which is exactly what we want for the warm-cache
# eviction story.
#
# Caveat: a pod from an older pull whose main container is still running
# has no gcroot of its own, so nix would treat its closure as dead and
# free it out from under the running process. In practice the cluster
# runs one closure per node at a time (ML training jobs aren't multi-
# tenant per node), so this race is theoretical for v1. If it becomes a
# real problem, the fix is per-pod gcroots cleaned up on pod termination.
GCROOT_DIR="$NIX_ROOT/var/nix/gcroots/seekr-chain"
mkdir -p "$GCROOT_DIR"
ln -sfn "$SEEKR_CHAIN_NIX_CLOSURE" "$GCROOT_DIR/active"

OVERAGE=$((CURRENT_BYTES - MAX_BYTES))
echo "[nix-gc] running 'nix store gc --max $OVERAGE'"

# nix store gc respects gcroots: the just-pulled closure and everything
# reachable from it (transitive deps) are LIVE and will not be deleted.
# Everything else (older pulls' closures + their unshared deps) is dead
# and gets freed up to --max bytes. `|| true` so a GC failure (lock
# contention, etc.) doesn't fail the init container.
nix store gc --max "$OVERAGE" 2>&1 || true

FINAL_BYTES=$({ du -sk "$NIX_ROOT" 2>/dev/null || true; } | awk '{print $1 * 1024}')
FINAL_BYTES=${FINAL_BYTES:-0}
FREED=$((CURRENT_BYTES - FINAL_BYTES))
echo "[nix-gc] freed $(($FREED / 1048576)) MiB; store now $(($FINAL_BYTES / 1048576)) MiB / $(($MAX_BYTES / 1048576)) MiB budget"
