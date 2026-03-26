import os
from fnmatch import fnmatch
from pathlib import Path


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


def _is_in_excluded_directory(relative_path: Path, exclude: list[str]) -> bool:
    """Check if the path is inside an excluded directory."""
    # Check each directory in the path hierarchy (excluding the file itself)
    # For a file at path "a/b/c.txt", we check directories "a" and "a/b", not "c.txt"
    for i in range(len(relative_path.parts) - 1):
        dir_name = relative_path.parts[i]

        # Check against all exclude patterns
        for pattern in exclude:
            if pattern.endswith("/"):
                # Pattern with trailing slash - matches only directories
                dir_pattern = pattern.rstrip("/")

                if dir_pattern.startswith("/"):
                    # Anchored directory pattern - check if path starts with this pattern
                    anchor_pattern = dir_pattern.lstrip("/")
                    # Build path up to this point
                    partial_path = str(Path(*relative_path.parts[: i + 1]))
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
                    # Build partial path and see if it matches
                    partial_path = str(Path(*relative_path.parts[: i + 1]))
                    pattern_clean = pattern.lstrip("/")

                    # Check if we're inside this anchored path
                    if partial_path == pattern_clean or fnmatch(partial_path, pattern_clean):
                        return True
                else:
                    # Unanchored - check directory name
                    if fnmatch(dir_name, pattern):
                        return True
    return False


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

    # Walk through source directory
    for root, dirs, files in os.walk(src_path, followlinks=follow_links):
        root_path = Path(root)

        # Process files
        for file in files:
            src_file = root_path / file
            relative_path = src_file.relative_to(src_path)

            if _should_include(src_file, str(relative_path), include, exclude):
                dst_file = dst_path / relative_path

                # Create parent directory only when needed
                dst_file.parent.mkdir(parents=True, exist_ok=True)

                # Remove existing symlink/file if it exists
                if dst_file.exists() or dst_file.is_symlink():
                    dst_file.unlink()

                # Create symlink
                dst_file.symlink_to(src_file)
