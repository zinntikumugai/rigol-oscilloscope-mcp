# MCP Toolカタログ

**対象文書:** [Requirements.md](Requirements.md) 5章の詳細仕様
**操作クラス・確認フロー:** [Requirements.md](Requirements.md) 6章
**機種差の吸収:** [device-profiles.md](device-profiles.md)

## 0. 共通規約

### 0.1 命名

- Tool名は `[a-z0-9_]` のスネークケースとする。MCPクライアントのTool名制約(`[a-zA-Z0-9_-]`)により、旧文書の `scope.identify` のようなドット付き名は使用しない
- 引数・返却のキーは スネークケース + SI単位サフィックス(`scale_v_per_div`, `frequency_hz`, `rise_time_s` など)とする

### 0.2 単位

API境界はSI基本単位(V, s, Hz, Ω, Sa/s)。「500 mV」「20 us」等の接頭辞付き表現の変換はLLM側の責務とし、Toolは float を受け取る。

### 0.3 共通返却フィールド

- 設定系Tool(`configure_*` ほか書き込みを伴うもの)は `requested` と `applied`(read-back値)を両方返す
- 危険操作は `confirm_token` フロー(6章)に従い、初回呼び出しで `USER_CONFIRMATION_REQUIRED` を返す
- エラーは機械可読形式: `{ "code": "<ERROR_CODE>", "message": "...", "detail": {...} }`。コード一覧は [Requirements.md](Requirements.md) 7.4

### 0.4 操作クラス表記

各Toolに操作クラス(READ_ONLY / SAFE_WRITE / RESTRICTED_WRITE / DANGEROUS_WRITE)とPhase(導入フェーズ)を明記する。

---

## 1. 接続管理

### `connect` — SAFE_WRITE / Phase 1

デバイスへ接続する。会話でユーザーが指定した接続先を渡すことが基本の使い方。

引数:

| 名前 | 型 | 必須 | 説明 |
|---|---|---|---|
| `address` | string | – | IPアドレス/ホスト名、またはVISAリソース文字列。省略時は設定のデフォルト接続先を使用 |
| `transport` | `"lan"` \| `"usb"` | – | 省略時はaddressの形式から推定(IP形式→lan、`USB::`等のVISA形式→usb) |
| `port` | int | – | LAN時のSCPIポート。省略時はプロファイル既定(Rigolは5555) |

動作(接続シーケンス):

1. 既存接続があれば切断して置換(単一アクティブ接続)
2. トランスポートを開く(LAN raw socket / USB USBTMC via PyVISA)
3. エラーキューを空になるまでdrain
4. `*IDN?` で機器識別
5. 機種プロファイル解決([device-profiles.md](device-profiles.md) 1章)

返却: `scope_identify` と同一の識別情報(下記)。

エラー: `address` 省略かつデフォルト設定も無い場合は `INVALID_PARAMETER` とし、メッセージで「接続先(IPアドレス等)をユーザーに確認するように」とLLMを誘導する。接続失敗は `DEVICE_NOT_FOUND`。

### `disconnect` — SAFE_WRITE / Phase 1

現在の接続を閉じる。未接続でもエラーとしない(冪等)。

### `scope_identify` — READ_ONLY / Phase 1

機器識別と接続状態を返す。専用の接続状態Toolは設けず、本Toolに統合する。

返却例:

```json
{
  "connected": true,
  "address": "192.168.1.120",
  "transport": "lan",
  "manufacturer": "RIGOL TECHNOLOGIES",
  "model": "MHO98",
  "serial": "...",
  "firmware": "00.01.00",
  "profile": { "name": "mho98", "confidence": "verified" }
}
```

未接続時は `connected: false` と、接続には `connect` が必要である旨を返す(エラーにしない)。

### `get_capabilities` — READ_ONLY / Phase 1

