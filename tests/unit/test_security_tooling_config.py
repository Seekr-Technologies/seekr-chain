"""Keep updater scope and CI tool selection tied to the declared security policy."""

from pathlib import Path

import yaml

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]


def test_ci_reads_uv_security_requirement_from_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["tool"]["uv"]["required-version"] == ">=0.13.4"
    installs = []
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        jobs = yaml.safe_load(workflow.read_text())["jobs"]
        for job in jobs.values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                    installs.append(step)
                    assert step["with"]["version-file"] == "pyproject.toml"
                    assert "version" not in step["with"]
    assert installs


def test_dependabot_covers_image_requirement_sources():
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    uv = next(update for update in config["updates"] if update["package-ecosystem"] == "uv")
    # Root uv discovery includes direct-child docker/, not deeper dev/docker/.
    assert set(uv["directories"]) == {"/", "/dev/docker"}
    for name in ("docker/certificate-requirements", "docker/controller-requirements", "dev/docker/requirements"):
        assert (ROOT / f"{name}.in").is_file()
        assert (ROOT / f"{name}.txt").is_file()
