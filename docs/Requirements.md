# rigol-oscilloscope-mcp 要件定義書

**文書バージョン:** 1.0
**対象機器:** Rigol製 SCPI対応デジタルオシロスコープ(第一検証機: MHO98)
**実装環境:** Python(mise + uv 管理)
**更新日:** 2026-08-25

関連文書:

- [tools.md](tools.md) — MCP Toolカタログ(引数・返却・操作クラスの詳細)
- [device-profiles.md](device-profiles.md) — 機種プロファイル仕様と検証済みプロファイル
- [phase0-results.md](phase0-results.md) — Phase 0 実機検証結果(実測エビデンス)
- [roadmap.md](roadmap.md) — 今後の対応予定(MVP対象外の機能・検討事項)

---

## 1. 概要・目的

### 1.1 背景

オシロスコープでの測定には Vertical Scale、Timebase、Trigger、Probe Ratio といった設定の知識が必要で、不慣れな利用者には敷居が高い。本システムは、Rigol製オシロスコープをLLMからModel Context Protocol(MCP)経由で操作可能にし、利用者が測定目的を自然言語で伝えるだけで、適切な設定・波形取得・測定・解析を行える環境を提供する。

想定する利用者の指示の例:

> 「Rigol MHO98を接続している。IPは192.168.1.120。」
> 「x10プローブで、1kHz 3Vの波形を見えるようにしてほしい」
> 「現在の波形をスクショして ~/captures に保存して」

制御経路はGUI自動操作ではなく、Rigol機が標準サポートするSCPI(LAN / USB)とする。

### 1.2 目的

1. オシロスコープの操作知識が十分でなくても測定を実施できること
2. 会話で指定された接続先・測定目的に応じて、LLMが適切な設定・取得・解析を行えること
3. 波形・測定値・スクリーンショットをLLMが取得し、スクリーンショットは指定場所へ画像ファイルとして保存できること
4. 測定結果に応じてLLMが設定を再調整できること
5. 危険な設定変更をMCPサーバー側で防止できること
6. 実行された操作を利用者が追跡できること
7. MHO98以外のRigol機種を、機種プロファイルの追加で段階的にサポートできること

### 1.3 名称

システム名は `rigol-oscilloscope-mcp` に統一する(旧仮称 `rigol-mho98-mcp` は廃止)。

## 2. コンセプトと責任分界

### 2.1 構成

```text
User ──自然言語──▶ LLM (Claude / Codex / ChatGPT 等)
                     │ MCP (stdio)
                     ▼
            rigol-oscilloscope-mcp
                     │ SCPI (LAN / USB)
                     ▼
            Rigolオシロスコープ ── Probe ──▶ DUT
```

MCPサーバーは単なるSCPIプロキシとせず、次の責務分離を行う:

```text
LLM → 意味的Tool → 安全性検証 → プロファイル適用 → SCPI生成 → 機器
```

### 2.2 意味的Tool原則(SCPIの直接公開を避ける)

LLMに `:CHAN1:SCAL 1` のようなSCPI文字列を直接生成・送信させる方式を標準動作としない。公開するのは `configure_channel` / `measure` のような意味的Toolのみとする。

理由: 不正コマンド送信の防止、機器状態破壊の防止、パラメータ範囲チェック、安全ポリシー適用、機種・ファームウェア差異の吸収、操作ログの意味的記録。

任意SCPI実行(`raw_scpi`)はデフォルト無効の開発用Toolとしてのみ存在する([tools.md](tools.md) 7章)。

### 2.3 責任分界(成功基準)

本システムの成功基準は「オシロスコープの各ノブやメニュー構造を詳しく知らない利用者でも、何を測定したいのかをAIへ伝えることで、安全性を維持しながら適切な測定を開始できること」とする。

同時に「AIが測定機器の物理的な安全性まで理解している」という前提には立たない:

| 主体 | 責務 |
|---|---|
| AI (LLM) | 測定設定の判断と結果解析の支援 |
| MCPサーバー | 機器制御、パラメータ検証、安全ポリシーの担保、操作記録 |
| 人間 | DUT・プローブ・グラウンド等の物理接続と電気的安全の担保 |

## 3. 対象範囲

