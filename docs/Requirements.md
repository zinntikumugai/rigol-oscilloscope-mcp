# RIGOL MHO98 AI操作MCPサーバー 要件定義書

**文書バージョン:** 0.1
**対象機器:** RIGOL MHO98 Limited Edition
**システム仮称:** `rigol-oscilloscope-mcp`
**作成日:** 2026-08-22

---

## 1. 概要

### 1.1 背景

RIGOL MHO98は高機能なデジタルオシロスコープであるが、オシロスコープに不慣れな利用者にとって、以下の設定を適切に行うには一定の知識が必要となる。

* Vertical Scale
* Horizontal Timebase
* Trigger
* Coupling
* Input Impedance
* Probe Ratio
* Acquisition Mode
* Memory Depth
* Measurement
* Protocol Decode
* Logic Analyzer
* AFG

本システムでは、これらの操作をLLMからModel Context Protocol（MCP）経由で実行できるようにし、利用者が

> 「3.3 VのUART 115200 bpsをCH1で確認したい」

> 「この電源のリップルを確認したい」

> 「この波形の立ち上がり時間とオーバーシュートを調べて」

といった**測定目的を自然言語で指定するだけで、適切なオシロスコープ設定・波形取得・測定・解析を行える環境**を構築する。

MHO98はLANおよびUSB Device経由のSCPIによるリモート制御をサポートしているため、本システムはGUIの自動操作ではなくSCPIを主要な制御経路として使用する。

---

# 2. 目的

本システムの目的は以下とする。

1. オシロスコープの操作知識が十分でなくても測定を実施できること
2. LLMが測定目的に応じて適切なMHO98設定を選択できること
3. 波形・測定値・スクリーンショットをLLMが取得できること
4. 測定結果に応じてLLMが設定を再調整できること
5. LLMによる危険な設定変更をMCPサーバー側で防止できること
6. 実行された操作を利用者が追跡できること
7. 将来的にMHO98固有機能を段階的に追加できること

---

# 3. システムコンセプト

システム構成は以下を基本とする。

```text
User
 │
 │ 自然言語
 ▼
LLM
(ChatGPT / Claude / Codex 等)
 │
 │ MCP
 ▼
rigol-mho98-mcp
 │
 ├─ Measurement Planner Interface
 ├─ Safety Policy
 ├─ State Manager
 ├─ SCPI Abstraction
 ├─ Waveform Acquisition
 └─ Audit Log
 │
 │ SCPI
 ▼
LAN / USB
 │
 ▼
RIGOL MHO98
```

MCPサーバーは単なるSCPI Proxyとはせず、

```text
LLM
 ↓
意味的なTool
 ↓
安全性検証
 ↓
SCPI生成
 ↓
MHO98
```

の責務分離を行う。

---

# 4. 基本方針

## 4.1 SCPIの直接公開を避ける

以下のようなToolを基本とする。

```text
scope.set_channel(...)
scope.set_timebase(...)
scope.set_trigger(...)
scope.measure(...)
```

LLMが

```text
:CHAN1:SCAL 1
```

などのSCPI文字列を直接生成して送信する方式を標準動作としない。

理由は以下。

* 不正なコマンド送信防止
* 機器状態破壊の防止
* パラメータ範囲チェック
* 安全ポリシー適用
* MHO98ファームウェア差異の吸収
* 操作ログの意味的記録

---

## 4.2 「設定」ではなく「測定目的」を扱えること

低レベルToolに加えて、高レベルなMeasurement Assistant用Toolを提供する。

例:

```text
prepare_measurement(
    type="uart",
    expected_voltage=3.3,
    baud_rate=115200,
    channel="CH1"
)
```

または汎用的に、

```text
recommend_setup(
    signal_type="uart",
    expected_voltage=3.3,
    expected_frequency=115200,
    objective="observe_waveform"
)
```

を提供する。

LLMが推奨設定を決定した後、別Toolを使用して実際に設定を反映する。

---

# 5. 対象範囲

## 5.1 MVP対象

初期バージョンでは以下を対象とする。

### 接続

* LAN接続
* USB接続
* `*IDN?` 等による機器識別
* 接続状態確認
* タイムアウト処理
* 再接続

### Analog Channel

* CH1～CH4 ON/OFF
* Vertical Scale
* Vertical Offset
* Coupling
* Probe Ratio
* Bandwidth Limit
* Input Impedance
* Channel Label取得

### Horizontal

