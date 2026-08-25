# rigol-oscilloscope-mcp

Rigol製オシロスコープを **LLMから操作する** MCPサーバー。

「x10プローブで1kHz 3Vの波形を見えるようにして」「今の波形をスクショして保存して」といった自然言語の指示を、
LLM(Claude / Codex 等)がMCP Tool呼び出しへ変換し、本サーバーがSCPI(LAN / USB)で機器を制御する。
GUI自動操作は使わない。

- **RIGOL MHO98 で実機検証済み**(→ [docs/verification/mho98-mvp.md](docs/verification/mho98-mvp.md))
- 他のRigol機種は機種プロファイルによるベストエフォート対応(未知の機種は generic プロファイルで動作し、その旨を明示する)
- Rigol以外のベンダーは対象外(接続時に警告を返すが拒否はしない)

## 特徴

- **会話ベースの接続** — 接続先はユーザーが会話で指示するのが基本(`connect(address="...")`)。環境変数のデフォルトは任意のフォールバック
- **20個のMCP Tool** — 接続 / 識別 / 状態取得 / 測定 / 波形 / スクリーンショット / チャンネル・タイムベース・トリガ設定 / Run・Stop・Single・Autoset / シリアルデコード設定。SCPI文字列をLLMに書かせず、意味的Toolのみを公開する
- **4クラスの安全ポリシー + confirmトークン** — 全操作を READ_ONLY / SAFE_WRITE / RESTRICTED_WRITE / DANGEROUS_WRITE に分類。50Ω入力やAuto SetupはホストUI非依存の2段階確認(confirmトークン)を必須とする
- **スクリーンショット保存** — png / jpg / bmp / webp で指定パスへ保存し、画像そのものもLLMへ返す(書き込み先は許可ルートで制限)
- **機種プロファイル** — SCPI方言・機能有無・パラメータ範囲を同梱YAMLで宣言し、モデル完全一致 → ファミリ → 汎用Rigol の3層で解決する
- **requested / applied の両値返却** — 機器が設定値をスナップするかは機種依存のため、要求値とread-back値を両方返す
- **監査ログ** — 書き込み操作を Before / Action / After 付きでJSONLに記録する

## インストール・起動

GitHubリポジトリからの `uvx` 起動を標準とする(PyPI公開はしていない)。

```bash
uvx --from git+https://github.com/zinntikumugai/rigol-oscilloscope-mcp rigol-oscilloscope-mcp
```

タグ付きリリースには `@<tag>` を付けてバージョンを固定できる。

### Claude Code — プラグイン(推奨)

本リポジトリはClaudeプラグインを兼ねており、MCPサーバーに加えて測定ワークフロースキル
(信号種別ごとの推奨設定・UART/未知信号の測定手順・安全プロンプト)が同時に導入される。

```
/plugin install rigol-oscilloscope@zinntikumugai/rigol-oscilloscope-mcp
```

### Claude Code(`.mcp.json` または `claude mcp add`)

```json
{
  "mcpServers": {
    "rigol-oscilloscope": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/zinntikumugai/rigol-oscilloscope-mcp", "rigol-oscilloscope-mcp"],
      "env": { "RIGOL_MCP_SCREENSHOT_DIR": "~/scope-captures" }
    }
  }
}
```

### Codex — プラグイン

Codexプラグイン(`.codex-plugin/` + マーケットプレイス定義)も同梱しており、MCPサーバーと測定ワークフロースキルを一括導入できる。

```bash
codex plugin marketplace add zinntikumugai/rigol-oscilloscope-mcp
codex plugin install rigol-oscilloscope
```

(プラグインを使わない場合、スキルだけなら `skills/measurement-workflows` を `~/.agents/skills/` へコピーしても認識される。MCPサーバーだけなら次の `config.toml` 設定で足りる)

### Codex(`~/.codex/config.toml`)

```toml
[mcp_servers.rigol-oscilloscope]
command = "uvx"
args = ["--from", "git+https://github.com/zinntikumugai/rigol-oscilloscope-mcp", "rigol-oscilloscope-mcp"]

[mcp_servers.rigol-oscilloscope.env]
RIGOL_MCP_SCREENSHOT_DIR = "~/scope-captures"
```

### ローカルのcloneから起動する場合

リポジトリを手元にcloneして開発版を使うときは、`uv run --directory` で起動する。

```json
{
  "mcpServers": {
    "rigol-oscilloscope": {
      "command": "/path/to/uv",
      "args": ["run", "--directory", "/path/to/rigol-oscilloscope-mcp", "rigol-oscilloscope-mcp"],
      "env": { "PYTHONDONTWRITEBYTECODE": "1" }
    }
  }
}
```

- `command` は、GUIホスト(デスクトップアプリ)のPATHに `uv` が無い場合に絶対パスで書く。パスは `which uv`(mise管理なら `mise which uv`)で確認する
- `PYTHONDONTWRITEBYTECODE=1` は明示する。プロジェクト外から起動すると `mise.toml` の `[env]` が効かないため、`__pycache__` がclone内に書かれるのを防ぐ
- スクリーンショットのデフォルト保存先は、`--directory` で移動した先ではなく**サーバーを起動した実行ディレクトリ**になる。固定したい場合は `RIGOL_MCP_SCREENSHOT_DIR` を指定する
- `path` に相対パスを渡した場合もこのデフォルト保存先が基準になる。デフォルト保存先・`RIGOL_MCP_ALLOWED_DIRS`・一時ディレクトリの外へは保存できない(拒否される)

