"""Portable closure memo persistence and submission integration (no Nix/K8s needed)."""

import json
import multiprocessing
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

import seekr_chain
from seekr_chain import nix_closure_cache as cache
from seekr_chain import nix_resolution, nix_utils
from seekr_chain.cli import main
from seekr_chain.config import WorkflowConfig
from seekr_chain.user_config import UserConfig

CLOSURE = "/nix/store/" + "0" * 32 + "-environment"


def _store_one(path, index):
    cache.store(
        Path(path),
        f"{index:064x}",
        source_digest="source",
        expression=".",
        attr=str(index),
        system="x86_64-linux",
        closure=CLOSURE,
    )


def test_concurrent_merge_and_stable_format(tmp_path):
    path = tmp_path / "cache.json"
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_store_one, args=(str(path), i)) for i in range(6)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    data = json.loads(path.read_text())
    assert len(data["entries"]) == 6
    assert path.read_text() == json.dumps(data, indent=2, sort_keys=True) + "\n"
    before = path.stat().st_mtime_ns
    _store_one(path, 0)
    assert path.stat().st_mtime_ns == before
    assert list(tmp_path.glob("*.tmp-*")) == []


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        '{"version": 2, "entries": {}}',
        '{"version": 1, "entries": []}',
        json.dumps({"version": 1, "entries": {"a" * 64: {"closure_path": "/etc/passwd"}}}),
        json.dumps({"version": 1, "entries": {"a" * 64: {"closure_path": CLOSURE + "/bin"}}}),
    ],
)
def test_invalid_cache_fails_without_overwrite(tmp_path, body):
    path = tmp_path / "cache.json"
    path.write_text(body)
    with pytest.raises(ValueError, match="Invalid Nix closure cache"):
        _store_one(path, 1)
    assert path.read_text() == body


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "flake.nix").write_text("{}")
    (root / "flake.lock").write_text("{}")
    monkeypatch.setattr(nix_utils, "_NIX_CLOSURE_CACHE_ROOT", tmp_path / "local-memos")
    return root


def resolve(source, staging, path, **kwargs):
    return nix_utils.maybe_eval_closure(
        code_path=str(source),
        staged_root=str(source),
        staging_dir=staging,
        resolved_expression=str(source),
        role_name="test",
        expression=".",
        nix_closure_cache=str(path),
        **kwargs,
    )


def test_additive_invalidation_and_fresh_ci_hit(source, tmp_path, monkeypatch):
    path = source / "chain-nix-lock.json"
    evaluate = Mock(return_value=CLOSURE)
    monkeypatch.setattr(nix_utils, "eval_closure_path", evaluate)
    first = resolve(source, tmp_path / "stage1", path)
    assert evaluate.call_count == 1
    assert len(json.loads(path.read_text())["entries"]) == 1
    # Simulate CI: no local/remote memo, no Nix. A persistent hit needs neither.
    monkeypatch.setattr(nix_utils, "lookup_cached_closure_path", Mock(side_effect=AssertionError("memo lookup")))
    monkeypatch.setattr(nix_utils, "eval_closure_path", Mock(side_effect=AssertionError("Nix invoked")))
    second = resolve(source, tmp_path / "stage2", path)
    assert first.source_digest == second.source_digest
    assert second.closure_path == CLOSURE

    monkeypatch.setattr(nix_utils, "lookup_cached_closure_path", lambda *a, **k: None)
    monkeypatch.setattr(nix_utils, "eval_closure_path", evaluate)
    (source / "flake.lock").write_text('{"changed": true}')
    third = resolve(source, tmp_path / "stage3", path)
    assert third.source_digest != first.source_digest
    assert evaluate.call_count == 2
    assert len(json.loads(path.read_text())["entries"]) == 2


def test_variants_and_excluded_edits(source, tmp_path, monkeypatch):
    path = tmp_path / "shared.json"
    evaluate = Mock(return_value=CLOSURE)
    monkeypatch.setattr(nix_utils, "eval_closure_path", evaluate)
    for attr, system in [("default", "x86_64-linux"), ("train", "x86_64-linux"), ("train", "aarch64-linux")]:
        resolve(
            source,
            tmp_path / f"stage-{attr}-{system}",
            path,
            source_include=["flake.nix", "flake.lock"],
            attr=attr,
            system=system,
        )
    (source / "unrelated.py").write_text("changed")
    resolve(source, tmp_path / "last", path, source_include=["flake.nix", "flake.lock"])
    assert evaluate.call_count == 3
    assert len(json.loads(path.read_text())["entries"]) == 3


