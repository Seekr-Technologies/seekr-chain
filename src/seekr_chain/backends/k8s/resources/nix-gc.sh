#!/bin/sh
# nix-gc: size-bounded cleanup for the warm-node hostPath store.
#
# Invoked at the end of chain-nix-init, AFTER the closure pull succeeded.
# If on-disk size of /nix-shared exceeds SEEKR_CHAIN_NIX_STORE_MAX_BYTES,
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

MAX_BYTES="${SEEKR_CHAIN_NIX_STORE_MAX_BYTES:-53687091200}"  # 50 GiB

CURRENT_BYTES="${SEEKR_CHAIN_NIX_STORE_CURRENT_BYTES:-}"
if [ -z "$CURRENT_BYTES" ]; then
  CURRENT_BYTES=$(du -sk /nix-shared 2>/dev/null | awk '{print $1 * 1024}')
fi
CURRENT_BYTES=${CURRENT_BYTES:-0}

if [ "$CURRENT_BYTES" -le "$MAX_BYTES" ]; then
  echo "[nix-gc] store size $(($CURRENT_BYTES / 1048576)) MiB / $(($MAX_BYTES / 1048576)) MiB budget — no GC needed"
  exit 0
fi

echo "[nix-gc] store size $(($CURRENT_BYTES / 1048576)) MiB exceeds $(($MAX_BYTES / 1048576)) MiB budget — collecting"

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
GCROOT_DIR="/nix-shared/nix/var/nix/gcroots/seekr-chain"
mkdir -p "$GCROOT_DIR"
ln -sfn "$SEEKR_CHAIN_NIX_CLOSURE" "$GCROOT_DIR/active"

OVERAGE=$((CURRENT_BYTES - MAX_BYTES))
echo "[nix-gc] running 'nix store gc --max $OVERAGE'"

# nix store gc respects gcroots: the just-pulled closure and everything
# reachable from it (transitive deps) are LIVE and will not be deleted.
# Everything else (older pulls' closures + their unshared deps) is dead
# and gets freed up to --max bytes. `|| true` so a GC failure (lock
# contention, etc.) doesn't fail the init container.
nix --store "local?root=/nix-shared" store gc --max "$OVERAGE" 2>&1 || true

FINAL_BYTES=$(du -sk /nix-shared 2>/dev/null | awk '{print $1 * 1024}')
FINAL_BYTES=${FINAL_BYTES:-0}
FREED=$((CURRENT_BYTES - FINAL_BYTES))
echo "[nix-gc] freed $(($FREED / 1048576)) MiB; store now $(($FINAL_BYTES / 1048576)) MiB / $(($MAX_BYTES / 1048576)) MiB budget"
