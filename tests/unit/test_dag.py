"""Unit tests for seekr_chain.dag (shared DAG utilities)."""

from seekr_chain.config import WorkflowConfig
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
