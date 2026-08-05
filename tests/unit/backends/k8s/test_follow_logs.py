"""
Unit tests for _spawn_follow_pod_thread()'s log decoding.

read_namespaced_pod_log(..., _preload_content=False) returns a urllib3
response iterator whose chunk boundaries don't respect UTF-8 character
boundaries, so a multi-byte character can be split across two chunks.
"""

from seekr_chain.backends.k8s.k8s_workflow import _spawn_follow_pod_thread


class FakeK8sV1:
    def __init__(self, chunks):
        self._chunks = chunks

    def read_namespaced_pod_log(self, **kwargs):
        return iter(self._chunks)


def _run_follow(chunks):
    k8s_v1 = FakeK8sV1(chunks)
    thread = _spawn_follow_pod_thread(k8s_v1, "pod-1", "ns", "step", None, 0)
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_multibyte_char_split_across_chunks(capsys):
    # '日' encodes to b'\xe6\x97\xa5'; split it across two chunks.
    chunks = [b"hello \xe6", b"\x97\xa5 world\n"]
    _run_follow(chunks)
    out = capsys.readouterr().out
    assert "日" in out
    assert "�" not in out
    assert "[ERROR]" not in out


def test_invalid_utf8_byte_is_replaced_not_fatal(capsys):
    # 0x88 is a continuation byte, never valid as a sequence start.
    chunks = [b"before ", b"\x88 after\n"]
    _run_follow(chunks)
    out = capsys.readouterr().out
    assert "[ERROR]" not in out
    assert "before " in out
    assert "after" in out
