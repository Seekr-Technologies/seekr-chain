#!/usr/bin/env python3

import logging
import time
from typing import overload

from seekr_chain.status import WorkflowStatus
from seekr_chain.workflow import Workflow

logger = logging.getLogger(__name__)


def _format_wait_message(jobs, statuses):
    return "\n".join([f"  {job.name} : {status.value}" for job, status in zip(jobs, statuses)])


@overload
def wait(jobs: Workflow, poll_interval: int) -> WorkflowStatus: ...
@overload
def wait(jobs: list[Workflow], poll_interval: int) -> list[WorkflowStatus]: ...


def wait(jobs: Workflow | list[Workflow], poll_interval: int = 10) -> WorkflowStatus | list[WorkflowStatus]:
    is_list = True
    if not isinstance(jobs, list):
        is_list = False
        jobs = [jobs]

    while True:
        statuses = [job.get_status() for job in jobs]
        n_complete = sum([status.is_finished() for status in statuses])
        logger.info(f"{n_complete}/{len(jobs)} workflows complete\n{_format_wait_message(jobs, statuses)}")

        if n_complete == len(jobs):
            break

        time.sleep(poll_interval)

    if not is_list:
        statuses = statuses[0]

    return statuses