* Timebase
* Horizontal Position
* Sample Rate取得
* Memory Depth取得

### Trigger

MVPではEdge Triggerを必須とする。

* Source
* Level
* Rising
* Falling
* Either
* Trigger Mode
* Trigger Status

### Acquisition

* Run
* Stop
* Single
* Auto Scale / Auto Setup
* Acquisition State取得

### Measurement

少なくとも以下を対象とする。

* Frequency
* Period
* Vpp
* Vmax
* Vmin
* Vavg
* RMS
* Duty Cycle
* Rise Time
* Fall Time

### データ取得

* 波形サンプル取得
* スクリーンショット取得
* Acquisition Metadata取得

### 状態取得

* 現在の主要設定一覧
* Channel状態
* Trigger状態
* Horizontal状態
* Acquisition状態

---

# 6. 将来対応範囲

以下はMVP後に追加する。

## 6.1 Serial Protocol Decode

MHO98は標準でRS232/UART、I²C、SPI、LIN、CAN、CAN-FDなど複数のシリアルデコード機能を備える。

対象候補:

* UART / RS232
* I²C
* SPI
* CAN
* CAN-FD
* LIN

例:

```text
protocol.configure_uart(...)
protocol.decode_uart(...)
```

---

## 6.2 Logic Analyzer

以下を対象とする。

* Digital Channel状態取得
* Threshold設定
* D0～D15設定
* Logic Capture
* Protocol Decode連携

ロジックプローブの物理接続状態についてはAIから完全には確認できないため、人間による確認を前提とする。

---

## 6.3 Function / Arbitrary Waveform Generator

MHO98は2ch、100 MHz、1 GSa/sのAFGを搭載する。

将来的に以下を提供する。

```text
afg.configure(...)
afg.get_state(...)
afg.enable(...)
afg.disable(...)
```

ただし**出力ONはDangerous Operationとして扱う**。

---

## 6.4 高度解析

オシロ本体だけでなくMCPホスト上で以下を解析できる構成を検討する。

* FFT
* Jitter
* Overshoot
* Undershoot
* Ringing
* Noise
* Signal Integrity
* Statistical Measurement

---

# 7. 非対象

初期バージョンでは以下を対象外とする。

* Firmware Update
* Calibration
* Factory Service操作
* Network設定変更
* Wi-Fi設定変更
* ライセンス管理
* 機器内部ファイルの任意操作
* 任意SCPIコマンドの無制限実行
* 電源ON/OFFの外部制御
* プローブの物理接続
* DUTへの物理配線
* 電気的安全性の自動保証

---

# 8. MCP Tool要件

## 8.1 Device

### `scope.identify`

機器を識別する。

返却例:

```json
{
  "manufacturer": "RIGOL TECHNOLOGIES",
  "model": "MHO98",
  "serial": "...",
  "firmware": "...",
  "connected": true
}
```

---

### `scope.get_capabilities`

接続された機器が利用可能な機能を返す。

```json
{
  "analog_channels": 4,
  "digital_channels": 16,
  "afg_channels": 2,
  "protocol_decode": true,
  "waveform_download": true
}
```

固定値ではなく、可能な限り実機から取得した情報と機種Capabilities定義を組み合わせる。

---

### `scope.get_state`

主要設定を一括取得する。

LLMが操作前に現在状態を把握するための主要Toolとする。

---

# 9. Channel操作

### `scope.get_channel`

入力:

```text
channel
```

取得項目:

* enabled
* scale
* offset
* coupling
* impedance
* probe_ratio
* bandwidth_limit

---

### `scope.configure_channel`

入力:

```text
channel
enabled?
scale?
offset?
coupling?
probe_ratio?
bandwidth_limit?
impedance?
```

未指定項目は変更しない。

---

## 9.1 Input Impedance

以下を区別する。

```text
1MΩ
50Ω
```

**50Ωへの変更は高リスク設定とする。**

LLMからの通常操作では自動変更しない。

50Ωへ変更する場合は、

1. 現在値確認
2. Safety Policy判定
3. 明示的なユーザー承認
4. 設定変更

を要求する。

---

# 10. Horizontal操作

### `scope.configure_timebase`

入力:

```text
scale
position?
```

### `scope.get_timebase`

返却:

* scale
* position
* sample_rate
* memory_depth

---

# 11. Trigger操作

### `scope.configure_trigger`

MVP:

```text
type = edge
source
level
slope
mode
```

例:

