import os
import shutil
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterator


def _matches_pattern(name: str, relative_path: str, pattern: str) -> bool:
    """
    Check if a name/path matches a pattern.

    Handles:
    - Simple patterns: "*.py", "test_*"  -> match filename or full path
    - Anchored patterns: "/file.txt"     -> match from root
    - Path patterns: "/dir/file.txt"     -> match specific path from root
    - Wildcards in paths: "/c/*"         -> match with wildcards
    """
    if pattern.startswith("/"):
        # Anchored pattern - must match from root
        pattern_clean = pattern.lstrip("/")

        # Use fnmatch to handle wildcards in the pattern
        return fnmatch(relative_path, pattern_clean)
    else:
        # Unanchored pattern - match filename or anywhere in path
        return fnmatch(name, pattern) or fnmatch(relative_path, pattern)


def _dir_matches_exclude(dir_name: str, partial_path: str, exclude: list[str]) -> bool:
    """True if a directory named `dir_name`, at root-relative path `partial_path`,
    matches an exclude pattern (as a directory). Shared by `_is_in_excluded_directory`
    (checking each ancestor of a file) and traversal pruning (checking a directory as
    we descend into it) so both stay in sync."""
    for pattern in exclude:
        if pattern.endswith("/"):
            # Pattern with trailing slash - matches only directories
            dir_pattern = pattern.rstrip("/")

            if dir_pattern.startswith("/"):
                # Anchored directory pattern - check if path starts with this pattern
                anchor_pattern = dir_pattern.lstrip("/")
                # Check if we're at or inside this anchored directory
                if partial_path == anchor_pattern or partial_path.startswith(anchor_pattern + "/"):
                    return True
            else:
                # Unanchored directory pattern - match anywhere
                if fnmatch(dir_name, dir_pattern):
                    return True
        else:
            # Pattern without trailing slash - matches files or directories
            if pattern.startswith("/"):
                # Anchored pattern for directory check
                pattern_clean = pattern.lstrip("/")

                # Check if we're inside this anchored path
                if partial_path == pattern_clean or fnmatch(partial_path, pattern_clean):
                    return True
            else:
                # Unanchored - check directory name
                if fnmatch(dir_name, pattern):
                    return True
    return False


def _is_in_excluded_directory(relative_path: Path, exclude: list[str]) -> bool:
    """Check if the path is inside an excluded directory."""
    # Check each directory in the path hierarchy (excluding the file itself)
    # For a file at path "a/b/c.txt", we check directories "a" and "a/b", not "c.txt"
    for i in range(len(relative_path.parts) - 1):
        dir_name = relative_path.parts[i]
        partial_path = str(Path(*relative_path.parts[: i + 1]))
        if _dir_matches_exclude(dir_name, partial_path, exclude):
            return True
    return False


def _pattern_could_match_under(dir_parts: tuple[str, ...], pattern: str) -> bool:
    """True if `pattern` could still match a path at or below a directory whose
    root-relative path components are `dir_parts`. Conservative: only returns False
    when a match is provably impossible, so it's safe to use for pruning traversal.

    - A pattern with no '/' (after stripping an optional trailing '/') can match by
      basename or directory name at any depth, so it's never prunable.
    - A pattern containing '/' is always compared against the full root-relative path
      regardless of a leading '/' (a bare basename can never match a slash-containing
      pattern), so it's effectively root-anchored - safe to prefix-check component by
      component from position 0.
    """
    p = pattern[:-1] if pattern.endswith("/") else pattern
    anchored = p.startswith("/")
    components = (p.lstrip("/") if anchored else p).split("/")
    if not anchored and len(components) == 1:
        return True
    n = min(len(dir_parts), len(components))
    return all(fnmatch(dir_parts[i], components[i]) for i in range(n))


def _is_in_included_directory(relative_path: Path, include: list[str]) -> bool:
    """Check if the path is inside an included directory."""
    # Check each directory in the path hierarchy (excluding the file itself)
    for i in range(len(relative_path.parts) - 1):
        dir_name = relative_path.parts[i]

        # Check against include patterns ending with /
        for pattern in include:
            if pattern.endswith("/"):
                # Directory inclusion pattern
                dir_pattern = pattern.rstrip("/")

                if dir_pattern.startswith("/"):
                    # Anchored directory pattern - only match at root level
                    anchor_pattern = dir_pattern.lstrip("/")
                    if i == 0 and fnmatch(dir_name, anchor_pattern):
                        return True
                else:
                    # Unanchored directory pattern - match anywhere
                    if fnmatch(dir_name, dir_pattern):
                        return True
    return False


