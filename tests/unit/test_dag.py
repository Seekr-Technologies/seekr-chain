"""Unit tests for seekr_chain.dag (shared DAG utilities)."""

from seekr_chain.config import DependsOnCondition, WorkflowConfig, normalize_depends_on
from seekr_chain.dag import topological_sort


class TestTopologicalSort:
    def test_single_step(self):
        config = WorkflowConfig.model_validate(
            {"name": "t", "steps": [{"name": "a", "image": "ubuntu:24.04", "script": "echo a"}]}
        )
        ordered = topological_sort(config.steps)
        assert [s.name for s in ordered] == ["a"]

    def test_linear_chain(self):
        config = WorkflowConfig.model_validate(
            {
                "name": "t",
                "steps": [
                    {"name": "a", "image": "ubuntu:24.04", "script": "echo a"},
                    {"name": "b", "image": "ubuntu:24.04", "script": "echo b", "depends_on": ["a"]},
                    {"name": "c", "image": "ubuntu:24.04", "script": "echo c", "depends_on": ["b"]},
                ],
            }
        )
        ordered = topological_sort(config.steps)
        names = [s.name for s in ordered]
        assert names.index("a") < names.index("b")
        assert names.index("b") < names.index("c")

    def test_diamond_dag(self):
        """a → b, a → c, b+c → d."""
        config = WorkflowConfig.model_validate(
            {
                "name": "t",
                "steps": [
                    {"name": "a", "image": "ubuntu:24.04", "script": "echo a"},
                    {"name": "b", "image": "ubuntu:24.04", "script": "echo b", "depends_on": ["a"]},
                    {"name": "c", "image": "ubuntu:24.04", "script": "echo c", "depends_on": ["a"]},
                    {"name": "d", "image": "ubuntu:24.04", "script": "echo d", "depends_on": ["b", "c"]},
                ],
            }
        )
        ordered = topological_sort(config.steps)
        names = [s.name for s in ordered]
        assert names.index("a") < names.index("b")
        assert names.index("a") < names.index("c")
        assert names.index("b") < names.index("d")
        assert names.index("c") < names.index("d")

    def test_structured_depends_on_entry_orders_correctly(self):
        """A structured (ON_FAILURE/ALWAYS) depends_on entry still gates ordering."""
        config = WorkflowConfig.model_validate(
            {
                "name": "t",
                "steps": [
                    {"name": "a", "image": "ubuntu:24.04", "script": "echo a"},
                    {
                        "name": "b",
                        "image": "ubuntu:24.04",
                        "script": "echo b",
                        "depends_on": [{"step": "a", "when": "ON_FAILURE"}],
                    },
                ],
            }
        )
        ordered = topological_sort(config.steps)
        names = [s.name for s in ordered]
        assert names.index("a") < names.index("b")


class TestNormalizeDependsOn:
    def test_none_returns_empty_list(self):
        assert normalize_depends_on(None) == []

    def test_bare_string_becomes_on_success_condition(self):
        [cond] = normalize_depends_on(["a"])
        assert cond == DependsOnCondition(step="a")

    def test_structured_condition_passed_through(self):
        cond = DependsOnCondition(step="a", when="ALWAYS")
        assert normalize_depends_on([cond]) == [cond]