```json
{
  "type": "edge",
  "source": "CH1",
  "level": 1.65,
  "slope": "rising",
  "mode": "normal"
}
```

---

### `scope.get_trigger`

現在のTrigger状態を取得する。

---

# 12. Acquisition操作

以下を提供する。

```text
scope.run()
scope.stop()
scope.single()
scope.autoset()
scope.get_acquisition_state()
```

---

## 12.1 Auto Setup

Auto Setupは便利である一方、利用者が設定した値を大きく変更する。

そのため、

* AIが勝手に最初からAuto Setupを使わない
* 現在設定を取得する
* 必要に応じてAuto Setupを使用する
* 使用したことをTool Resultに明記する

ものとする。

---

# 13. Measurement

### `scope.measure`

入力例:

```json
{
  "channel": "CH1",
  "measurements": [
    "frequency",
    "vpp",
    "rms",
    "rise_time"
  ]
}
```

返却例:

```json
{
  "channel": "CH1",
  "frequency_hz": 1000123,
  "vpp_v": 3.28,
  "rms_v": 1.72,
  "rise_time_s": 4.2e-9
}
```

---

## 13.1 測定品質情報

可能であれば単なる値だけでなく、

```text
valid
overflow
no_signal
unstable
unknown
```

等の状態を返す。

LLMが無効な測定値を正常値として解釈しない構造とする。

---

# 14. 波形取得

### `scope.capture_waveform`

入力:

```text
channel
range?
max_points?
format?
```

返却:

* Samples
* Sample Interval
* Time Origin
* Voltage Scale
* Voltage Offset
* Channel
* Acquisition Timestamp

巨大波形をMCPレスポンスへ直接格納することは避ける。

大量データの場合は一時ファイル等として保持し、LLMにはメタデータと参照情報を返す。

---

# 15. スクリーンショット

### `scope.capture_screenshot`

現在のMHO98画面を画像として取得する。

用途:

* 波形形状確認
* Trigger状態確認
* Protocol Decode確認
* オシロ画面上の異常確認
* LLM Visionによる解析

数値測定についてはスクリーンショットOCRではなく、可能な限りSCPI Measurement結果を優先する。

---

# 16. Measurement Assistant

本プロジェクトで特に重要な機能とする。

## 16.1 `measurement.recommend_setup`

入力:

```text
signal_type
expected_voltage?
expected_frequency?
expected_baud_rate?
channel?
objective
```

例:

```json
{
  "signal_type": "uart",
  "expected_voltage": 3.3,
  "expected_baud_rate": 115200,
  "channel": "CH1",
  "objective": "inspect_data"
}
```

返却:

```json
{
  "recommended": {
    "coupling": "DC",
    "probe_ratio": 10,
    "vertical_scale": 1.0,
    "timebase": 0.00002,
    "trigger_source": "CH1",
    "trigger_level": 1.65,
    "trigger_slope": "rising"
  },
  "reasoning_summary": [
    "3.3 V logic signal",
    "115200 bps UART"
  ],
  "warnings": []
}
```

本Tool自体では機器設定を変更しない。

---

## 16.2 推奨プリセット

将来的に少なくとも以下をサポートする。

```text
digital
uart
i2c
spi
pwm
clock
power_ripple
switching_power_supply
audio
unknown_signal
```

---

# 17. AIフィードバックループ

システムは以下のワークフローを許容する。

```text
測定目的
   ↓
現在状態取得
   ↓
設定提案
   ↓
設定変更
   ↓
Single Acquisition
   ↓
Measurement取得
   ↓
Waveform / Screenshot取得
   ↓
LLM解析
   ↓
必要なら設定微調整
   ↓
再測定
   ↓
最終結果
```

ただし無制限ループを禁止する。

標準最大再測定回数を設定可能とする。

例:

```text
max_iterations = 5
```

---

# 18. 安全要件

本項目を最重要要件とする。

MHO98は**非絶縁オシロスコープ**であり、各入出力GNDは筐体およびUSB/HDMI等のデジタルインターフェースGNDから絶縁されていない。RIGOLも浮遊測定を絶縁プローブなしで行わないことを明示している。また測定カテゴリはCategory Iである。

したがってAIによる自動制御で電気的安全性を保証してはならない。

---

## 18.1 操作クラス

すべての操作を以下に分類する。

### READ_ONLY

自動実行可能。

例:

* 設定取得
* Measurement取得
* Screenshot
* Waveform取得
* Trigger Status取得
* Device情報取得