## 設定(環境変数)

すべての設定は環境変数で指定できる(TOML設定ファイルも任意で使える)。
優先順位は **Tool引数(会話でのユーザー指示) > 環境変数 > 設定ファイル > 組み込みデフォルト**。

| 環境変数 | 内容 | デフォルト |
|---|---|---|
| `RIGOL_MCP_ADDRESS` | デフォルト接続先(IP / VISAリソース) | なし(会話指示を要求) |
| `RIGOL_MCP_TRANSPORT` | `lan` / `usb` | addressから推定 |
| `RIGOL_MCP_PORT` | LAN SCPIポート | プロファイル既定(5555) |
| `RIGOL_MCP_TIMEOUT_S` | 単一クエリのタイムアウト(秒) | 5 |
| `RIGOL_MCP_SCREENSHOT_DIR` | スクリーンショットの既定保存先 | 実行ディレクトリ(`PWD`。無効時はカレントディレクトリ) |
| `RIGOL_MCP_ALLOWED_DIRS` | 書き込み許可ルート(パス区切りで複数) | 既定保存先 + 一時ディレクトリ |
| `RIGOL_MCP_WAVEFORM_MAX_POINTS` | 波形取得の既定上限 | 100000 |
| `RIGOL_MCP_RAW_SCPI` | `raw_scpi` Toolの有効化(予約: Tool自体が未実装) | false |
| `RIGOL_MCP_LOG_LEVEL` | ログレベル(error / warn / info / debug) | info |
| `RIGOL_MCP_AUDIT_LOG` | 監査ログの出力先 | 有効(`~/.local/state/rigol-oscilloscope-mcp/audit.jsonl`。`XDG_STATE_HOME` に従う)。`off` で無効 |
| `RIGOL_MCP_CONFIG` | TOML設定ファイルのパス | なし |

詳細は [docs/Requirements.md](docs/Requirements.md) 9章。

## 実機なしで試す

`RIGOL_MCP_FAKE=1` を付けて起動すると、実機の代わりに内蔵のFakeScopeへ接続する。
ホスト側のMCP設定や会話フローを、オシロスコープを用意せずに確認できる。

```bash
RIGOL_MCP_FAKE=1 uvx --from git+https://github.com/zinntikumugai/rigol-oscilloscope-mcp rigol-oscilloscope-mcp
```

## 開発

Pythonバージョンは mise、依存と仮想環境は uv で管理する。

```bash
mise install          # Python + uv
uv sync               # 依存の解決
uv run pytest         # ユニットテスト(実機不要)
uv run rigol-oscilloscope-mcp   # stdioで起動
```

実機テストは接続先を環境変数で渡したときだけ実行される(未設定なら自動でskip)。
**実機のIPアドレスはリポジトリへ絶対に書かないこと**(`tests/test_ip_guard.py` が機械的に検査している)。

```bash
# read-only スイート(機器の設定を変更しない)
RIGOL_TEST_ADDRESS=<あなたのオシロのIP> uv run pytest -m device

# write スイート(設定変更 → read-back → 必ず復元)。二重ゲート
RIGOL_TEST_ADDRESS=<あなたのオシロのIP> RIGOL_TEST_ALLOW_WRITE=1 uv run pytest -m device_write
```

`<あなたのオシロのIP>` にはご自身の機器のアドレスを入れる(例示が必要な場合は
ドキュメント用に予約された `192.0.2.x`(TEST-NET-1)を使うこと)。

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `spawn uv ENOENT`(サーバーが起動しない) | GUIホストのPATHに `uv` が無い。MCP設定の `command` を絶対パス(`which uv` / `mise which uv` の出力)に書き換える |
| スクリーンショットが意図しない場所に保存される | 既定はサーバーを起動した実行ディレクトリ。`RIGOL_MCP_SCREENSHOT_DIR` で保存先を明示指定する |

## 安全上の注意

**MHO98をはじめ多くのRigolオシロは非絶縁である**(各入力のGNDが筐体・USB等のGNDと共通、測定カテゴリ Category I)。
本サーバーは危険な設定変更を防止するが、電気的安全性そのものを保証するものではない。

| 主体 | 責務 |
|---|---|
| AI (LLM) | 測定設定の判断と結果解析の支援 |
| MCPサーバー | 機器制御、パラメータ検証、安全ポリシーの担保、操作記録 |
| **人間** | **DUT・プローブ・グラウンド等の物理接続と電気的安全の担保** |

- プローブの接続先・Ground Clipの接続先・DUTの実電圧・プローブ耐圧・絶縁状態は、MCPから確認できない
- **商用電源(100V AC、コンセント、一次側、AC mains)の測定は対象外。**通常のパッシブプローブによる測定手順を自動実行しない。差動・絶縁プローブの使用を人間が確認することが前提
- Firmware Update / Calibration / Factory Service操作 / ネットワーク設定変更は非対象

詳細は [docs/Requirements.md](docs/Requirements.md) 6章(安全要件)。

## ドキュメント

[docs/README.md](docs/README.md) に文書一覧と読む順序をまとめている。

- [docs/Requirements.md](docs/Requirements.md) — 要件定義書(規範)
- [docs/tools.md](docs/tools.md) — MCP Toolカタログ
- [docs/device-profiles.md](docs/device-profiles.md) — 機種プロファイル仕様
- [docs/verification/](docs/verification/) — 実機検証の記録
- [docs/roadmap.md](docs/roadmap.md) — 今後の対応予定
