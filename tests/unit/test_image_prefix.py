from seekr_chain.utils import resolve_image


class TestResolveImage:
    def test_no_prefix(self, monkeypatch):
        monkeypatch.delenv("SEEKR_CHAIN_IMAGE_PREFIX", raising=False)
        assert resolve_image("python:3.12") == "python:3.12"

    def test_prefix_applied(self, monkeypatch):
        monkeypatch.setenv("SEEKR_CHAIN_IMAGE_PREFIX", "registry.example.com/mirror")
        assert resolve_image("python:3.12") == "registry.example.com/mirror/python:3.12"

    def test_prefix_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("SEEKR_CHAIN_IMAGE_PREFIX", "registry.example.com/mirror/")
        assert resolve_image("python:3.12") == "registry.example.com/mirror/python:3.12"

    def test_prefix_empty_string(self, monkeypatch):
        monkeypatch.setenv("SEEKR_CHAIN_IMAGE_PREFIX", "")
        assert resolve_image("python:3.12") == "python:3.12"

    def test_prefix_with_port(self, monkeypatch):
        monkeypatch.setenv("SEEKR_CHAIN_IMAGE_PREFIX", "registry.example.com:7443/mirror")
        assert resolve_image("alpine:3.22.0") == "registry.example.com:7443/mirror/alpine:3.22.0"

    def test_namespaced_image(self, monkeypatch):
        monkeypatch.setenv("SEEKR_CHAIN_IMAGE_PREFIX", "registry.example.com/mirror")
        assert resolve_image("amazon/aws-cli:2.25.11") == "registry.example.com/mirror/amazon/aws-cli:2.25.11"
