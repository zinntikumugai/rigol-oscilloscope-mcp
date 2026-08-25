# AGENTS.md — エージェント向け開発ガイド

Rigol製オシロスコープをSCPI経由で操作するMCPサーバー(Python)。要件・仕様の規範は [docs/README.md](docs/README.md) から辿ること(Requirements / tools / device-profiles / verification / roadmap)。

## コマンド

```bash
mise install                  # Python + uv(バージョンはmise管理、依存はuv管理)
uv sync                       # 依存解決(dev含む)
uv run pytest                 # ユニットテスト全件(実機不要、FakeScopeで完結)
uv run pytest tests/test_xxx.py -v          # 単一ファイル
RIGOL_MCP_FAKE=1 uv run rigol-oscilloscope-mcp   # 実機なしでstdio起動(FakeScope接続)

# 実機テスト(env-gated。未設定なら自動skip)
RIGOL_TEST_ADDRESS=<実機IP> uv run pytest -m device                          # read-only
RIGOL_TEST_ADDRESS=<実機IP> RIGOL_TEST_ALLOW_WRITE=1 uv run pytest -m device_write  # write(復元付き)
```

mise外のシェルでは `mise exec -- uv run ...` を使う。

## アーキテクチャ

```
server.py(FastMCP、20 Tool、同期def+lock直列化)
  → service/(connection / state / measurement / waveform / screenshot / control / paths)
  → safety/(操作クラス表 / confirmトークン / 監査ログJSONL)
  → driver/(session: drain・set_and_verify / scope: プロファイル対応SCPI生成 / decode: :BUS変換表)
  → transport/(lan: raw socket 5555 / usb: PyVISA / blocks: IEEE488.2ブロック)
profiles/(YAML機種プロファイル: verified→family→generic の3層解決)
testing/(FakeScope: MHO98方言のフェイク機器。Transport層に差し込む)
skills/ + .claude-plugin/ + .codex-plugin/ + .agents/(Claude/Codex両プラグイン: 共有スキル+ホスト別マニフェスト。tests/test_plugin.py が整合を検証)
```

- MCP SDKのimportは `server.py` に閉じ込める。Toolは全て同期defで、返却は bare `dict` 注釈(structured content回避、実測済みの仕様)
- エラーは例外でなく `{"error": true, "code": ..., "message": ..., "detail": ...}` のJSONを正常contentで返す(デコレータが変換)

## 絶対に守るルール

1. **実機のIPアドレス・シリアルをリポジトリ内のいかなるファイルにも書かない。** 実機は `RIGOL_TEST_ADDRESS` 環境変数でのみ渡す。`tests/test_ip_guard.py` がlintしており、コミット前に `git grep "172\.16\."` が空であることを確認する。例示IPが必要なら TEST-NET(`192.0.2.x`)を使う
2. **プロファイルで確認されていないSCPIニモニックを実機に送らない。** 実機MHO98は未定義ヘッダのクエリ1発でSCPIサーバー全体が沈黙する(再接続でも回復せず、空行1本で回復 — `LanTransport.open()` が対策済み)。未対応機能は送信前に `UNSUPPORTED_FEATURE` を返す。新しいコマンドは機種プロファイル(`profiles/data/*.yaml`)への宣言とセットで追加する。`*OPT?` はRigolオシロ全シリーズで未定義ヘッダ(ガイド確認済み)。オプション照会は `:SYSTem:OPTion:STATus?` +ガイド記載トークンのみを使う(リスト外トークンでも沈黙する)
3. **設定系は set → エラーキュー確認 → read-back を必須**とし、requested / applied の両値を返す(機器は1-2-5にスナップしない。yorigin等のプリアンブル値は動的なので必ずライブ値を使う)
4. **実機で実行禁止**: 50Ω設定(FIFT)、autoset、factory reset(confirmフローはFakeScopeテストのみで担保)。実機writeテストは必ず「現在値取得→set→readback→finallyで復元」パターン
5. **新Toolの追加手順**: `safety/classes.py` の `TOOL_CLASSES` へ分類を追記(未登録は起動失敗する)。RESTRICTED_WRITE / DANGEROUS_WRITE に分類したら `confirm_token` 引数が必須(起動時チェックが強制)。引数条件付きの昇格(configure_channelの50Ω等)は `service/control.py` の責務

## 開発規約

- **TDD必須**: 失敗するテストを先に書き、redを確認してから実装。ユニットテストはFakeScope/mockで実機なしに完結させる(FakeScopeはTransport層に差し込み、SCPI文字列生成自体を検証対象にする)
- **言語**: 実行時に外部へ出る文字列(Tool description、エラーメッセージ、confirm文言、ログ、パッケージメタデータ)は**英語**。docs/・README・コード内コメント・内部docstringは**日本語**
- **Python**: `requires-python = ">=3.12"`、上限ピン禁止。バージョン固有機能に依存しない。TOMLはstdlib `tomllib`。依存追加は最小限(現在: mcp, pyyaml, pillow, pyvisa, pyvisa-py / dev: pytest)
- **キャッシュ生成禁止**: `__pycache__` / `.pytest_cache` を作らない(mise.tomlの `PYTHONDONTWRITEBYTECODE=1`、pytestの `-p no:cacheprovider` が設定済み。壊さないこと)
- **ブランチ**: mainへ直接コミットしない。作業はfeatureブランチで行いPRを作る
- **コミット**: メッセージは日本語。`Claude-Session` 等のセッションリンクを付けない(`.claude/settings.json` で設定済み)
- 保存パスの基準は「実行ディレクトリ」(PWD環境変数)。相対パスは `screenshot_dir` 基準で解決し、許可ルート(実行ディレクトリ+一時ディレクトリ+`RIGOL_MCP_ALLOWED_DIRS`)外への書き込みは拒否する

## 検証(タスク完了の条件)

1. `uv run pytest` 全件グリーン(現在932件+device 20件skip)
2. `git grep "172\.16\."` が空
3. 機器通信に触れた変更は実機スモーク(`-m device` のread-onlyスイート)を実行し、結果を `docs/verification/`(IP・シリアル非記載)に記録する
