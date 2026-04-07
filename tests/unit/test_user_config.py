"""Tests for the layered user configuration system."""

from unittest.mock import patch

import pytest

from seekr_chain.user_config import _load_config
from tests.unit.conftest import no_dotenv, no_toml_files


class TestLoadConfig:
    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("SEEKRCHAIN_DATASTORE_ROOT", "s3://bucket/prefix/")
        with no_dotenv(), no_toml_files():
            cfg = _load_config()
        assert cfg.datastore_root == "s3://bucket/prefix/"

    def test_dotenv_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SEEKRCHAIN_DATASTORE_ROOT", raising=False)
        dotenv_file = tmp_path / ".env"
        dotenv_file.write_text("SEEKRCHAIN_DATASTORE_ROOT=s3://from-dotenv/\n")
        with patch("seekr_chain.user_config.dotenv.find_dotenv", return_value=str(dotenv_file)), no_toml_files():
            cfg = _load_config()
        assert cfg.datastore_root == "s3://from-dotenv/"

    def test_local_seekrchain_toml(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SEEKRCHAIN_DATASTORE_ROOT", raising=False)
        toml_file = tmp_path / ".seekrchain.toml"
        toml_file.write_text('datastore_root = "s3://from-toml/"\n')
        with no_dotenv(), patch("seekr_chain.user_config._find_file_walking_up", return_value=toml_file):
            cfg = _load_config()
        assert cfg.datastore_root == "s3://from-toml/"

    def test_global_seekrchain_toml(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SEEKRCHAIN_DATASTORE_ROOT", raising=False)
        global_toml = tmp_path / ".seekrchain.toml"
        global_toml.write_text('datastore_root = "s3://from-global/"\n')
        with (
            no_dotenv(),
            no_toml_files(),
            patch("seekr_chain.user_config.Path.home", return_value=tmp_path),
        ):
            cfg = _load_config()
        assert cfg.datastore_root == "s3://from-global/"

    def test_env_var_beats_dotenv(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEEKRCHAIN_DATASTORE_ROOT", "s3://from-env/")
        dotenv_file = tmp_path / ".env"
        dotenv_file.write_text("SEEKRCHAIN_DATASTORE_ROOT=s3://from-dotenv/\n")
        with patch("seekr_chain.user_config.dotenv.find_dotenv", return_value=str(dotenv_file)), no_toml_files():
            cfg = _load_config()
        assert cfg.datastore_root == "s3://from-env/"

    def test_env_var_beats_toml(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SEEKRCHAIN_DATASTORE_ROOT", "s3://from-env/")
        toml_file = tmp_path / ".seekrchain.toml"
        toml_file.write_text('datastore_root = "s3://from-toml/"\n')
        with no_dotenv(), patch("seekr_chain.user_config._find_file_walking_up", return_value=toml_file):
            cfg = _load_config()
        assert cfg.datastore_root == "s3://from-env/"

    def test_dotenv_beats_toml(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SEEKRCHAIN_DATASTORE_ROOT", raising=False)
        dotenv_file = tmp_path / ".env"
        dotenv_file.write_text("SEEKRCHAIN_DATASTORE_ROOT=s3://from-dotenv/\n")
        toml_file = tmp_path / ".seekrchain.toml"
        toml_file.write_text('datastore_root = "s3://from-toml/"\n')
        with (
            patch("seekr_chain.user_config.dotenv.find_dotenv", return_value=str(dotenv_file)),
            patch("seekr_chain.user_config._find_file_walking_up", return_value=toml_file),
        ):
            cfg = _load_config()
        assert cfg.datastore_root == "s3://from-dotenv/"

    def test_local_toml_beats_global_toml(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SEEKRCHAIN_DATASTORE_ROOT", raising=False)
        local_toml = tmp_path / "local.seekrchain.toml"
        local_toml.write_text('datastore_root = "s3://from-local/"\n')
        global_dir = tmp_path / "home"
        global_dir.mkdir()
        global_toml = global_dir / ".seekrchain.toml"
        global_toml.write_text('datastore_root = "s3://from-global/"\n')
        with (
            no_dotenv(),
            patch("seekr_chain.user_config._find_file_walking_up", return_value=local_toml),
            patch("seekr_chain.user_config.Path.home", return_value=global_dir),
        ):
            cfg = _load_config()
        assert cfg.datastore_root == "s3://from-local/"

    def test_returns_none_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("SEEKRCHAIN_DATASTORE_ROOT", raising=False)
        with no_dotenv(), no_toml_files():
            cfg = _load_config()
        assert cfg.datastore_root is None
