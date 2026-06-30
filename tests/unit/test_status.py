"""Tests for status enums."""

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