接続機器の利用可能機能を返す。プロファイルのcapabilities([device-profiles.md](device-profiles.md) 2.1)と実機Queryの組み合わせ。`profile`(名前・信頼度)を含み、`generic` の場合は未検証機能が制限されることを明示する。

返却:

| フィールド | 型 | 説明 |
|---|---|---|
| `profile` | object | `name` / `confidence` |
| `capabilities` | object | プロファイルのcapabilitiesブロックそのまま |
| `options` | object \| null | 導入済みライセンスオプション(意味的な名前 → `true` / `false`)。判定できなかった項目は `null` |
| `unsupported_vendor` | bool | 製造者がRIGOLでない場合 `true` |

`options` はプロファイルが `option_query` / `option_types` を宣言する機種でのみ得られ、**非対応機種では `null`**(照会自体を実機へ送らない。[device-profiles.md](device-profiles.md) 2.2)。実機MHO98では未ライセンス状態でも `afg_50mhz` / `memory_500mpts` が `true`(工場出荷標準)である点に注意([verification/mho98-unlicensed.md](verification/mho98-unlicensed.md))。オプションの照会結果は接続中キャッシュされる(ライセンス適用は再起動を伴うため、接続中に変化しない)。

---

## 2. 状態取得

### `get_state` — READ_ONLY / Phase 1

主要設定の一括取得。LLMが操作前に現在状態を把握するための主要Tool。

引数:

| 名前 | 型 | 必須 | 説明 |
|---|---|---|---|
| `sections` | string[] | – | 取得セクションの絞り込み: `channels` / `timebase` / `trigger` / `acquisition`。省略時は全セクション |

全取得は約39クエリ・実測1.3〜1.5秒(負荷時はさらに増加)かかるため、目的が明確な場合は `sections` での絞り込みを推奨する(この旨をTool descriptionに記載する)。

### `get_channel` — READ_ONLY / Phase 1

引数: `channel`(`"CH1"`〜`"CH4"`)

返却: `enabled`, `scale_v_per_div`, `offset_v`, `coupling`, `impedance`, `probe_ratio`, `bandwidth_limit`

### `get_timebase` — READ_ONLY / Phase 1

返却: `scale_s_per_div`, `position_s`, `sample_rate_sa_per_s`, `memory_depth`

### `get_trigger` — READ_ONLY / Phase 1

返却: `type`, `source`, `level_v`, `slope`, `sweep_mode`, `status`

### `get_acquisition_state` — READ_ONLY / Phase 1

返却: `state`(run / stop / single待機 等)、トリガ状態

---

## 3. 設定変更

### `configure_channel` — SAFE_WRITE(一部RESTRICTED_WRITE)/ Phase 2

引数(未指定項目は変更しない):

| 名前 | 型 | 説明 |
|---|---|---|
| `channel` | string | 必須。`"CH1"`〜`"CH4"` |
| `enabled` | bool | 表示ON/OFF |
| `scale_v_per_div` | float | 垂直感度 |
| `offset_v` | float | 垂直オフセット |
| `coupling` | `"DC"` \| `"AC"` \| `"GND"` | カップリング |
| `probe_ratio` | float | プローブ比(例: x10プローブ → 10) |
| `bandwidth_limit` | bool/string | 帯域制限 |
| `impedance` | `"1M"` \| `"50"` | 入力インピーダンス。**`"50"` はRESTRICTED_WRITE**(confirmトークン必須) |

返却: `requested` / `applied`(read-back値)。機器はスケールを1-2-5にスナップするとは限らないため、LLMは `applied` を信頼する。

### `configure_timebase` — SAFE_WRITE / Phase 2

引数: `scale_s_per_div`(必須)、`position_s`(任意)

### `configure_trigger` — SAFE_WRITE / Phase 2

MVPはEdge Triggerのみ。

引数: `type`(現状 `"edge"` 固定)、`source`(`"CH1"`等)、`level_v`、`slope`(`"rising"` / `"falling"` / `"either"`)、`sweep_mode`(`"auto"` / `"normal"` / `"single"`)

