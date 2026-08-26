"""Unit tests for the shared status model (resources/controller package).

Same import-bootstrap approach as test_controller.py: the controller package
has no seekr_chain dependency, so it's imported directly off sys.path rather
than through seekr_chain.
"""

import sys
from pathlib import Path

_RESOURCES = Path(__file__).resolve().parents[5] / "src/seekr_chain/backends/k8s/resources"
sys.path.insert(0, str(_RESOURCES))

from controller.status_model import Status, aggregate  # noqa: E402


class TestStatus:
    def test_all_values_present(self):
        values = {s.value for s in Status}
        assert values == {
            "UNKNOWN",
            "PENDING",
            "STARTING",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "CANCELED",
            "SKIPPED",
            "ERROR",
        }

    def test_is_terminal(self):
        terminal = {Status.SUCCEEDED, Status.FAILED, Status.CANCELED, Status.SKIPPED, Status.ERROR}
        for s in Status:
            assert s.is_terminal() == (s in terminal)

    def test_is_failed(self):
        failed = {Status.FAILED, Status.CANCELED, Status.ERROR}
        for s in Status:
            assert s.is_failed() == (s in failed)

    def test_is_successful(self):
        assert Status.SUCCEEDED.is_successful()
        for s in Status:
            if s != Status.SUCCEEDED:
                assert not s.is_successful()

    def test_is_running(self):
        assert Status.RUNNING.is_running()
        assert not Status.STARTING.is_running()
        for s in Status:
            if s != Status.RUNNING:
                assert not s.is_running()

    def test_str_equality_still_holds(self):
        assert Status.RUNNING == "RUNNING"


class TestAggregate:
    def test_empty_is_unknown(self):
        assert aggregate([]) == Status.UNKNOWN

    def test_all_succeeded_is_succeeded(self):
        assert aggregate([Status.SUCCEEDED, Status.SUCCEEDED]) == Status.SUCCEEDED

    def test_all_skipped_is_skipped(self):
        assert aggregate([Status.SKIPPED, Status.SKIPPED]) == Status.SKIPPED

    def test_mix_of_succeeded_and_skipped_is_succeeded(self):
        assert aggregate([Status.SUCCEEDED, Status.SKIPPED]) == Status.SUCCEEDED

    def test_any_error_wins(self):
        assert aggregate([Status.SUCCEEDED, Status.ERROR, Status.FAILED]) == Status.ERROR

    def test_any_failed_beats_terminated_and_running(self):
        assert aggregate([Status.CANCELED, Status.RUNNING, Status.FAILED]) == Status.FAILED

    def test_any_terminated_beats_running(self):
        assert aggregate([Status.RUNNING, Status.CANCELED]) == Status.CANCELED

    def test_any_running_beats_starting_and_pending(self):
        assert aggregate([Status.PENDING, Status.STARTING, Status.RUNNING]) == Status.RUNNING

    def test_starting_outranks_pending(self):
        assert aggregate([Status.PENDING, Status.STARTING]) == Status.STARTING

    def test_unknown_never_masks_a_real_signal(self):
        """Regression: the old min()-by-declaration-order aggregation let an
        UNKNOWN sibling mask a genuine FAILED/RUNNING in the same group."""
        assert aggregate([Status.UNKNOWN, Status.FAILED]) == Status.FAILED
        assert aggregate([Status.UNKNOWN, Status.RUNNING]) == Status.RUNNING

    def test_unknown_blocks_success_rather_than_being_ignored(self):
        """A genuinely unknown sibling should not be silently upgraded to
        SUCCEEDED -- that would hide a real anomaly, unlike the priority
        signals above which are informative on their own."""
        assert aggregate([Status.UNKNOWN, Status.SUCCEEDED]) == Status.UNKNOWN

    def test_unknown_only_wins_when_nothing_else_applies(self):
        assert aggregate([Status.UNKNOWN, Status.UNKNOWN]) == Status.UNKNOWN
