#!/usr/bin/env python3

import io
import queue
import re
import sys
import threading
import time

import pytest

import seekr_chain
from seekr_chain._testing import assert_nested_match


class QueueStdin(io.TextIOBase):
    def __init__(self):
        super().__init__()
        self._q = queue.Queue()

    def readline(self, *args, **kwargs):
        # Blocks until a line is available
        line = self._q.get()
        if line is None:
            return ""  # EOF
        return line

    def feed_line(self, line: str):
        self._q.put(line)

    def close(self):
        # Signal EOF
        self._q.put(None)
        super().close()


@pytest.mark.interactive
class TestInteractiveActual:
    """
    Run interactive job interactively. For development, normally skipped
    """

    def test_basic(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic-interactive",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": """
                        pwd
                        ls ./
                        ls /seekr-chain
                        ls ../entrypoints
                        """,
                    }
                ],
                "code": {"path": "./"},
            }
        )

        seekr_chain.launch_k8s_workflow(config, interactive=True)


class TestInteractive:
    def test_basic(self, monkeypatch):
        orig_stdin = sys.stdin
        orig_stderr = sys.stderr
        orig_stdout = sys.stdout

        stdin = QueueStdin()
        stdout = io.StringIO()

        job_id = None
        try:
            # Patch sys.stdin/stdout for the duration of this test
            monkeypatch.setattr(sys, "stdin", stdin)
            monkeypatch.setattr(sys, "stdout", stdout)
            monkeypatch.setattr(sys, "stderr", stdout)

            config = seekr_chain.WorkflowConfig.model_validate(
                {
                    "name": "test-basic-interactive",
                    "namespace": "argo-workflows",
                    "ttl": "1:00:00",
                    "steps": [
                        {
                            "name": "step",
                            "image": "ubuntu:24.04",
                            "script": """
                            pwd
                            ls ./
                            ls /seekr-chain
                            ls ../entrypoints
                            """,
                        }
                    ],
                    "code": {"path": "./"},
                }
            )

            # Run the interactive workflow in a background thread
            t = threading.Thread(
                target=seekr_chain.launch_k8s_workflow,
                kwargs=dict(config=config, interactive=True),
                daemon=True,
            )
            t.start()

            # Wait until shell is ready
            timeout = 300
            t0 = time.time()
            dt = time.time() - t0
            while "To run this job" not in stdout.getvalue():
                dt = time.time() - t0
                if dt > timeout:
                    raise AssertionError(
                        f"Shell never became ready\n\nOutput:\n{stdout.getvalue()}\n\n----END OUTPUT----"
                    )
                time.sleep(0.05)
            print(f"CONNECTED AFTER {dt}")

            # Now simulate interactive user input
            # This CURRENTLY IS NOT PICKED UP in our pipe, because we attach to the interactive job
            # with a subprocess. run
            stdin.feed_line("echo hello\n")
            stdin.feed_line("exit\n")
            stdin.close()

            t.join(timeout=10)

        finally:
            # --- ALWAYS restore ---
            monkeypatch.setattr(sys, "stdin", orig_stdin)
            monkeypatch.setattr(sys, "stdout", orig_stdout)
            monkeypatch.setattr(sys, "stderr", orig_stderr)

            out = stdout.getvalue()

            for line in out.split("\n"):
                if match := re.match(r"chain delete (.+)", line.strip()):
                    print("DELETING WORKFLOW")
                    job_id = match.groups()[0]
                    seekr_chain.K8sWorkflow(id=job_id, namespace=config.namespace).delete()
                    break
            else:
                print("Workflow not found")

        assert job_id is not None

        out = stdout.getvalue().split("\n")

        expected = [
            rf"First running/finished pod: {job_id}-.*",
            (".*", "*"),
            f"    Argo Workflow Name: {job_id}",
            "",
            "    Type `c-d` to exit this shell",
            "",
            "    To run this job, use `/seekr-chain/entrypoint.sh`",
            "    ",
            (".*", "*"),
            "Disconnected",
            "",
            "This workflow will continue to run until terminated! To terminate this job, run:",
            "",
            f"  chain delete {job_id}",
            (".*", "*"),
        ]

        assert_nested_match(out, expected)