### SAFE_WRITE

原則として自動実行可能。

例:

* Vertical Scale
* Vertical Offset
* Timebase
* Trigger Level
* Trigger Source
* Channel ON/OFF
* Bandwidth Limit

ただしPolicy Engineによる範囲確認を必須とする。

### RESTRICTED_WRITE

ユーザー承認または事前Policyを要求する。

例:

* 50Ω Input Impedance
* Probe Ratioの大幅変更
* Auto Setup
* Factory Default

### DANGEROUS_WRITE

ユーザーの明示確認なしで実行してはならない。

例:

* AFG Output Enable
* 任意SCPI
* Safety Policyを無効化する操作

---

# 19. 物理安全確認

AIが判断できない項目を明示する。

以下はMCP側で自動確認できない。

* Probeが実際にどこへ接続されているか
* Ground Clipの接続先
* DUTの実電圧
* Probeの最大入力電圧
* Probe種類
* Differential Probeの実装着
* DUTが商用電源へ接続されているか
* 絶縁状態

したがって危険が想定される測定では、

```text
requires_physical_confirmation = true
```

を返す。

---

# 20. 商用電源測定

通常のMCP操作対象外とする。

利用者が、

```text
100V AC
コンセント
商用電源
一次側
AC mains
```

等を指定した場合、MCP/LLMは通常のパッシブプローブによる測定手順を自動実行してはならない。

必要な測定については適切な差動・絶縁プローブ等が使用されていることを人間が確認する必要がある。

---

# 21. Raw SCPI

### `scope.raw_scpi`

MVPではデフォルト無効とする。

設定例:

```yaml
raw_scpi:
  enabled: false
```

有効化した場合でも、

* Queryのみ許可
* Allowlist
* Denylist
* Audit Log
* Confirmation

を適用できること。

開発・デバッグ目的以外では使用しない。

---

# 22. 状態管理

設定変更前後の状態を保持する。

例:

```text
Before
 ↓
Action
 ↓
After
```

Audit Log:

```json
{
  "timestamp": "...",
  "tool": "scope.configure_channel",
  "requested": {
    "channel": "CH1",
    "scale": 1.0
  },
  "before": {
    "scale": 0.5
  },
  "after": {
    "scale": 1.0
  },
  "result": "success"
}
```

---

# 23. Exclusive Control

同一MHO98に対する複数LLMセッションからの同時設定変更を防止する。

MHO98のWeb Controlについても同一IPへの同時リモートログインに制限があることが公式資料に記載されているため、MCP側でも単一の制御セッションとして扱う。

以下を実装する。

```text
Device Lock
Session Owner
Lock Timeout
Force Unlock
```

READ_ONLYアクセスについては将来的に並列化を検討する。

---

# 24. エラー処理

以下を区別する。

```text
DEVICE_NOT_FOUND
DEVICE_DISCONNECTED
DEVICE_BUSY
TIMEOUT
INVALID_PARAMETER
UNSUPPORTED_COMMAND
UNSUPPORTED_FEATURE
SAFETY_POLICY_DENIED
USER_CONFIRMATION_REQUIRED
ACQUISITION_FAILED
NO_SIGNAL
WAVEFORM_TRANSFER_FAILED
SCPI_ERROR
```

LLMが原因を判断できるよう、Tool Errorは機械可読形式とする。

---

# 25. SCPI Error Queue

コマンド送信成功だけで処理成功とはみなさない。

必要に応じてSCPI Error Queueを確認し、

```text
send command
 ↓
query error
 ↓
validate
```

する。

複数コマンドを連続実行する場合も、どの操作でエラーとなったか追跡できること。

---

# 26. Parameter Validation

LLMが指定した値をそのままSCPIへ渡してはならない。

例:

```text
CH1 scale = -100 V/div
```

等はMCP側で拒否する。

Validation情報は、

1. MHO98 Programming Guide
2. Capabilities定義
3. 実機Query

の順で管理する。

---

# 27. 単位

MCP APIではSI基本単位を原則とする。

例:

```text
Voltage       V
Time          s
Frequency     Hz
Resistance    Ω
Sample Rate   Sa/s
```

LLMから

```text
500 mV
20 us
115.2 kHz
```

などが指定された場合の変換はLLMまたはMCP Adapterで行う。

Tool内部ではfloat + SI単位に正規化する。

---

# 28. 非機能要件

## 28.1 レスポンス

通常の設定Query:

```text
目標: 1秒以内
```

波形転送:

