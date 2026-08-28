"""Claude / Codex プラグイン同梱物の整合テスト(Requirements.md 10.3 / roadmap.md 6章)。

サーバーコードには触れない静的検証のみ: 各プラグインマニフェストが有効で、
両マニフェストが同じ実体(name / version / MCP起動列 / skills)を指しており、
同梱スキルが実在するToolを参照していること(スキルとサーバーの乖離ガード)。
"""

import json
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
CODEX_MANIFEST = REPO_ROOT / ".codex-plugin" / "plugin.json"
CODEX_MCP = REPO_ROOT / ".codex-plugin" / "mcp.json"
CODEX_MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
SKILL = REPO_ROOT / "skills" / "measurement-workflows" / "SKILL.md"


def test_plugin_manifest_is_valid():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["name"] == "rigol-oscilloscope"
    server = data["mcpServers"]["rigol-oscilloscope"]
    assert server["command"] == "uvx"
    # 起動コマンドはRequirements.md 10.1の標準形と一致させる
    assert "rigol-oscilloscope-mcp" in server["args"][-1] or "rigol-oscilloscope-mcp" in server["args"]


def test_codex_manifest_is_valid():
    data = json.loads(CODEX_MANIFEST.read_text(encoding="utf-8"))
    assert data["name"] == "rigol-oscilloscope"
    # skills はClaudeプラグインと同じディレクトリを共有する
    assert data["skills"] == "./skills/"
    assert (REPO_ROOT / "skills" / "measurement-workflows" / "SKILL.md").is_file()
    # mcpServers はプラグインルート内の相対パスを指す
    mcp_path = REPO_ROOT / data["mcpServers"]
    assert mcp_path.resolve().is_relative_to(REPO_ROOT)
    assert mcp_path.is_file()


def test_claude_marketplace_references_the_plugin():
    """Claude Codeの /plugin install はマーケットプレイス必須(実機で確認済み)。

    `@` 以降はマーケットプレイスの name を指すため、README記載のインストール
    手順(rigol-oscilloscope@rigol-oscilloscope-mcp)と一致することを固定する。
    """
    data = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert data["name"] == "rigol-oscilloscope-mcp"
    entries = {p["name"]: p for p in data["plugins"]}
    assert entries["rigol-oscilloscope"]["source"] == "./"


def test_codex_marketplace_references_the_plugin():
    data = json.loads(CODEX_MARKETPLACE.read_text(encoding="utf-8"))
    entries = {p["name"] for p in data["plugins"]}
    assert "rigol-oscilloscope" in entries


def test_manifests_agree():
    """Claude / Codex 両マニフェストと pyproject の二重管理ガード。"""
    claude = json.loads(MANIFEST.read_text(encoding="utf-8"))
    codex = json.loads(CODEX_MANIFEST.read_text(encoding="utf-8"))
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert claude["name"] == codex["name"]
    assert claude["version"] == codex["version"] == pyproject["project"]["version"]

    claude_server = claude["mcpServers"]["rigol-oscilloscope"]
    codex_servers = json.loads(CODEX_MCP.read_text(encoding="utf-8"))
    codex_server = codex_servers["rigol-oscilloscope"]
    assert codex_server["command"] == claude_server["command"]
    assert codex_server["args"] == claude_server["args"]


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
        "run",
        "stop",
        "single",
        "measure",
        "capture_waveform",
        "capture_screenshot",
        "configure_decode",
        "get_decode_result",
        "analyze_waveform",
    ):
        assert tool in body, f"skill does not mention tool {tool}"
        assert tool in TOOL_CLASSES
