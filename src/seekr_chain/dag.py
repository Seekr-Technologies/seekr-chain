"""DAG utilities shared across execution backends."""

from collections import deque


def topological_sort(steps):
    """Return steps in a valid execution order respecting depends_on constraints.

    Uses Kahn's algorithm. The WorkflowConfig validator guarantees no cycles
    and no references to non-existent steps, so neither is checked here.

    Ties (steps with no mutual dependency) are resolved in config order.
    """
    step_map = {step.name: step for step in steps}
    in_degree = {step.name: len(step.depends_on or []) for step in steps}
    dependents: dict[str, list] = {step.name: [] for step in steps}

    for step in steps:
        for dep in step.depends_on or []:
            dependents[dep].append(step.name)

    queue = deque(name for name, deg in in_degree.items() if deg == 0)
    result = []

    while queue:
        name = queue.popleft()
        result.append(step_map[name])
        for dependent in dependents[name]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    return result