def _should_include(path: Path, relative_path: str, include: list[str], exclude: list[str]) -> bool:
    """Check if a path should be included based on include/exclude rules."""
    rel_path = Path(relative_path)

    # Check if file is inside an excluded directory
    if _is_in_excluded_directory(rel_path, exclude):
        return False

    # Check against exclude patterns for the file itself
    for pattern in exclude:
        # Patterns ending with '/' only match directories, not files
        if pattern.endswith("/"):
            continue

        if _matches_pattern(path.name, relative_path, pattern):
            return False

    # If include list is empty, include everything (except excluded)
    if not include:
        return True

    # Check if file is in an included directory
    if _is_in_included_directory(rel_path, include):
        return True

    # Check against include patterns for the file itself
    for pattern in include:
        # Skip directory-only patterns - already checked above
        if pattern.endswith("/"):
            continue

        if _matches_pattern(path.name, relative_path, pattern):
            return True

    return False


# Shared by symlink() and copy_filtered() so both select the identical file
# set; only the final per-file operation (symlink vs copy) differs.
def _iter_included_files(
    src_path: Path,
    include: list[str],
    exclude: list[str],
    follow_links: bool,
) -> Iterator[tuple[Path, Path]]:
    """Yield (src_file, relative_path) for every file passing include/exclude."""
    for root, dirs, files in os.walk(src_path, followlinks=follow_links, topdown=True):
        root_path = Path(root)

        # Prune subdirectories that provably can't contain an included file, so
        # os.walk never descends into (or stats) huge unrelated trees.
        kept_dirs = []
        for d in dirs:
            relative_dir = (root_path / d).relative_to(src_path)
            if exclude and _dir_matches_exclude(d, str(relative_dir), exclude):
                continue
            if include and not any(_pattern_could_match_under(relative_dir.parts, p) for p in include):
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for file in files:
            src_file = root_path / file
            relative_path = src_file.relative_to(src_path)
            if _should_include(src_file, str(relative_path), include, exclude):
                yield src_file, relative_path


def symlink(src, dst, include: list[str] | None = None, exclude: list[str] | None = None, follow_links: bool = True):
    """
    Symlink files from src to dst with include/exclude filtering.

    Args:
        src: Source directory path
        dst: Destination directory path
        include: List of patterns to include (gitignore-style):
                - 'name' matches files or directories named 'name' anywhere
                - 'name/' matches only directories named 'name' anywhere
                - '/name' matches files/dirs named 'name' only at root
                - '/name/' matches only directories named 'name' only at root
                - '/path/to/file' matches specific path from root
                If empty/None, includes everything by default
        exclude: List of patterns to exclude (same syntax as include)
                Exclude takes precedence over include
        follow_links: If True, follow symbolic links to directories and files

    Example:
        symlink('source/', 'dest/', include=['*.py'], exclude=['test_*.py', '.git/'])
    """
    src_path = Path(src).resolve()
    dst_path = Path(dst).resolve()

    include = include or []
    exclude = exclude or []

    # Create destination directory if it doesn't exist
    dst_path.mkdir(parents=True, exist_ok=True)

    for src_file, relative_path in _iter_included_files(src_path, include, exclude, follow_links):
        dst_file = dst_path / relative_path

        # Create parent directory only when needed
        dst_file.parent.mkdir(parents=True, exist_ok=True)

        # Remove existing symlink/file if it exists
        if dst_file.exists() or dst_file.is_symlink():
            dst_file.unlink()

        # Create symlink
        dst_file.symlink_to(src_file)


def copy_filtered(
    src, dst, include: list[str] | None = None, exclude: list[str] | None = None, follow_links: bool = True
):
    """
    Copy real files from src to dst under the same include/exclude rules as
    ``symlink()``.

    Unlike ``symlink()`` this materializes real file content (``shutil.copy2``,
    dereferencing symlinks the same way ``tar_directory`` does at upload time).
    Needed for nix flake eval: ``nix``'s ``path:`` fetcher preserves symlinks
    into the store, so a symlink tree would leave dangling store paths. Copying
    the same selection the upload uses keeps submit-time eval byte-identical to
    what the build pod builds.
    """
    src_path = Path(src).resolve()
    dst_path = Path(dst).resolve()

    include = include or []
    exclude = exclude or []

    dst_path.mkdir(parents=True, exist_ok=True)

    for src_file, relative_path in _iter_included_files(src_path, include, exclude, follow_links):
        dst_file = dst_path / relative_path
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        # copy2 follows symlinks (copies target content), matching tar_directory's
        # file_path.resolve() dereference so both produce the same bytes.
        shutil.copy2(src_file, dst_file)
