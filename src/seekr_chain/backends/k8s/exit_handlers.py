#!/usr/bin/env python3

from dataclasses import dataclass

from seekr_chain.config import ExitHandlerConfig, WorkflowConfig, handler_step_name


@dataclass(frozen=True)
class HandlerPlan:
    """One exit handler resolved against its parent step, ready to render/index.

    parent_step and pseudo_step name the manifest/asset side of the handler:
    ``assets/step=<pseudo_step>/jobset.yaml`` and the ``handlers.json`` entry
    both key off these.
    """

    parent_step: str
    handler: ExitHandlerConfig
    pseudo_step: str


def plan_handlers(config: WorkflowConfig) -> list[HandlerPlan]:
    """Flatten every step's ``exit_handlers`` into a deterministic plan list.

    Order is steps in config order, handlers within a step in declared order —
    callers (rendering, ``handlers.json``) share this order so names line up.
    """
    plans = []
    for step in config.steps:
        for handler in step.exit_handlers:
            plans.append(
                HandlerPlan(
                    parent_step=step.name,
                    handler=handler,
                    pseudo_step=handler_step_name(step.name, handler.run.name),
                )
            )
    return plans
