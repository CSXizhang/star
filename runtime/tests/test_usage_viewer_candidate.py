import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("candidate_usage", Path(__file__).resolve().parents[2] / "tools/view-usage.py")
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)

def test_instance_only_and_unknown_requests(tmp_path):
    (tmp_path / "chat_commands.jsonl").write_text(json.dumps({"requestId": "candidate-one", "usage": {"requests": [{"index": 1, "inputOther": 23, "output": None}]}}), encoding="utf-8")
    commands = viewer.load_chat_commands(tmp_path)
    assert len(commands) == 1
    output = tmp_path / "report.html"
    viewer.generate_html_report([], output, commands)
    html = output.read_text(encoding="utf-8")
    assert "每次模型请求（1）" in html and "<td>23</td>" in html and "未知" in html
    assert "class=\"stats-grid\"" not in html
    assert "会话底层汇总明细" not in html

def test_installed_instance_scope(tmp_path):
    (tmp_path / "config").mkdir()
    game = tmp_path / "game"
    metadata = tmp_path / "config/installed-candidate.json"
    normal = game / "Mods/StardewAI.Companion.Mod"
    metadata.write_text(json.dumps({"gameDirectory": str(game), "modDirectory": str(normal)}))
    assert viewer.installed_usage_directory(tmp_path) == normal.resolve()
    metadata.write_text(json.dumps({"gameDirectory": str(game), "modDirectory": str(tmp_path / ".test-runs/fake")}))
    with pytest.raises(ValueError):
        viewer.installed_usage_directory(tmp_path)
