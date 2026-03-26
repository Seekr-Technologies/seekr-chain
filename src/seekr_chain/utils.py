#!/usr/bin/env python3

import os
import random
import re
import string
from pathlib import Path


def resolve_image(image: str) -> str:
    """Prepend SEEKR_CHAIN_IMAGE_PREFIX to an image name when the env var is set."""
    prefix = os.environ.get("SEEKR_CHAIN_IMAGE_PREFIX", "")
    if prefix:
        return f"{prefix.rstrip('/')}/{image}"
    return image


def generate_id(N=6):
    """
    Generate N character ID, from set [a-z,0-9].

    First character is guaranteed to be letter
    """
    return "".join(
        random.choices(string.ascii_lowercase, k=1) + random.choices(string.ascii_lowercase + string.digits, k=N - 1)
    )


def human_to_int(byte_str: str) -> int:
    """Format human readable to integer"""
    prefixes = ["", "K", "M", "G", "T", "P"]
    match = re.fullmatch(r"^(\d+\.?\d*)([KMGTP])?(I)?$", byte_str.upper())
    if not match:
        raise ValueError(f"Unable to parse value: {byte_str}")

    value, prefix, base2 = match.groups()
    value = float(value)

    exponent = prefixes.index(prefix)
    base = 1000
    if base2:
        base = 1024

    result = value * base**exponent

    return int(result)


def format_bytes(num_bytes: int) -> str:
    """Format bytes into human-readable"""
    prefixes = ["", "K", "M", "G", "T", "P"]

    if num_bytes < 1024:
        return f"{int(num_bytes)} B"

    size = float(num_bytes)

    for prefix in prefixes:
        if size < 1024:
            if size < 1000:
                return f"{size:.3g} {prefix}iB"
            else:
                return f"{int(size)} {prefix}iB"
        size /= 1024

    return f"{size:.3g} {prefixes[-1]}iB"


def get_size(p: Path) -> int:
    """Get size of a file or directory (including symlinks as normal files)."""
    if p.is_file() or p.is_symlink():
        try:
            return p.stat().st_size
        except (OSError, FileNotFoundError):
            return 0
    elif p.is_dir():
        total = 0
        try:
            for item in p.rglob("*"):
                if item.is_file() or item.is_symlink():
                    try:
                        total += item.stat().st_size
                    except (OSError, FileNotFoundError):
                        continue
        except (OSError, PermissionError):
            pass
        return total
    return 0


def summarize_dir(path, sort_by_size: bool = True, detail: bool = False) -> str:
    """
    Print summary statistics for a directory. Includes symlinks as if they were normal files

    Example output:
    levels=0
        Total size: 1.4 GB
    levels=1
        Total size: 1.4 GB

        1.2 GB .venv/
        201 MB src/
        ...
    """
    path = Path(path)

    if not path.exists():
        return f"Path does not exist: {path}"

    # Calculate total size
    total_size = get_size(path)

    # Build output
    lines = [f"Total size: {format_bytes(total_size)}"]

    if detail:
        lines.append("Contents")  # Blank line

        items = []
        try:
            for item in path.iterdir():
                size = get_size(item)
                name = item.name
                if item.is_dir():
                    name += "/"
                items.append((size, name))
        except (OSError, PermissionError):
            pass

        # Sort by size (descending) or alphabetically
        if sort_by_size:
            items.sort(key=lambda x: x[0], reverse=True)
        else:
            items.sort(key=lambda x: x[1])

        # Calculate max width needed for size column
        max_width = max((len(format_bytes(size)) for size, _ in items), default=0)

        # Format items with right-aligned sizes
        for size, name in items:
            size_str = format_bytes(size)
            lines.append(f"  {size_str:>{max_width}}  {name}")

    return "\n".join(lines)
