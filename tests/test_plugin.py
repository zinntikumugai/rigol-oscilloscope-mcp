"""Claudeプラグイン同梱物の整合テスト(Requirements.md 10.3 / roadmap Phase 3)。

サーバーコードには触れない静的検証のみ: プラグインマニフェストが有効で、
同梱スキルが実在するToolを参照していること(スキルとサーバーの乖離ガード)。
"""

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
SKILL = REPO_ROOT / "skills" / "measurement-workflows" / "SKILL.md"


def test_plugin_manifest_is_valid():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["name"] == "rigol-oscilloscope"
    server = data["mcpServers"]["rigol-oscilloscope"]
    assert server["command"] == "uvx"
    # 起動コマンドはRequirements.md 10.1の標準形と一致させる
    assert "rigol-oscilloscope-mcp" in server["args"][-1] or "rigol-oscilloscope-mcp" in server["args"]


def test_skill_frontmatter():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = yaml.safe_load(text.split("---\n", 2)[1])
    assert frontmatter["name"] == "measurement-workflows"
    assert frontmatter["description"]


def test_skill_references_real_tools():
    """スキル本文が実在Tool名を使っていること(改名時にここで気づく)。"""
    from rigol_oscilloscope_mcp.safety.classes import TOOL_CLASSES

    body = SKILL.read_text(encoding="utf-8")
    for tool in (
        "get_state",
        "configure_channel",
        "configure_timebase",
        "configure_trigger",
        "single",
        "measure",
        "capture_screenshot",
    ):
        assert tool in body, f"skill does not mention tool {tool}"
        assert tool in TOOL_CLASSES
