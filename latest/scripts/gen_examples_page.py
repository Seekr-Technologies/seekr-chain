"""Generate the Examples page from the examples/ directory.

This script is invoked by mkdocs-gen-files during `mkdocs build`.
It walks each subdirectory under examples/, reads an optional README.md
for the description, and embeds all other files with syntax highlighting.
"""

from pathlib import Path

import mkdocs_gen_files

OUT_PATH = "examples.md"
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"

# File extensions to syntax-highlight language mapping
LANG_MAP = {
    ".py": "python",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "bash",
    ".toml": "toml",
    ".json": "json",
    ".txt": "",
}


def _lang_for(path: Path) -> str:
    return LANG_MAP.get(path.suffix, "")


def main() -> None:
    import sys

    print("[gen_examples_page] Starting...", file=sys.stderr)

    lines: list[str] = []
    lines.append("# Examples\n")
    lines.append(
        "Each example below can be run with:\n"
        "\n"
        "```bash\n"
        "chain submit examples/<name>/config.yaml --follow\n"
        "```\n"
        "\n"
        "Source code is in the [`examples/`]({{ examples_url }}) directory.\n"
    )

    if not EXAMPLES_DIR.is_dir():
        print(f"[gen_examples_page] WARNING: {EXAMPLES_DIR} not found", file=sys.stderr)
        return

    for example_dir in sorted(EXAMPLES_DIR.iterdir()):
        if not example_dir.is_dir():
            continue

        # Derive a human-readable title from the directory name
        # e.g. "0_hello_world" -> "Hello World"
        raw_name = example_dir.name
        # Strip leading number + underscore (e.g. "0_")
        title = raw_name.lstrip("0123456789").lstrip("_")
        title = title.replace("_", " ").title()

        lines.append(f"## {title}\n")

        # Read optional README.md for description
        readme = example_dir / "README.md"
        if readme.exists():
            desc = readme.read_text().strip()
            if desc:
                lines.append(f"{desc}\n")

        # Collect files (config.yaml first, then others, skip README)
        files = sorted(example_dir.iterdir())
        config_file = example_dir / "config.yaml"
        ordered_files = []
        if config_file.exists():
            ordered_files.append(config_file)
        for f in files:
            if f.is_file() and f != config_file and f.name != "README.md":
                ordered_files.append(f)

        for filepath in ordered_files:
            rel_path = filepath.relative_to(EXAMPLES_DIR.parent)
            lang = _lang_for(filepath)
            content = filepath.read_text().rstrip()

            lines.append(f"**`{rel_path}`**\n")
            lines.append(f"```{lang}")
            lines.append(content)
            lines.append("```\n")

    content = "\n".join(lines)

    print(f"[gen_examples_page] Writing {len(lines)} lines to {OUT_PATH}", file=sys.stderr)

    with mkdocs_gen_files.open(OUT_PATH, "w") as f:
        f.write(content)

    print("[gen_examples_page] Done.", file=sys.stderr)


try:
    main()
except Exception as e:
    import sys
    import traceback

    print(f"[gen_examples_page] ERROR: {e}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
