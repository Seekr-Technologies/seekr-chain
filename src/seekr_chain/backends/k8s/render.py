from pathlib import Path

import jinja2

_TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
    undefined=jinja2.StrictUndefined,
    # trim_blocks: removes first newline after {% %} tags — prevents blank lines from control flow
    # lstrip_blocks: strips leading whitespace from {% %} tags — allows indented control flow
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def _to_yaml_bool(value: bool) -> str:
    """Render a Python bool as a YAML boolean. Kubernetes rejects True/False (Python repr)."""
    return "true" if value else "false"


_env.filters["to_yaml_bool"] = _to_yaml_bool


def render(template_name: str, context: dict) -> str:
    """Render a Jinja2 template by name with the given context dict."""
    template = _env.get_template(template_name)
    return template.render(**context)
