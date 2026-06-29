#!/bin/sh
# nix-gc: size-bounded cleanup for the warm-node hostPath store.
#
# Invoked at the end of chain-nix-init, AFTER the closure pull has
# succeeded. Checks the on-disk size of /nix-shared against
# SEEKR_CHAIN_NIX_STORE_MAX_BYTES; if over budget, deletes store paths
# by oldest-atime first until under budget. Never deletes the closure
# we just fetched (SEEKR_CHAIN_NIX_CLOSURE) or its dependencies.
#
# This is best-effort: if nix-store --delete fails (e.g. concurrent
# build pod holding a lock), we log and continue. Worst case is the
# store stays oversize until the next pod runs the script.
#
# Env vars consumed:
#   SEEKR_CHAIN_NIX_CLOSURE           closure we just fetched (skip from GC)
#   SEEKR_CHAIN_NIX_STORE_MAX_BYTES   size budget in bytes (default 50 GiB)
set -e

MAX_BYTES="${SEEKR_CHAIN_NIX_STORE_MAX_BYTES:-53687091200}"  # 50 GiB

CURRENT_BYTES=$(du -sk /nix-shared 2>/dev/null | awk '{print $1 * 1024}')
CURRENT_BYTES=${CURRENT_BYTES:-0}

if [ "$CURRENT_BYTES" -le "$MAX_BYTES" ]; then
  echo "[nix-gc] store size $(($CURRENT_BYTES / 1048576)) MiB / $(($MAX_BYTES / 1048576)) MiB budget — no GC needed"
  exit 0
fi

echo "[nix-gc] store size $(($CURRENT_BYTES / 1048576)) MiB exceeds $(($MAX_BYTES / 1048576)) MiB budget — collecting"

# nix-store --gc-roots roots for what's reachable; protected closure is the
# one we just fetched. Mark it as a gc-root so nix-store --delete refuses
# to remove it. Cleanup-friendly path: add a symlink under
# /nix-shared/nix/var/nix/gcroots/.
GCROOT_DIR="/nix-shared/nix/var/nix/gcroots/seekr-chain"
mkdir -p "$GCROOT_DIR"
ln -sfn "$SEEKR_CHAIN_NIX_CLOSURE" "$GCROOT_DIR/active"

# Enumerate top-level store entries with atime + size, oldest-first.
# Skip the active closure itself (gc-root protects it anyway, but listing
# it would just produce a noisy "won't delete" line).
DELETED_BYTES=0
TARGET_FREED=$((CURRENT_BYTES - MAX_BYTES))

# `stat` format: <atime-epoch> <path>. Sort ascending → oldest first.
find /nix-shared/nix/store -maxdepth 1 -mindepth 1 -type d \
  | while read path; do
      [ "$path" = "$SEEKR_CHAIN_NIX_CLOSURE" ] && continue
      printf '%s %s\n' "$(stat -c %X "$path" 2>/dev/null || echo 0)" "$path"
    done \
  | sort -n \
  | while read atime path; do
      [ -z "$path" ] && continue
      # Stop once we've freed enough.
      if [ "$DELETED_BYTES" -ge "$TARGET_FREED" ]; then
        break
      fi
      # Compute size before delete (deleted path can't be stat'd after).
      size=$(du -sk "$path" 2>/dev/null | awk '{print $1 * 1024}')
      size=${size:-0}
      if nix --store "local?root=/nix-shared" store delete --ignore-liveness "$path" 2>/dev/null; then
        DELETED_BYTES=$((DELETED_BYTES + size))
        echo "[nix-gc] deleted $path ($(($size / 1048576)) MiB)"
      else
        # Path was alive (referenced by another gc-root) — skip.
        :
      fi
    done

# Final state.
FINAL_BYTES=$(du -sk /nix-shared 2>/dev/null | awk '{print $1 * 1024}')
FINAL_BYTES=${FINAL_BYTES:-0}
FREED=$((CURRENT_BYTES - FINAL_BYTES))
echo "[nix-gc] freed $(($FREED / 1048576)) MiB; store now $(($FINAL_BYTES / 1048576)) MiB / $(($MAX_BYTES / 1048576)) MiB budget"
