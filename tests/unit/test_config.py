"""Tests for config validation."""

import pytest
from pydantic import ValidationError

from seekr_chain.config import WorkflowConfig


def _minimal_step(name, depends_on=None):
    step = {"name": name, "image": "ubuntu:24.04", "script": "echo hello"}
    if depends_on is not None:
        step["depends_on"] = depends_on
    return step


class TestDependsOnValidation:
    def test_valid_depends_on(self):
        config = WorkflowConfig(
            name="test",
            steps=[
                _minimal_step("a"),
                _minimal_step("b", depends_on=["a"]),
            ],
        )
        assert config.steps[1].depends_on == ["a"]

    def test_invalid_depends_on_raises(self):
        with pytest.raises(ValidationError, match="non-existent steps"):
            WorkflowConfig(
                name="test",
                steps=[
                    _minimal_step("a"),
                    _minimal_step("b", depends_on=["missing"]),
                ],
            )

    def test_invalid_depends_on_names_step(self):
        """Error message includes the step that has the bad reference."""
        with pytest.raises(ValidationError, match="Step 'b'"):
            WorkflowConfig(
                name="test",
                steps=[
                    _minimal_step("a"),
                    _minimal_step("b", depends_on=["nope"]),
                ],
            )

    def test_no_depends_on_passes(self):
        config = WorkflowConfig(
            name="test",
            steps=[_minimal_step("a"), _minimal_step("b")],
        )
        assert len(config.steps) == 2