---

## 4. Acquisition

### `run` / `stop` / `single` — SAFE_WRITE / Phase 2

波形取り込みの開始 / 停止 / シングルショット。

### `autoset` — RESTRICTED_WRITE / Phase 2

Auto Setupは利用者の設定を大きく上書きするため、confirmトークンによる承認を要求する。実行した場合は返却に「Auto Setupを実行し設定が変更された」ことを明記し、実行後の主要設定を併せて返す。

---

## 5. 測定・データ取得

### `measure` — READ_ONLY / Phase 1

引数:

| 名前 | 型 | 説明 |
|---|---|---|
| `channel` | string | 必須 |
| `measurements` | string[] | 必須。`frequency`, `period`, `vpp`, `vmax`, `vmin`, `vavg`, `rms`, `duty`, `rise_time`, `fall_time` |

- 意味的測定名 → SCPIニモニックの変換はプロファイルの対応表で行う。プロファイルに無い項目は送信せず `UNSUPPORTED_FEATURE`(不正ニモニックはタイムアウト+キュー汚染のコストがあるため。[device-profiles.md](device-profiles.md) 4.2)
- 返却キーはSI単位付き: `frequency_hz`, `vpp_v`, `rise_time_s`, `duty_ratio`(dutyは比率。MHO98実測 0.5002)
- 可能な範囲で測定品質(`valid` / `overflow` / `no_signal` / `unstable` / `unknown`)を付与し、無効値を正常値としてLLMに解釈させない

### `capture_waveform` — READ_ONLY / Phase 1

引数: `channel`(必須)、`max_points`(任意、既定/上限は設定による)、`format`(任意)

返却: サンプル配列(小規模時)またはファイル参照(大規模時)+ メタデータ(`sample_interval_s`, `time_origin_s`, 電圧変換係数, `channel`, `timestamp`)。巨大データはMCPレスポンスに直接格納せず、一時ファイルに保存してパスとメタデータを返す。電圧変換はプロファイルのプリアンブル規約(yorigin / yreference)に従いサーバー側で実施し、LLMには物理量(V)で返す。

注: 画面表示データは間引きされている(実測: 表示500 kSa/s vs 実サンプルレート5 MSa/s)。返却メタデータに実効サンプルレートを含める。

### `capture_screenshot` — READ_ONLY / Phase 1

現在の画面を画像として取得し、ファイル保存と画像返却を行う。

引数:

| 名前 | 型 | 必須 | 説明 |
|---|---|---|---|
| `path` | string | – | 保存先。ディレクトリまたはファイルパス。省略時は設定のデフォルトディレクトリ + タイムスタンプ名(`scope_YYYYmmdd_HHMMSS_mmm.png`。`mmm` はミリ秒3桁で、同一秒内の連続撮影でも上書きしない)。**相対パスはデフォルトディレクトリを基準**に解決する(プロセスのカレントディレクトリ基準ではない)。デフォルトディレクトリの既定はサーバーを起動した実行ディレクトリ(`PWD` 環境変数。無効時はプロセスのカレントディレクトリ)で、`RIGOL_MCP_SCREENSHOT_DIR` で明示指定できる |
| `format` | `"png"` \| `"jpg"` \| `"jpeg"` \| `"bmp"` \| `"webp"` | – | 省略時は `path` の拡張子から推定。拡張子も無ければ png |
| `return_image` | bool | – | 既定 true。MCP image content として画像も返す(LLM Visionでの波形確認用)。トークン節約時は false |

動作:

1. プロファイルのスクリーンショットコマンド(MHO98: `:DISPlay:DATA?` → PNG 約97KB)で画像取得
2. `path` を `~` 展開・(相対ならデフォルトディレクトリ基準で)正規化し、許可ルート([Requirements.md](Requirements.md) 9章 = 明示指定 + デフォルト保存先 + 一時ディレクトリ)内であることを検証。外なら `INVALID_PARAMETER`(`detail.hint` で `RIGOL_MCP_ALLOWED_DIRS` を案内)
3. 指定形式へ変換(Pillow)して保存。機器返却形式と同一ならそのまま保存
4. 返却: 保存した絶対パス、形式、サイズ、(`return_image=true` なら)画像本体

用途は波形形状・トリガ状態・画面異常の目視確認とLLM Vision解析。**数値はスクリーンショットのOCRではなく `measure` の結果を優先する**。

---

## 6. プロトコルデコード(Phase 4)

### `configure_decode` — SAFE_WRITE / Phase 4

シリアルプロトコルデコードのバス(`:BUS1`〜`:BUS4`)を設定する。**表示・解析層のみを変える**操作で、取り込み設定(垂直軸・水平軸・トリガ)にも出力にも触れず完全に可逆なため、`configure_channel` より侵襲性が低い SAFE_WRITE とする(confirmトークン不要)。

引数(未指定項目は変更しない):

| 名前 | 型 | 説明 |
|---|---|---|
| `protocol` | string | 必須。`uart` / `i2c` / `spi` / `can` / `lin` / `parallel` |
| `bus` | int | デコードバス番号。既定 1(MHO98は1〜4) |
| `enabled` | bool | バス表示のON/OFF(`:BUS<n>:DISPlay`) |
| `event_table` | bool | イベントテーブル表示(`:BUS<n>:EVENt`)。**有効化にはバス表示ONが先に必要**なため、`enabled=true` と同時に指定する |
| `data_format` | string | `hex` / `ascii` / `dec` / `bin` |
| `settings` | object | プロトコル別の設定(下表)。ソース値は `"CH1"`〜`"CH4"` / `"D0"`〜`"D15"` / `"off"` |

`settings` のキー(全て任意。単位付きキーはSI基本単位):

| プロトコル | キー |
|---|---|
| `uart` | `tx_source`, `rx_source`, `baud_bps`(1〜20000000), `data_bits`(5/6/7/8/9), `parity`(none/odd/even), `stop_bits`(1/1.5/2), `endian`(msb/lsb), `polarity`(positive/negative), `tx_threshold_v`, `rx_threshold_v` |
| `i2c` | `scl_source`, `sda_source`, `swap_sda_scl`, `address_bits`(7/8/10), `scl_threshold_v`, `sda_threshold_v` |
| `spi` | `clk_source`, `clk_slope`(rising/falling), `mosi_source`, `miso_source`, `cs_source`, `cs_polarity`(high/low), `frame_mode`(cs/timeout), `timeout_s`(8e-9〜10), `data_bits`(4〜32), `endian`, `polarity`(high/low), `clk_threshold_v`, `mosi_threshold_v`, `miso_threshold_v`, `cs_threshold_v` |
| `can` | `source`, `signal_type`(tx/rx/canh/canl/differential), `baud_bps`(10000〜5000000), `sample_point_percent`(10〜90), `threshold_v` |
| `lin` | `source`, `baud_bps`(2400〜20000000), `parity_enabled`, `standard`(v1x/v2x/mixed), `threshold_v` |
| `parallel` | `clk_source`, `clk_slope`, `bus_width`, `endian`, `polarity` |

動作・規範:

- **送信順は固定**: `:MODE` → `:FORMat` → プロトコル別設定 → `:DISPlay` → `:EVENt`。各項目は set → エラーキュー確認 → read-back(0.3節)
- **検証は全て送信前**に行う(不正な列挙値・範囲外は1コマンドも送らずに `INVALID_PARAMETER`。実機は不正トークン1発でSCPIサーバーが沈黙するため)。他プロトコルのキーを混ぜた場合は `detail.allowed` にそのプロトコルの許容キーを返す
- `uart` の `tx_source`/`rx_source`、`spi` の `mosi_source`/`miso_source` を**両方 `off` にはできない**(デコード対象が無くなる)
- 対応プロトコルは機種プロファイルの `decode_protocols` が持つ([device-profiles.md](device-profiles.md) 2.2)。**未宣言のプロトコル(I2S / FlexRay / MIL-STD-1553 / CAN-FD)は送信前に `UNSUPPORTED_FEATURE`** — これらはライセンスオプション必須で、標準搭載6種のみを扱う
- 返却: `bus` / `requested` / `applied`(read-back値)/ `changed`

### `get_decode_result` — READ_ONLY / Phase 4(未実装)

デコード結果(イベントテーブル)の取得。**未実装**(PR C 予定)。結果取得SCPIとイベントテーブルの応答形式は実機検証が必要なため、本PRでは設定系のみを提供する。現時点でデコード結果を読むには `event_table=true` にしたうえで `capture_screenshot` で画面を確認する。

---

## 7. Measurement Assistant(Phase 3)

Phase 3は**同梱スキルで実現した**(サーバー側Toolなし)。測定目的→推奨設定の対応表(信号種別10種)、UART・未知信号のワークフロー、安全プロンプト、反復上限ガイダンスは [`skills/measurement-workflows/SKILL.md`](../skills/measurement-workflows/SKILL.md) に記載し、Claudeプラグイン([Requirements.md](Requirements.md) 10.3)として配布する。

### `recommend_setup` — READ_ONLY / Phase 3(未実装・フォールバック)

測定目的(signal_type, expected_voltage, expected_frequency など)から推奨設定と根拠(`reasoning_summary`)・警告(`warnings`)を返す。**機器設定は変更しない**。

注記: 推奨ロジックはLLM自身の知識+同梱スキルで代替しており、**本Toolは実装していない**。スキルで精度不足が実証された場合のフォールバックとして仕様のみ残す。

---

## 8. Raw SCPI(デフォルト無効)

### `raw_scpi` — DANGEROUS_WRITE / 開発用

デフォルト無効(設定で明示的に有効化した場合のみ登録)。有効時もQueryのみ許可・Denylist・監査ログ・confirmトークンを適用する。開発・デバッグ用途以外では使用しない。

---

## 9. Tool一覧(サマリ)

| Tool | クラス | Phase |
|---|---|---|
| `connect` | SAFE_WRITE | 1 |
| `disconnect` | SAFE_WRITE | 1 |
| `scope_identify` | READ_ONLY | 1 |
| `get_capabilities` | READ_ONLY | 1 |
| `get_state` | READ_ONLY | 1 |
| `get_channel` | READ_ONLY | 1 |
| `get_timebase` | READ_ONLY | 1 |
| `get_trigger` | READ_ONLY | 1 |
| `get_acquisition_state` | READ_ONLY | 1 |
| `measure` | READ_ONLY | 1 |
| `capture_waveform` | READ_ONLY | 1 |
| `capture_screenshot` | READ_ONLY | 1 |
| `configure_channel` | SAFE_WRITE(50ΩはRESTRICTED) | 2 |
| `configure_timebase` | SAFE_WRITE | 2 |
| `configure_trigger` | SAFE_WRITE | 2 |
| `run` / `stop` / `single` | SAFE_WRITE | 2 |
| `autoset` | RESTRICTED_WRITE | 2 |
| `recommend_setup`(未実装・スキルで代替) | READ_ONLY | 3 |
| `configure_decode` | SAFE_WRITE | 4 |
| `get_decode_result`(未実装) | READ_ONLY | 4 |
| `raw_scpi` | DANGEROUS_WRITE | 開発用 |

将来(Phase 4の残り): デコード結果取得(`get_decode_result`)、Logic Analyzer、AFG(出力ONはDANGEROUS_WRITE)、高度解析。
