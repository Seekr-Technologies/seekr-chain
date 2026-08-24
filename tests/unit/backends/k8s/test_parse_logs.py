"""Unit tests for LogStore.to_dict()."""

from seekr_chain.backends.k8s.parse_logs import LogStore


def test_to_dict_returns_nested_step_role_index_attempt():
    """to_dict() nests logs as step -> role -> index -> attempt, with no
    special-casing for single-role steps' "main" role."""
    store = LogStore()
    store.append(step="a", role="main", index=0, attempt=0, lines=["hello"])

    assert store.to_dict() == {"step=a": {"role=main": {"index=0": {"attempt=0": ["hello"]}}}}
