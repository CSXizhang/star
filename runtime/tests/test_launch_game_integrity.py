"""Exercise launcher safety blocks without starting the game."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher")
SOURCE = (ROOT / "tools/launch-game.ps1").read_text(encoding="utf-8-sig")


def run_ps(code):
    return subprocess.run([shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", code],
                          capture_output=True, encoding="utf-8", errors="replace", timeout=20)


def helper(name):
    path = (ROOT / "tools/launch-game.ps1").as_posix()
    return f"""
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$null)
$fn = $ast.Find({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq '{name}' }}, $true)
Invoke-Expression $fn.Extent.Text
"""


def test_override_preserves_run_identity_and_allows_appdata():
    result = run_ps(helper("Assert-ChildEnvironmentOverride") + """
Assert-ChildEnvironmentOverride 'APPDATA'
$blocked = @()
foreach ($name in @('stardew_ai_isolated_saves','STARDEW_AI_SESSION_CONFIG','STARDEW_AI_EVIDENCE_DIR','STARDEW_AI_EVIDENCE_OUTPUT','STARDEW_AI_RUN_ID')) {
    try { Assert-ChildEnvironmentOverride $name } catch { $blocked += $name }
}
ConvertTo-Json @($blocked)
""")
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 5
    # Both explicit and forwarded overrides go through the tested guard.
    assert "Assert-ChildEnvironmentOverride $key" in SOURCE
    assert "Assert-ChildEnvironmentOverride $target" in SOURCE


@pytest.mark.parametrize("failure", ["none", "missing-baseline", "hash-error"])
def test_integrity_only_claims_success_after_complete_checks(failure):
    block = SOURCE.split("    # 4. Verify and Write Daily Saves Post Hashes", 1)[1].split("\n}\n\n# --- OUTCOME EVALUATION ---", 1)[0]
    result = run_ps("""
$ErrorActionPreference = 'Stop'
$violations = [System.Collections.Generic.List[string]]::new()
$preSavesHashes = @{}; $preModsHashes = @{}; $preGoldenHashes = @{ 'save' = 'unchanged' }
$CustomSavePath = ''; $dailySavesDir = 'saves'; $dailyModsDir = 'mods'; $goldenV1Save = 'golden'; $runEvidenceDir = $env:TEMP
function Write-Utf8File { }
function Get-FolderHashes($path) {
    if ($path -eq 'golden') { return @{ 'save' = 'unchanged' } }
    return @{}
}
""" + ("$preSavesHashes=$null\n" if failure == "missing-baseline" else "function Get-FolderHashes { throw 'hash failure' }\n" if failure == "hash-error" else "") + block + "\nWrite-Output ('VIOLATIONS=' + $violations.Count)")
    assert result.returncode == 0, result.stderr
    assert ("INTEGRITY VERIFIED" in result.stdout) == (failure == "none")
    assert ("VIOLATIONS=0" in result.stdout) == (failure == "none")


def test_folder_hashes_do_not_swallow_enumeration_error():
    result = run_ps(helper("Get-FolderHashes") + """
function Test-Path { return $true }
function Get-ChildItem { [CmdletBinding()] param([string]$LiteralPath, [switch]$Recurse, [switch]$File) Write-Error 'Cannot enumerate' }
try { Get-FolderHashes 'unreadable'; Write-Output 'FALSE_SUCCESS' } catch { Write-Output 'CHECK_FAILED' }
""")
    assert result.returncode == 0, result.stderr
    assert "CHECK_FAILED" in result.stdout and "FALSE_SUCCESS" not in result.stdout
