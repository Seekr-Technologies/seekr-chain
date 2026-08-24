"""Deciding whether a failed JobSet should be retried.

This logic used to live entirely in the rendered JobSet YAML
(podFailurePolicy + failurePolicy.rules). That's structurally racy: each
role is one K8s Job with backoffLimit: 0/restartPolicy: Never, and the Job
controller evaluates podFailurePolicy per pod-failure event as it arrives,
not after every sibling pod has reported a final exit code. With
backoffLimit: 0, the first unmatched pod failure immediately fails the Job
via BackoffLimitExceeded before a later, matching (real root-cause) failure
is even evaluated -- nondeterministic when multiple pods fail close
together (e.g. multinode: one node exits on the real root cause, another
exits moments later as a symptom of the first going away).

Evaluating here, after the JobSet has reached terminal Failed, is race-free:
every pod has a final exit code by then.
"""


def _collect_pod_failures(k8s_v1, namespace: str, workflow_id: str, step_name: str, attempt: int) -> list[dict]:
    """Return one {"role": str, "exit_code": int | None} entry per pod
    belonging to this (step, attempt)'s JobSet."""
    label_selector = f"seekr-chain/job-id={workflow_id},seekr-chain/step={step_name},seekr-chain/attempt={attempt}"
    pods = k8s_v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)

    results = []
    for pod in pods.items:
        role = pod.metadata.labels.get("seekr-chain/role", "")
        exit_code = None
        for cs in pod.status.container_statuses or []:
            if cs.name == "main" and cs.state.terminated is not None:
                exit_code = cs.state.terminated.exit_code
        results.append({"role": role, "exit_code": exit_code})
    return results


def _pod_matches_rule(pod: dict, rule: dict) -> bool:
    on_exit_codes = rule.get("on_exit_codes")
    if on_exit_codes is None:
        # A rule with no on_exit_codes matches unconditionally.
        return True
    if pod["exit_code"] is None:
        return False
    in_set = pod["exit_code"] in set(on_exit_codes)
    return in_set if rule.get("operator", "IN") == "IN" else not in_set


def decide_retry(pods: list[dict], failure_policy: dict | None, attempt: int) -> tuple[bool, str]:
    """Evaluate failure_policy["rules"] against a failed JobSet's pods.

    First-match-wins, in declaration order. A rule only considers pods whose
    role is in its target_roles (all pods if target_roles is None). If no
    rule matches -- or the step has no failure_policy at all -- fall back to
    retry-until-max_restarts, matching the JobSet-level default this
    replaces.

    Returns (should_retry, reason).
    """
    max_restarts = (failure_policy or {}).get("max_restarts") or 0
    rules = (failure_policy or {}).get("rules") or []

    for rule in rules:
        target_roles = rule.get("target_roles")
        scoped = [p for p in pods if target_roles is None or p["role"] in target_roles]
        if any(_pod_matches_rule(p, rule) for p in scoped):
            action = rule["action"]
            if action == "FAIL_JOB_SET":
                return False, f"rule matched ({action})"
            if action == "RESTART_JOB_SET_AND_IGNORE_MAX_RESTARTS":
                return True, f"rule matched ({action})"
            return attempt < max_restarts, f"rule matched ({action})"

    return attempt < max_restarts, "no rule matched, default retry policy"


def evaluate_step_failure(
    k8s_v1,
    namespace: str,
    workflow_id: str,
    step_name: str,
    attempt: int,
    failure_policy: dict | None,
) -> tuple[bool, str]:
    """List a failed JobSet's pods and decide whether the step should retry."""
    pods = _collect_pod_failures(k8s_v1, namespace, workflow_id, step_name, attempt)
    return decide_retry(pods, failure_policy, attempt)
