"""Generate a single Configuration Reference page from Pydantic models.

This script is invoked by mkdocs-gen-files during `mkdocs build`.
It walks the WorkflowConfig model tree and produces a nested markdown
page where each Pydantic model is rendered as a field table, with
nested models expanded inline under their parent.
"""

# NOTE: Do NOT use `from __future__ import annotations` here.
# We need runtime type objects (not strings) for isinstance checks.

import datetime
import enum
import inspect
import re
import types
from typing import Any, Literal, Union, get_args, get_origin

import mkdocs_gen_files
import pydantic

from seekr_chain import config as cfg

OUT_PATH = "reference/configuration.md"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# In Python 3.10+, `int | str` produces types.UnionType, not typing.Union.
_UNION_TYPES: tuple[type, ...] = (Union,)
if hasattr(types, "UnionType"):
    _UNION_TYPES = (Union, types.UnionType)


def _is_union(origin: Any) -> bool:
    return origin is not None and any(origin is t for t in _UNION_TYPES)


# ---------------------------------------------------------------------------
# Docstring parsing – extract per-field descriptions from numpy-style params
# ---------------------------------------------------------------------------


def _parse_docstring_params(cls: type) -> dict[str, str]:
    """Parse per-field descriptions from a class docstring.

    Supports both Google-style and NumPy-style docstrings::

        # Google-style
        Attributes:
            field_name: Description.
            field_name (type): Description.

        # NumPy-style
        Parameters
        ----------
        field_name : Description.
        field_name : type
            Description.
    """
    doc = inspect.getdoc(cls)
    if not doc:
        return {}

    params: dict[str, str] = {}
    in_section = False
    current_name = None
    current_desc_lines: list[str] = []

    for line in doc.splitlines():
        stripped = line.strip()

        # Google-style section header: "Attributes:" or "Parameters:"
        if stripped in ("Attributes:", "Parameters:"):
            in_section = True
            continue

        # NumPy-style section header: "Parameters" followed by "----------"
        if stripped == "Parameters":
            in_section = True
            continue
        if re.match(r"^-{3,}$", stripped):
            continue

        # End of section: another section header (Google or NumPy style)
        if in_section and re.match(r"^[A-Z]\w+:?$", stripped) and stripped.rstrip(":") not in (cls.model_fields or {}):
            in_section = False

        if not in_section:
            continue

        # Match "field_name: desc" or "field_name (type): desc" or "field_name : type\n    desc"
        m = re.match(r"^(\w+)(?:\s*\([^)]*\))?\s*:\s*(.*)$", stripped)
        if m:
            if current_name:
                params[current_name] = " ".join(current_desc_lines).strip()
            current_name = m.group(1)
            desc = m.group(2).strip()
            current_desc_lines = [desc] if desc else []
        elif current_name and stripped:
            current_desc_lines.append(stripped)

    if current_name:
        params[current_name] = " ".join(current_desc_lines).strip()

    return params


# ---------------------------------------------------------------------------
# Type formatting
# ---------------------------------------------------------------------------


def _format_type(annotation: Any) -> str:
    """Produce a clean, human-readable type string."""
    if annotation is type(None):
        return "None"

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Union (typing.Union or types.UnionType) and Optional
    if _is_union(origin):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and len(args) == 2:
            return f"{_format_type(non_none[0])}, optional"
        return " \\| ".join(_format_type(a) for a in non_none)

    if origin is list:
        if args:
            return f"list[{_format_type(args[0])}]"
        return "list"

    if origin is dict:
        if args:
            return f"dict[{_format_type(args[0])}, {_format_type(args[1])}]"
        return "dict"

    # Literal
    if origin is Literal:
        vals = ", ".join(repr(v) for v in args)
        return f"Literal[{vals}]"

    # Enum
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        vals = ", ".join(f'`"{m.value}"`' for m in annotation)
        return f"enum({vals})"

    # Pydantic model – render as anchor link
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return f"[{annotation.__name__}](#{annotation.__name__.lower()})"

    # datetime.timedelta
    if annotation is datetime.timedelta:
        return "duration"

    # builtins and fallback
    name = getattr(annotation, "__name__", None) or str(annotation)
    name = name.replace("typing.", "")
    return f"`{name}`"


def _format_default(default: Any) -> str:
    """Format a default value for display."""
    if default is pydantic.fields.PydanticUndefined:
        return "**required**"
    # Some pydantic versions use _Unset
    _unset = getattr(pydantic.fields, "_Unset", None)
    if _unset is not None and default is _unset:
        return "**required**"
    if default is None:
        return "`None`"
    if isinstance(default, datetime.timedelta):
        total = int(default.total_seconds())
        if total >= 86400 and total % 86400 == 0:
            return f"`{total // 86400}d`"
        if total >= 3600 and total % 3600 == 0:
            return f"`{total // 3600}h`"
        if total >= 60 and total % 60 == 0:
            return f"`{total // 60}m`"
        return f"`{total}s`"
    if isinstance(default, enum.Enum):
        return f'`"{default.value}"`'
    if isinstance(default, pydantic.BaseModel):
        return "*(see below)*"
    if isinstance(default, list):
        if not default:
            return "`[]`"
        return f"`{default}`"
    return f"`{default}`"