### 3.1 対象機種

- **第一検証機:** RIGOL MHO98(Phase 0 実機検証済み → [phase0-results.md](phase0-results.md))
- **プロファイル対応機種:** 同型のSCPI対応Rigolオシロスコープ(MHO/DHO系など)。機種ごとの個体差(SCPI方言、機能有無、パラメータ範囲)は機種プロファイル([device-profiles.md](device-profiles.md))で吸収する
- **未知のRigol機種:** `*IDN?` に基づく汎用プロファイルでベストエフォート動作(degradedであることを明示)
- Rigol以外のベンダーは対象外(接続時に警告を返すが拒否はしない)

### 3.2 MVP機能範囲

- 接続管理: 会話指示ベースの接続(LAN / USB)、機器識別、プロファイル解決、切断・再接続
- Analog Channel: CH ON/OFF、Vertical Scale/Offset、Coupling、Probe Ratio、Bandwidth Limit、Input Impedance
- Horizontal: Timebase Scale/Position、Sample Rate・Memory Depth取得
- Trigger: Edge Trigger(Source / Level / Slope / Sweep Mode)、Trigger Status
- Acquisition: Run / Stop / Single / Auto Setup(要承認)
- Measurement: frequency, period, vpp, vmax, vmin, vavg, rms, duty, rise_time, fall_time
- データ取得: 波形サンプル、スクリーンショット(ファイル保存 + 画像返却)、状態一括取得

### 3.3 将来対応(Phase 4以降)

シリアルプロトコルデコード(UART/I²C/SPI/CAN/LIN)、Logic Analyzer(D0–D15)、AFG(出力ONはDANGEROUS_WRITE)、ホスト側高度解析(FFT等)。予定の詳細は [roadmap.md](roadmap.md) に記録し、着手時に要件へ昇格させる。

### 3.4 非対象

- Firmware Update、Calibration、Factory Service操作、ネットワーク/Wi-Fi設定変更、ライセンス管理、機器内ファイルの任意操作
- 任意SCPIの無制限実行、電源ON/OFFの外部制御
- プローブ・DUTの物理接続、電気的安全性の自動保証(人間の責務)
- 複数台の同時接続(単一アクティブ接続のみ。将来拡張の余地は残す)
- 機器の自動探索(mDNS / ネットワークスキャン)
- ネットワークMCP(HTTP/SSE公開)。stdioローカル利用のみ

## 4. アーキテクチャ要件

### 4.1 レイヤ構成

```text
MCP Layer(Toolの公開・入出力変換)
   ↓
Service Layer(測定・波形・状態管理のユースケース)
   ↓
Safety Layer(操作クラス判定・confirmトークン・パラメータ検証)
   ↓
Profile-aware Driver(機種プロファイルに基づくSCPI生成・応答解釈)
   ↓
Transport(LAN raw socket / USB USBTMC)
```

### 4.2 機種プロファイル

機種差の吸収は宣言的なプロファイルデータ(パッケージ同梱YAML)で行い、コード変更なしで新機種を追加できる構造とする。プロファイルは capabilities(機能有無)/ dialect・quirks(SCPI方言と実機挙動の癖)/ limits(パラメータ範囲)からなり、モデル完全一致 → ファミリ → 汎用Rigol の3層で解決する。詳細は [device-profiles.md](device-profiles.md)。

### 4.3 トランスポート

- **LAN:** raw socket SCPI。ポートはプロファイル既定(Rigolは5555)。実機検証済み
- **USB:** USBTMC(PyVISA + pyvisa-py)。VISAリソース文字列での指定に対応

### 4.4 接続ライフサイクル

- 接続先は**会話でのユーザー指示が基本**。`connect(address, transport?, port?)` で接続し、設定(環境変数/コンフィグ)のデフォルト接続先は任意のフォールバック。優先順位: **Tool引数(=会話指示) > 設定デフォルト**。どちらも無ければ、接続先をユーザーに確認するようLLMを誘導するエラーを返す
- 接続シーケンス: トランスポートopen → エラーキューdrain → `*IDN?` → プロファイル解決 → 識別情報返却
- 単一アクティブ接続とし、再 `connect` は既存接続を置換する
- 接続状態は `scope_identify` で確認できる(未接続時もエラーでなく `connected: false` を返す)

