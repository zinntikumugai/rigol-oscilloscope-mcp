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
  "address": "192.0.2.10",
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

ニモニックは世代で分岐する(MHO900/DHO系: `:AUToset` / 旧世代: `:AUToscale`)ため、dialect `autoset_command` の宣言必須 — 未宣言の機種では送信せず `UNSUPPORTED_FEATURE`。経緯と実機プローブは [verification/mho98-autoset.md](verification/mho98-autoset.md)(かつての `:AUToscale` ハードコードはMHO900に存在しない未定義ヘッダだった)。

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

### `clear_measurements` — SAFE_WRITE / Phase 4

画面のResultビューから**全測定項目を消す**(issue #16)。`measure` が使う `:MEASure:ITEM?`(クエリ形)は測定値を返すと同時に項目を有効化するため、測定のたびにResultビューへ項目が蓄積する — その掃除を担う。

- 引数なし・全消しのみ(ガイドに部分クリアの構文が存在しない)。返却は `{"result": "ok"}`(readback対象が無い。run/stopと同型)
- SAFE_WRITEの根拠: 表示のみの変更で取得条件(垂直・水平・トリガ)に触れず、再測定で完全に可逆
- ニモニックはファミリで分岐する(MHO900: `:MEASure:DELete` / DHO800・900系: `:MEASure:CLEar`。説明文は両ガイドで同一)ため、プロファイルdialect `measurement_clear` の宣言必須 — 未宣言の機種では送信せず `UNSUPPORTED_FEATURE`。実機検証: [verification/mho98-measure-clear.md](verification/mho98-measure-clear.md)(MHO98は両ニモニックを受理するが、ガイド記載の `DELete` を採用)

### `capture_waveform` — READ_ONLY / Phase 1

引数: `channel`(必須)、`max_points`(任意、既定/上限は設定による)、`format`(任意)

返却: サンプル配列(小規模時)またはファイル参照(大規模時)+ メタデータ(`sample_interval_s`, `time_origin_s`, 電圧変換係数, `channel`, `timestamp`。FFT演算のMATHトレースのみ時間軸のキーの代わりに `frequency_step_hz` / `frequency_start_hz` を返す。下記「MATHトレースの取得」)。巨大データはMCPレスポンスに直接格納せず、一時ファイルに保存してパスとメタデータを返す。電圧変換はプロファイルのプリアンブル規約(yorigin / yreference)に従いサーバー側で実施し、LLMには物理量(V)で返す。

注: 画面表示データは間引きされている(実測: 表示500 kSa/s vs 実サンプルレート5 MSa/s)。返却メタデータに実効サンプルレートを含める。

**MATHトレースの取得(Phase M1):** `channel` は `"CH1"`〜`"CH4"` に加えて `"MATH1"`〜`"MATH4"` を受理する(新しい引数は増やさない。11章の `configure_math` で設定したトレースをそのまま読む)。

- `:WAVeform:SOURce MATH<n>` を送る(ガイド3.28.1 の `{CHANnel1-4|MATH1-4}`)。**取得できるのは画面に表示されているデータ・`NORMal` モードのみ**(ガイド逐語。本サーバーは元から `:WAVeform:MODE NORMal` で読むため追加処理は無い)。**先にそのMATHトレースの表示をONにしておくこと**
- MATHソースのときだけ `:MATH<n>:OPERator?` を**1回**追加照会する。アナログチャンネルの取得経路には `:MATH` 系のコマンドを一切送らず、返却の形も従来と変わらない(後方互換)
- **演算子が `fft` のトレースは横軸が周波数**になる。この場合の返却は `x_unit: "Hz"` / `frequency_step_hz` / `frequency_start_hz` で、**時間軸前提のキー(`sample_interval_s` / `time_origin_s` / `effective_sample_rate_sa_per_s`)は返さない**(意味を持たないか、誤読を招くため)。`fft` 以外の演算子(および全アナログチャンネル)は従来どおり時間軸で、`x_unit` は付かない
- **FFTのx軸は実機検証で確定済み**(MHO98 / fw 00.01.00 / 2026-08-27 → [verification/mho98-math.md](verification/mho98-math.md) (c))。プリアンブルの xincrement は Hz/pt ではなく **GHz/pt** で、`frequency_step_hz` = xincrement × 1e9(表示範囲3通りで `点数 × 刻み = 表示終端周波数` が厳密に一致)。**xorigin は開始周波数ではなく時間軸の値が残る**ため、`frequency_start_hz` は `:MATH<n>:FFT:FREQuency:STARt?` から読む(FFT時のみ問い合わせ1本を追加。合計2本)。この関係は返却の `note` にも英語で記載している
- MATHチャンネル番号の検証は `math_channels` capability に委ね、未宣言の機種・範囲外の番号は**送信ゼロ**で `UNSUPPORTED_FEATURE` / `INVALID_PARAMETER`

### `analyze_waveform` — READ_ONLY / Phase 4

波形を取得し、**ホスト側(サーバー側)で**解析して要約数値だけを返す。機器の解析機能は使わない(未確認SCPIを送らない方針のため)。

引数:

| 名前 | 型 | 必須 | 説明 |
|---|---|---|---|
| `channel` | string | – | 既定 `"CH1"`。`capture_waveform` と同じく `"MATH1"`〜`"MATH4"` も受理する(下記のとおりFFT演算のトレースのみ拒否) |
| `analyses` | string[] | – | `"stats"` / `"fft"` の部分集合。省略時は全解析。未知の名前は `INVALID_PARAMETER`(`detail.valid` に有効名) |
| `max_points` | int | – | `capture_waveform` と同一の意味(既定/上限は設定値。超過は丸めて `max_points_clamped: true`) |

返却:

| キー | 説明 |
|---|---|
| `channel` / `points` / `sample_interval_s` / `effective_sample_rate_sa_per_s` / `note` | `capture_waveform` と同じメタデータ |
| `stats` | `min_v`, `max_v`, `mean_v`, `rms_v`, `std_v`(母標準偏差), `vpp_v` |
| `fft` | `dominant_frequency_hz`(ピーク無しなら `null`), `frequency_resolution_hz`, `window`(`"hann"`), `peaks`(最大5本。`frequency_hz` / `amplitude_v` を振幅降順) |

**サンプル配列は返さない。** 生データが必要なときは `capture_waveform` を使う(解析結果に巨大配列を混ぜてトークンを浪費しないための分業)。

FFTの実装:

- Hann窓 → 2の冪へゼロパディング → radix-2 FFT(stdlibのみ。追加依存なし)
- 振幅はコヒーレントゲイン(0.5)補正と単側スペクトルの×2を適用済み
- 直流はピーク探索から除外し、窓掛け前に平均を引く(1レコードに数周期しか無い波形で直流の窓漏れが信号ビンを覆うのを防ぐ)

**MATHトレースの解析(Phase M1):** 非FFTのMATHトレース(加減乗除・微積分・フィルタ等)は通常どおり解析できる。**演算子が `fft` のMATHトレースは波形を取得する前に `INVALID_PARAMETER` で拒否する** — 横軸が既に周波数であり、時間軸前提の統計もホスト側FFTも意味を持たないため。機上のピーク表が要るなら `get_math_state`(11章)、スペクトルの点列が要るなら `capture_waveform` を使う(エラーメッセージでも両者を案内する)。判定は `capture_waveform` と同じ `:MATH<n>:OPERator?` 1回の照会で行い、アナログチャンネルには何も送らない。

注(周波数確度): ゼロパディングでビン間隔は細かくなるが、**真の分解能は `frequency_resolution_hz` = 1 /(点数 × `sample_interval_s`)が上限**。返り値の桁をそれ以上に信用しない。画面データが間引きされている場合は `sample_interval_s` 自体が実サンプルレートより粗い。周波数・周期の高確度な値が要るときは機器のカウンタを使う `measure` を優先する。

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

### `get_decode_result` — READ_ONLY / Phase 4

デコード結果(イベントテーブル)を `:BUS<n>:DATA?` から読む。**書き込みを一切行わない**(取り込みの停止もしない)。

引数:

| 名前 | 型 | 説明 |
|---|---|---|
| `bus` | int | デコードバス番号。既定 1 |
| `max_events` | int | 返すイベント数の上限(ホスト側で切り詰め)。未指定なら全件 |

返却:

| キー | 説明 |
|---|---|
| `bus` | バス番号 |
| `protocol` | `:BUS<n>:MODE?` 由来の意味的プロトコル名(未対応プロトコルは生の名前を小文字化) |
| `columns` | 列名。**プロトコル・機種依存**(実機MHO98のRS232は `["time_s", "tx_rx", "data", "error"]`) |
| `events` | 行(列名 → 値)。`time_s` のみ秒のfloat、他は文字列(表記は `data_format` に従う) |
| `event_count` | 切り詰め**前**の総件数 |
| `truncated` | `max_events` で切り詰めたか |
| `warnings` | 自然文の警告(下記) |

動作・規範:

- **前提は `configure_decode(enabled=true, event_table=true)`**。どちらかがOFFのバスでは `:DATA?` を**送らず**、`configure_decode(bus=N, ...)` を促す警告付きで空の結果を返す(OFF時の `:DATA?` の実機挙動が未確認のため送らない)
- **取り込み中は警告を出すだけで停止しない**(read-onlyを崩さない)。安定した表が要るなら先に `stop` を呼ぶ。ガイドが停止を要求するのは `:BUS<n>:EEXPort` で、`:DATA?` については記載がない
- 応答はTMCブロックで、中身は「デコード種別トークン / ヘッダ行 / 行...」の改行区切りCSV。**列構成はガイドに記載が無く**、実装はヘッダ行が与える列をそのまま採用する(スキーマを持たない)。列名は小文字snake_case化のみ行い(`Tx/Rx` → `tx_rx`)、`Time` だけ `time_s` として秒へ変換する(`-2.47us` のような工学表記)
- 行が0件(信号なし)でも正常に `events: []` を返す(実機実測)
- クエリ後にエラーキューを確認する(値が返ってもエラーが積まれる実機挙動、[verification/mho98-unlicensed.md](verification/mho98-unlicensed.md) 4章)
- 列構成の実機観測は [verification/mho98-phase4.md](verification/mho98-phase4.md) に記録する(RS232のヘッダは実測済み。他プロトコルは未観測)

---

## 7. 信号発生(AFG)(Phase 4)

内蔵の任意波形/ファンクションジェネレータ(`:SOURce<n>`。MHO900は2ch)を扱う。実測根拠は [verification/mho98-afg.md](verification/mho98-afg.md)。

**設定(`configure_afg`)と出力制御(`enable_afg` / `disable_afg`)は別Toolに分けている。** `configure_afg` / `get_afg_state` は出力状態(`:SOURce<n>:OUTPut:STATe`)を**変更しない**(`get_afg_state` は読み取りのみ行う)。設定変更だけでは信号が外部の被測定回路へ出ない(= SAFE_WRITE で足りる)。**実際に信号を外へ出すのは `enable_afg` だけ**で、これのみ DANGEROUS_WRITE(confirmトークン必須)。

### `configure_afg` — SAFE_WRITE / Phase 4

引数(未指定項目は変更しない。1項目も指定しなければ `INVALID_PARAMETER`):

| 名前 | 型 | 説明 |
|---|---|---|
| `channel` | int | 信号発生チャンネル。既定 1(MHO98は1〜2。**範囲外は送信前に拒否**) |
| `waveform` | string | `sine` / `square` / `ramp` / `noise` / `dc` / `arb` / `exp_rise` / `exp_fall` / `ecg` / `gaussian` / `lorentz` / `haversine` / `sinc` |
| `frequency_hz` | float | 周波数(Hz)。> 0 |
| `amplitude_vpp` | float | 振幅(**Vpp = peak-to-peak**)。> 0 |
| `offset_v` | float | DCオフセット(V) |
| `phase_deg` | float | 位相(度)。0〜360 |
| `duty_percent` | float | 方形波のデューティ比(%)。1〜99 |
| `symmetry_percent` | float | ランプ波の対称性(%)。0〜100 |
| `impedance` | `"highz"` \| `"50"` | **信号発生器側の出力インピーダンス設定**(振幅がどの負荷を前提とするか)。`configure_channel` のオシロ**入力**インピーダンスとは無関係 |
| `arb_file` | string | 機器内蔵ストレージの**既存**ARBファイルを選択する(`C:/...` ローカル / `D:/...` USB。拡張子必須、空白・制御文字禁止)。`:SOURce<n>:LOAD:ARBitrary`(ガイド3.25.3) |
| `modulation` | object | 変調設定。下表参照(ガイド3.25.15-25) |

`modulation` のキー(いずれも省略可。1項目も無ければ他の項目と合わせて全体で1項目も無いのと同じ扱い):

| キー | 型 | 説明 |
|---|---|---|
| `enabled` | bool | 変調ON/OFF(`:MOD:STATe`)。**有効化はパラメータより先、無効化は最後**(下記quirk) |
| `type` | `"am"` \| `"fm"` \| `"pm"` | 変調タイプ(`:MOD:TYPe`) |
| `am_depth_percent` | float | AM深さ(%)。0〜120(`:MOD:AM:DEPTh`) |
| `fm_deviation_hz` | float | FM偏移(Hz)。> 0(`:MOD:FM:DEViation`。上限は搬送波依存のためハードコードしない) |
| `pm_deviation_deg` | float | PM偏移(度)。0〜360(`:MOD:PM:DEViation`) |
| `frequency_hz` | float | **変調周波数**(搬送波の`frequency_hz`とは別物)。2 mHz〜1 MHz目安、> 0のみ検証 |
| `waveform` | string | 変調波形。`sine` / `square` / `triangle` / `upramp` / `dnramp` / `noise` |

返却: `channel` / `requested` / `applied`(read-back値。`modulation` を指定した場合は `applied["modulation"]` にネストして返る)/ `changed`。

動作・規範:

- **送信順は固定**: `:FUNCtion` → (`arb_file` があれば `:LOAD:ARBitrary`) → `:IMPedance` → `:FREQuency` → `:VOLTage:AMPLitude` → `:VOLTage:OFFSet` → `:PHASe` → `:FUNCtion:SQUare:DUTY` → `:FUNCtion:RAMP:SYMMetry` → (`modulation` があれば変調ブロック)。インピーダンスと周波数が振幅の、振幅がオフセットの許容範囲を決めるため、**範囲の広い側から順に**送る(ガイド3.25)。`arb_file` は `waveform="arb"` と同じ呼び出しで使えるよう `:FUNCtion` の直後・周波数/振幅より前に送る。各項目は set → エラーキュー確認 → read-back(0.3節)
- **変調ブロックの送信順(実機quirk対応)**: 実機は **`MOD:STATe` OFF中の変調パラメータ書き込みをエラーなしで無視する**(2026-08-27実測、表示OFFチャンネルへの書き込み無視と同族 → [verification/mho98-afg.md](verification/mho98-afg.md) 6章)。このため有効化時は `TYPe` → `STATe ON` → パラメータの順、無効化時はパラメータ → `STATe OFF`(最後)。パラメータのみ指定で変調がOFFの場合は送信前に `INVALID_PARAMETER` を返し `enabled=true` の併用を促す(`MOD:STATe ON` にしても出力自体はONにならないことを実測確認済み)
- **`frequency_hz` / `waveform` のルーティング**: 変調の配下(`:MOD:<TYPE>:INTernal:*`)は「今回の呼び出しで指定した `type`」、無ければ「機器の現在の `:MOD:TYPe?`」へ送る。後者は**1回だけ**問い合わせる(検証が全て通った後、送信の直前)
- **検証は全て送信前**(1項目でも不正なら1コマンドも送らない)。特にチャンネル番号: 実機は `:SOURce3` の**1発でSCPIサーバー全体が沈黙する**(空行付き再接続で復旧)ため、`afg_channels` による範囲検証は必須
- **範囲外の振幅はサイレントにクランプされる**(HighZ上限20 Vppに対し `50` を送ると `20` になり、**エラーキューには何も積まれない**)。実測で確認した唯一の検出手段が `applied` との突合であり、Tool descriptionでもLLMに `applied` を見るよう明示する
- **周波数・振幅の上限はハードコードしない**(オプション AFG50/AFG100 と出力インピーダンスに依存する)。下限(> 0)のみ検証し、上限は機器のクランプに委ねて `applied` で見せる
- 波形依存パラメータ(デューティ・対称性)は**現在の波形に関わらず**保存され、書き込みもエラーにならない(実測)。クライアント側での波形連動チェックは行わない
- DC / NOISe 中の周波数書き込みは機器が `-200` で明示拒否する(沈黙しない)。クライアント側でゲートせず、set後のエラーキュー確認でそのまま拾う
- 対応機種はプロファイルの `afg_prefix` / `afg_waveforms` / `afg_impedances` が持つ([device-profiles.md](device-profiles.md) 2.2)。変調は追加で `afg_mod_types` / `afg_mod_waveforms` を要求する。**未宣言の機種は送信前に `UNSUPPORTED_FEATURE`** — DHO800/900の番号なし `:SOURce`(DGモジュール)は別方言なので意図的に宣言していない
- **`arb_file` はARBファイルの選択のみ**: 機器内蔵ストレージに既にあるファイルのパスを`:LOAD:ARBitrary`へ指定するだけで、**ファイルの作成・転送・削除は一切行わない**(Requirements.md 3.4)

### `get_afg_state` — READ_ONLY / Phase 4

信号発生の現在設定と**出力状態**を返す。読むだけで出力状態は変えない。

引数: `channel`(int、任意)。省略時は全チャンネル。

返却: `channel` 指定時は1チャンネル分をフラットに返す(`channel`, `output`, `waveform`, `impedance`, `frequency_hz`, `amplitude_vpp`, `offset_v`, `phase_deg`, `duty_percent`, `symmetry_percent`, `modulation`)。省略時は `{"channels": {"1": {...}, "2": {...}}}`(キーはチャンネル番号の文字列)。

`output` は現在出力がONかどうかの bool。`modulation` は現在**有効なtype配下のみ**を返す(`enabled`, `type`, その type の深さ/偏移キー, `frequency_hz`, `waveform`)。1チャンネルあたり14クエリ(基本9 + 変調5)。

### `enable_afg` — DANGEROUS_WRITE / Phase 4

信号発生の出力をONにする。**本Toolだけが実際に信号を外へ出す**(Requirements.md 6.1 の DANGEROUS_WRITE の代表例)。

引数: `channel`(int、既定 1)、`confirm_token`(string、任意)。

返却: `result: "ok"` / `channel` / `state`(切替後の全設定。`get_afg_state` と同じ形)。

動作・規範:

- **確認フローは2段階**(Requirements.md 6.2、`autoset` と同じ実装): 1段目は `confirm_token` 無しで呼ばれ、**機器へ書き込みを1つも送らずに**(現在設定の読み取りのみ)`USER_CONFIRMATION_REQUIRED` を返す(`detail` に `confirm_token` / `description` / `risk` / `instruction` / `expires_in_s`)。2段目でそのトークンを渡して初めて出力がONになる。トークンは**チャンネル単位**かつ**発行時点のAFG設定スナップショット**にバインドされ(ch1の承認でch2はONにできない。発行後に振幅等を変更するとトークンは無効になり再承認が必要)、単回・期限つき・接続世代つき
- **信頼モデル**: この確認フローが防ぐのは**LLMの誤操作・早とちり**であり、悪意あるMCPホストへの防御ではない(トークンは同じ呼び出し元に返る)。物理的な安全は配線を管理する人間の責務(Requirements.md 2.3)
- **リスク文言の趣旨**(実行時文字列は英語): ①出力ONは「今そこに物理的に繋がっているもの」へ実信号を注入する行為であること、②**何が繋がっていて駆動して安全かを人間の利用者に確認させる**こと(生きた回路・通電中の回路を知らずに駆動しない)、③波形・周波数・振幅・オフセットは**ONにした瞬間に効く**ので、`get_afg_state` で読み戻して利用者に提示してから確認を求めること。LLMが自分で承認を代行しないよう、Tool description でも同じことを求める
- 切替後の設定一式を `state` で返すのは、「実際に何が出ているか」をそのまま利用者へ示せるようにするため
- 出力ONのまま設定を変えると外へ出る信号がその場で変わる。設定は原則ONにする前に済ませる

### `disable_afg` — SAFE_WRITE / Phase 4

信号発生の出力をOFFにする。

引数: `channel`(int、既定 1)。返却は `enable_afg` と同じ形(`state.output` が `false`)。

動作・規範:

- **承認を要求しない**(1呼び出しで通る)。根拠: 出力停止は常に安全側への操作であり、**緊急OFFを確認フローでブロックしてはならない**。「止めて」と言われてから往復が要る設計は、その一往復ぶんだけ信号を出し続けることになる
- 波形などの設定は保持されるため、`enable_afg` で同じ信号を再び出せる
- 既にOFFのチャンネルへの呼び出しはエラーではない(冪等)
- 監査(Before / Action / After)は `enable_afg` と同じく記録する

### `sync_afg_phase` — SAFE_WRITE / Phase 4

両AFGチャンネルの位相を同期する(`:SOURce<n>:PHASe:SYNChronize`、ガイド3.25.7)。ガイドの記載:「実行すると、両方の出力チャンネルがプリセットの周波数・位相設定に従って再設定される。周波数が等しいか整数倍の関係にあるときに、この機能で位相を揃えられる」。

引数: `channel`(int、既定 1)。コマンドの送信先(`:SOURce<n>`)を選ぶだけで、**両チャンネルとも影響を受ける**。

返却: `result: "ok"`。read-back対象を持たないwrite-onlyコマンド(`run` / `stop` / `clear_measurements` と同型)。

動作・規範:

- **SAFE_WRITEの根拠**: 振幅・出力状態(信号が出るかどうか)には一切触れず、プリセットの周波数・位相を再適用するだけの整列操作のため、承認は要求しない
- チャンネル番号の範囲検証は他のAFG Toolと同じく `afg_channels` に委ね、範囲外(MHO98ならチャンネル3以上)は送信前に `INVALID_PARAMETER`
- 未対応機種(`afg_prefix` 未宣言)は送信前に `UNSUPPORTED_FEATURE`

---

## 8. Measurement Assistant(Phase 3)

Phase 3は**同梱スキルで実現した**(サーバー側Toolなし)。測定目的→推奨設定の対応表(信号種別10種)、UART・未知信号のワークフロー、安全プロンプト、反復上限ガイダンスは [`skills/measurement-workflows/SKILL.md`](../skills/measurement-workflows/SKILL.md) に記載し、Claudeプラグイン([Requirements.md](Requirements.md) 10.3)として配布する。

### `recommend_setup` — READ_ONLY / Phase 3(未実装・フォールバック)

測定目的(signal_type, expected_voltage, expected_frequency など)から推奨設定と根拠(`reasoning_summary`)・警告(`warnings`)を返す。**機器設定は変更しない**。

注記: 推奨ロジックはLLM自身の知識+同梱スキルで代替しており、**本Toolは実装していない**。スキルで精度不足が実証された場合のフォールバックとして仕様のみ残す。

---

## 9. Raw SCPI(デフォルト無効)

### `raw_scpi` — DANGEROUS_WRITE / 開発用

デフォルト無効(設定で明示的に有効化した場合のみ登録)。有効時もQueryのみ許可・Denylist・監査ログ・confirmトークンを適用する。開発・デバッグ用途以外では使用しない。

---

## 10. Tool一覧(サマリ)

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
| `clear_measurements` | SAFE_WRITE | 4 |
| `capture_waveform` | READ_ONLY | 1 |
| `analyze_waveform` | READ_ONLY | 4 |
| `capture_screenshot` | READ_ONLY | 1 |
| `configure_channel` | SAFE_WRITE(50ΩはRESTRICTED) | 2 |
| `configure_timebase` | SAFE_WRITE | 2 |
| `configure_trigger` | SAFE_WRITE | 2 |
| `run` / `stop` / `single` | SAFE_WRITE | 2 |
| `autoset` | RESTRICTED_WRITE | 2 |
| `recommend_setup`(未実装・スキルで代替) | READ_ONLY | 3 |
| `configure_decode` | SAFE_WRITE | 4 |
| `get_decode_result` | READ_ONLY | 4 |
| `configure_afg` | SAFE_WRITE | 4 |
| `get_afg_state` | READ_ONLY | 4 |
| `enable_afg` | DANGEROUS_WRITE | 4 |
| `disable_afg` | SAFE_WRITE | 4 |
| `sync_afg_phase` | SAFE_WRITE | 4 |
| `configure_math` | SAFE_WRITE | M1 |
| `get_math_state` | READ_ONLY | M1 |
| `raw_scpi` | DANGEROUS_WRITE | 開発用 |

登録Tool数は30(Phase 1: 12 + Phase 2: 7 + Phase 4: 9 + Phase M1: 2。`recommend_setup` / `raw_scpi` は未登録)。Phase M1(MATH演算)の詳細は**11章**にある(既存章番号の参照を壊さないため末尾に追加している)。

将来(Phase 4の残り): `:PERiod` / `:VOLTage:HIGH`/`:LOW`(恒久スキップ。`frequency_hz`/`amplitude_vpp`+`offset_v`で表現可能なため)、DHOファミリの `:SOURce`(番号なし・DGモジュール)対応、Logic Analyzer。

---

## 11. MATH演算(Phase M1)

オシロ内蔵の演算トレース(`:MATH1`〜`:MATH4`、ガイド3.16章)を扱う。加減乗除・論理演算・FFT・微積分系・デジタルフィルタを機器側で計算させ、その結果を1本のトレースとして表示・取得する。

**本章は章番号を末尾に置いている。** コード内コメントやテストが「tools.md 6章」「10章」のように既存の章番号を参照しているため、機能章の途中へ挿入して既存章を繰り下げることはしない(位置づけとしては5〜7章と同列の機能章)。

MATHトレースの**波形取得は新Toolを作らず** `capture_waveform` / `analyze_waveform` の `channel` 引数拡張で行う(5章)。FFTのピーク探索結果も専用Toolを作らず `get_math_state` の返却に含める。

### `configure_math` — SAFE_WRITE / Phase M1

MATH演算トレース(`:MATH<n>`)を設定する。**既に取り込まれている波形から機器内部で別トレースを計算させるだけ**の操作で、取り込み設定(垂直軸・水平軸・トリガ)にも信号発生の出力にも一切触れず、完全に可逆である。したがって `configure_decode`(6章)と同じ根拠で SAFE_WRITE とし、confirmトークンを要求しない。引数条件付きの昇格(`configure_channel` の50Ωのようなもの)も無い。

引数(未指定項目は変更しない。1項目も指定しなければ `INVALID_PARAMETER`):

| 名前 | 型 | 説明 |
|---|---|---|
| `channel` | int | MATHトレース番号。既定 1(MHO98は1〜4。範囲は `math_channels` capability。**範囲外は送信前に拒否**) |
| `display` | bool | トレース表示のON/OFF(`:DISPlay`)。**ONは最初・OFFは最後**に送る(下記) |
| `operator` | string | 演算子。`add` / `subtract` / `multiply` / `divide` / `and` / `or` / `xor` / `not` / `fft` / `integrate` / `differentiate` / `sqrt` / `log10` / `ln` / `exp` / `abs` / `lowpass` / `highpass` / `bandpass` / `bandstop` / `axb` |
| `source1` | string | 算術演算の第1オペランド(`:SOURce1`)。`"CH1"`〜`"CH4"` / `"REF1"`〜`"REF10"` / **自分より小さい番号の** `"MATH1"`〜`"MATH3"` |
| `source2` | string | 同じく第2オペランド(`:SOURce2`) |
| `lsource1` | string | 論理演算(`and` / `or` / `xor` / `not`)の第1オペランド(`:LSOurce1`)。`"D0"`〜`"D15"` / `"CH1"`〜`"CH4"`。算術のソースとは**別コマンド**なので別引数にしている |
| `lsource2` | string | 同じく第2オペランド(`:LSOurce2`) |
| `scale` | float | 演算結果トレースの垂直スケール(1目盛あたり。`:SCALe`)。単位は演算子依存 |
| `offset_v` | float | 演算結果トレースの垂直オフセット(V。`:OFFSet`) |
| `invert` | bool | 演算結果トレースの上下反転(`:INVert`) |
| `fft` | object | FFT演算の設定。下表(`operator="fft"` 用) |
| `filter` | object | デジタルフィルタの設定。下表(`operator` が `lowpass` / `highpass` / `bandpass` / `bandstop` のとき用) |

`fft` のキー(全て任意。ガイド3.16.14-3.16.29):

| キー | 型 | 説明 |
|---|---|---|
| `source` | string | **FFTの入力チャンネル**(`:FFT:SOURce`)。FFTで実際に使われるのは `source1` ではなく**こちら**。トークンの規則は `source1` と同じ(CH / REF / 下位のMATH) |
| `window` | string | 窓関数。`rectangle` / `blackman` / `hanning` / `hamming` / `flattop` / `triangle` |
| `unit` | string | 縦軸単位。`vrms`(実効電圧)/ `db`(デシベル) |
| `mode` | string | 演算モード。`normal` / `average` / `maxhold` |
| `average_count` | int | 平均回数(`mode="average"` 用)。2〜1000 |
| `scale` | float | FFTトレースの縦軸スケール(1目盛あたり。単位は `unit` に従う) |
| `offset` | float | FFTトレースの縦軸オフセット(単位は `unit` に従う) |
| `freq_start_hz` | float | 表示する周波数範囲の開始(**Hz**) |
| `freq_end_hz` | float | 表示する周波数範囲の終了(**Hz**) |
| `search_enabled` | bool | 機器内蔵のピーク探索表のON/OFF。**ONのときだけ `get_math_state` が `peaks` を返す** |
| `search_num` | int | 探索するピーク本数。1以上(**上限はガイド抽出がページ跨ぎで欠落しているため置いていない** — 機器のクランプに委ね `applied` で見せる) |
| `search_threshold` | float | ピーク判定のしきい値(縦軸単位 = `unit`) |
| `search_excursion` | float | ピーク判定の振れ幅(縦軸単位 = `unit`) |
| `search_order` | string | ピーク表の並び順。`amplitude`(振幅順)/ `frequency`(周波数順) |

`filter` のキー(全て任意。ガイド3.16.31-3.16.33):

| キー | 型 | 説明 |
|---|---|---|
| `type` | string | フィルタ種別。`lowpass` / `highpass` / `bandpass` / `bandstop` |
| `w1_hz` | float | カットオフ周波数1(**Hz**) |
| `w2_hz` | float | カットオフ周波数2(**Hz**)。`bandpass` / `bandstop` では `w1_hz` < `w2_hz` であること(判定は機器側) |

返却: `channel` / `requested` / `applied`(read-back値。`fft` / `filter` を指定した場合は `applied["fft"]` / `applied["filter"]` にネストして返る)/ `changed`(呼び出し前後の `get_math_state` 相当が変化したか。ピーク表 `peaks` / `peak_warnings` は測定のたびに変わる動的値のため判定から除外する — 監査ログには完全なスナップショットが残る)。

動作・規範:

- **送信順は固定**: `display=true` を**最初**に、`display=false` を**最後**に送り、その間を `:OPERator` → `:SOURce1` → `:SOURce2` → `:LSOurce1` → `:LSOurce2` → `:FFT:*` → `:FILTer:*` → `:SCALe` → `:OFFSet` → `:INVert` の順で送る。根拠は**表示OFF中の書き込みがエラーなく無視される実機quirk**(表示OFFチャンネルへの `:SCALe`([verification/mho98-mvp.md](verification/mho98-mvp.md) 3.3)、AFGの `MOD:STATe` OFF中のパラメータ書き込み([verification/mho98-afg.md](verification/mho98-afg.md) 6章)と同族)への対策で、**効かせたい書き込みは表示ONの後・表示OFFは全部書き終えてから**という形にしてある。MATHでの同quirkの**実機確認は未実施**(順序はFakeScopeでテスト固定済み。[verification/mho98-math.md](verification/mho98-math.md) で確認する)。各項目は set → エラーキュー確認 → read-back(0.3節)
- **検証は全て送信前**に行う(1項目でも不正なら**1コマンドも送らずに** `INVALID_PARAMETER`。実機は不正トークン1発でSCPIサーバー全体が沈黙するため)。対象はMATHチャンネル番号、ソーストークンの形と範囲、列挙値、`average_count`(2〜1000)、`search_num`(1以上)、`fft` / `filter` の未知キー(`detail.allowed` に許容キーを返す)
- **MATHソースのカスケード則**: `source1` / `source2` / `fft.source` に別のMATHトレースを指定できるが、**自分より小さい番号だけ**(`MATH3` は `MATH1` / `MATH2` を読めるが、`MATH3` 自身や `MATH4` は読めない。ガイド3.16.3 / 3.16.4 の Remarks)。自己参照・上位参照は送信前に `INVALID_PARAMETER`。`REF<n>` の範囲は `ref_channels`、`lsource*` の `D<n>` の範囲は `digital_channels` capability が持つ
- **演算子とパラメータの結合制約は機器が強制する**(ホスト側では検証しない): 論理演算(`and` / `or` / `xor` / `not`)とFFTには `:SCALe` / `:OFFSet` が存在せず(ガイド3.16.7 / 3.16.8)、FFTは自前の `fft.scale` / `fft.offset` を持つ。演算子ごとの許容パラメータ表をホストに持たせると、ガイド未記載の組み合わせで正当な操作まで塞いでしまうため、**拒否は機器のエラーキューに委ね**、set直後のエラーキュー確認でそのまま拾う。**演算子とそのパラメータは同じ呼び出しで指定する**(送信順で `:OPERator` が先に出るため、1回の呼び出しで整合が取れる)
- **`applied` を信用し `requested` を信用しない**: 機器は範囲外の値をエラーなくクランプ・スナップすることがある(AFGの振幅で実測 — [verification/mho98-afg.md](verification/mho98-afg.md) 2章)。唯一信頼できる検出手段は read-back した `applied` との突合であり、Tool description でもLLMに `applied` を見るよう明示している
- **プロファイルゲート**: `math_channels` capability が未宣言なら**送信ゼロ**で `UNSUPPORTED_FEATURE`。演算子・FFT窓・FFT単位・FFTモード・探索順・フィルタ種別の各対応表(`math_operators` / `math_fft_windows` / `math_fft_units` / `math_fft_modes` / `math_fft_search_orders` / `math_filter_types`)も同様に、未宣言なら該当項目を送らず `UNSUPPORTED_FEATURE`([device-profiles.md](device-profiles.md) 2.1 / 2.2)。現在の宣言は `mho98.yaml` のみで、DHO800/900系は別ガイドの逐語解読が未了のため意図的に宣言していない。なお `:MATH<n>` の接頭辞に**方言キーは作っていない**(ファミリで分岐する実例が無いためドライバのハードコードとし、`math_channels` の宣言の不在をそのままゲートにしている)
- **意図的に非対応**(ガイド3.16に存在するが実装しない): `:FFT:HSCale` / `:FFT:HCENter`(`fft.freq_start_hz` / `fft.freq_end_hz` で表現できる別表現。AFGの `:PERiod` 恒久スキップと同じ原則)、`GRID` / `EXPand` / `RESet` / `WAVetype` / `SENSitivity` / `DISTance` / `THReshold`(論理演算のしきい値)/ `WINDow:TITLe?` / `LABel:SHOW` / `DISMode`

### `get_math_state` — READ_ONLY / Phase M1

MATH演算の現在設定を読む。**書き込みは一切行わず**、表示状態も変えない。

引数: `channel`(int、任意)。省略時は全MATHチャンネル。

返却: `channel` 指定時は1トレース分をフラットに返す。省略時は `{"channels": {"1": {...}, ..., "4": {...}}}`(キーはトレース番号の文字列。`get_afg_state` と同じ形)。

| キー | 説明 |
|---|---|
| `channel` / `display` / `operator` / `source1` / `source2` / `invert` | 常に返る |
| `scale` / `offset_v` | 論理演算・FFT**以外**の演算子のときだけ返る(ガイド3.16.7 / 3.16.8) |
| `lsource1` / `lsource2` | 論理演算(`and` / `or` / `xor` / `not`)のときだけ返る |
| `fft` | `operator="fft"` のときだけ返る。`configure_math` の `fft` と同じキー一式 |
| `peaks` | `operator="fft"` かつ `fft.search_enabled` が true のときだけ返る。各要素は `index` / `frequency_hz` / `amplitude` / `amplitude_unit` |
| `peak_warnings` | 解釈できないピーク行があったときだけ返る(自然文) |
| `filter` | `operator` がフィルタ系のときだけ返る。`type` / `w1_hz` / `w2_hz` |

動作・規範:

- **演算子に応じた条件付き読み取り**: 問い合わせ本数を抑えるためだけでなく、**実機未検証のサブツリーを不用意に突かない**ため(不正・未定義ヘッダ1発でSCPIサーバーが沈黙するため)、読むのは「その演算子で意味を持つ項目」だけに絞る。`add` のような算術演算ではFFT配下を1本も問い合わせない。1トレースあたりの問い合わせは共通部が5本(`display` / `operator` / `source1` / `source2` / `invert`)、算術・論理演算ではこれに2本を足して7本、FFT演算では19本(+ピーク表を読む場合1本)
- **`:DISPlay?` を最初に読む**。**MATH表示OFF時もクエリは沈黙しない**ことを実機で確認済み(MHO98 / fw 00.01.00 / 2026-08-27。MATH1〜4の全チャンネル)。短絡は不要だが、機種によって挙動が違った場合にここを短絡点にできるよう先頭のまま維持する
- **ピーク表は複数行応答**: `:MATH<n>:FFT:SEARch:RES?` は行を**改行で区切り、末尾に終端の空行を1本**返す(ピーク無し・探索OFFなら空行1本のみ)。`query()` は1行しか読まず、読み残しが以降の全クエリをdesyncさせる(実機で `ConnectionResetError` を観測)ため、**この応答だけは `Transport.query_lines()` で終端の空行まで読み切る**。`;` 区切りは実機に現れないがパーサ側では引き続き受理する
- **ピーク表は fail-open**: 各行を `5,6.50125MHz,-32.34dBV` 形式(ガイド3.16.30)としてパースし、**解釈できない行は例外にせず** `{"raw": "<元の行>"}` として残して `peak_warnings` に説明を積む。**周波数・振幅とも SI接頭辞を換算する**(周波数は `Hz` / `kHz` / `MHz` / `GHz`、振幅は実機実測の `851.6mVrms` → `amplitude=0.8516` / `amplitude_unit="Vrms"`)。ただし `dBV` / `dBm` の先頭 `d` はデシ接頭辞ではないため、**dB系は換算せず値も単位もそのまま**返す
- `channel` 省略時も、非対応機(`math_channels` 未宣言)では**1本だけ問い合わせて `UNSUPPORTED_FEATURE` を返させる**(空の `channels` を「正常」に見せない)
