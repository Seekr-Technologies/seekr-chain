import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Schema version written to S3 alongside each job's data. Increment this when
# the on-disk/S3 directory layout changes, NOT when the package version changes.
DATA_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class LogKey:
    step: str
    role: str
    index: int
    attempt: int


class LogStore:
    def __init__(self, logs: dict[LogKey, list[str]] | None = None, pod_names: dict[LogKey, str] | None = None):
        self._logs: dict[LogKey, list[str]] = logs if logs else {}
        self._pod_names: dict[LogKey, str] = pod_names if pod_names else {}

    def get_steps(self) -> list[str]:
        steps = list(set([key.step for key in self._logs.keys()]))
        return steps

    def get_roles(self) -> list[str]:
        roles = list(set([key.role for key in self._logs.keys()]))
        return roles

    def get_indexes(self) -> list[int]:
        indexes = list(set([key.index for key in self._logs.keys()]))
        return indexes

    def get_attempts(self) -> list[int]:
        attemptes = list(set([key.attempt for key in self._logs.keys()]))
        return attemptes

    def __len__(self) -> int:
        return len(self._logs)

    def items(self) -> Iterable[tuple[LogKey, list[str]]]:
        for item in self._logs.items():
            yield item

    def append(
        self,
        *,
        step: str,
        role: str | None,
        index: int,
        attempt: int,
        lines: list[str],
    ) -> None:
        self._logs.setdefault(LogKey(step, role, index, attempt), []).extend(lines)

    def set_pod_name(
        self,
        *,
        step: str,
        role: str | None,
        index: int,
        attempt: int,
        pod_name: str,
    ) -> None:
        self._pod_names[LogKey(step, role, index, attempt)] = pod_name

    def filter(
        self,
        *,
        step: str | None = None,
        role: str | None = None,
        index: int | None = None,
        attempt: int | None = None,
    ) -> dict[LogKey, list[str]]:
        # Select matching keys
        keys = {
            k
            for k in self._logs.keys()
            if (step is None or k.step == step)
            and (role is None or k.role == role)
            and (
                index is None
                or (isinstance(index, int) and k.index == index)
                or (not isinstance(index, int) and k.index in index)
            )
            and (attempt is None or k.attempt == attempt)
        }
        logs = {k: self._logs[k] for k in keys}
        pod_names = {k: self._pod_names[k] for k in keys}

        return LogStore(logs=logs, pod_names=pod_names)

    def select(
        self,
        *,
        step: str | None = None,
        role: str | None = None,
        index: int | None = None,
        attempt: int | None = None,
    ) -> dict[LogKey, list[str]]:
        return {
            k: v
            for k, v in self._logs.items()
            if (step is None or k.step == step)
            and (role is None or k.role == role)
            and (index is None or k.index == index)
            and (attempt is None or k.attempt == attempt)
        }

    def select_pod(
        self,
        *,
        step: str | None = None,
        role: str | None = None,
        index: int | None = None,
        attempt: int | None = None,
    ) -> dict[LogKey, str]:
        return {
            k: v
            for k, v in self._pod_names.items()
            if (step is None or k.step == step)
            and (role is None or k.role == role)
            and (index is None or k.index == index)
            and (attempt is None or k.attempt == attempt)
        }

    def select_one(
        self,
        *,
        step: str | None = None,
        role: str | None = None,
        index: int | None = None,
        attempt: int | None = None,
    ) -> list[str]:
        matches = self.select(
            step=step,
            role=role,
            index=index,
            attempt=attempt,
        )
        if not matches:
            raise KeyError("No logs match the given selector")

        if len(matches) > 1:
            raise ValueError(f"Selector is not unique; matched {len(matches)} log streams")

        return next(iter(matches.values()))

    def select_pod_one(
        self,
        *,
        step: str | None = None,
        role: str | None = None,
        index: int | None = None,
        attempt: int | None = None,
    ) -> str:
        matches = self.select_pod(
            step=step,
            role=role,
            index=index,
            attempt=attempt,
        )
        if not matches:
            raise KeyError("No logs match the given selector")

        if len(matches) > 1:
            raise ValueError(f"Selector is not unique; matched {len(matches)} log streams")

        return next(iter(matches.values()))

    def to_dict(self) -> dict[str, Any]:
        """
        Return logs as a nested dict with explicit, labeled keys.

        If there is exactly one role and it is the empty string '',
        the role level is omitted.
        """
        root: dict[str, Any] = {}

        # ---- Pass 1: detect role behavior ----
        roles = {k.role for k in self._logs}
        omit_role = roles == {""}

        # ---- Pass 2: build structure ----
        for key, lines in self._logs.items():
            step_k = f"step={key.step}"
            role_k = f"role={key.role}"
            index_k = f"index={key.index}"
            attempt_k = f"attempt={key.attempt}"

            step_d = root.setdefault(step_k, {})

            if omit_role:
                # step -> index -> attempt
                index_d = step_d.setdefault(index_k, {})
                index_d[attempt_k] = list(lines)
            else:
                role_d = step_d.setdefault(role_k, {})
                index_d = role_d.setdefault(index_k, {})
                index_d[attempt_k] = list(lines)

        return root


def parse_logs(path: Path, timestamps: bool = False) -> LogStore:
    """Parse job logs from a local directory synced from S3."""
    logs = LogStore()

    for step_dir in path.glob("step=*/role=*/job_index=*/pod_index=*/attempt=*"):
        step_parts = {k: v for k, v in [item.split("=", 1) for item in step_dir.relative_to(path).parts]}
        log_key = {
            "step": step_parts["step"],
            "role": step_parts["role"],
            "index": int(step_parts["job_index"]),
            "attempt": int(step_parts["attempt"]),
        }

        with open(step_dir / "md.json", "r") as f:
            step_md = json.load(f)

        logs.set_pod_name(**log_key, pod_name=step_md["pod_name"])
        for log_file in sorted(step_dir.glob("**/*.log.gz*")):
            with gzip.open(log_file, "rt") as f:
                data = [json.loads(line) for line in f.read().splitlines()]
                if timestamps is False:
                    data = [item["log"] for item in data]
                logs.append(**log_key, lines=data)

    return logs