```text
取得ポイント数に依存
```

巨大Memory全体を不用意にダウンロードしない。

---

## 28.2 信頼性

SCPI通信切断時には、

1. エラー返却
2. 自動再接続試行
3. Device ID再確認

を行う。

自動再接続後に設定変更を勝手に再実行しない。

---

## 28.3 可観測性

ログレベル:

```text
ERROR
WARN
INFO
DEBUG
TRACE
```

TRACEではSCPI送受信を記録可能とする。

ただしデフォルトではSCPI全文の大量ログを抑制する。

---

# 29. セキュリティ

## 29.1 Network

MHO98はインターネットへ直接公開しない。

推奨:

```text
MHO98
 │
LAN
 │
Trusted LAN
 │
MCP Host
```

MCP Hostを経由して制御する。

---

## 29.2 MCP Server

初期構成ではローカル用途を想定する。

優先:

```text
MCP stdio
```

将来的にネットワークMCPを使用する場合:

* Authentication
* TLS
* Network ACL
* Device ACL
* Session管理

を追加する。

---

# 30. 設定ファイル

例:

```yaml
devices:
  mho98:
    transport: lan
    host: 192.168.1.100
    timeout: 5s

safety:
  raw_scpi: false
  auto_setup: confirm
  impedance_50ohm: confirm
  afg_enable: confirm
  factory_reset: confirm

waveform:
  default_max_points: 100000
  absolute_max_points: 1000000

agent:
  max_measurement_iterations: 5

logging:
  level: info
  audit: true
```

---

# 31. 実装方針

要件上は言語を固定しない。

ただしPoCについては以下を第一候補とする。

```text
Python
+
PyVISA / Socket SCPI
+
MCP SDK
```

理由:

* VISA/SCPI検証が容易
* 波形解析ライブラリが豊富
* NumPy等との連携が容易
* MCP Tool実装が容易

SCPI Adapterを独立させ、

```text
MCP Layer
    ↓
Service Layer
    ↓
Safety Layer
    ↓
Rigol MHO98 Driver
    ↓
Transport
```

とする。

将来的にGo等へ移行してもMCP Tool仕様が変わらない設計とする。

---

# 32. モジュール構成案

```text
rigol-mho98-mcp/

  mcp/
    tools
    resources

  service/
    oscilloscope
    measurement
    waveform

  safety/
    policy
    confirmation
    validator

  driver/
    mho98/
      channel
      horizontal
      trigger
      acquisition
      measurement
      waveform
      system

  transport/
    visa
    tcp

  analyzer/
    waveform
    fft

  audit/

  config/
```

---

# 33. MCP Resource

ToolだけでなくResourceとして以下を公開することを検討する。

```text
rigol://mho98/state
rigol://mho98/capabilities
rigol://mho98/last-measurement
rigol://mho98/safety-policy
```

LLMが毎回Toolを実行せず状態を参照できる構成を目指す。

---

# 34. 操作例

## 34.1 UART測定

利用者:

> CH1につないだ3.3V UART、115200bpsを見たい。

想定処理:

```text
scope.get_state

        ↓

measurement.recommend_setup
 signal_type=uart
 voltage=3.3
 baud=115200

        ↓

scope.configure_channel
 CH1
 DC
 1 V/div程度

        ↓

scope.configure_timebase

        ↓

scope.configure_trigger
 CH1
 rising
 約1.65 V

        ↓

scope.single

        ↓

scope.measure

        ↓

scope.capture_screenshot

        ↓

LLM解析
```

---

# 35. Unknown Signal測定

利用者:

> CH1の信号が何なのかよく分からない。調べて。

AIは一度に大きく設定を変更せず、

```text
状態確認
 ↓
安全な入力条件確認
 ↓
粗いTimebase
 ↓
波形取得
 ↓
振幅確認
 ↓
Timebase調整
 ↓
Trigger設定
 ↓
再取得
 ↓
Frequency / Vpp測定
```

のように段階的に探索する。

---

# 36. Acceptance Criteria

MVP完成条件を以下とする。

## 接続

* [ ] MHO98をLAN経由で検出できる
* [ ] `scope.identify`が成功する
* [ ] 切断時に適切なエラーとなる

## 状態

* [ ] CH1～CH4状態を取得できる
* [ ] Timebaseを取得できる
* [ ] Trigger状態を取得できる

## 操作

