"""Text-level guards for the runner image's fixed Nix account contract.

The runtime behavior is exercised against a built image by docker/test-nix-runner.sh
in the image publishing workflow. These tests make accidental Dockerfile edits fail
quickly in the regular Python unit suite as well.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker/Dockerfile.nix-runner"
SMOKE_TEST = ROOT / "docker/test-nix-runner.sh"


def test_runner_preserves_upstream_nix_build_user_contract():
    dockerfile = DOCKERFILE.read_text()

    assert "echo 'build-users-group = nixbld';" in dockerfile
    assert "echo 'build-users-group =';" not in dockerfile
    assert 'expected_group="nixbld:x:30000:${nixbld_members}"' in dockerfile
    assert 'while [ "$i" -le 32 ]; do' in dockerfile
    assert "uid=$((30000 + i))" in dockerfile
    assert 'expected_user="nixbld${i}:x:${uid}:30000:' in dockerfile
    assert "chown 0:30000 /nix-baked/store && chmod 1775 /nix-baked/store" in dockerfile


def test_image_smoke_test_covers_fresh_and_warm_shared_stores():
    smoke_test = SMOKE_TEST.read_text()

    assert "docker volume create" in smoke_test
    assert "uid=(3000[1-9]|300[12][0-9]|3003[0-2])" in smoke_test
    assert "home=/homeless-shelter" in smoke_test
    assert "homeless-shelter-must-remain-unwritable" in smoke_test
    assert smoke_test.count("export PATH=/usr/bin:/bin") == 3
    assert smoke_test.count("nix --option substituters '' build") == 2
    assert "grep -i 'permission denied'" in smoke_test
    assert "chown 0:0 /nix/store; chmod 0755 /nix/store" in smoke_test
    assert '"0:30000:1775"' in smoke_test
