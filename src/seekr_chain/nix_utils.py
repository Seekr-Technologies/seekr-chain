"""Helpers for resolving nix expressions and checking binary cache contents.

The submit path uses these to:

- Turn a user-supplied nix expression + attribute into a concrete
  ``/nix/store/<hash>-<name>`` closure path (``eval_closure_path``).
- Check whether that closure already exists in the configured binary
  cache (``closure_exists``).

Evaluation requires ``nix`` on the local PATH. Evaluation is pure (no
compilation, no system-specific code execution) so a Mac can resolve
the closure path for an ``x86_64-linux`` expression — only the
*realization* (building) needs to happen on the target system, which
seekr-chain hands off to an in-cluster build step.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class NixNotInstalledError(RuntimeError):
    """Raised when ``nix`` is required on the submit machine but isn't on PATH.

    Install from https://nixos.org/download.
    """


class NixEvalError(RuntimeError):
    """Raised when ``nix eval`` exits non-zero (syntax error, missing attr, etc.)."""


def is_nix_installed() -> bool:
    """Return True iff ``nix`` is on the local PATH."""
    return shutil.which("nix") is not None


def closure_hash_from_path(closure_path: str) -> str:
    """Extract the content-addressed hash from a ``/nix/store/<hash>-<name>`` path.

    The hash is the leading component of the basename (everything before
    the first ``-``). It's the same hash that names the ``<hash>.narinfo``
    in a binary cache, which is what we look up to test existence.
    """
    if not closure_path.startswith("/nix/store/"):
        raise ValueError(
            f"expected absolute /nix/store path, got {closure_path!r}"
        )
    basename = closure_path.removeprefix("/nix/store/")
    hash_part, _, _ = basename.partition("-")
    if not hash_part:
        raise ValueError(
            f"could not extract hash from {closure_path!r} "
            "(expected /nix/store/<hash>-<name>)"
        )
    return hash_part


def eval_closure_path(expression: str, attr: str = "default", system: str = "x86_64-linux") -> str:
    """Evaluate ``<expression>#<attr>.outPath`` and return the closure store path.

    Parameters
    ----------
    expression
        Path to a ``.nix`` file or to a directory containing ``flake.nix``.
        Relative paths are resolved against the current working directory.
    attr
        Attribute path within the expression. Defaults to ``"default"``,
        which for a flake means ``packages.<system>.default``. For a flake
        the ``--system`` argument selects the system entry.
    system
        Target system (default ``x86_64-linux``). Eval is pure, so this
        works cross-system on a Mac; only realization needs to match.

    Raises
    ------
    NixNotInstalledError
        If ``nix`` isn't on PATH.
    NixEvalError
        If ``nix eval`` exits non-zero. The stderr is included in the
        exception message — usually a syntax error or missing attribute.
    """
    if not is_nix_installed():
        raise NixNotInstalledError(
            "nix is required on the submit machine to evaluate `nix.expression`. "
            "Install from https://nixos.org/download."
        )

    expr_path = Path(expression).resolve()
    if not expr_path.exists():
        raise FileNotFoundError(f"nix expression path does not exist: {expr_path}")

    # Both flake.nix and plain .nix are supported.
    # - Flake (dir with flake.nix OR `.#attr` syntax):  use `nix eval` with flake ref.
    # - Plain .nix file:  pass as a path expression `import <file>`-style.
    if expr_path.is_dir() and (expr_path / "flake.nix").exists():
        target = f"path:{expr_path}#packages.{system}.{attr}.outPath"
    elif expr_path.suffix == ".nix" and expr_path.is_file():
        # For a classic .nix file, evaluate the attr on the imported expression.
        # We can't use the flake ref syntax; use --expr to wrap.
        target_expr = f"((import {expr_path}) {{}}).{attr}.outPath" if attr != "default" \
                      else f"((import {expr_path}) {{}}).outPath"
        # nix eval --raw --impure --expr '...'
        cmd = ["nix", "eval", "--raw", "--impure", "--expr", target_expr]
        return _run_nix_eval(cmd, expression, attr)
    else:
        raise ValueError(
            f"nix.expression must point to a .nix file or a directory containing "
            f"flake.nix; got {expr_path}"
        )

    cmd = ["nix", "eval", "--raw", target]
    return _run_nix_eval(cmd, expression, attr)


def _run_nix_eval(cmd: list[str], expression: str, attr: str) -> str:
    """Run `nix eval`, returning the closure path on stdout.

    Stderr is passed through to the parent's terminal so the user sees
    nix's download / build progress (which can take minutes on a cold
    nixpkgs unstable fetch). Without this, ``chain submit`` looks hung
    while nix silently downloads ~500 MB. Stderr isn't captured for
    the error message — if eval fails, the user has already seen the
    error scroll past on stderr.
    """
    logger.info(
        "Evaluating nix expression %r (attr=%r) — `nix eval` output follows",
        expression,
        attr,
    )
    logger.debug("running nix eval: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            text=True,
            check=False,
            # stderr=None: inherit parent's stderr so progress prints live.
        )
    except FileNotFoundError as e:
        # PATH-changed-after-import case.
        raise NixNotInstalledError("nix binary not found on PATH") from e

    if result.returncode != 0:
        raise NixEvalError(
            f"`nix eval` failed for expression={expression!r} attr={attr!r} "
            f"(exit {result.returncode}); see error output above."
        )

    out = result.stdout.strip()
    if not out.startswith("/nix/store/"):
        raise NixEvalError(
            f"nix eval returned an unexpected output (expected /nix/store/...): {out!r}"
        )
    logger.info("Resolved closure path: %s", out)
    return out


def closure_exists(store_uri: str, closure_path: str) -> bool:
    """Return True iff the closure's narinfo exists at the configured store.

    Looks up ``{store_uri}/{hash}.narinfo`` in the store. For ``s3://`` URIs
    we use boto3 directly (already a seekr-chain dep). For other schemes
    (``oci://``, ``azure://`` etc.) we route through seekr-fs, imported
    lazily — those users need to ``pip install seekr-fs`` themselves.
    """
    hash_ = closure_hash_from_path(closure_path)
    narinfo_uri = f"{store_uri.rstrip('/')}/{hash_}.narinfo"

    if narinfo_uri.startswith("s3://"):
        import boto3

        from seekr_chain import s3_utils

        s3 = boto3.client("s3")
        return s3_utils.exists(narinfo_uri, s3)

    try:
        import seekr_fs as sfs
    except ImportError as e:
        raise ImportError(
            f"nix.store={store_uri!r} uses a non-s3 scheme; seekr-fs is required "
            "for that. Install it with `pip install seekr-fs` (or any compatible "
            "internal source)."
        ) from e
    return sfs.exists(narinfo_uri)


# Label every nix-mode pod (consumer or build) carries: identifies the
# closure that pod fetched/produced. Used both by the rendered podAffinity
# (concurrent co-scheduling) and by find_warm_nodes() below.
NIX_CLOSURE_LABEL = "seekr-chain.nix/closure"


def find_warm_nodes(
    closure_hash: str,
    namespace: str,
    limit: int = 10,
    partial_limit: int = 20,
) -> tuple[list[str], list[str]]:
    """Return (exact, partial) warm-cache node names for a closure.

    One k8s API call (label-existence selector for ``NIX_CLOSURE_LABEL``);
    partitioning happens client-side, in two passes:

    - **exact**: nodes whose nix-mode pods carry
      ``NIX_CLOSURE_LABEL == closure_hash``. The closure literally lives
      at ``/nix-shared/nix/store/<hash>-...`` on that node — substituters
      hit local disk for the full closure on next consume. Rendered as a
      high-weight nodeAffinity preference (weight 90 in jobset.py).

    - **partial**: nodes whose nix-mode pods carry the label with *any
      other* value (and never the requested one). The exact closure isn't
      there, but other closures' paths are — many will be shared
      (glibc, gcc, bash, openssl, …), so substituters hit local disk for
      the overlap even on a "cold" closure. Rendered as a low-weight
      nodeAffinity preference (weight 30 in jobset.py). Disjoint from
      ``exact`` — a node never appears in both.

    Recency ordering uses the most-recent pod's ``creation_timestamp``
    per node; each list is independently capped.

    Returns ``([], [])`` on any error (kubeconfig not set, RBAC denied,
    network unreachable, …). Warm-cache is a soft hint; a missing one
    means the scheduler falls back to a cold pull. Never raises.
    """
    try:
        from seekr_chain import k8s_utils
    except ImportError:
        return [], []

    try:
        v1 = k8s_utils.get_core_v1_api()
        # Existence-only selector: returns every nix-mode pod, regardless
        # of which closure it pulled. Partitioning is cheap in Python and
        # halves the API load compared to two separate queries.
        result = v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=NIX_CLOSURE_LABEL,
        )
    except Exception as e:
        logger.warning(
            "could not query warm nodes for closure %s in %s: %s; "
            "scheduler will pick without warm-cache hint",
            closure_hash, namespace, e,
        )
        return [], []

    pods = [p for p in result.items if p.spec.node_name]
    pods.sort(key=lambda p: p.metadata.creation_timestamp or 0, reverse=True)

    # Pass 1: which nodes have AT LEAST ONE exact-match pod? Those are
    # always exact — even if a more recent pod on the same node belongs
    # to a different closure. The closure paths are on disk either way.
    exact_nodes: set[str] = set()
    for p in pods:
        labels = p.metadata.labels or {}
        if labels.get(NIX_CLOSURE_LABEL) == closure_hash:
            exact_nodes.add(p.spec.node_name)

    # Pass 2: recency walk. Each node lands in its pre-determined bucket
    # at the timestamp of its most-recent pod (which sorted to the front).
    exact: list[str] = []
    partial: list[str] = []
    seen_exact: set[str] = set()
    seen_partial: set[str] = set()
    for p in pods:
        node = p.spec.node_name
        if node in exact_nodes:
            if node not in seen_exact and len(exact) < limit:
                exact.append(node)
                seen_exact.add(node)
        else:
            if node not in seen_partial and len(partial) < partial_limit:
                partial.append(node)
                seen_partial.add(node)
    return exact, partial