* [ ] CH ON/OFFを変更できる
* [ ] Vertical Scaleを変更できる
* [ ] Timebaseを変更できる
* [ ] Edge Triggerを設定できる
* [ ] Run/Stop/Singleを実行できる

## Measurement

* [ ] Frequencyを取得できる
* [ ] Vppを取得できる
* [ ] RMSを取得できる
* [ ] Rise/Fall Timeを取得できる

## Data

* [ ] Waveform Samplesを取得できる
* [ ] Screenshotを取得できる

## Safety

* [ ] 50Ωへの変更を無条件実行しない
* [ ] AFG ONを無条件実行しない
* [ ] Raw SCPIがデフォルト無効
* [ ] 危険操作にはConfirmationが必要
* [ ] 操作ログを保存する

## AI

* [ ] LLMからMCP経由で状態取得できる
* [ ] 自然言語要求から適切な設定変更を実行できる
* [ ] Acquisition後のMeasurementを取得できる
* [ ] 必要に応じて設定変更→再測定を行える

---

# 37. 開発フェーズ

## Phase 0 — SCPI検証

MCPを実装する前に実機で確認する。

```text
LAN接続
*IDN?
CH Query
Timebase Query
Trigger Query
Measurement
Waveform Download
Screenshot
```

ここでMHO98 Programming Guideと実機挙動の差異を確認する。

---

## Phase 1 — Read Only MCP

実装:

```text
identify
get_state
get_channel
get_timebase
get_trigger
measure
capture_screenshot
capture_waveform
```

AIから機器を変更できない安全な状態でMCP連携を検証する。

---

## Phase 2 — Basic Control

追加:

```text
configure_channel
configure_timebase
configure_trigger
run
stop
single
```

Safety Policyを導入する。

---

## Phase 3 — Measurement Assistant

追加:

```text
recommend_setup
measurement workflow
automatic re-measurement
```

この段階で、

> 「UARTを見たい」

のような自然言語操作を実用化する。

---

## Phase 4 — Advanced Features

追加:

```text
Serial Decode
Logic Analyzer
AFG
FFT
Advanced Waveform Analysis
```

---

# 38. 成功基準

本システムの最終的な成功基準は、

> オシロスコープの各ノブやメニュー構造を詳しく知らない利用者でも、「何を測定したいのか」をAIへ伝えることで、安全性を維持しながら適切な測定を開始できること

とする。

同時に、

> AIが測定機器の物理的な安全性まで理解している

という前提には立たず、

```text
AI
= 測定設定と解析を支援する

MCP
= 機器制御と安全ポリシーを担保する

人間
= DUT・Probe・Ground等の物理安全を担保する
```

という責任分界を維持する。

---

# 39. 今後の詳細設計で決定する事項

以下は要件定義では確定せず、実機SCPI検証後に決定する。

1. LAN接続時のTransport方式
2. USB接続時のVISA Resource形式
3. Screenshot取得SCPI
4. Waveform転送フォーマット
5. Binary Block処理方式
6. 各Measurement SCPIの対応表
7. 全パラメータ許容範囲
8. SCPI Error Queue処理
9. AFG SCPI仕様
10. Logic Analyzer SCPI仕様
11. Serial Decode SCPI仕様
12. Firmware Versionごとの差異
13. MCP SDKおよび実装言語
14. Waveform一時ファイルの受け渡し方式
15. User ConfirmationのMCP上での表現方法

これらは**MHO98 Programming Guideと実機を照合した上で詳細設計書に落とし込む**。

RIGOL公式サイトではMHO98専用Programming Guideが公開されており、2026-02-26版が掲載されている。

---

# 40. 最終構成イメージ

```text
                   ┌─────────────────┐
                   │      User       │
                   └────────┬────────┘
                            │
                         Natural
                         Language
                            │
                   ┌────────▼────────┐
                   │       LLM       │
                   └────────┬────────┘
                            │ MCP
             ┌──────────────▼──────────────┐
             │       rigol-mho98-mcp       │
             │                             │
             │  Measurement Assistant      │
             │          │                  │
             │     Safety Policy           │
             │          │                  │
             │     Scope Service           │
             │          │                  │
             │      SCPI Driver            │
             └──────────────┬──────────────┘
                            │
                     LAN / USB SCPI
                            │
                  ┌─────────▼─────────┐
                  │    RIGOL MHO98    │
                  └───────────────────┘
                            │
                Probe / Logic / AFG
                            │
                  ┌─────────▼─────────┐
                  │       DUT         │
                  └───────────────────┘
```
