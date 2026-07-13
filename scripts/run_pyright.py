"""Run the pinned Pyright distribution with a minimal, digest-pinned Node image."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pyright

from flowspec2.observability import log_event

PYRIGHT_NODE_IMAGE = (
    "gcr.io/distroless/nodejs24-debian13:nonroot@"
    "sha256:70a2c12a0d76018b54d7bd01c5e3677632eeed9f890ba318d6db55fc54cf3baa"
)
PYRIGHT_NODE_ENTRYPOINT = "/nodejs/bin/node"
PYRIGHT_CONTAINER_TIMEOUT_SECONDS = 300
PYRIGHT_TEMP_FILESYSTEM = "/tmp:rw,noexec,nosuid,size=64m"
logger = logging.getLogger(__name__)


def _pyright_distribution_path() -> Path:
    package_file = pyright.__file__
    if package_file is None:
        raise RuntimeError("cannot locate the installed Pyright package")
    distribution_path = Path(package_file).resolve().parent / "dist"
    if not (distribution_path / "index.js").is_file():
        raise RuntimeError("the pinned Pyright package has no bundled distribution")
    return distribution_path


def _run_pyright() -> int:
    project_root = Path(__file__).resolve().parents[1]
    pyright_distribution = _pyright_distribution_path()
    docker_command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        PYRIGHT_TEMP_FILESYSTEM,
        "--mount",
        f"type=bind,source={project_root},target=/workspace,readonly",
        "--mount",
        f"type=bind,source={pyright_distribution},target=/pyright,readonly",
        "--workdir",
        "/workspace",
        "--entrypoint",
        PYRIGHT_NODE_ENTRYPOINT,
        PYRIGHT_NODE_IMAGE,
        "/pyright/index.js",
    ]
    completed_process = subprocess.run(
        docker_command,
        check=False,
        timeout=PYRIGHT_CONTAINER_TIMEOUT_SECONDS,
    )
    return completed_process.returncode


def main() -> int:
    try:
        return _run_pyright()
    except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired) as runner_error:
        log_id = log_event(
            logger,
            logging.ERROR,
            "Pyright runner failed",
            operation="typecheck",
            context={"error_type": type(runner_error).__name__},
            exc_info=True,
        )
        print(f"ERROR [log_id={log_id}]: {runner_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