### 4.5 実装環境

- Python(バージョンは mise で管理、依存・仮想環境は uv で管理)
- 主要依存: MCP SDK(Python)、PyVISA + pyvisa-py(USB)、Pillow(画像変換)。依存は最小限に保つ
- MCPサーバーはstdioで動作し、対話的TTYを前提としない

## 5. MCP Tool要件

詳細仕様(引数・返却・エラー)は [tools.md](tools.md)。ここでは一覧のみ示す。

| 分類 | Tool | クラス | Phase |
|---|---|---|---|
| 接続 | `connect` / `disconnect` | SAFE_WRITE | 1 |
| 識別 | `scope_identify` / `get_capabilities` | READ_ONLY | 1 |
| 状態 | `get_state` / `get_channel` / `get_timebase` / `get_trigger` / `get_acquisition_state` | READ_ONLY | 1 |
| 測定 | `measure` | READ_ONLY | 1 |
| データ | `capture_waveform` / `capture_screenshot` | READ_ONLY | 1 |
| 設定 | `configure_channel` / `configure_timebase` / `configure_trigger` | SAFE_WRITE(50ΩのみRESTRICTED) | 2 |
| 取込 | `run` / `stop` / `single` | SAFE_WRITE | 2 |
| 取込 | `autoset` | RESTRICTED_WRITE | 2 |
| 支援 | `recommend_setup` | READ_ONLY | 3 |
| 開発 | `raw_scpi`(デフォルト無効) | DANGEROUS_WRITE | – |

### 5.1 利用例

**例1: 接続(会話指示ベース)**

> 「Rigol MHO98を接続している。IPは192.168.1.120。」

```text
connect(address="192.168.1.120")
→ { connected: true, model: "MHO98", profile: { name: "mho98", confidence: "verified" } }
```

**例2: 波形を見えるようにする**

> 「x10プローブで、1kHz 3Vの波形を見えるようにしてほしい」

```text
get_state(sections=["channels","timebase","trigger"])
→ configure_channel(channel="CH1", probe_ratio=10, coupling="DC", scale_v_per_div=1.0)
→ configure_timebase(scale_s_per_div=0.0002)      # 1kHz × 数周期
→ configure_trigger(type="edge", source="CH1", level_v=1.5, slope="rising", sweep_mode="auto")
→ run()
→ measure(channel="CH1", measurements=["frequency","vpp"])   # 意図通りか検証
→ capture_screenshot()                                        # Visionで波形確認
```

**例3: スクリーンショット**

> 「現在の波形をスクショして ~/captures に保存して」

```text
capture_screenshot(path="~/captures")
→ { saved_path: "/Users/.../captures/scope_20260825_143000.png", format: "png", ... } + 画像
```

## 6. 安全要件

本章を最重要要件とする。MHO98をはじめ多くのRigolオシロは**非絶縁**であり(各入力GNDは筐体・USB等のGNDと共通、測定カテゴリ Category I)、AIによる自動制御で電気的安全性を保証してはならない。

### 6.1 操作クラス

すべての操作を4クラスに分類する。

| クラス | 実行条件 | 例 |
|---|---|---|
| READ_ONLY | 自動実行可 | 各種取得、measure、screenshot、waveform |
| SAFE_WRITE | 原則自動実行可(パラメータ範囲検証必須) | scale / offset / timebase / trigger level / CH ON-OFF / run / stop / single / connect |
| RESTRICTED_WRITE | ユーザー承認(confirmトークン)必須 | 50Ω入力インピーダンス、Auto Setup、Factory Default |
| DANGEROUS_WRITE | ユーザーの明示確認なしで実行禁止 | AFG出力ON(将来)、raw_scpi、安全ポリシー無効化 |

### 6.2 確認フロー(confirmトークン方式)

RESTRICTED_WRITE / DANGEROUS_WRITE の承認は、ホストUIに依存しない2段階呼び出しで表現する:

1. 1回目の呼び出し: 実行せず `USER_CONFIRMATION_REQUIRED` を返す。返却には操作内容の説明、リスク説明、`confirm_token`(短寿命・単回有効)を含め、**「トークンを使う前に、必ず人間の利用者へ操作可否を確認すること」**をLLMへ指示する文言を含める
2. 2回目の呼び出し: 同一引数 + `confirm_token` で実行