def test_cached_missing_binary_stages_without_cache_artifacts(source, tmp_path, monkeypatch):
    path = source / "cache.json"
    monkeypatch.setattr(nix_utils, "eval_closure_path", lambda *a, **k: CLOSURE)
    resolve(source, tmp_path / "stage1", path)
    Path(str(path) + ".tmp-old").write_text("temporary")
    monkeypatch.setattr(nix_utils, "eval_closure_path", Mock(side_effect=AssertionError("Nix invoked")))
    monkeypatch.setattr(nix_utils, "closure_exists", lambda *a: False)
    result = resolve(source, tmp_path / "stage2", path, store_uri="s3://bucket")
    assert not result.closure_in_store
    assert sorted(p.name for p in result.staged_source_dir.iterdir()) == ["flake.lock", "flake.nix"]


def test_existing_local_memo_backfills_file(source, tmp_path, monkeypatch):
    path = tmp_path / "shared.json"
    monkeypatch.setattr(nix_utils, "lookup_cached_closure_path", lambda *a, **k: CLOSURE)
    monkeypatch.setattr(nix_utils, "eval_closure_path", Mock(side_effect=AssertionError("Nix invoked")))
    resolve(source, tmp_path / "stage", path)
    assert len(json.loads(path.read_text())["entries"]) == 1


def test_two_python_configs_share_cache_and_explicit_overrides_default(source, tmp_path, monkeypatch):
    path = source / "cache.json"
    unused_default = tmp_path / "default.json"
    monkeypatch.setattr(nix_resolution, "_user_config", UserConfig(nix_closure_cache=str(unused_default)))
    monkeypatch.setattr(nix_resolution, "find_warm_nodes", lambda *a, **k: [])
    monkeypatch.setattr(nix_utils, "closure_exists", lambda *a: True)
    evaluate = Mock(return_value=CLOSURE)
    monkeypatch.setattr(nix_utils, "eval_closure_path", evaluate)
    for name in ["train", "eval"]:
        config = WorkflowConfig(
            name=name,
            code={"path": str(source)},
            steps=[{"name": name, "nix": {"expression": ".", "store": "s3://bucket"}, "script": "echo"}],
        )
        nix_resolution.process_nix(
            config, staged_code_dir=str(source), staging_dir=tmp_path / name, nix_closure_cache=str(path)
        )
        assert config.steps[0].nix._resolved_closure == CLOSURE
    assert evaluate.call_count == 1
    assert not unused_default.exists()


def test_public_python_override_forwarded(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(seekr_chain, "launch_k8s_workflow", launch)
    seekr_chain.launch_workflow({}, nix_closure_cache="shared.json")
    assert launch.call_args.kwargs["nix_closure_cache"] == "shared.json"


def test_cli_override_forwarded(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("name: test\nsteps:\n  - name: test\n    image: ubuntu\n    script: echo\n")
    launch = Mock()
    monkeypatch.setattr(seekr_chain, "launch_workflow", launch)
    result = CliRunner().invoke(main, ["submit", str(config), "--nix-closure-cache", "shared.json"])
    assert result.exit_code == 0, result.output
    assert launch.call_args.kwargs["nix_closure_cache"] == "shared.json"


def test_configured_default_is_used(source, tmp_path, monkeypatch):
    path = source / "default-cache.json"
    monkeypatch.setattr(nix_resolution, "_user_config", UserConfig(nix_closure_cache=str(path)))
    monkeypatch.setattr(nix_resolution, "find_warm_nodes", lambda *a, **k: [])
    monkeypatch.setattr(nix_utils, "closure_exists", lambda *a: True)
    monkeypatch.setattr(nix_utils, "eval_closure_path", lambda *a, **k: CLOSURE)
    config = WorkflowConfig(
        name="test",
        code={"path": str(source)},
        steps=[{"name": "test", "nix": {"expression": ".", "store": "s3://bucket"}, "script": "echo"}],
    )
    nix_resolution.process_nix(config, staged_code_dir=str(source), staging_dir=tmp_path / "stage")
    assert len(json.loads(path.read_text())["entries"]) == 1


def test_eval_failure_does_not_record_entry(source, tmp_path, monkeypatch):
    path = source / "cache.json"
    monkeypatch.setattr(nix_utils, "eval_closure_path", Mock(side_effect=nix_utils.NixEvalError("failed")))
    with pytest.raises(nix_utils.NixEvalError):
        resolve(source, tmp_path / "stage", path)
    assert not path.exists()


def test_cache_key_distinguishes_relative_expressions(source, tmp_path, monkeypatch):
    path = tmp_path / "shared.json"
    monkeypatch.setattr(nix_utils, "eval_closure_path", lambda *a, **k: CLOSURE)
    for expression in ["one.nix", "two.nix"]:
        (source / expression).write_text("{}")
    for expression in ["one.nix", "two.nix"]:
        nix_utils.maybe_eval_closure(
            code_path=str(source),
            staged_root=str(source),
            staging_dir=tmp_path / expression,
            resolved_expression=str(source / expression),
            role_name="test",
            expression=expression,
            nix_closure_cache=str(path),
        )
    assert {entry["expression"] for entry in json.loads(path.read_text())["entries"].values()} == {"one.nix", "two.nix"}
