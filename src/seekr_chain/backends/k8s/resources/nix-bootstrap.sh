#!/bin/sh
# nix-bootstrap: seed the shared /nix volume from the runner image's own
# preserved toolchain before any real nix command runs against it.
#
# Shared by chain-nix-init.sh (pull-pod init container) and nix-build.sh
# (build-pod main container) — both mount the node's shared hostPath (or
# emptyDir) volume directly at /nix, which replaces whatever the runner
# image itself had there. The runner image's Dockerfile preserves a full
# copy of its own /nix at /nix-baked specifically so this script can
# reseed /nix from it. Content-addressed store paths are immutable, so
# `cp -a -u` (archive, update) is always safe to re-run.
#
# Invoked as a subprocess (`sh /seekr-chain/resources/nix-bootstrap.sh`),
# not sourced: this script sets `set -eu`, and sourcing it would leak
# those shell options into the caller. nix-build.sh in particular only
# sets `set -e` today — a subprocess call lets `set -e` in the caller
# abort on a non-zero exit here without silently also picking up `-u` as
# a side effect.
#
# The lock file below lives on the shared hostPath volume, keyed only by
# its presence — not by which pod created it. This means it also
# arbitrates races between a build pod and a consumer pod landing on the
# same fresh node concurrently: whichever wins `mkdir` first bootstraps,
# the other waits on BOOTSTRAP_DONE. No special-casing needed since both
# callers run the identical operation.
#
# Plain POSIX sh, deliberately: `local` (the only non-strictly-POSIX
# construct used here, inside do_bootstrap_copy()) is a de facto
# standard extension supported by busybox ash, dash, and bash alike,
# despite not being specified by POSIX itself — verified directly
# against dash. Nothing else in this script (no process substitution,
# no arrays, no `[[`, no pipelines) needs a real bash — in particular,
# there's no `|` anywhere below, so `pipefail` was never doing anything;
# dropped it since Ubuntu's dash (unlike busybox ash) doesn't support it.
#
# /tmp (used below for BOOTSTRAP_MKDIR_ERR) is a container-local ephemeral
# path in both callers, but arrives there differently: chain-nix-init's
# init container gets its own container filesystem (no explicit /tmp
# mount), while nix-build.sh's main container gets the shared `tmp`
# emptyDir mount. Harmless either way — just worth being explicit now
# that this script runs under two different /tmp setups.
set -eu

# Overridable so tests can point the script at a throwaway tree instead of
# the real /nix and /nix-baked; unset in production, where they're always
# the real mounts. Same idea as nix-gc.sh's SEEKR_CHAIN_NIX_ROOT.
NIX_ROOT="${SEEKR_CHAIN_NIX_ROOT:-/nix}"
NIX_BAKED="${SEEKR_CHAIN_NIX_BAKED_ROOT:-/nix-baked}"

BOOTSTRAP_DONE="$NIX_ROOT/.seekr-chain-bootstrap.done"
BOOTSTRAP_LOCK="$NIX_ROOT/.seekr-chain-bootstrap.lock"
BOOTSTRAP_MKDIR_ERR=/tmp/seekr-chain-bootstrap-mkdir.err
# Overridable so tests can exercise the stale-lock reclaim path without a
# real 300s wait; matches nix_utils.py's SEEKR_CHAIN_NIX_EVAL_TIMEOUT_S.
BOOTSTRAP_WAIT_S="${SEEKR_CHAIN_NIX_BOOTSTRAP_WAIT_S:-300}"

