"""Launcher argument propagation with an offline Python module, no providers."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher")


def _fake_bridge(tmp_path):
    module = tmp_path / "modules/stardew_ai_runtime"
    module.mkdir(parents=True)
    marker = tmp_path / "arguments.json"
    (module / "__init__.py").write_text("")
    (module / "chat_bridge.py").write_text(
        "import json,sys; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(json.dumps(sys.argv[1:]))",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(module.parent)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return marker, env


@pytest.mark.parametrize("owner_pid", [None, 12345])
def test_development_script_forwards_optional_owner_without_dropping_arguments(tmp_path, owner_pid):
    root = tmp_path / "dev package with spaces"
    tools = root / "tools"
    tools.mkdir(parents=True)
    script = tools / "start-companion.ps1"
    shutil.copyfile(ROOT / "tools/start-companion.ps1", script)
    (tools / "candidate-package.ps1").write_text("")
    marker, env = _fake_bridge(tmp_path)
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(script)]
    if owner_pid is not None:
        command += ["-OwnerProcessId", str(owner_pid)]
    command += ["--backend", "fake"]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = ["--backend", "fake"]
    if owner_pid is not None:
        expected = ["--owner-pid", str(owner_pid)] + expected
    assert json.loads(marker.read_text()) == expected


@pytest.mark.parametrize("owner_pid", [None, 12345])
def test_release_function_forwards_optional_owner_without_dropping_arguments(tmp_path, owner_pid):
    root = tmp_path / "release package with spaces"
    (root / "config").mkdir(parents=True)
    (root / "config/chat-backend.json").write_text(json.dumps({"backend": "codex"}))
    marker, env = _fake_bridge(tmp_path)
    source = (ROOT / "tools/release-package.ps1").as_posix().replace("'", "''")
    python = Path(sys.executable).as_posix().replace("'", "''")
    package = root.as_posix().replace("'", "''")
    code = f"""
. '{source}'
function Read-VerifiedRelease {{ return @{{ python = 'fixture' }} }}
function Get-ReleasePath {{ return '{python}' }}
function Find-ReleaseAiCli {{ return '{python}' }}
function Assert-ReleaseAiCli {{ }}
Start-ReleaseCompanion '{package}' {f'-OwnerProcessId {owner_pid}' if owner_pid is not None else ''} -ForwardArgs @('--model', 'offline-fixture')
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-Command", code], env=env, capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = ["--run-dir", root.as_posix()]
    if owner_pid is not None:
        expected += ["--owner-pid", str(owner_pid)]
    assert json.loads(marker.read_text()) == expected + ["--model", "offline-fixture"]
