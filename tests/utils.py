"""Shared filesystem helpers for unit tests."""

from pathlib import Path


def _populate(path: Path, structure: dict):
    """
    Create directory structure from a dict.

    Dict format:
    - key: filename or dirname
    - value: list -> file with lines of content
    - value: dict -> directory with nested structure

    Example:
        {
            "file.txt": ["line1", "line2"],
            "subdir": {
                "nested.py": ["print('hello')"]
            }
        }
    """
    path.mkdir(parents=True, exist_ok=True)

    for name, content in structure.items():
        item_path = path / name

        if isinstance(content, dict):
            _populate(item_path, content)
        elif isinstance(content, list):
            item_path.write_text("\n".join(content))
        else:
            raise ValueError(f"Invalid content type for {name}: {type(content)}")


def _files_to_dict(base: Path) -> dict:
    """
    Read a directory tree into a dict.

    Returns a dict with the same format as _populate expects:
    - files map to a list of their lines
    - directories map to a nested dict
    """
    result = {}
    for item in sorted(base.iterdir()):
        if item.is_file():
            result[item.name] = item.read_text().split("\n")
        elif item.is_dir():
            result[item.name] = _files_to_dict(item)
    return result
