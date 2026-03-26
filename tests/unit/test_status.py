"""Tests for status enums and render_status helpers."""

import pytest

from seekr_chain.render_status import render_compact_pod_status
from seekr_chain.status import ContainerStatus, PodStatus

# ---------------------------------------------------------------------------
# PodStatus
# ---------------------------------------------------------------------------


class TestPodStatus:
    def test_all_values_present(self):
        values = {s.value for s in PodStatus}
        assert values == {
            "UNKNOWN",
            "PENDING",
            "INIT:WAITING",
            "INIT:RUNNING",
            "INIT:ERROR",
            "PULL:ERROR",
            "PULLING",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "TERMINATED",
        }

    def test_dead_values_removed(self):
        """INITIALIZING and WAITING must no longer exist."""
        values = {s.value for s in PodStatus}
        assert "INITIALIZING" not in values
        assert "WAITING" not in values

    def test_is_running(self):
        assert PodStatus.RUNNING.is_running()
        for s in PodStatus:
            if s != PodStatus.RUNNING:
                assert not s.is_running(), f"{s} should not be running"

    def test_is_successful(self):
        assert PodStatus.SUCCEEDED.is_successful()
        for s in PodStatus:
            if s != PodStatus.SUCCEEDED:
                assert not s.is_successful()

    def test_is_failed(self):
        assert PodStatus.FAILED.is_failed()
        assert PodStatus.TERMINATED.is_failed()
        for s in PodStatus:
            if s not in {PodStatus.FAILED, PodStatus.TERMINATED}:
                assert not s.is_failed()

    def test_is_finished(self):
        assert PodStatus.SUCCEEDED.is_finished()
        assert PodStatus.FAILED.is_finished()
        assert PodStatus.TERMINATED.is_finished()
        assert not PodStatus.RUNNING.is_finished()
        assert not PodStatus.PULLING.is_finished()
        assert not PodStatus.PULL_ERROR.is_finished()

    def test_order_covers_all_values(self):
        assert set(PodStatus.RUNNING.order) == set(PodStatus)

    def test_order_is_total(self):
        """Every status must be comparable via min() because order is defined."""
        statuses = list(PodStatus)
        # min() uses __lt__ which relies on order; should not raise
        result = min(statuses)
        assert isinstance(result, PodStatus)


# ---------------------------------------------------------------------------
# ContainerStatus
# ---------------------------------------------------------------------------


class TestContainerStatus:
    def test_pull_error_present(self):
        assert ContainerStatus.PULL_ERROR.value == "PULL:ERROR"

    def test_is_running_uses_container_status(self):
        """Regression: was comparing against PodStatus.RUNNING (wrong enum)."""
        assert ContainerStatus.RUNNING.is_running()
        for s in ContainerStatus:
            if s != ContainerStatus.RUNNING:
                assert not s.is_running(), f"{s} should not report is_running()"

    def test_is_successful_uses_container_status(self):
        """Regression: was comparing against PodStatus.SUCCEEDED."""
        assert ContainerStatus.SUCCEEDED.is_successful()
        for s in ContainerStatus:
            if s != ContainerStatus.SUCCEEDED:
                assert not s.is_successful(), f"{s} should not report is_successful()"

    def test_is_failed_uses_container_status(self):
        """Regression: was comparing against PodStatus.FAILED / PodStatus.TERMINATED."""
        assert ContainerStatus.FAILED.is_failed()
        assert ContainerStatus.TERMINATED.is_failed()
        for s in ContainerStatus:
            if s not in {ContainerStatus.FAILED, ContainerStatus.TERMINATED}:
                assert not s.is_failed(), f"{s} should not report is_failed()"


# ---------------------------------------------------------------------------
# render_status — graphical
# ---------------------------------------------------------------------------


class TestGraphicalRender:
    def _g(self, statuses):
        return render_compact_pod_status(statuses, format="GRAPHICAL")

    def test_pending_states_render_as_dot(self):
        pending = [PodStatus.PENDING, PodStatus.INIT_WAITING, PodStatus.INIT_RUNNING, PodStatus.UNKNOWN]
        assert self._g(pending) == "[....]"

    def test_pulling_renders_as_tilde(self):
        assert self._g([PodStatus.PULLING]) == "[~]"

    def test_pull_error_renders_as_cross(self):
        assert self._g([PodStatus.PULL_ERROR]) == "[✗]"

    def test_init_error_renders_as_cross(self):
        assert self._g([PodStatus.INIT_ERROR]) == "[✗]"

    def test_running_renders_as_circle(self):
        assert self._g([PodStatus.RUNNING]) == "[○]"

    def test_succeeded_renders_as_filled_circle(self):
        assert self._g([PodStatus.SUCCEEDED]) == "[●]"

    def test_failed_renders_as_cross(self):
        assert self._g([PodStatus.FAILED]) == "[✗]"

    def test_terminated_renders_as_cross(self):
        assert self._g([PodStatus.TERMINATED]) == "[✗]"

    def test_mixed(self):
        statuses = [
            PodStatus.PENDING,
            PodStatus.PULLING,
            PodStatus.RUNNING,
            PodStatus.SUCCEEDED,
            PodStatus.FAILED,
        ]
        assert self._g(statuses) == "[.~○●✗]"

    def test_empty(self):
        assert self._g([]) == "[]"


# ---------------------------------------------------------------------------
# render_status — numeric
# ---------------------------------------------------------------------------


class TestNumericRender:
    def _n(self, statuses):
        return render_compact_pod_status(statuses, format="NUMERIC")

    def test_all_pending(self):
        assert self._n([PodStatus.PENDING] * 3) == "[0/3]"

    def test_pulling_counts_as_pending(self):
        assert self._n([PodStatus.PULLING] * 2) == "[0/2]"

    def test_all_succeeded(self):
        assert self._n([PodStatus.SUCCEEDED] * 4) == "[4/4]"

    def test_running_shown_separately(self):
        statuses = [PodStatus.RUNNING, PodStatus.SUCCEEDED, PodStatus.PENDING]
        assert self._n(statuses) == "[1+1/3]"

    def test_failed_shown(self):
        statuses = [PodStatus.SUCCEEDED, PodStatus.FAILED]
        assert self._n(statuses) == "[2/2 F: 1]"

    def test_pull_error_counts_as_failed(self):
        statuses = [PodStatus.PULL_ERROR, PodStatus.SUCCEEDED]
        assert self._n(statuses) == "[2/2 F: 1]"

    def test_init_error_counts_as_failed(self):
        statuses = [PodStatus.INIT_ERROR, PodStatus.RUNNING]
        assert self._n(statuses) == "[1+1/2 F: 1]"

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            render_compact_pod_status([PodStatus.RUNNING], format="BOGUS")
