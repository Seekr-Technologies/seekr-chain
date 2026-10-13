#!/bin/sh
# Static Nix post-build hook for nix-build.sh.
#
# Nix invokes this after each successful derivation and supplies its realized
# outputs in OUT_PATHS. Each output gets its own directory entry: mkdir's
# directory creation is atomic, so concurrent hook processes never share an
# append file or expose a partially written record.
set -u
umask 077

JOURNAL=${SEEKR_CHAIN_NIX_BUILD_JOURNAL:-}
if [ -z "$JOURNAL" ] || [ ! -d "$JOURNAL" ]; then
    echo "nix post-build hook: journal directory is unavailable" >&2
    exit 0
fi

for store_path in ${OUT_PATHS:-}; do
    case "$store_path" in
      /nix/store/*)
        store_name=${store_path#/nix/store/}
        case "$store_name" in
          ""|*/*)
            echo "nix post-build hook: ignoring malformed output path: $store_path" >&2
            ;;
          *)
            # An existing directory means another concurrent hook recorded the
            # same output first. Other failures remain visible without turning
            # an otherwise successful derivation into a failed build.
            if ! mkdir "$JOURNAL/$store_name" 2>/dev/null && [ ! -d "$JOURNAL/$store_name" ]; then
                echo "nix post-build hook: failed to journal output: $store_path" >&2
            fi
            ;;
        esac
        ;;
      *)
        echo "nix post-build hook: ignoring non-store output path: $store_path" >&2
        ;;
    esac
done

exit 0
