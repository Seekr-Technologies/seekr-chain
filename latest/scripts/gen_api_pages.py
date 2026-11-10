from __future__ import annotations

import importlib
import inspect
import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import mkdocs_gen_files


@dataclass(frozen=True)
class Config:
    # Dotted name of the top-level package
    root_pkg: str = "my_pkg"
    # Where to write docs under docs/
    out_dir: str = "api"


def _public_member_names(mod) -> list[str]:
    """
    Names of public members of a module/package.

    Preference:
      1) module.__all__ if present and non-empty
      2) otherwise: dir(mod) filtered to non-underscore, and skip submodules
    """
    names = getattr(mod, "__all__", None)
    if isinstance(names, (list, tuple)) and names:
        return [n for n in names if isinstance(n, str)]

    out: list[str] = []
    for n in dir(mod):
        if n.startswith("_"):
            continue
        try:
            obj = getattr(mod, n)
        except Exception:
            continue
        # If you want modules to show up as "submodules" only, skip them here.
        if inspect.ismodule(obj):
            continue
        out.append(n)

    # keep deterministic output
    return sorted(set(out))


def _is_module(obj: Any) -> bool:
    return inspect.ismodule(obj)


def _public_exports(mod) -> list[tuple[str, Any]]:
    names = getattr(mod, "__all__", None)
    if not names:
        return []
    out: list[tuple[str, Any]] = []
    for name in names:
        if not isinstance(name, str):
            continue
        try:
            out.append((name, getattr(mod, name)))
        except AttributeError:
            # __all__ contains something not present; ignore
            continue
    return out


def _in_root_namespace(dotted: str, root_pkg: str) -> bool:
    return dotted == root_pkg or dotted.startswith(root_pkg + ".")


def _doc_dir_for_module(dotted: str, cfg: Config) -> PurePosixPath:
    # my_pkg.submod_a -> api/my_pkg/submod_a
    parts = dotted.split(".")
    return PurePosixPath(cfg.out_dir, *parts)


def _write_index_page(
    dotted: str,
    doc_dir: PurePosixPath,
    *,
    submodules: list[tuple[str, str]],
) -> None:
    mod = importlib.import_module(dotted)
    members = _public_member_names(mod)

    with mkdocs_gen_files.open(str(doc_dir / "index.md"), "w") as f:
        f.write(f"# `{dotted}`\n\n")

        if submodules:
            f.write("## Submodules\n\n")
            for name, rel_link in sorted(submodules):
                # Make each one a heading so it appears in the right-side TOC
                f.write(f"### [{name}]({rel_link})\n\n")

        if members:
            f.write("## Methods\n\n")
            f.write(f"::: {dotted}\n")
            f.write("    options:\n")
            f.write("      members:\n")
            for name in sorted(members):
                f.write(f"        - {name}\n")
            f.write("      show_root_heading: false\n")
            f.write("      show_root_toc_entry: false\n")
            f.write("      heading_level: 3\n")  # nest under "## Methods"
            f.write("\n")


def _walk_and_generate(dotted: str, cfg: Config, visited: set[str]) -> None:
    if dotted in visited:
        return
    visited.add(dotted)

    mod = importlib.import_module(dotted)
    doc_dir = _doc_dir_for_module(dotted, cfg)

    exports = _public_exports(mod)  # uses __all__ only

    submodules: list[tuple[str, str]] = []

    for name, obj in sorted(exports):
        if _is_module(obj):
            child_dotted = obj.__name__
            if not _in_root_namespace(child_dotted, cfg.root_pkg):
                continue

            child_doc_dir = _doc_dir_for_module(child_dotted, cfg)
            rel_link = posixpath.relpath(str(child_doc_dir / "index.md"), start=str(doc_dir)).rstrip("/")
            submodules.append((name, rel_link))

            _walk_and_generate(child_dotted, cfg, visited)

    _write_index_page(dotted, doc_dir, submodules=submodules)


def main() -> None:
    cfg = Config(root_pkg="seekr_chain", out_dir="api")
    visited: set[str] = set()
    _walk_and_generate(cfg.root_pkg, cfg, visited)


main()
