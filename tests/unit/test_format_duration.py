"""Unit tests for ``seekr_chain.utils.format_duration``."""

import datetime

from seekr_chain.utils import format_duration

UTC = datetime.timezone.utc


class TestFormatDuration:
    def test_none_start_returns_empty(self):
        assert format_duration(None) == ""
        assert format_duration(None, datetime.datetime(2026, 1, 1, tzinfo=UTC)) == ""

    def test_completed_short(self):
        start = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        end = datetime.datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
        assert format_duration(start, end) == "0:05"

    def test_completed_minutes(self):
        start = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        end = datetime.datetime(2026, 1, 1, 12, 3, 12, tzinfo=UTC)
        assert format_duration(start, end) == "3:12"

    def test_completed_hours_uses_hms(self):
        start = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        end = datetime.datetime(2026, 1, 1, 13, 4, 8, tzinfo=UTC)
        assert format_duration(start, end) == "1:04:08"

    def test_one_hour_boundary(self):
        start = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        end = datetime.datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC)
        assert format_duration(start, end) == "1:00:00"

    def test_just_under_one_hour_uses_ms(self):
        start = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        end = datetime.datetime(2026, 1, 1, 12, 59, 59, tzinfo=UTC)
        assert format_duration(start, end) == "59:59"

    def test_dt_end_before_dt_start_clamps_to_zero(self):
        start = datetime.datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
        end = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert format_duration(start, end) == "0:00"

    def test_naive_start_treated_as_utc(self):
        start = datetime.datetime(2026, 1, 1, 12, 0, 0)
        end = datetime.datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
        assert format_duration(start, end) == "0:30"

    def test_running_uses_now_when_end_none(self, monkeypatch):
        fake_now = datetime.datetime(2026, 1, 1, 12, 0, 42, tzinfo=UTC)

        class _FakeDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        monkeypatch.setattr("seekr_chain.utils.datetime.datetime", _FakeDatetime)
        start = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert format_duration(start, None) == "0:42"