# ---------------------------------------------------------------------------
# Model tree walker
# ---------------------------------------------------------------------------


def _extract_all_models(annotation: Any) -> list[type[pydantic.BaseModel]]:
    """Extract all Pydantic model types from an annotation (recursively)."""
    models = []
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        models.append(annotation)
        return models

    for arg in get_args(annotation):
        models.extend(_extract_all_models(arg))

    return models


def _render_model(
    cls: type[pydantic.BaseModel],
    heading_level: int,
    lines: list[str],
    rendered: set[str],
) -> None:
    """Render a single model as a markdown section with a field table."""
    if cls.__name__ in rendered:
        return
    rendered.add(cls.__name__)

    hashes = "#" * heading_level
    doc_params = _parse_docstring_params(cls)

    # Class docstring – take just the summary line(s)
    class_doc = inspect.getdoc(cls) or ""
    summary_lines = []
    for line in class_doc.splitlines():
        stripped = line.strip()
        if stripped == "Parameters" or re.match(r"^-{3,}$", stripped):
            break
        if re.match(r"^\w+\s*:", stripped) and stripped.split(":")[0].strip() in cls.model_fields:
            break
        if stripped:
            summary_lines.append(stripped)

    lines.append(f"{hashes} {cls.__name__}\n")
    if summary_lines:
        lines.append(" ".join(summary_lines) + "\n")

    # Field table
    lines.append("| Field | Type | Default | Description |")
    lines.append("|-------|------|---------|-------------|")

    nested_models: list[type[pydantic.BaseModel]] = []

    for field_name, field_info in cls.model_fields.items():
        annotation = field_info.annotation
        type_str = _format_type(annotation)
        default_str = _format_default(field_info.default)
        desc = field_info.description or doc_params.get(field_name, "")

        lines.append(f"| `{field_name}` | {type_str} | {default_str} | {desc} |")

        for model_cls in _extract_all_models(annotation):
            if model_cls.__name__ not in rendered:
                nested_models.append(model_cls)

    lines.append("")  # blank line after table

    # Render nested models at one deeper heading level
    for model_cls in nested_models:
        _render_model(model_cls, heading_level + 1, lines, rendered)


# ---------------------------------------------------------------------------
# StepConfig union handling
# ---------------------------------------------------------------------------


def _render_step_config_union(heading_level: int, lines: list[str], rendered: set[str]) -> None:
    """Special handling for the StepConfig union type."""
    hashes = "#" * heading_level
    lines.append(f"{hashes} StepConfig\n")
    lines.append(
        "Each entry in `steps` is either a **SingleRoleStepConfig** (most common — a single "
        "container job) or a **MultiRoleStepConfig** (multiple roles running in parallel, e.g. "
        "server + workers). seekr-chain auto-detects which type based on whether the `roles` "
        "field is present.\n"
    )

    _render_model(cfg.SingleRoleStepConfig, heading_level + 1, lines, rendered)
    _render_model(cfg.MultiRoleStepConfig, heading_level + 1, lines, rendered)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    import sys

    print("[gen_config_page] Starting...", file=sys.stderr)

    lines: list[str] = []
    rendered: set[str] = set()

    lines.append("# Configuration Reference\n")
    lines.append(
        "This page is auto-generated from the Pydantic models in "
        "[`config.py`]({{ repo_blob }}src/seekr_chain/config.py). "
        'All configuration is validated with `extra="forbid"` — any unknown fields will raise an error.\n'
    )

    # Render WorkflowConfig as the top-level
    _render_model(cfg.WorkflowConfig, 2, lines, rendered)

    # StepConfig union gets special treatment
    _render_step_config_union(2, lines, rendered)

    # Enums section
    lines.append("## Enums\n")
    lines.append("### GPUType\n")
    lines.append("| Value | Resource key |")
    lines.append("|-------|-------------|")
    for member in cfg.GPUType:
        lines.append(f"| `{member.name}` | `{member.value}` |")
    lines.append("")

    content = "\n".join(lines)

    print(f"[gen_config_page] Writing {len(lines)} lines to {OUT_PATH}", file=sys.stderr)

    with mkdocs_gen_files.open(OUT_PATH, "w") as f:
        f.write(content)

    print("[gen_config_page] Done.", file=sys.stderr)


try:
    main()
except Exception as e:
    import sys
    import traceback

    print(f"[gen_config_page] ERROR: {e}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
