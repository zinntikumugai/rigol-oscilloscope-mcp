# rigol-oscilloscope-mcp 要件定義書

**文書バージョン:** 1.0
**対象機器:** Rigol製 SCPI対応デジタルオシロスコープ(第一検証機: MHO98)
**実装環境:** Python(mise + uv 管理)
**更新日:** 2026-08-25

関連文書:

- [tools.md](tools.md) — MCP Toolカタログ(引数・返却・操作クラスの詳細)
- [device-profiles.md](device-profiles.md) — 機種プロファイル仕様と検証済みプロファイル
- [verification/mho98-phase0.md](verification/mho98-phase0.md) — Phase 0 実機検証結果(実測エビデンス)
- [verification/mho98-mvp.md](verification/mho98-mvp.md) — MVP(Phase 1 + 2)実機検証結果
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

任意SCPI実行(`raw_scpi`)はデフォルト無効の開発用Toolとしてのみ存在する([tools.md](tools.md) 9章)。

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

- **第一検証機:** RIGOL MHO98(Phase 0 実機検証済み → [verification/mho98-phase0.md](verification/mho98-phase0.md))
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
- シリアルプロトコルデコード(Phase 4で昇格): 標準搭載6種(UART/RS232、I²C、SPI、CAN、LIN、パラレル)のバス設定・表示・イベントテーブル(`configure_decode`)と、デコード結果(イベントテーブル)の取得(`get_decode_result`)。ライセンスオプション必須のプロトコル(I2S、FlexRay、MIL-STD-1553、CAN-FD)は非対象
- 信号発生(AFG。Phase 4で昇格): 内蔵ジェネレータ(`:SOURce<n>`)の設定(`configure_afg`: 波形13種・周波数・振幅Vpp・オフセット・位相・デューティ・対称性・出力インピーダンス)、状態取得(`get_afg_state`)、**出力制御**(`enable_afg` / `disable_afg`)。設定と出力制御は別Toolで、**実際に信号を外へ出す `enable_afg` のみ DANGEROUS_WRITE**(確認フロー必須)。出力OFFは緊急停止をブロックしないため SAFE_WRITE

### 3.3 将来対応(Phase 4以降)

Logic Analyzer(D0–D15)、AFGの変調・ARB波形ロード、ホスト側高度解析(FFT等)。予定の詳細は [roadmap.md](roadmap.md) に記録し、着手時に要件へ昇格させる。

### 3.4 非対象

- Firmware Update、Calibration、Factory Service操作、ネットワーク/Wi-Fi設定変更、ライセンス管理、機器内ファイルの任意操作
  - ライセンスの**適用・解除**が非対象であり、読み取り専用の**導入済みオプション検出**(`:SYSTem:OPTion:STATus?`、`get_capabilities` の `options`)は対象に含む
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
| 信号発生 | `configure_afg` / `get_afg_state` | SAFE_WRITE / READ_ONLY | 4 |
| 信号発生 | `enable_afg` / `disable_afg` | DANGEROUS_WRITE / SAFE_WRITE | 4 |
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
| DANGEROUS_WRITE | ユーザーの明示確認なしで実行禁止 | AFG出力ON(`enable_afg`)、raw_scpi、安全ポリシー無効化 |

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

Phase 0 実機検証([verification/mho98-phase0.md](verification/mho98-phase0.md))から導かれた、全機種共通の規範。

### 7.1 エラーキュー管理

- **接続時drain:** エラーキューは前セッションの残留で汚染されうる(実測)。接続確立時に `:SYSTem:ERRor?` を空になるまで読み捨てる
- **set後検証:** コマンド送信成功だけで処理成功とみなさない。設定系は send → error queue確認 → read-back を必須とし、連続実行時もどのコマンドで失敗したか追跡できること

### 7.2 未確認ニモニックの送信禁止

不正なニモニックは機器が無応答となり、タイムアウト(既定5秒)とエラーキュー汚染のコストを伴う(実測)。プロファイルで確認されていないニモニック・測定項目は実機へ送信せず、Tool呼び出し時点で `UNSUPPORTED_FEATURE` を返す。

