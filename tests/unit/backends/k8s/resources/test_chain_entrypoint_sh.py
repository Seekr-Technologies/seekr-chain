"""Behavior tests for the workload entrypoint."""

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[5] / "src/seekr_chain/backends/k8s/resources/chain-entrypoint.sh"
FLUENTBIT_SCRIPT = Path(__file__).resolve().parents[5] / "src/seekr_chain/backends/k8s/resources/fluentbit.sh"


def _wait_for(path: Path, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"{path} was not created")


class TestChainEntrypoint:
    def test_forwards_sigterm_to_the_running_user_script_and_requests_log_flush(self, tmp_path):
        entrypoint = tmp_path / "chain-entrypoint.sh"
        entrypoint.write_text(SCRIPT.read_text().replace("/seekr-chain", str(tmp_path)))
        entrypoint.chmod(0o755)

        started = tmp_path / "started"
        terminated = tmp_path / "terminated"
        (tmp_path / "before_script.sh").write_text("#!/bin/sh\nexit 0\n")
        (tmp_path / "script.sh").write_text(
            f"#!/bin/sh\ntouch {started}\ntrap 'touch {terminated}; exit 0' TERM\nwhile :; do sleep 1; done\n"
        )
        (tmp_path / "after_script.sh").write_text("#!/bin/sh\nexit 0\n")
        for script in ("before_script.sh", "script.sh", "after_script.sh"):
            (tmp_path / script).chmod(0o755)

        env = os.environ.copy()
        env["LOG_FLUSH_TIMEOUT"] = "0"
        process = subprocess.Popen(
            ["sh", str(entrypoint)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        try:
            _wait_for(started)
            process.send_signal(signal.SIGTERM)
            _wait_for(terminated)
            process.wait(timeout=5)
            output = process.stdout.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        assert (
            terminated.exists(),
            (tmp_path / ".shutdown").exists(),
            bool(
                re.search(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\] chain \| Received SIGTERM", output, re.M)
            ),
            "SIGTERM shutdown: final log upload did not complete before timeout." in output,
        ) == (True, True, True, True)


class TestFluentBitEntrypoint:
    def test_flushes_logs_when_it_receives_sigterm(self, tmp_path):
        fluentbit = tmp_path / "fluentbit.sh"
        fake_fluentbit = tmp_path / "fluent-bit"
        fluentbit.write_text(
            FLUENTBIT_SCRIPT.read_text()
            .replace("/seekr-chain", str(tmp_path))
            .replace('FLUENTBIT_BIN="/fluent-bit/bin/fluent-bit"', f'FLUENTBIT_BIN="{fake_fluentbit}"')
            .replace("SHUTDOWN_GRACE_PERIOD=5", "SHUTDOWN_GRACE_PERIOD=0")
        )
        fluentbit.chmod(0o755)

        started = tmp_path / "fluentbit-started"
        terminated = tmp_path / "fluentbit-terminated"
        fake_fluentbit.write_text(
            f"#!/bin/sh\ntouch {started}\ntrap 'touch {terminated}; exit 0' TERM\nwhile :; do sleep 1; done\n"
        )
        fake_fluentbit.chmod(0o755)
        (tmp_path / "resources").mkdir()
        (tmp_path / "resources" / "fluentbit.conf").write_text("")
        (tmp_path / ".hb").write_text("")
        (tmp_path / "logs.txt").write_text("")

        env = os.environ.copy()
        env.update({"SEEKR_CHAIN_LOGS": str(tmp_path / "logs.txt"), "S3_STEP_DATA_PREFIX": "logs"})
        process = subprocess.Popen(["sh", str(fluentbit)], env=env)
        try:
            _wait_for(started)
            process.send_signal(signal.SIGTERM)
            _wait_for(terminated)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        assert ((tmp_path / ".shutdown").exists(), (tmp_path / ".logs_flushed").exists()) == (True, True)

    def test_finishes_final_upload_within_four_seconds_of_shutdown_request(self, tmp_path):
        fluentbit = tmp_path / "fluentbit.sh"
        fake_fluentbit = tmp_path / "fluent-bit"
        fluentbit.write_text(
            FLUENTBIT_SCRIPT.read_text()
            .replace("/seekr-chain", str(tmp_path))
            .replace('FLUENTBIT_BIN="/fluent-bit/bin/fluent-bit"', f'FLUENTBIT_BIN="{fake_fluentbit}"')
        )
        fluentbit.chmod(0o755)

        started = tmp_path / "fluentbit-started"
        fake_fluentbit.write_text(f"#!/bin/sh\ntouch {started}\nwhile :; do sleep 1; done\n")
        fake_fluentbit.chmod(0o755)
        (tmp_path / "resources").mkdir()
        (tmp_path / "resources" / "fluentbit.conf").write_text("")
        (tmp_path / ".hb").write_text("")
        (tmp_path / "logs.txt").write_text("")

        env = os.environ.copy()
        env.update({"SEEKR_CHAIN_LOGS": str(tmp_path / "logs.txt"), "S3_STEP_DATA_PREFIX": "logs"})
        process = subprocess.Popen(["sh", str(fluentbit)], env=env)
        try:
            _wait_for(started)
            start = time.monotonic()
            (tmp_path / ".shutdown").write_text("")
            _wait_for(tmp_path / ".logs_flushed", timeout=8)
            elapsed = time.monotonic() - start
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        assert elapsed < 4

    def test_log_message_formats_busybox_timestamps_to_milliseconds(self, tmp_path):
        entrypoint = tmp_path / "chain-entrypoint.sh"
        entrypoint.write_text(
            SCRIPT.read_text().replace("/seekr-chain", str(tmp_path)).replace('main "$@"', 'log_message "test"')
        )
        entrypoint.chmod(0o755)
        (tmp_path / "bin").mkdir()
        date = tmp_path / "bin" / "date"
        date.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *%3N*) echo '2026-08-25T15:11:59.%3NZ';;\n"
            f'  *) exec {shutil.which("date")} "$@";;\n'
            "esac\n"
        )
        date.chmod(0o755)

        result = subprocess.run(["sh", str(entrypoint)], capture_output=True, text=True, check=True)

        assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{2}0Z\] chain \| test\n", result.stdout)
