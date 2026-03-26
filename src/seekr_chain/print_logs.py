import datetime

from rich.console import Console
from rich.text import Text

from seekr_chain import ArgoWorkflow


def _parse_pod_index_str(x: str) -> list[int]:
    import re

    out = []
    if x.upper() == "ALL":
        return out

    match = re.match(r"^(\d+(?:-\d+)?)(?:,(\d+(?:-\d+)?))*$", x)
    if not match:
        raise ValueError(
            f"Invalid pod index string. Must be comma-separated list of indexes or ranges, e.g. '4-6,9,12'.\nGot: {x}"
        )
    for group in match.groups():
        if group:
            if "-" in group:
                low, high = group.split("-")
                out += list(range(int(low), int(high) + 1))
            else:
                out += [int(group)]

    out = sorted(list(set(out)))
    return out


def _print_wrapped(console, prefix, message) -> None:
    from rich.text import Text

    # 1) How wide is the prefix on screen?
    prefix_width = console.measure(prefix).maximum
    term_width = console.size.width
    msg_width = max(1, term_width - prefix_width)

    # 2) Wrap the message into chunks that fit after the prefix
    wrapped = message.wrap(console, msg_width)  # list[Text]

    if not wrapped:
        console.print(prefix)
        return

    # 3) First line: prefix + first chunk
    console.print(prefix + wrapped[0])

    # 4) Continuation lines: spaces to cover the prefix, then chunk
    pad = Text(" " * prefix_width)
    for part in wrapped[1:]:
        console.print(pad + part)


def print_logs(job_id, step, role, pod_index, attempt, timestamps):
    pod_indexes = _parse_pod_index_str(pod_index)

    workflow = ArgoWorkflow(id=job_id)
    logs = workflow.get_logs(timestamps=True)

    # Narrow down to step
    # -------------------
    all_steps = logs.get_steps()
    if step is None:
        step = all_steps[0]
        if len(all_steps) > 1:
            print(f"Selecting step '{step}', all steps: {all_steps}")
    step_logs = logs.filter(step=step)
    if len(step_logs) == 0:
        raise ValueError(f"Unknown step: '{step}'.\nAvailable steps: {all_steps}")

    # Filter attempt
    all_attempts = step_logs.get_attempts()
    attempt = all_attempts[attempt]
    step_logs = step_logs.filter(attempt=attempt)

    # Narrow down to role
    # -------------------
    all_roles = step_logs.get_roles()
    if role is None:
        role = all_roles[0]
        if len(all_roles) > 1:
            print(f"Selecting role '{role}', all steps: {all_roles}")

    role_logs = step_logs.filter(role=role)
    if len(role_logs) == 0:
        raise ValueError(f"Unknown role: '{role}'.\nAvailable roles: {all_roles}")

    # Narrow down to pod(s)
    # ------------------
    if pod_indexes == []:
        pod_indexes = role_logs.get_indexes()
    pod_logs = role_logs.filter(index=pod_indexes)

    # Collect lines
    lines = []
    for key, logs in pod_logs.items():
        # if index not in role_logs:
        #     raise ValueError(f"Pod index out of range: {index}. Number of replicas: {len(role_logs)}")
        for line in logs:
            lines.append((datetime.datetime.fromisoformat(line["date"]), key.index, line["log"]))

    console = Console()

    # Sort and print lines
    index_len = len(str(max(pod_indexes)))
    for dt, index, line in sorted(lines):
        prefix_parts = []

        if timestamps:
            ts = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")
            prefix_parts.append(Text(f"{ts} ", style="bright_black"))

        if len(pod_indexes) > 1:
            prefix_parts.append(Text(f"[{index:{index_len}d}] ", style="bright_black"))

        prefix = Text.assemble(*prefix_parts)

        # Select color from the 16 base colors
        # - Skip 0 (black)
        # - Start at `7` (white)
        style = ""
        if len(pod_indexes) > 1:
            style = f"color({((index + 6) % 15) + 1})"
        msg = Text(line, style=style)

        _print_wrapped(console, prefix, msg)