さらにMVPの実機検証で、**未定義ヘッダのクエリを1回送るだけで機器のSCPIサーバー全体が沈黙し、TCP再接続でも回復しない**ことが判明した(空行 `\n` を1本送ると即座に回復する)。本規範の重要度はPhase 0時点の想定より一段高い。回復手段として、LAN接続の確立直後に空行を1本送る([verification/mho98-mvp.md](verification/mho98-mvp.md) 3.1)。

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

実測(単一クエリ 30–40 ms、負荷時 0.9–3.0 s、`get_state` ≒ 39クエリで 1.3–1.5 s)に基づき、旧v0.1の「設定Query 1秒以内」目標は撤回する。

- 単一SCPIクエリのデフォルトタイムアウト: **5秒**(実測で妥当性確認済み。設定で変更可)
- 複合操作(`get_state` 全取得など)は数秒かかりうることをTool descriptionに明示し、`sections` 絞り込みを提供する
- 巨大メモリ全体の波形を不用意にダウンロードしない(`max_points` 既定値と上限を設定で持つ)

### 8.2 信頼性

SCPI通信断時は (1) エラー返却 → (2) 次回Tool呼び出し時に自動再接続試行 → (3) `*IDN?` で機器再確認、とする。自動再接続後に未完了の設定変更を勝手に再実行しない。

### 8.3 可観測性

ログレベルは ERROR / WARN / INFO / DEBUG の4段階(旧TRACEはDEBUGに統合)。DEBUGでSCPI送受信を記録できるが、デフォルトでは抑制する。通常ログの出力先は**stderr**(stdioのstdoutはMCPプロトコル専用)。監査ログ(7.6)は通常ログと分離して保存する。

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
| `RIGOL_MCP_SCREENSHOT_DIR` | スクリーンショットのデフォルト保存先 | 実行ディレクトリ(`PWD`。無効時はカレントディレクトリ) |
| `RIGOL_MCP_ALLOWED_DIRS` | 書き込み許可ルート(パス区切りで複数) | デフォルト保存先 + 一時ディレクトリ |
| `RIGOL_MCP_WAVEFORM_MAX_POINTS` | 波形取得の既定上限 | 100000 |
| `RIGOL_MCP_RAW_SCPI` | `raw_scpi` Toolの有効化 | false |
| `RIGOL_MCP_LOG_LEVEL` | ログレベル | info |
| `RIGOL_MCP_AUDIT_LOG` | 監査ログ出力先 | 有効(`$XDG_STATE_HOME`(既定 `~/.local/state`)`/rigol-oscilloscope-mcp/audit.jsonl`)。`off` / `false` / `0` / `no` で無効 |

保存先パスの扱い:

- `path` 引数の**相対パスはデフォルト保存先を基準**に解決する。プロセスのカレントディレクトリは基準にしない(`uv run --directory` 起動ではサーバー自身のプロジェクトを指してしまうため)
- 書き込み許可ルートは「`RIGOL_MCP_ALLOWED_DIRS` の明示指定 + デフォルト保存先 + 一時ディレクトリ(`tempfile.gettempdir()`、POSIXでは `/tmp` の実体)」。プロセスのカレントディレクトリは**含めない**
- 許可ルート外への保存は `INVALID_PARAMETER` で拒否し、エラーの `detail.hint` で `RIGOL_MCP_ALLOWED_DIRS` による追加方法を案内する

デバイスをコンフィグに固定する運用(旧v0.1の `devices.mho98.host` 直書き)は廃止し、上記デフォルト+会話指示の組み合わせに置き換える。

## 10. 配布・ホスト統合

本章はパッケージング・起動・ホスト設定のみを扱う。本体動作へ課す制約は次の3点に限る: (1) stdioで対話的TTYなしに動作すること、(2) 全設定が環境変数で指定可能なこと、(3) 危険操作の確認がホストUI非依存(confirmトークン方式)であること。

### 10.1 配布

