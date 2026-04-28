#!/usr/bin/env python3

import sys
from pathlib import Path

import pytest

import seekr_chain

# Make the tests/ package importable (needed for tests.hermetic.*)
_TESTS_DIR = Path(__file__).parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


def pytest_addoption(parser):
    parser.addoption("--run-interactive", action="store_true", default=False, help="Run tests marked as interactive")
    parser.addoption(
        "--real-cluster",
        action="store_true",
        default=False,
        help="Run tests against a real K8s cluster instead of hermetic k3d+MinIO",
    )
    parser.addoption(
        "--gpu", action="store_true", default=False, help="Run ONLY gpu-marked tests (implies --real-cluster)"
    )
    # parser.addoption("--debug", action="store_true", default=False, help="Enable debug logging")


def pytest_configure(config):
    config.addinivalue_line("markers", "interactive: mark test as interactive")
    config.addinivalue_line("markers", "gpu: mark test as requiring GPU hardware (run with --gpu)")
    config.addinivalue_line("markers", "integration: mark test as an integration test (requires hermetic cluster)")

    if config.getoption("--debug", default=False):
        seekr_chain.configure_root_logger("DEBUG")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-interactive"):
        pass  # --run-interactive given, do not skip interactive tests
    else:
        skip_interactive = pytest.mark.skip(reason="need --run-interactive option to run")
        for item in items:
            if "interactive" in item.keywords:
                item.add_marker(skip_interactive)

    if config.getoption("--gpu"):
        # --gpu: collect ONLY gpu-marked tests (deselect everything else)
        deselected = [item for item in items if "gpu" not in item.keywords]
        items[:] = [item for item in items if "gpu" in item.keywords]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
    else:
        # Default: skip gpu tests (need --gpu to run them)
        skip_gpu = pytest.mark.skip(reason="requires GPU hardware (run with --gpu)")
        for item in items:
            if "gpu" in item.keywords:
                item.add_marker(skip_gpu)


@pytest.fixture
def unique_test_name(request):
    """
    Returns a unique test name based on relative path, class name (if any), and function name.
    Format: relative/path/to/test_file.py::ClassName::test_function
    """
    test_path = Path(request.fspath)
    root_path = Path(str(request.config.rootdir))
    rel_path = test_path.relative_to(root_path).as_posix()

    cls_name = request.node.cls.__name__ if request.node.cls else None
    func_name = request.node.name

    parts = [rel_path]
    if cls_name:
        parts.append(cls_name)
    parts.append(func_name)

    return "::".join(parts)
