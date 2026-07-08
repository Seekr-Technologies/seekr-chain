# Task: fix-status-sort

**Status**: complete
**Branch**: hatchery/fix-status-sort
**Created**: 2026-07-01 09:52

## Objective

Make `min()`/`sorted()` and the ordering operators on `PodStatus` respect its
hand-written `order` property (definition order, "least advanced" → "most
advanced"), not lexicographic string order. Also audit `WorkflowStatus` and
`ContainerStatus` for the same latent bug.

## Context

`PodStatus` in `src/seekr_chain/status.py` is a `class PodStatus(str, Enum)`
hybrid. Because it inherits from `str`, Python's rich comparison operators
fell back to lexicographic byte comparison — the same source of truth used
by `min()`, `max()`, and `sorted()`.

Concretely, `_collect_role_state` in
`src/seekr_chain/backends/k8s/workflow_state.py:347` aggregates role status
with `min([pod.status for pod in out.pods])`. Under lexicographic order,
`min([FAILED, RUNNING])` returned `FAILED` — so a role with one failed pod
and one still-running pod displayed as FAILED, even though the intent was
that the still-running pod should keep the role in RUNNING until it too
terminated. A second identical `min()` at `workflow_state.py:373` handles
the JobSet aggregation path. The `order` property has always encoded the
correct ranking; nothing was consulting it.

## Summary

### Changes

- `src/seekr_chain/status.py`: added a private `_rank()` helper and explicit
  `__lt__`, `__le__`, `__gt__`, `__ge__` methods on `PodStatus`. Each
  returns `NotImplemented` for non-`PodStatus` operands so any mixed-type
  boundary (e.g. `PodStatus.RUNNING == "RUNNING"`) still resolves via
  `str`. `order` remains the single source of truth.
- `tests/unit/test_status.py`: added seven ordering tests under
  `TestPodStatus` covering the discriminating cases where lexicographic
  and definition orders disagree, plus a regression test for
  `PodStatus.RUNNING == "RUNNING"` so the str-hybrid contract stays
  documented.

Two commits behind `Fill in Agreed Plan...`:
`62dfec4` (fix) and `94a72fe` (tests).

### Key decision: don't use `@functools.total_ordering`

The task brief suggested `@functools.total_ordering` + `__lt__`. **This does
not work for `str`-subclass enums.** `total_ordering` skips any dunder that
the class already has via a base other than `object`, and `str` supplies all
four rich-comparison methods. Empirically confirmed:
`PodStatus.FAILED > PodStatus.RUNNING` returned `False` after adding only
`__lt__`, because `str.__gt__` was still winning. The fix is four explicit
methods; the code has an inline comment explaining the gotcha so the next
person who tries `total_ordering` here knows why it's absent.

If this pattern comes up again (any `str, Enum` hybrid that needs a custom
sort order), define all four operators explicitly. Don't rely on
`total_ordering`.

### Scope of other enums

Both `WorkflowStatus` and `ContainerStatus` are `str, Enum` hybrids too, but
grep found no `min()`/`max()`/`sorted()`/`<`/`>` usage on either anywhere
under `src/seekr_chain/`. `ContainerStatus` is aggregated by rule-based
`_resolve_status()` in `workflow_state.py`, not by ordering. Adding an
`order` property to either would be speculative — left alone.

### Verification

`uv run pytest tests/unit/ -q` → 455 passed (up from 448; the new tests
added 7). The role-aggregation tests in
`tests/unit/backends/k8s/test_collect_states.py` now exercise the new
`__lt__` transparently and none regressed, so no test silently depended on
the old lexicographic behavior.

### Gotchas for future agents

- `PodStatus.order` is now load-bearing. Keep it in sync with the enum
  member list; a value missing from `order` would blow up `_rank()` with
  `ValueError` on any comparison. If you add a new member, add it to
  `order` in the position that reflects its progression state.
- Don't strip the `str` base from `PodStatus`. Callers rely on
  `status == "RUNNING"` and JSON serialization treating members as strings.
  The comparison override is layered on top of that — don't undo it.
- If you ever move to Python's `StrEnum` (3.11+), verify the same
  `total_ordering` gotcha still holds before deleting the explicit
  operators; `StrEnum` also inherits `str`'s comparison methods.