# Runs the actual /nix-baked -> /nix copy. Assumes the caller already
# holds BOOTSTRAP_LOCK. Factored out so both the normal
# "we won the lock" path and the "reclaimed a stale lock" recovery
# path below run the identical copy, instead of two copies of this
# logic silently drifting apart over time.
do_bootstrap_copy() {
  local start
  start=$(date +%s)
  # One bulk copy, single process, no per-file fork/exec overhead.
  # `/nix-baked/.` (GNU's "copy contents" convention) silently copies
  # nothing at all under BusyBox's cp, verified empirically --
  # `/nix-baked/*` copies the visible top-level entries (store/, var/
  # -- confirmed that's everything directly under /nix in the base
  # image, no top-level dotfiles); the second glob catches hidden
  # entries too, without erroring when none match.
  #
  # -u (update: copy only if source is newer or dest doesn't exist)
  # rather than -n: verified empirically that BusyBox's -n skips an
  # entire directory the instant the destination entry already
  # exists, rather than recursing in and checking file-by-file like
  # GNU cp does -- so on anything but a pristine /nix, -n would
  # silently skip merging the toolchain in at all. -u recurses and
  # merges correctly. Whether -u's timestamp check ever gets a
  # "skip vs. copy" call "wrong" doesn't matter for correctness here
  # -- nix store paths are content-addressed and immutable, so a path
  # that already exists is guaranteed byte-identical; the only thing
  # -u affects is how many bytes get redundantly rewritten on a retry
  # after a prior attempt crashed mid-copy (before reaching `touch
  # "$BOOTSTRAP_DONE"` below), never whether the result ends up
  # correct.
  cp -a -u "$NIX_BAKED"/* "$NIX_ROOT"/
  for f in "$NIX_BAKED"/.[!.]*; do
    [ -e "$f" ] && cp -a -u "$f" "$NIX_ROOT"/
  done
  touch "$BOOTSTRAP_DONE"
  # || true: the lock-reclaim path below can race a legitimately-still-
  # alive original holder finishing at the same moment -- whichever of the
  # two rmdirs runs second would otherwise hit "no such directory" and,
  # under set -e, abort the whole script even though bootstrap succeeded.
  rmdir "$BOOTSTRAP_LOCK" 2>/dev/null || true
  echo "bootstrap copy done in $(($(date +%s) - start))s"
}

if [ ! -e "$BOOTSTRAP_DONE" ]; then
  if [ ! -d "$NIX_BAKED" ]; then
    # A cp against a missing /nix-baked would still fail under set -e,
    # but with a bare "No such file or directory" that gives no hint
    # this means "wrong runner image" -- /nix-baked only exists
    # because the runner image's own Dockerfile explicitly preserves
    # it for exactly this bootstrap step. Name the real cause instead.
    echo "fatal: /nix-baked is missing -- this image was not built with the" >&2
    echo "seekr-nix-runner Dockerfile's toolchain-preservation step, so there is" >&2
    echo "nothing to bootstrap /nix from. Check role.nix_init.image (or, for a" >&2
    echo "build step, role.nix.build's runner image)." >&2
    exit 1
  fi
  if mkdir "$BOOTSTRAP_LOCK" 2>"$BOOTSTRAP_MKDIR_ERR"; then
    echo "bootstrapping /nix from the runner image's preserved toolchain..."
    do_bootstrap_copy
  elif [ -d "$BOOTSTRAP_LOCK" ]; then
    # Genuine race: another pod already holds the lock. Only possible
    # with a shared (hostPath) volume — mkdir is atomic on POSIX
    # filesystems, so exactly one concurrently-starting pod on the
    # same node wins. Wait for it to finish instead of racing a
    # second copy onto the same volume. With a per-pod volume
    # (emptyDir), this branch can never be reached — the lock dir
    # can't exist yet on a fresh, exclusively-owned volume.
    echo "another pod is bootstrapping /nix on this node — waiting..."
    # Loop rather than a single wait+reclaim: when the wait times out,
    # every waiting pod races to reclaim the lock, but mkdir is atomic so
    # only one wins. Without this loop, every loser treated "lock dir
    # already exists" as a genuine mkdir error and exited 1 -- a
    # thundering herd where N-1 waiters fail outright instead of simply
    # resuming the wait behind the pod that just won.
    while [ ! -e "$BOOTSTRAP_DONE" ]; do
      i=0
      while [ ! -e "$BOOTSTRAP_DONE" ] && [ "$i" -lt "$BOOTSTRAP_WAIT_S" ]; do
        sleep 1
        i=$((i + 1))
      done
      if [ -e "$BOOTSTRAP_DONE" ]; then
        break
      fi
      # The bootstrap copy is a local /nix-baked -> /nix copy of a
      # fixed, bounded toolchain -- it should never take anywhere near
      # 300s regardless of caller (chain-nix-init.sh or nix-build.sh
      # invoke the identical operation here). Timing out here almost
      # always means the pod that created the lock crashed or was
      # killed before reaching `rmdir` above, leaving a permanently-
      # stuck lock: every future pod scheduled on this node would
      # otherwise wait the same 300s and fail forever, since nothing
      # ever removes a lock its own creator didn't clean up. Reclaim
      # it and let THIS pod retry the bootstrap once instead of
      # propagating a one-time crash into a permanent per-node
      # deadlock.
      echo "timed out waiting for /nix bootstrap -- assuming the lock holder" >&2
      echo "died without finishing; reclaiming $BOOTSTRAP_LOCK and retrying..." >&2
      rmdir "$BOOTSTRAP_LOCK" 2>/dev/null || true
      if mkdir "$BOOTSTRAP_LOCK" 2>"$BOOTSTRAP_MKDIR_ERR"; then
        do_bootstrap_copy
        break
      elif [ -d "$BOOTSTRAP_LOCK" ]; then
        # Lost the reclaim race to another waiter -- not a real error,
        # just resume waiting on whoever won.
        echo "another pod won the bootstrap-lock reclaim race — resuming wait..." >&2
        continue
      else
        # mkdir failed for a real reason (permissions, read-only mount,
        # missing parent, etc.), not because the dir already exists.
        echo "failed to reclaim bootstrap lock at $BOOTSTRAP_LOCK:" >&2
        cat "$BOOTSTRAP_MKDIR_ERR" >&2
        exit 1
      fi
    done
  else
    # mkdir failed for a real reason (permissions, read-only mount,
    # missing parent, etc.) — NOT a race, since the lock dir doesn't
    # actually exist. Surface the real error instead of silently
    # treating it as "someone else has it" and hanging until timeout
    # — this exact conflation was a real bug: on a private (emptyDir)
    # volume, no other pod can ever hold this lock, so a failed mkdir
    # there always means something else is genuinely wrong.
    echo "failed to create bootstrap lock at $BOOTSTRAP_LOCK:" >&2
    cat "$BOOTSTRAP_MKDIR_ERR" >&2
    exit 1
  fi
fi