- GitHubリポジトリからの `uvx` 起動を標準とする(PyPI公開は当面しない):

```bash
uvx --from git+https://github.com/zinntikumugai/rigol-oscilloscope-mcp rigol-oscilloscope-mcp
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
      "args": ["--from", "git+https://github.com/zinntikumugai/rigol-oscilloscope-mcp", "rigol-oscilloscope-mcp"],
      "env": { "RIGOL_MCP_SCREENSHOT_DIR": "~/scope-captures" }
    }
  }
}
```

**Codex(`~/.codex/config.toml`):**

```toml
[mcp_servers.rigol-oscilloscope]
command = "uvx"
args = ["--from", "git+https://github.com/zinntikumugai/rigol-oscilloscope-mcp", "rigol-oscilloscope-mcp"]

[mcp_servers.rigol-oscilloscope.env]
RIGOL_MCP_SCREENSHOT_DIR = "~/scope-captures"
```

### 10.3 プラグイン(Claude / Codex)

リポジトリルートをプラグインルートとし、ClaudeとCodexの両プラグインを同梱する。スキル(`skills/`)とMCPサーバー起動定義(10.1の `uvx` 標準形)は両者で共有し、マニフェストのみホストごとに持つ:

- `.claude-plugin/plugin.json` — Claude用マニフェスト(MCPサーバー定義をインラインで含む。`skills/` は自動発見)
- `.codex-plugin/plugin.json` — Codex用マニフェスト(`skills` は `./skills/` を、`mcpServers` は `./.codex-plugin/mcp.json` を参照)
- `.agents/plugins/marketplace.json` — Codexのマーケットプレイス定義(Codexはプラグイン導入がマーケットプレイス経由のため単一プラグインでも必要)
- `skills/measurement-workflows/SKILL.md` — 測定ワークフロースキル(1本に統合、Agent Skillsオープン標準形式で両ホスト共通)。信号種別→推奨設定の対応表、UART測定・未知信号探索のワークフロー、安全プロンプト(物理確認の促し、商用電源測定の拒否)、測定反復の上限ガイダンス(目安5回)を含む

スキルは実行時にLLMホストへ渡る文字列のため**英語**で記述する(実行時に外部へ出る文字列は英語、という言語方針に従う)。旧v0.1の操作例・`max_iterations` はサーバー要件ではなくこのスキルへ吸収した。整合は `tests/test_plugin.py` が検証する(各マニフェストの妥当性、両マニフェストとpyprojectのname/version/起動列の一致、スキルが実在Tool名を参照していること)。

**Codex側の未検証事項**(公式ドキュメント準拠で作成したが実CLIでの動作確認は未実施): (1) `mcpServers` にプラグインルート直下以外の相対パス(`./.codex-plugin/mcp.json`)を指定できること、(2) マーケットプレイスのsource `path: "./"` でプラグインルート=リポジトリルートを表現できること。Codex CLIでの実確認は [roadmap.md](roadmap.md) 3章へ。

## 11. 開発フェーズと受入基準

### 11.1 フェーズ

- **Phase 0 — SCPI検証: 完了。** 結果は [verification/mho98-phase0.md](verification/mho98-phase0.md)
- **Phase 1 — Read Only MCP: 完了。** `connect` / `disconnect` / `scope_identify` / `get_capabilities` / `get_state` / `get_*` / `measure` / `capture_waveform` / `capture_screenshot`。機器を変更できない状態でMCP連携とプロファイル機構を検証
- **Phase 2 — Basic Control: 完了。** `configure_*` / `run` / `stop` / `single` / `autoset`。Safety Layer(操作クラス・confirmトークン)導入。MVPの実機検証結果は [verification/mho98-mvp.md](verification/mho98-mvp.md)
- **Phase 3 — Measurement Assistant: 完了。** 同梱スキル(`skills/measurement-workflows/`)による測定目的→設定の実用化とClaudeプラグイン化(10.3)。サーバー側Tool `recommend_setup` は実装せず、スキルで精度不足が実証された場合のフォールバックとして据え置き([tools.md](tools.md) 8章)
- **Phase 4 — Advanced: 進行中。** シリアルデコード(標準6種の設定 `configure_decode` と結果取得 `get_decode_result`)、AFGの設定・状態取得・出力制御(`configure_afg` / `get_afg_state` / `enable_afg` / `disable_afg`)、導入済みオプション照会(`get_capabilities` の `options`)を実装。以降 AFGの変調・ARB波形ロード、Logic Analyzer、高度解析