トークンは操作内容にバインドし、引数が変われば無効とする。トークン発行・消費は監査ログに記録する。

### 6.3 物理安全確認

プローブの接続先、Ground Clipの接続先、DUTの実電圧、プローブ耐圧、絶縁状態などはMCPから確認できない。危険が想定される測定では返却に `requires_physical_confirmation: true` を含め、人間による物理確認を促す。

### 6.4 商用電源測定

利用者が商用電源(100V AC、コンセント、一次側、AC mains 等)の測定を指示した場合、通常のパッシブプローブによる測定手順を自動実行してはならない。適切な差動・絶縁プローブの使用を人間が確認することを前提とし、その旨を返却する。

### 6.5 排他制御

単一アクティブ接続・プロセス内ロックとする。同時に複数のTool呼び出しが到達した場合も機器へのSCPI送受信は直列化する。複数プロセス/セッション間の分散ロックは設けない(非対象)。

## 7. 動作原則

Phase 0 実機検証([phase0-results.md](phase0-results.md))から導かれた、全機種共通の規範。

### 7.1 エラーキュー管理

- **接続時drain:** エラーキューは前セッションの残留で汚染されうる(実測)。接続確立時に `:SYSTem:ERRor?` を空になるまで読み捨てる
- **set後検証:** コマンド送信成功だけで処理成功とみなさない。設定系は send → error queue確認 → read-back を必須とし、連続実行時もどのコマンドで失敗したか追跡できること

### 7.2 未確認ニモニックの送信禁止

不正なニモニックは機器が無応答となり、タイムアウト(既定5秒)とエラーキュー汚染のコストを伴う(実測)。プロファイルで確認されていないニモニック・測定項目は実機へ送信せず、Tool呼び出し時点で `UNSUPPORTED_FEATURE` を返す。

### 7.3 requested / applied 両値返却

機器が設定値をスナップするかは機種依存(MHO98は1-2-5にスナップせず指定値をそのまま適用)。設定系Toolは要求値と、read-backで得た実際の適用値を両方返し、LLMは適用値を後続判断に使う。

### 7.4 パラメータ検証とエラーコード

LLM指定値をそのままSCPIへ渡さず、プロファイルのlimits → 保守的デフォルト → 実機read-back の順で検証する。Toolエラーは機械可読形式とし、以下のコードを区別する:

```text
DEVICE_NOT_FOUND / DEVICE_DISCONNECTED / DEVICE_BUSY / TIMEOUT
INVALID_PARAMETER / UNSUPPORTED_FEATURE
SAFETY_POLICY_DENIED / USER_CONFIRMATION_REQUIRED
ACQUISITION_FAILED / NO_SIGNAL / WAVEFORM_TRANSFER_FAILED / SCPI_ERROR
```

### 7.5 単位の正規化

API境界はSI基本単位(V, s, Hz, Ω, Sa/s)。キー名に単位を含める(`frequency_hz`, `scale_v_per_div`)。接頭辞付き表現(500 mV等)の変換はLLMの責務。内部は float + SI に正規化する。

### 7.6 監査ログ

書き込み操作は Before / Action / After を記録する:

```json
{
  "timestamp": "...",
  "tool": "configure_channel",
  "requested": { "channel": "CH1", "scale_v_per_div": 1.0 },
  "before": { "scale_v_per_div": 0.5 },
  "after": { "scale_v_per_div": 1.0 },
  "result": "success"
}
```

confirmトークンの発行・消費、プロファイル解決結果も記録対象とする。

## 8. 非機能要件

### 8.1 レスポンスとタイムアウト

実測(単一クエリ 30–40 ms、負荷時 0.9–3.0 s、`get_state` ≒ 38クエリで約1.3 s)に基づき、旧v0.1の「設定Query 1秒以内」目標は撤回する。

- 単一SCPIクエリのデフォルトタイムアウト: **5秒**(実測で妥当性確認済み。設定で変更可)
- 複合操作(`get_state` 全取得など)は数秒かかりうることをTool descriptionに明示し、`sections` 絞り込みを提供する
- 巨大メモリ全体の波形を不用意にダウンロードしない(`max_points` 既定値と上限を設定で持つ)

