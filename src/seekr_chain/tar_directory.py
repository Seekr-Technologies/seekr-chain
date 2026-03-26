#!/usr/bin/env python3

import fnmatch
import os
import tarfile
from pathlib import Path
from typing import List, Optional


def _path_boundary_match(relpath: str, needle: str) -> bool:
    """
    True if `needle` occurs in relpath at a path boundary, and matches either:
      - the exact path, or
      - a prefix of relpath (i.e. relpath is under that directory)
    `relpath` and `needle` are expected to start with '/'.
    """
    if not needle.startswith("/"):
        raise ValueError("needle must start with '/'")
    if not relpath.startswith("/"):
        raise ValueError("relpath must start with '/'")

    # Fast path: anchored prefix match
    if relpath == needle or relpath.startswith(needle + "/"):
        return True

    # Unanchored: find needle in relpath at a boundary, and ensure it's a whole path component sequence.
    # Example: needle="/src" matches "/a/src" and "/a/src/file", but not "/a/src2".
    start = 0
    while True:
        idx = relpath.find(needle, start)
        if idx == -1:
            return False

        # Left boundary: must be at start or preceded by '/'
        if idx != 0 and relpath[idx - 1] != "/":
            start = idx + 1
            continue

        # Right boundary: must be end or followed by '/'
        end_idx = idx + len(needle)
        if end_idx == len(relpath) or relpath[end_idx] == "/":
            return True

        start = idx + 1


def _matches_patterns(
    root: Path,
    fname: str,
    patterns: List[str],
    input_path: Path,
    *,
    is_dir: bool,
) -> bool:
    """
    Matching rules:
      - If pattern ends with '/', it's directory-only AND matches subtree.
      - If pattern starts with '/', it's anchored at archive root (path-based).
      - If pattern contains '/', it's path-based (relative path inside archive).
      - Otherwise it's name-based (basename only).
    """
    relpath = "/" + str((root.relative_to(input_path) / fname))

    for pattern in patterns:
        if not pattern:
            continue

        dir_only = pattern.endswith("/")
        pat = pattern[:-1] if dir_only else pattern

        anchored = pat.startswith("/")
        pat_no_anchor = pat[1:] if anchored else pat

        # Directory subtree patterns (".../") should match the directory and anything under it.
        if dir_only:
            # If we're evaluating a file but the pattern is dir-only, it may still match
            # if the file is inside a matching directory subtree.
            # Also: if dir_only and no '/', treat it as matching any path segment.
            if "/" not in pat_no_anchor:
                # Directory name subtree match:
                # - "venv/" matches any path component named "venv" (dir subtree)
                # - "/venv/" matches only the top-level "venv" subtree
                parts = relpath.strip("/").split("/")  # "/c/venv/a" -> ["c", "venv", "a"]

                # For dir-only patterns:
                # - if we're evaluating a directory, the directory name itself counts
                # - if we're evaluating a file, only parent directories count (exclude last component)
                haystack = parts if is_dir else parts[:-1]

                if anchored:
                    # anchored means top-level directory subtree only
                    # for files, require at least one more component under the dir
                    if haystack and haystack[0] == pat_no_anchor:
                        return True
                else:
                    if pat_no_anchor in haystack:
                        return True
            else:
                # path-based subtree (e.g. "/a/b/" or "a/b/")
                needle = "/" + pat_no_anchor if not anchored else "/" + pat_no_anchor
                if anchored:
                    if relpath == needle or relpath.startswith(needle + "/"):
                        return True
                else:
                    if _path_boundary_match(relpath, needle):
                        return True

            continue

        # Non-subtree patterns:
        # Path-based if anchored OR contains a slash after stripping leading slash.
        is_path_pattern = anchored or ("/" in pat_no_anchor)

        if is_path_pattern:
            # Compare against relpath.
            # Anchored: match from archive root.
            if anchored:
                needle = "/" + pat_no_anchor
                if fnmatch.fnmatch(relpath, needle):
                    return True
            else:
                # Unanchored path pattern: allow it to appear anywhere in the relpath.
                # We do this by trying boundary placements.
                needle = "/" + pat_no_anchor
                if fnmatch.fnmatch(relpath, needle):
                    return True
                if fnmatch.fnmatch(relpath, "*/" + pat_no_anchor):
                    return True
        else:
            # Name-only: match basename
            if fnmatch.fnmatch(fname, pat_no_anchor):
                return True

    return False


def _should_include(
    root: Path,
    fname: str,
    *,
    include: Optional[List[str]],
    exclude: List[str],
    input_path: Path,
    is_dir: bool,
) -> bool:
    # If include is set, must match at least one include pattern
    if include:
        if not _matches_patterns(root, fname, include, input_path, is_dir=is_dir):
            return False

    # Exclude always wins
    if exclude and _matches_patterns(root, fname, exclude, input_path, is_dir=is_dir):
        return False

    return True


def tar_directory(
    input_path: Path,
    output_path: Path,
    *,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
) -> None:
    """
    Compress directory, respecting include/exclude patterns.

    Semantics:
      - include: if provided and non-empty, only paths matching include are eligible.
      - exclude: paths matching exclude are always omitted (exclude wins).
      - Patterns ending with '/' are directory subtree patterns (match dir + descendants).
      - Leading '/' anchors to archive root.
      - Traversal is pruned ONLY by exclude (so include can match descendants).
      - Empty dirs are added explicitly if they pass include/exclude.
    """
    if exclude is None:
        exclude = []

    with tarfile.open(output_path, "w:gz") as tar:
        for root, dirs, files in os.walk(input_path, followlinks=True, topdown=True):
            root_path = Path(root)

            # Prune traversal only by exclude
            dirs[:] = [d for d in dirs if not _matches_patterns(root_path, d, exclude, input_path, is_dir=True)]

            # Add empty dirs explicitly (if they pass include/exclude)
            for d in dirs:
                dir_path = root_path / d
                if not any(dir_path.iterdir()):
                    if _should_include(
                        root_path,
                        d,
                        include=include,
                        exclude=exclude,
                        input_path=input_path,
                        is_dir=True,
                    ):
                        tar.add(dir_path, arcname=dir_path.relative_to(input_path))

            for f in files:
                if not _should_include(
                    root_path,
                    f,
                    include=include,
                    exclude=exclude,
                    input_path=input_path,
                    is_dir=False,
                ):
                    continue

                file_path = root_path / f
                tar.add(
                    file_path.resolve(),
                    arcname=file_path.relative_to(input_path),
                    recursive=False,
                )