### 11.2 受入基準(MVP = Phase 1 + 2)

消化状況(2026-08-25)。各項目の末尾に根拠を示す。「実機PASS」は
[verification/mho98-mvp.md](verification/mho98-mvp.md) に記録したMHO98実機での確認、
「FakeScope」は内蔵のフェイク機器を使った実機不要の検証を指す
(`tests/test_server_phase1.py` / `tests/test_server_phase2.py` はMCPクライアントセッション経由)。

**接続・識別**

- [x] 会話で指定したIPアドレスへ `connect` で接続できる(tests/device/test_readonly.py::test_identify、LAN実機PASS)
- [ ] USBはVISAリソースで接続できる — **実機未検証**。VISAリソース文字列からのUSB推定・USBTMCの送受信は tests/test_usb_transport.py / tests/test_connection.py::test_visa_resource_address_infers_usb でユニット検証済み(PyVISAのフェイク)。実機確認は [roadmap.md](roadmap.md) 4章へ
- [x] 接続先未指定かつデフォルト設定なしのとき、ユーザーへの確認を促すエラーが返る(tests/test_server_phase1.py::test_connect_without_address_asks_the_user, tests/test_connection.py, FakeScope)
- [x] `scope_identify` がモデル・プロファイル名・信頼度を返す(tests/device/test_readonly.py::test_identify で `mho98`/`verified` を実機PASS、tests/test_server_phase1.py でTool返却を検証)
- [x] 未知のRigol機種で generic プロファイルにフォールバックし、その旨が明示される(tests/test_profiles.py::test_resolve_unknown_rigol_model_falls_back_to_generic ほか、FakeScope。非Rigolベンダーの警告も同ファイル)
- [x] 切断時に適切なエラーとなり、次回呼び出しで再接続を試行する(tests/test_connection.py::test_require_scope_reconnects_a_dropped_link / ::test_require_scope_reports_disconnected_when_reconnect_fails、FakeScope)

**状態・操作**

- [x] CH1〜CH4 / Timebase / Trigger の状態を取得できる(`sections` 絞り込み含む)(tests/device/test_readonly.py::test_get_state_all_sections / ::test_get_state_trigger_section_only、実機PASS。全取得1.500 s / trigger のみ0.267 s)
- [x] CH ON/OFF・Vertical Scale・Probe Ratio・Timebase・Edge Trigger を変更でき、requested / applied が返る(tests/device/test_write.py 全9件、実機PASS・復元漏れゼロ)
- [x] Run / Stop / Single を実行できる(tests/device/test_write.py::test_run_stop_single、実機PASS)

**Measurement・データ**

- [x] frequency / vpp / rms / rise_time / fall_time を取得できる(SI単位付きキー)(tests/device/test_readonly.py::test_measure_all_items_on_ch1、10項目すべて実機PASS)
- [x] プロファイル未対応の測定項目は実機へ送信されず `UNSUPPORTED_FEATURE` が返る(tests/test_scope_driver.py::test_measure_unsupported_name_is_not_sent / ::test_measure_rejects_unknown_name_without_sending で「送信していないこと」を検証、tests/test_measurement_service.py::test_measure_propagates_unsupported_feature、FakeScope)
- [x] 波形サンプルを取得でき、電圧値(V)へ正しく変換されている(tests/device/test_readonly.py::test_capture_waveform_ch1、実機PASS。1000点・実効200 kSa/s)
- [x] スクリーンショットを指定パスへ png / jpg で保存でき、image content も返る(保存: tests/device/test_readonly.py::test_capture_screenshot_png / ::test_capture_screenshot_jpg、実機PASS。image content の返却: tests/test_server_phase1.py::test_capture_screenshot_returns_image_content、FakeScope)
- [x] 許可ルート外への保存指定が `INVALID_PARAMETER` で拒否される(tests/test_server_phase1.py::test_capture_screenshot_rejects_path_outside_allowed_roots, tests/test_paths.py、FakeScope)