### 8.2 信頼性

SCPI通信断時は (1) エラー返却 → (2) 次回Tool呼び出し時に自動再接続試行 → (3) `*IDN?` で機器再確認、とする。自動再接続後に未完了の設定変更を勝手に再実行しない。

### 8.3 可観測性

ログレベルは ERROR / WARN / INFO / DEBUG の4段階(旧TRACEはDEBUGに統合)。DEBUGでSCPI送受信を記録できるが、デフォルトでは抑制する。監査ログ(7.6)は通常ログと分離して保存する。

### 8.4 セキュリティ

MCPサーバーはstdioでローカル実行し、オシロスコープは信頼できるLAN内に置く(インターネット直接公開しない)。ネットワークMCP化(認証・TLS等)は非対象。スクリーンショット保存は許可ルート検証(9章)によりファイルシステムへの書き込み範囲を制限する。

## 9. 設定

すべての設定は**環境変数で指定可能**とする(config.tomlしか持たないホストからも渡せるようにするため)。任意でTOML設定ファイルも読めるものとし、優先順位は:

```text
Tool引数(会話でのユーザー指示) > 環境変数 > 設定ファイル > 組み込みデフォルト
```

| 環境変数 | 内容 | デフォルト |
|---|---|---|
| `RIGOL_MCP_ADDRESS` | デフォルト接続先(IP / VISAリソース) | なし(会話指示を要求) |
| `RIGOL_MCP_TRANSPORT` | `lan` / `usb` | addressから推定 |
| `RIGOL_MCP_PORT` | LAN SCPIポート | プロファイル既定(5555) |
| `RIGOL_MCP_TIMEOUT_S` | 単一クエリタイムアウト | 5 |
| `RIGOL_MCP_SCREENSHOT_DIR` | スクリーンショットのデフォルト保存先 | カレントディレクトリ |
| `RIGOL_MCP_ALLOWED_DIRS` | 書き込み許可ルート(パス区切りで複数) | デフォルト保存先 + カレント |
| `RIGOL_MCP_WAVEFORM_MAX_POINTS` | 波形取得の既定上限 | 100000 |
| `RIGOL_MCP_RAW_SCPI` | `raw_scpi` Toolの有効化 | false |
| `RIGOL_MCP_LOG_LEVEL` | ログレベル | info |
| `RIGOL_MCP_AUDIT_LOG` | 監査ログ出力先 | 有効(既定パス) |

デバイスをコンフィグに固定する運用(旧v0.1の `devices.mho98.host` 直書き)は廃止し、上記デフォルト+会話指示の組み合わせに置き換える。

## 10. 配布・ホスト統合

本章はパッケージング・起動・ホスト設定のみを扱う。本体動作へ課す制約は次の3点に限る: (1) stdioで対話的TTYなしに動作すること、(2) 全設定が環境変数で指定可能なこと、(3) 危険操作の確認がホストUI非依存(confirmトークン方式)であること。

### 10.1 配布

- GitHubリポジトリからの `uvx` 起動を標準とする(PyPI公開は当面しない):

```bash
uvx --from git+https://github.com/<owner>/rigol-oscilloscope-mcp rigol-oscilloscope-mcp
```

- タグ付きリリースを行い、`@<tag>` でのバージョン固定起動をサポートする
- 開発環境は mise(Pythonバージョン)+ uv(依存・仮想環境)で管理する

### 10.2 ホスト設定例

**Claude Code(`.mcp.json` または `claude mcp add`):**

```json
{
  "mcpServers": {
    "rigol-oscilloscope": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/<owner>/rigol-oscilloscope-mcp", "rigol-oscilloscope-mcp"],
      "env": { "RIGOL_MCP_SCREENSHOT_DIR": "~/scope-captures" }
    }
  }
}
```

**Codex(`~/.codex/config.toml`):**

```toml
[mcp_servers.rigol-oscilloscope]
command = "uvx"
args = ["--from", "git+https://github.com/<owner>/rigol-oscilloscope-mcp", "rigol-oscilloscope-mcp"]

[mcp_servers.rigol-oscilloscope.env]
RIGOL_MCP_SCREENSHOT_DIR = "~/scope-captures"
```

