"""Security regression tests; the fixture is a public CA certificate, not a key."""

import hashlib
import importlib.util
import re
import runpy
import shutil
import ssl
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import certifi
import pytest

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "docker/build_gates"
GATE = runpy.run_path(str(GATES / "gate_certificates.py"))
BAD_ROOT = Path(__file__).parent / "fixtures/emsign-ecc-root-g3.pem"
GOOD_ROOT = Path(__file__).parent / "fixtures/emsign-root-g1.pem"


@pytest.mark.parametrize(
    "package,version",
    [("certifi", "2026.10.19"), ("pip", "26.2.2"), ("uv", "0.13.5"), ("pip", "26.3"), ("uv", "1.0")],
)
def test_patched_versions_pass(package, version):
    GATE["check_version"](package, version)


@pytest.mark.parametrize(
    "package,version",
    [
        ("certifi", "2026.4.22"),
        ("pip", "26.2.1"),
        ("pip", "26.2"),
        ("uv", "0.13.3"),
        ("pip", "26.2.2rc1"),
        ("pip", "26.3rc1"),
        ("pip", "26.3.dev1"),
        ("uv", "garbage"),
    ],
)
def test_old_prerelease_or_unknown_versions_fail(package, version):
    with pytest.raises(SystemExit, match=package):
        GATE["check_version"](package, version)


def test_application_bundle_is_patched():
    GATE["check_version"]("certifi", certifi.__version__)
    GATE["check_bundle"](Path(certifi.where()))


def test_allow_unaffected_emsign_g1():
    # Upstream restored unaffected emSign roots in 2026.10.19.
    GATE["check_bundle"](GOOD_ROOT)


def test_reject_compromised_root_even_in_otherwise_valid_bundle(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_bytes(Path(certifi.where()).read_bytes() + BAD_ROOT.read_bytes())
    with pytest.raises(SystemExit, match="compromised emSign root"):
        GATE["check_bundle"](bundle)


def test_reject_empty_and_malformed_bundles(tmp_path):
    bundle = tmp_path / "ca.pem"
    for content in ["", "not a certificate"]:
        bundle.write_text(content)
        with pytest.raises(SystemExit, match="empty CA bundle"):
            GATE["check_bundle"](bundle)
    bundle.write_text("-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----\n")
    with pytest.raises(ssl.SSLError):
        GATE["check_bundle"](bundle)


def test_runtime_allows_absent_optional_tools():
    main = GATE["main"]
    with (
        patch.dict(main.__globals__, check_bundle=lambda path: None),
        patch.object(importlib.util, "find_spec", return_value=None),
        patch.object(shutil, "which", return_value=None),
    ):
        main()


def test_runtime_rejects_vendored_pip_before_loading_its_bundle():
    main = GATE["main"]
    real_import = main.__globals__["importlib"].import_module

    def load(name):
        return SimpleNamespace(__version__="26.2.1") if name == "pip" else real_import(name)

    with (
        patch.dict(main.__globals__, check_bundle=lambda path: None),
        patch("importlib.import_module", side_effect=load),
        patch.object(importlib.util, "find_spec", return_value=object()),
        pytest.raises(SystemExit, match="pip 26.2.1"),
    ):
        main()


def test_runtime_checks_vendored_pip_bundle_and_uv():
    main = GATE["main"]
    modules = {
        "certifi": SimpleNamespace(__version__="2026.10.23", where=lambda: "/application.pem"),
        "pip": SimpleNamespace(__version__="26.2.2"),
        "pip._vendor.certifi": SimpleNamespace(where=lambda: "/vendored.pem"),
    }
    checked = []
    with (
        patch.dict(main.__globals__, check_bundle=lambda path: checked.append(str(path))),
        patch("importlib.import_module", side_effect=modules.__getitem__),
        patch.object(importlib.util, "find_spec", return_value=object()),
        patch.object(shutil, "which", return_value="/usr/bin/uv"),
        patch.object(subprocess, "check_output", return_value="uv 0.13.5 (build info)"),
    ):
        main()
    assert checked == ["/etc/ssl/certs/ca-certificates.crt", "/application.pem", "/vendored.pem"]


def test_python_free_gate_accepts_only_verified_bundle(tmp_path):
    bundle = tmp_path / "ca.pem"
    # Unit-test shell behavior with an independent approved fixture. Actual
    # bootstrap pin/hash agreement is checked by uncached image builds in CI.
    bundle.write_bytes(GOOD_ROOT.read_bytes())
    expected = hashlib.sha256(bundle.read_bytes()).hexdigest()
    script, count = re.subn(
        r'(?m)^EXPECTED_SHA256="[0-9a-f]{64}"$',
        f'EXPECTED_SHA256="{expected}"',
        (GATES / "gate_system_bundle.sh").read_text(),
    )
    assert count == 1
    gate = tmp_path / "gate.sh"
    gate.write_text(script)
    command = ["sh", str(gate), str(bundle)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    for content in [b"", BAD_ROOT.read_bytes(), bundle.read_bytes() + BAD_ROOT.read_bytes()]:
        bundle.write_bytes(content)
        assert subprocess.run(command, capture_output=True).returncode != 0
    bundle.unlink()
    assert subprocess.run(command, capture_output=True).returncode != 0


@pytest.mark.parametrize("kind", ["controller", "init", "nix-runner"])
def test_image_workflows_keep_final_container_checks_and_uncached_builds(kind):
    workflow = (ROOT / f".github/workflows/build-{kind}-image.yml").read_text()
    image_tag = "smoke" if kind == "nix-runner" else "certificates"
    assert f"sh docker/test-certificates.sh seekr-chain-{kind}:{image_tag} {kind}" in workflow
    assert "no-cache: true" in workflow


def test_dev_uv_is_copied_from_digest_pinned_official_image():
    dockerfile = (ROOT / "dev/docker/dockerfile").read_text()
    assert re.search(
        r"COPY --from=ghcr.io/astral-sh/uv:0\.13\.5@sha256:[0-9a-f]{64} /uv /uvx /usr/local/bin/",
        dockerfile,
    )
    assert "pip install --no-cache-dir 'uv" not in dockerfile
    for line in dockerfile.splitlines():
        if "pip install" in line:
            assert "--require-hashes" in line
    assert "--require-hashes -r /tmp/dev-requirements.txt" in dockerfile


def test_dev_requirements_are_fully_hash_locked():
    requirements = (ROOT / "dev/docker/requirements.txt").read_text()
    entries = [line for line in requirements.splitlines() if line and not line.startswith(("#", " "))]
    assert entries
    for entry in entries:
        assert re.fullmatch(r"[a-zA-Z0-9_-]+==[^\s]+ \\", entry)
        assert entry + "\n    --hash=sha256:" in requirements
    assert "oci-cli==" in requirements
    bootstrap = (ROOT / "docker/certificate-requirements.txt").read_text()
    bootstrap_certifi = re.search(r"(?m)^certifi==[^\s]+", bootstrap)[0]
    assert bootstrap_certifi in requirements
