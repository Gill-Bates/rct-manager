#!/usr/bin/env python3
#
# tests/test_docker_entrypoint.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Static checks of the container entrypoint and its Dockerfile/Compose wiring."""

import shutil
import subprocess
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker"
ENTRYPOINT = DOCKER_DIR / "entrypoint.sh"


def _entrypoint() -> str:
    return ENTRYPOINT.read_bytes().decode()


def test_entrypoint_exists_and_is_bash_strict() -> None:
    text = _entrypoint()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in text
    assert "\r" not in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_entrypoint_passes_bash_syntax_check() -> None:
    result = subprocess.run(["bash", "-n", str(ENTRYPOINT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_entrypoint_drops_privileges_by_uid_not_argv() -> None:
    text = _entrypoint()
    assert 'exec gosu "${APP_USER}" "$@"' in text
    assert '"$(id -u)" -eq 0' in text
    assert "--run" not in text
    # DATA_DIR is validated before the first chown/chmod can run.
    assert text.index('validate_managed_dir "DATA_DIR"') < text.index("bootstrap_as_root\n    log")


def test_dockerfile_wires_entrypoint_and_gosu() -> None:
    text = (DOCKER_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh" in text
    assert "bash -n /entrypoint.sh" in text
    assert "install -y --no-install-recommends gosu" in text
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in text
    assert 'CMD ["serve"]' in text
    assert not any(line.startswith("USER ") for line in text.splitlines())


def test_dockerignore_lets_entrypoint_into_the_build_context() -> None:
    lines = (DOCKER_DIR.parent / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert lines.index("docker/") < lines.index("!docker/entrypoint.sh")


def test_compose_keeps_hardening_and_adds_minimal_capabilities() -> None:
    text = (DOCKER_DIR / "compose.yaml").read_text(encoding="utf-8")
    assert "cap_drop:\n      - ALL\n    cap_add:\n      - CHOWN\n      - SETUID\n      - SETGID\n" in text
    assert "DAC_OVERRIDE" not in text.split("cap_add:")[1].split("read_only")[0]
    assert "read_only: true" in text
    assert "no-new-privileges:true" in text
