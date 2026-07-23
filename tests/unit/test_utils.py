import pytest

from seekr_chain.utils import human_to_int


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1024", 1024),
        ("100", 100),
        ("50G", 50 * 1000**3),
        ("50GB", 50 * 1000**3),
        ("50GiB", 50 * 1024**3),
        ("50Gi", 50 * 1024**3),
        ("100M", 100 * 1000**2),
        ("1T", 1000**4),
        ("1TiB", 1024**4),
    ],
)
def test_human_to_int(value, expected):
    assert human_to_int(value) == expected


def test_human_to_int_rejects_garbage():
    with pytest.raises(ValueError):
        human_to_int("not-a-size")