**Safety**

- [x] 50Ωへの変更・Auto Setup が confirmトークンなしで実行されない(tests/test_server_phase2.py::test_50ohm_requires_confirmation_then_succeeds / ::test_autoset_requires_confirmation_then_returns_state / ::test_confirm_token_is_bound_to_the_arguments、FakeScope。**どちらも危険操作のため実機へは意図的に送っていない**)
- [x] `raw_scpi` がデフォルト無効(tests/test_config.py::test_defaults_with_empty_env で `raw_scpi is False`、tests/test_server_phase2.py::test_list_tools_exposes_every_phase で公開Tool 20個に含まれないことを検証)
- [x] 書き込み操作が監査ログに Before / After 付きで記録される(tests/device/test_write.py::test_audit_log_records_every_write で実機の書き込み40行を検証、tests/test_server_phase2.py::test_write_operations_are_audited、FakeScope)

**AI連携**

- [ ] 1.1の3つの利用例(接続指示 / x10プローブで1kHz 3V波形表示 / スクショ保存)がLLMからMCP経由で完遂できる — **LLMホストからの通し確認は未実施**。構成要素は分割して担保済み(MCPプロトコル経由のTool呼び出し: tests/test_server_phase1.py / tests/test_server_phase2.py、FakeScope / 機器操作そのもの: tests/device/ 18件、実機PASS)

### 11.3 受入基準(Phase 3)

消化状況(2026-08-25)。Phase 3はサーバーコードに触れないため実機検証は不要(機器通信ゼロ)。

- [x] プラグインマニフェスト(`.claude-plugin/plugin.json`)が有効なJSONで、10.1の標準形によるMCPサーバー起動定義を含む(tests/test_plugin.py::test_plugin_manifest_is_valid)
- [x] Codexプラグイン(`.codex-plugin/` + `.agents/plugins/marketplace.json`)がClaude側とname/version/MCP起動列で一致し、同じ `skills/` を共有する(tests/test_plugin.py::test_codex_manifest_is_valid / ::test_manifests_agree / ::test_codex_marketplace_references_the_plugin)— **Codex CLIでの実動作確認は未実施**(10.3の未検証事項、[roadmap.md](roadmap.md) 3章)
- [x] 同梱スキルに信号種別→推奨設定の対応表、UART・未知信号ワークフロー、安全プロンプト(物理確認・AC mains拒否)、反復上限ガイダンスを記載(tests/test_plugin.py::test_skill_frontmatter)
- [x] スキルが参照するTool名がすべて実在する(tests/test_plugin.py::test_skill_references_real_tools、Tool改名時の乖離ガード)

## 12. 未決事項

以下は実装・追加検証の中で決定する(解決済みの項目は削除済み。直近ではPhase 3でスキル構成を「1本に統合」で解決):

1. USB(USBTMC)接続の実機検証と、VISAリソース文字列の推奨形式
2. RAWモード波形ダウンロードのチャンク処理・上限(実機未検証)
3. 50Ω設定ニモニック(`FIFT` 想定)を含むRESTRICTED_WRITE系コマンドの実機確認
4. パラメータlimitsの境界値収集(機種プロファイルへの反映方法を含む)
5. `AUToset` 書き込みの実機確認(`RUN` / `STOP` / `SINGle` はMVPで確認済み。`:RUN` 直後の `:TRIGger:STATus?` は約0.2秒 `STOP` を返すためポーリングが必要 → [verification/mho98-mvp.md](verification/mho98-mvp.md))
6. MHO98以外の最初の対応機種と、ファミリプロファイルの括り出し時期
7. 波形一時ファイルの受け渡し方式(保存場所・寿命・クリーンアップ)
