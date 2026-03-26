from seekr_chain.status import PodStatus

INIT = "."
PULLING = "~"
RUNNING = "○"
SUCCESS = "●"
FAILED = "✗"


def _render_graphical_status(pod_statuses: list[PodStatus]) -> str:
    """
    Render list of pod statuses into a compact status string
    """
    out = "["
    for status in pod_statuses:
        if status.is_failed() or status in {PodStatus.PULL_ERROR, PodStatus.INIT_ERROR}:
            out += FAILED
        elif status.is_successful():
            out += SUCCESS
        elif status.is_running():
            out += RUNNING
        elif status == PodStatus.PULLING:
            out += PULLING
        else:
            out += INIT
    out += "]"
    return out


def _render_numeric_status(pod_statuses: list[PodStatus]) -> str:
    """
    Render list of pod statuses into a compact status string
    """
    n_running = 0
    n_success = 0
    n_failed = 0
    n_pending = 0

    for status in pod_statuses:
        if status.is_failed() or status in {PodStatus.PULL_ERROR, PodStatus.INIT_ERROR}:
            n_failed += 1
        elif status.is_successful():
            n_success += 1
        elif status.is_running():
            n_running += 1
        else:
            n_pending += 1

    out = f"{n_success + n_failed}"
    if n_running:
        out += f"+{n_running}"
    out += f"/{len(pod_statuses)}"

    if n_failed:
        out += f" F: {n_failed}"

    out = f"[{out}]"
    return out


def render_compact_pod_status(pod_statuses: list[PodStatus], format="NUMERIC") -> str:
    """
    Render list of pod statuses into a compact status string
    """
    formats = {
        "NUMERIC": _render_numeric_status,
        "GRAPHICAL": _render_graphical_status,
    }

    if format.upper() not in formats:
        raise ValueError(f"Invalid format: {format}. Options: {formats.keys()}")

    return formats[format.upper()](pod_statuses)