### 10.3 Claudeプラグイン化(将来)

MCPサーバー本体に加え、測定ワークフロー(段階的な未知信号探索、UART確認手順など)と安全プロンプト(物理確認の促し、商用電源測定の拒否)をスキルとして同梱するClaudeプラグインを提供する。旧v0.1の操作例(UART測定、Unknown Signal探索)や測定反復の上限ガイダンス(旧 `max_iterations`)は、サーバー要件ではなくこのスキルの素材とする。

## 11. 開発フェーズと受入基準

### 11.1 フェーズ

- **Phase 0 — SCPI検証: 完了。** 結果は [phase0-results.md](phase0-results.md)
- **Phase 1 — Read Only MCP:** `connect` / `disconnect` / `scope_identify` / `get_capabilities` / `get_state` / `get_*` / `measure` / `capture_waveform` / `capture_screenshot`。機器を変更できない状態でMCP連携とプロファイル機構を検証
- **Phase 2 — Basic Control:** `configure_*` / `run` / `stop` / `single` / `autoset`。Safety Layer(操作クラス・confirmトークン)導入
- **Phase 3 — Measurement Assistant:** スキル(または `recommend_setup`)による測定目的→設定の実用化
- **Phase 4 — Advanced:** シリアルデコード、Logic Analyzer、AFG、高度解析

### 11.2 受入基準(MVP = Phase 1 + 2)

**接続・識別**

- [ ] 会話で指定したIPアドレスへ `connect` で接続できる(USBはVISAリソースで接続できる)
- [ ] 接続先未指定かつデフォルト設定なしのとき、ユーザーへの確認を促すエラーが返る
- [ ] `scope_identify` がモデル・プロファイル名・信頼度を返す
- [ ] 未知のRigol機種で generic プロファイルにフォールバックし、その旨が明示される
- [ ] 切断時に適切なエラーとなり、次回呼び出しで再接続を試行する

**状態・操作**

- [ ] CH1〜CH4 / Timebase / Trigger の状態を取得できる(`sections` 絞り込み含む)
- [ ] CH ON/OFF・Vertical Scale・Probe Ratio・Timebase・Edge Trigger を変更でき、requested / applied が返る
- [ ] Run / Stop / Single を実行できる

**Measurement・データ**

- [ ] frequency / vpp / rms / rise_time / fall_time を取得できる(SI単位付きキー)
- [ ] プロファイル未対応の測定項目は実機へ送信されず `UNSUPPORTED_FEATURE` が返る
- [ ] 波形サンプルを取得でき、電圧値(V)へ正しく変換されている
- [ ] スクリーンショットを指定パスへ png / jpg で保存でき、image content も返る
- [ ] 許可ルート外への保存指定が `INVALID_PARAMETER` で拒否される

**Safety**

- [ ] 50Ωへの変更・Auto Setup が confirmトークンなしで実行されない
- [ ] `raw_scpi` がデフォルト無効
- [ ] 書き込み操作が監査ログに Before / After 付きで記録される

**AI連携**

- [ ] 1.1の3つの利用例(接続指示 / x10プローブで1kHz 3V波形表示 / スクショ保存)がLLMからMCP経由で完遂できる

## 12. 未決事項

以下は実装・追加検証の中で決定する(Phase 0で解決済みの項目は削除済み):

1. USB(USBTMC)接続の実機検証と、VISAリソース文字列の推奨形式
2. RAWモード波形ダウンロードのチャンク処理・上限(実機未検証)
3. 50Ω設定ニモニック(`FIFT` 想定)を含むRESTRICTED_WRITE系コマンドの実機確認
4. パラメータlimitsの境界値収集(機種プロファイルへの反映方法を含む)
5. `RUN` / `STOP` / `SINGle` / `AUToset` 書き込みの実機確認
6. MHO98以外の最初の対応機種と、ファミリプロファイルの括り出し時期
7. 波形一時ファイルの受け渡し方式(保存場所・寿命・クリーンアップ)
8. Claudeプラグインに同梱するスキルの構成(測定ワークフロー・安全プロンプトの分割)
