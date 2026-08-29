# 機種プロファイル仕様

**対象文書:** [Requirements.md](Requirements.md) 4章の詳細仕様
**実測根拠:** [verification/mho98-phase0.md](verification/mho98-phase0.md)

本システム `rigol-oscilloscope-mcp` は、Rigolオシロスコープの機種ごとの個体差(SCPI方言、対応機能、パラメータ範囲)を**機種プロファイル**で吸収する。プロファイルはコードから分離した宣言的データ(パッケージ同梱のYAML)とし、新機種対応をプロファイル追加だけで完結できることを目標とする。

---

## 1. プロファイル解決

接続確立時に `*IDN?` の応答からプロファイルを解決する。

```text
*IDN? → "RIGOL TECHNOLOGIES,<model>,<serial>,<firmware>"
   │
   ├─ 1. モデル完全一致プロファイル   (例: MHO98)        … 信頼度 verified
   ├─ 2. ファミリプロファイル         (実機検証2機種〜)  … 信頼度 family
   ├─ 3. ガイドベースプロファイル     (例: DHO800/900)   … 信頼度 guide
   └─ 4. 汎用Rigolプロファイル        (フォールバック)   … 信頼度 generic
```

| 信頼度 | 根拠 | 意味 |
|---|---|---|
| `verified` | 実機検証 | quirk・制限値がプロファイルに記録されている |
| `family` | 同系列機種の実機検証 | ニモニックは概ね互換だが未検証項目あり |
| `guide` | 公式プログラミングガイドの逐語解読のみ | **実機未検証**。ニモニックはガイド記載どおりだが、quirk(不正ニモニック時の沈黙挙動、scale値のスナップ有無、NR3の指数桁数)と limits の境界値は**未確認**。宣言範囲外の機能は `UNSUPPORTED_FEATURE` に倒す(6章) |
| `generic` | なし(共通部分のみ) | 未知のRigol機種。共通性の高いSCPIコマンドのみでベストエフォート動作 |

同じモデル文字列に複数のプロファイルが一致した場合は、この表の上から順(verified → family → guide → generic)に優先される。

解決したプロファイル名と信頼度は `scope_identify` / `get_capabilities` の返却に必ず含め、generic時はdegraded動作であることをLLMへ明示する。

製造者が `RIGOL TECHNOLOGIES` でない機器へ接続した場合は警告を返す(接続自体は拒否しない。返却に `unsupported_vendor: true` を含める)。

## 2. プロファイルの構成要素

プロファイルは以下の3ブロックからなる。ファミリ/汎用プロファイルからの**継承**(差分定義)を可能とする。

### 2.1 capabilities — 機能有無と構成

| 項目 | 例 (MHO98) | 用途 |
|---|---|---|
| `analog_channels` | 4 | channel引数の検証 |
| `digital_channels` | 16 | 将来のLA対応 |
| `afg_channels` | 2 | 信号発生チャンネル数(`channel` 引数の検証。**範囲外の `:SOURce3` は実機を沈黙させる**) |
| `math_channels` | 4 | MATH演算トレース本数(`:MATH<n>`)。`configure_math` / `get_math_state` の `channel` 引数と `capture_waveform` の `"MATH<n>"` ソースの検証に使う。**未宣言ならMATH操作自体を行わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) |
| `ref_channels` | 10 | リファレンス波形の枠数。用途は2つ: (1) `configure_math` の `source1` / `source2` / `fft.source` に指定された `"REF<n>"` の範囲検証(未宣言ならREFソース指定が `INVALID_PARAMETER`)、(2) `configure_reference` / `get_reference_state` の `ref` 引数の範囲検証。**未宣言(または0)ならリファレンス操作自体を行わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) |
| `bandwidth_hz` | 実機依存 | 参考情報 |
| `protocol_decode` | true | デコード対応(`configure_decode` / `get_decode_result` の可否) |
| `decode_buses` | 4 | デコードバス本数(`bus` 引数の検証。未宣言ならデコード自体を行わない) |
| `waveform_download` | true | capture_waveform 可否 |
| `screenshot` | true | capture_screenshot 可否 |
| `measurements` | 対応測定項目リスト | measure の項目検証 |
| `impedance_control` | true | `:CHANnel<n>:IMPedance` の可否(問い合わせ含む) |
| `impedance_50ohm` | true | 50Ω設定の可否 |
| `cursor` | true | カーソル測定(`:CURSor`)の可否。`configure_cursor` / `get_cursor_measurement` が使う。**未宣言ならカーソル操作自体を行わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) |
| `frequency_counter` | true | 周波数カウンタ(`:COUNter`)の可否。`configure_meter(kind="counter")` / `get_meter_value(kind="counter")` が使う。**未宣言ならカウンタ操作自体を行わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) |
| `dvm` | true | 電圧計(`:DVM`)の可否。`configure_meter(kind="dvm")` / `get_meter_value(kind="dvm")` が使う。**未宣言なら電圧計操作自体を行わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) |
| `histogram` | true | 波形ヒストグラム(`:HISTogram`)の可否。`configure_histogram` / `get_histogram_result` が使う。**未宣言ならヒストグラム操作自体を行わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) |

未対応機能のToolが呼ばれた場合、実機へコマンドを送らず `UNSUPPORTED_FEATURE` を返す(理由は4.2参照)。

### 2.2 dialect / quirks — SCPI方言と実機挙動の癖

| 項目 | 内容 | MHO98での実測値 |
|---|---|---|
| `measurement_items` | 意味的測定名 → SCPIニモニック対応表 | `vavg` → `VAVG`(`VAVerage` は**不可**、無応答+`-222`) |
| `measurement_clear` | Resultビューの全測定項目を消すコマンド(未宣言 = 送信ゼロで `UNSUPPORTED_FEATURE`) | mho98: `":MEASure:DELete"`。**DHO800/900系はガイド上 `":MEASure:CLEar"`**(ニモニックがファミリで分岐。MHO98実機は両方受理 → [verification/mho98-measure-clear.md](verification/mho98-measure-clear.md)) |
| `trigger_types` | `:TRIGger:MODE` の値(種別名 → トークン)。**宣言した種別しか書き込めない** — 未宣言は送信ゼロで `UNSUPPORTED_FEATURE` | rigol-generic: `edge` のみ(全シリーズ共通)。mho98: 標準11種(ガイド3.27.1)。**罠**: `window` のトークンは `WINDow`(単数)だがサブツリーは `:WINDows`、`setup_hold` は `SETup` / `:SHOLd` |
| `trigger_slopes` / `trigger_edge_slopes` / `trigger_polarities` | エッジの向き / 極性。**同じ `POSitive`・`NEGative` でも意味が違うので分ける**(polarity=パルスの正負、slope=エッジの向き) | mho98: `trigger_slopes` は両エッジ `RFALl` を含む3値、他は2値 |
| `trigger_window_slopes` | `:TRIGger:WINDows:SLOPe` の値 | mho98: `rising` / `falling` の2値のみ。**ガイドの Range欄(`RFALI`)と Remarks欄(`RFALl`)で綴りが割れているため両エッジは宣言しない** |
| `trigger_pulse_when` / `trigger_duration_when` / `trigger_runt_when` / `trigger_delay_types` | 条件(WHEN)の値。**種別ごとに値域が違うので表を分ける**(共有すると値域外を送る) | mho98: 3〜4値(ガイド3.27.9.3 / 3.27.13.3 / 3.27.15.3 / 3.27.17.5) |
| `trigger_slope_windows` / `trigger_pattern_levels` / `trigger_duration_types` / `trigger_window_positions` / `trigger_shold_types` / `trigger_shold_patterns` | 各種別固有の列挙 | mho98のみ宣言 |
| `measure_areas` | `:MEASure:AREA` の値(`main` / `zoom` / `cursor`)。未宣言 = `configure_measurement` が送信ゼロで `UNSUPPORTED_FEATURE` | mho98: 3種(ガイド3.17.19)。**DHO系は実機未検証のため意図的に不在** |
| `measure_threshold_types` | `:MEASure:THReshold:TYPE` の値 | mho98: `percent` → `PERCent` / `absolute` → `ABSolute`(3.17.17) |
| `measure_amp_types` | `:MEASure:AMP:TYPE` の値 | mho98: `auto` → `AUTO` / `manual` → `MANual`(3.17.28) |
| `measure_amp_methods` | `:MEASure:AMP:MANual:TOP`・`BASE` の値(両者で共通) | mho98: `histogram` → `HISTogram` / `maxmin` → `MAXMin`(3.17.29/30) |
| `measure_statistic_types` | `:MEASure:STATistic:ITEM?` の `<type>`。未宣言 = `get_measurement_statistics` が送信ゼロ | mho98: 6種(3.17.8)。省略時はこの全種別を読む |
| `autoset_command` | オートセットアップの実行コマンド(未宣言 = 送信ゼロで `UNSUPPORTED_FEATURE`) | mho98: `":AUToset"`(DHO系も同じ。旧世代DS1000Z等は `:AUToscale`)。`:AUToscale` はMHO900では `:SYSTem:AUToscale`(AUTOキー有効化)しか存在しない → [verification/mho98-autoset.md](verification/mho98-autoset.md) |
| `screenshot_command` | スクリーンショット取得コマンドと引数形式 | `:DISPlay:DATA?`(引数なし、PNG返却) |
| `screenshot_timeout_s` | 画像転送に与えるタイムアウト猶予(秒)。未宣言ならコード既定 30 | 30(通常問い合わせの5秒では約97KBの転送が間に合わない) |
| `bwlimit_on` | 帯域制限「入」に送る値。未宣言なら帯域制限の設定自体を `UNSUPPORTED_FEATURE` とする | `20M`(選択肢 OFF/20M/100M/250M は要実機確認) |
| `waveform_preamble` | プリアンブル解釈規約 | `yreference=128`、`volts=(raw-yorigin-yref)*yinc`。**yorigin はプロファイルに持たない**(定数ではなくチャンネルoffsetの生カウント換算で決まる動的値。実測: offset −0.064 V のとき `yorigin=-9.0`)。変換には必ずライブの `:WAVeform:PREamble?` の値を使う |
| `nr3_quirks` | 数値応答の非標準形式 | 指数部1桁(`1.000000E+1`)を許容すること |
| `snaps_to_125` | scale設定値の1-2-5スナップ有無 | **false**(3 V/div、0.3 ms/div がそのまま適用される) |
| `invalid_query_behavior` | 不正ニモニック送信時の挙動 | **無応答**(クライアント側タイムアウト)+ エラーキューに `-100,"Command err"` |
| `error_queue_stale` | 接続時にエラーキューが前セッションの残留で汚染されうるか | true(接続時drain必須) |
| `option_query` | 導入済みオプションの照会コマンド。**未宣言ならオプション照会自体を行わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) | `:SYSTem:OPTion:STATus?`(`:VALid?` は後方互換形。応答は `0` / `1`) |
| `option_types` | 意味的なオプション名 → `<type>` トークン対応表。ここに載るトークンだけを送る | `bundle: BND` / `afg_50mhz: AFG50` / `memory_500mpts: RLU-05` ほか計11個 |
| `decode_protocols` | 意味的プロトコル名 → `:BUS<n>:MODE` の値。**未宣言なら `configure_decode` 自体を行わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) | `uart: RS232` / `i2c: IIC` / `spi: SPI` / `can: CAN` / `lin: LIN` / `parallel: PARallel` の6種 |
| `decode_formats` | デコード表示形式の対応表(`:BUS<n>:FORMat`) | `hex: HEX` / `ascii: ASCii` / `dec: DEC` / `bin: BIN` |
| `afg_prefix` | 信号発生のコマンド接頭辞(`{n}` がチャンネル番号。`{n}` を含まないテンプレート=番号なし方言も可)。**未宣言なら `configure_afg` / `get_afg_state` 自体を行わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) | `:SOURce{n}`(MHO900は2ch・番号付き)。dho900は `:SOURce`(番号なし・1ch) |
| `afg_presence_query` | ジェネレータ搭載有無の実行時照会(宣言時のみ、最初のAFG操作前に1回・接続中キャッシュ。`0` なら送信ゼロで `UNSUPPORTED_FEATURE`) | dho900: `:SYSTem:DGSTatus?`(S型のみ搭載のため) |
| `afg_waveforms` | 意味的な波形名 → `:SOURce<n>:FUNCtion` の値。ここに載るトークンだけを送る | `sine: SINusoid` / `square: SQUare` / `ramp: RAMP` / `noise: NOISe` / `dc: DC` / `arb: ARB` / `exp_rise: EXPRise` ほか計13種(**`PULSe` は実機に存在しない** — 送ると `-222`) |
| `afg_impedances` | 信号発生器の出力インピーダンス対応表(`:SOURce<n>:IMPedance`) | `highz: OMEG` / `50: FIFTy`(問い合わせの返却は `OMEG` / `FIFTy`) |
| `afg_mod_types` | 変調タイプの対応表(`:SOURce<n>:MOD:TYPe`)。**未宣言なら `modulation` 引数自体を扱わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) | `am: AM` / `fm: FM` / `pm: PM`(変調ソースは内蔵のみ。`EXTernal` は存在しない) |
| `afg_mod_waveforms` | 変調波形の対応表(`:MOD:<TYPE>:INTernal:FUNCtion`) | `sine: SINusoid` / `square: SQUare` / `triangle: TRIangle` / `upramp: UPRamp` / `dnramp: DNRamp` / `noise: NOISe` |
| `math_operators` | 意味的な演算子名 → `:MATH<n>:OPERator` の値。ここに載るトークンだけを送る。**未宣言なら `operator` 引数自体を扱わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) | `add: ADD` / `subtract: SUBTract` / `multiply: MULTiply` / `divide: DIVision` / `and: AND` / `or: OR` / `xor: XOR` / `not: NOT` / `fft: FFT` / `integrate: INTG` / `differentiate: DIFF` / `sqrt: SQRT` / `log10: LG` / `ln: LN` / `exp: EXP` / `abs: ABS` / `lowpass: LPASs` / `highpass: HPASs` / `bandpass: BPASs` / `bandstop: BSTop` / `axb: AXB` の21種(ガイド3.16.2) |
| `math_fft_windows` | FFTの窓関数の対応表(`:MATH<n>:FFT:WINDow`)。**未宣言なら `fft.window` を送らず `UNSUPPORTED_FEATURE`**(送信ゼロ) | `rectangle: RECTangle` / `blackman: BLACkman` / `hanning: HANNing` / `hamming: HAMMing` / `flattop: FLATtop` / `triangle: TRIangle`(ガイド3.16.15) |
| `math_fft_units` | FFTの縦軸単位の対応表(`:MATH<n>:FFT:UNIT`)。**未宣言なら `fft.unit` を送らず `UNSUPPORTED_FEATURE`**(送信ゼロ) | `vrms: VRMS` / `db: DB`(ガイド3.16.16) |
| `math_fft_modes` | FFTの演算モードの対応表(`:MATH<n>:FFT:MODE`)。**未宣言なら `fft.mode` を送らず `UNSUPPORTED_FEATURE`**(送信ゼロ) | `normal: NORMal` / `average: AVERage` / `maxhold: MAXHold`(ガイド3.16.17) |
| `math_fft_search_orders` | FFTピーク探索の並び順の対応表(`:MATH<n>:FFT:SEARch:ORDer`)。**未宣言なら `fft.search_order` を送らず `UNSUPPORTED_FEATURE`**(送信ゼロ) | `amplitude: AMPorder` / `frequency: FREQorder`(ガイド3.16.29) |
| `math_filter_types` | デジタルフィルタ種別の対応表(`:MATH<n>:FILTer:TYPE`)。**未宣言なら `filter.type` を送らず `UNSUPPORTED_FEATURE`**(送信ゼロ) | `lowpass: LPASs` / `highpass: HPASs` / `bandpass: BPASs` / `bandstop: BSTop`(ガイド3.16.31) |
| `cursor_modes` | カーソルのモードの対応表(`:CURSor:MODE`)。**未宣言なら `mode` 引数自体を扱わない**(`UNSUPPORTED_FEATURE`、送信ゼロ) | `off: OFF` / `manual: MANual` / `track: TRACk` / `xy: XY`(ガイド3.8.1)。`xy` は水平時間軸がXYのときのみ有効で、**`:CURSor:XY:*` サブツリー自体はM2スコープ外**(YAML 1.1 では裸の `off` が真偽値になるため引用符が必須) |
| `cursor_types` | manualカーソルの種別の対応表(`:CURSor:MANual:TYPE`)。**未宣言なら `type` を送らず `UNSUPPORTED_FEATURE`**(送信ゼロ) | `time: TIME` / `amplitude: AMPLitude`(ガイド3.8.2)。`TUNit` / `VUNit` は**宣言しない**(`VUNit` はガイド本文がページ欠落で値域不明、`TUNit` は値が `{SECond}` の1つのみ) |
| `counter_modes` | 周波数カウンタの測定モードの対応表(`:COUNter:MODE`)。**未宣言なら `mode` を送らず `UNSUPPORTED_FEATURE`**(送信ゼロ) | `frequency: FREQuency` / `period: PERiod` / `totalize: TOTalize`(ガイド3.7.4) |
| `dvm_modes` | 電圧計の測定モードの対応表(`:DVM:MODE`)。**未宣言なら `mode` を送らず `UNSUPPORTED_FEATURE`**(送信ゼロ) | `ac_rms: ACRMs`(DC成分を除いた実効値)/ `dc: DC`(平均)/ `dc_rms: DCRMs`(実効値)(ガイド3.10.4) |
| `histogram_types` | ヒストグラム種別の対応表(`:HISTogram:TYPE`)。**未宣言なら `type` を送らず `UNSUPPORTED_FEATURE`**(送信ゼロ) | `horizontal: HORizontal` / `vertical: VERTical`(ガイド3.11.3) |
| `reference_colors` | リファレンス波形の表示色の対応表(`:REFerence:COLor`)。**未宣言なら `color` を送らず `UNSUPPORTED_FEATURE`**(送信ゼロ) | `gray: GRAY` / `green: GREen` / `blue: BLUE` / `red: RED` / `orange: ORANge`(ガイド3.20.7)。**実機の応答はガイド記載と違う** — 緑はガイドが `GRE` と書くのに対し実機は `GREE` を返す(工場出荷の枠4・枠9が緑 → [verification/mho98-m3.md](verification/mho98-m3.md) 4章)。**枠数のゲートは `ref_channels`**(2.1)で、`:REFerence` は枠番号をコマンド引数で取るため接頭辞の方言キー(`math_prefix` / `afg_prefix` に相当するもの)は置かない |

`:SYSTem:OPTion:*` は**MHO900専用**でDHO800/900のガイドには存在しない。したがって `option_query` / `option_types` は `mho98.yaml` にのみ宣言し、`rigol-generic.yaml` には置かない — **キーの不在がそのままゲート**である(4.2の原則の適用例)。`*OPT?` はRigolオシロ全シリーズで未定義ヘッダのため使わない。`<type>` リスト外のトークン(実測: `AUTOA`)でもSCPIサーバーが沈黙するので、`option_types` に載っていないトークンを送ってはならない。実測根拠は [verification/mho98-unlicensed.md](verification/mho98-unlicensed.md)(未ライセンス状態でも `AFG50` / `RLU-05` は `1` を返す)。

`decode_protocols` に載せるのは**標準搭載の6種のみ**。I2S(`IIS`)/ FlexRay / MIL-STD-1553(`M1553`)/ CAN-FD(`:BUS<n>:CAN:FDBaud`)はライセンスオプション必須のため意図的に載せず、**不在がそのままゲート**になる(未宣言のプロトコルは送信前に `UNSUPPORTED_FEATURE`)。実測根拠は [verification/mho98-unlicensed.md](verification/mho98-unlicensed.md) 3章(未ライセンス状態でも RS232/IIC/SPI/CAN/LIN/PAR は全て正常応答)。

**AFGの機種差(`afg_*` を MHO98 にしか宣言しない理由):** MHO900シリーズの信号発生は**番号付きの `:SOURce<n>`**(n=1,2)だが、DHO800/900の内蔵ジェネレータは**番号なしの `:SOURce`** で、しかも別売のDGモジュール前提(有無は `:SYSTem:DGSTatus?` で照会する)という**別物の方言**である。共通化せず、検証済み機種のプロファイルにだけ宣言する(不在=非対応のゲート)。実測根拠は [verification/mho98-afg.md](verification/mho98-afg.md)。

**MATHに `math_prefix` 方言キーを作らない理由:** AFGの `afg_prefix` と違い、`:MATH<n>` は**ファミリで分岐している実例が知られていない**(MHO900・DHO800/900のいずれのガイドでも番号付き `:MATH<n>`)。そのため接頭辞はドライバ側で `f":MATH{n}"` とハードコードし、**対応可否のゲートは `math_channels` capability の宣言の有無だけ**が担う(不在=非対応、送信ゼロで `UNSUPPORTED_FEATURE`)。実在しない方言キーを先回りで作らないのが本仕様の原則であり、接頭辞が分岐するファミリが実際に現れた時点で `math_prefix` を導入する。演算子・FFT・フィルタの各対応表(`math_*` の6キー)は方言として持つ(トークンの綴りはガイド逐語で確認したものだけを載せる)。実装は現在 `mho98.yaml` にのみ宣言しており、DHO800/900系は別ガイドの逐語解読が未了のため意図的に未宣言(6.2)。

なお `:BUS` のコア(`:MODE` / `:DISPlay` / `:FORMat` / `:THReshold` と主要プロトコル配下)は、プログラミングガイドを比較する限り **DHO800/900 とMHO900で共通**である。ただし現時点で実機検証済みなのはMHO98だけなので `mho98.yaml` にのみ宣言する。DHO系の実機が1台でも検証できたら、共通部分をファミリプロファイルへ引き上げる(1章の3層解決)。

LAN raw socket ポート(既定 5555)はプロファイル外で扱う。プロファイルは接続後のIDN照合で確定するため、接続設定(connect の引数 / config)の担当([Requirements.md](Requirements.md) 4.3、[tools.md](tools.md) connect)。

### 2.3 limits — パラメータ範囲

| 項目 | 内容 |
|---|---|
| `vertical_scale` | チャンネル感度の最小/最大 (V/div) |
| `vertical_offset` | オフセット範囲(scale依存の場合はその規則) |
| `timebase_scale` | 掃引レンジの最小/最大 (s/div) |
| `trigger_level` | トリガレベル範囲(scale/offset依存の規則) |
| `probe_ratio` | 許容プローブ比のリスト |
| `memory_depth` | 選択可能なメモリ深度 |

limits が未定義の項目は、(1) 保守的なデフォルト範囲で検証し、(2) 可能なら設定後のread-backで実機の受理結果を確認する(適用値の返却は全機種共通の必須動作。[Requirements.md](Requirements.md) 7章)。

## 3. 検証済みプロファイル: MHO98

Phase 0 実機検証([verification/mho98-phase0.md](verification/mho98-phase0.md)、firmware 00.01.00)に基づく最初の verified プロファイル。プロファイル仕様の実例を兼ねる。

```yaml
# profiles/mho98.yaml (イメージ)
model: MHO98
match: "^MHO9[0-9]"        # IDNモデル文字列の一致規則(完全一致系)
inherits: rigol-generic
confidence: verified

capabilities:
  analog_channels: 4
  digital_channels: 16
  afg_channels: 2
  protocol_decode: true
  waveform_download: true
  screenshot: true
  impedance_50ohm: true
  measurements:
    [frequency, period, vpp, vmax, vmin, vavg, rms, duty, rise_time, fall_time]

dialect:
  screenshot_command: ":DISPlay:DATA?"
  measurement_items:
    frequency: FREQuency
    period: PERiod
    vpp: VPP
    vmax: VMAX
    vmin: VMIN
    vavg: VAVG          # VAVerage は不可(実測)
    rms: VRMS
    duty: PDUTy         # 返却単位は比率(0.5002 = 50.02%)
    rise_time: RTIMe
    fall_time: FTIMe
  waveform_preamble:
    # yorigin は定数ではなく設定依存の動的値(= offset / yincrement の生カウント換算。
    # 実測: offset -0.064 V で yorigin=-9.0)のためプロファイルには持たせない。
    # 電圧変換には必ずライブの :WAVeform:PREamble? の値を使うこと。
    yreference: 128
  nr3_single_digit_exponent: true
  snaps_to_125: false
  invalid_query_behavior: silent_timeout   # + error queue に -100
  error_queue_stale_on_connect: true

limits:
  probe_ratio: [0.001, 0.01, 0.1, 1, 10, 100, 1000, 10000]   # 要実機確認
  # vertical_scale / timebase_scale 等は未検証 → 実機read-backフォールバック
```

### 3.1 MHO98で未検証の項目

以下はプロファイル上「未検証」として扱い、該当操作時は実機read-backに依存する。実機確認が取れ次第プロファイルへ反映する。

- 50Ω設定ニモニック(`FIFT` 想定、リスクが高く未実施)
- `RUN` / `STOP` / `SINGle` / `AUToset` の書き込み動作
- RAWモード波形ダウンロード、チャンク上限、大容量メモリ深度
- パラメータ範囲の境界値(limits全般)
- Bandwidth Limit の設定値、offset / position の設定コマンド

## 4. quirkから導かれる全機種共通の規範

以下はMHO98の実測から導かれたが、**プロファイル依存でなく全機種共通の動作原則**として [Requirements.md](Requirements.md) 7章に定める。

### 4.1 接続時エラーキューdrain

エラーキューは前セッションの残留エラーで汚染されうる(実測: 接続直後に `-222` が残留)。接続確立時、`:SYSTem:ERRor?` を空(`0,"No error"`)になるまで読み捨ててから運用を開始する。

### 4.2 未確認ニモニックを送信しない

MHO98では不正なクエリに対し機器が**無応答**となり、クライアントはタイムアウト(既定5秒)まで待たされ、さらにエラーキューが汚染される。つまり「送って試す」のコストが極めて高い。

したがって、プロファイル(dialect / capabilities)で確認されていないニモニックは実機へ送信せず、Tool呼び出しの時点で `UNSUPPORTED_FEATURE` を返す。generic プロファイルでは共通性の高いコマンド群のみを「確認済み」として扱う。

### 4.3 requested / applied の両値返却

機器がscale値を1-2-5にスナップするかは機種依存であり、MHO98はスナップしない(3 V/div がそのまま適用された)。設定系Toolは set → エラーキュー確認 → read-back を必須とし、**要求値(requested)と実際の適用値(applied)を両方返す**。LLMは applied を後続の判断に使う。

## 5. プロファイルの追加・保守

- プロファイルはパッケージ同梱の `profiles/*.yaml` とし、リリースに含めて配布する
- 新機種の対応手順: (1) generic で接続し動作確認 → (2) 実機検証結果を quirk として記録 → (3) verified プロファイルを追加
- ガイドしか根拠が無い段階では `confidence: guide` で登録し、宣言はガイドで逐語確認できたニモニックだけに絞る(6章)
- ファミリプロファイル(例: DHO900系)は、同系機種の検証結果が2機種以上揃った段階で共通部分を括り出して作成する(先回りで作らない)
- プロファイルの適用結果(名前・信頼度)は監査ログに記録する

## 6. ガイドベースプロファイル: DHO800 / DHO900 / DHO1000 / DHO4000

出典は公式プログラミングガイド(DHO800/900: **PGA39106-1110** / DHO1000/4000: **PGA34101-1110**)のみで、**実機は未検証**(`confidence: guide`)。ガイドに逐語で載っているニモニックだけを宣言し、実機挙動に依存する項目は宣言しない。シリーズ別の対応状況一覧は [compatibility.md](compatibility.md)、新規プロファイルの作成手順は [profile-authoring.md](profile-authoring.md)。

| プロファイル | `match` | 継承 | 対象機種 |
|---|---|---|---|
| `dho800` | `^DHO8[0-9]{2}` | `rigol-generic` | DHO802 / DHO804 / DHO812 / DHO814 |
| `dho900` | `^DHO9[0-9]{2}` | `dho800` | DHO914 / DHO914S / DHO924 / DHO924S(LA D0-D15) |
| `dho1000` | `^DHO1[0-9]{3}` | `rigol-generic` | DHO1072 / 1074 / 1102 / 1104 / 1202 / 1204(50Ω非対応) |
| `dho4000` | `^DHO4[0-9]{3}` | `dho1000` | DHO4204 / 4404 / 4804(50Ωあり — guideプロファイルで唯一RESTRICTED_WRITEの50Ω確認フローが有効) |

`mho98` の `match` は `^MHO9[0-9]` であり先頭文字が違うため、DHO9xx とは衝突しない。

### 6.1 宣言する範囲(ガイドで確定できたもの)

- コアの読み取り・チャンネル/タイムベース/トリガ設定・アクイジション(全機種共通のニモニック)
- `screenshot_command: ":DISPlay:DATA? PNG"` — ガイド3.9.7の `<type>`={BMP|PNG|JPG} は**既定がBMP**なので、MHO98と違い**PNG引数が必須**
- `measurement_clear: ":MEASure:CLEar"` — ガイド3.17.3(MHO900の `:MEASure:DELete` とはニモニックが分岐する)
- `autoset_command: ":AUToset"` — ガイド3.2.1(MHO900と同じ)
- `measurement_items` 10項目 — ガイド3.17.2。`VAVG` の綴りもMHO900と同一
- `bwlimit_on: "20M"` — ガイド3.6.1(選択肢は `OFF|20M` のみ)
- `limits.probe_ratio` 24値 — ガイド3.6.8。**要実機検証**

quirk系(`invalid_query_behavior` / `error_queue_stale_on_connect` / `snaps_to_125` / `nr3_single_digit_exponent`)は実機で観測していないため独自宣言せず、`rigol-generic` の保守的な既定を継承する。

### 6.2 意図的に宣言しない範囲(実機検証まで据え置き)

以下は**キーの不在がそのままゲート**(4.2の原則)で、DHO機ではToolが `UNSUPPORTED_FEATURE` を返し実機へは1バイトも送らない。実機で1台でも検証できた時点で宣言を追加し、`verified` へ昇格させる。

- **シリアルデコード**(`protocol_decode: false`、`decode_protocols` / `decode_formats` / `decode_buses` 未宣言)
- **AFG**(`afg_channels: 0`、`afg_prefix` ほか未宣言)。DHO914S/924S は1chのジェネレータを内蔵するが、DHO系は**番号なしの `:SOURce`** でMHO900の `:SOURce<n>` とは別方言
- **MATH演算**(`math_channels` / `ref_channels` / `math_*` の方言6キーとも未宣言)。DHO系ガイドの `:MATH<n>` 章はまだ逐語解読していないため、演算子トークンの集合が同一である保証が無い
- **カーソル・周波数カウンタ・電圧計・ヒストグラム**(`cursor` / `frequency_counter` / `dvm` / `histogram` と `cursor_modes` / `cursor_types` / `counter_modes` / `dvm_modes` / `histogram_types` の方言5キーとも未宣言)。M1と同じ理由でDHO系ガイドの該当章(3.7 / 3.8 / 3.10 / 3.11 相当)が未解読
- **ロジックアナライザ**(`digital_channels` は情報として持つがLA操作Tool自体が未実装)
- **オプション照会**(`:SYSTem:OPTion:*` はMHO900専用でDHOのガイドに存在しない)
- **50Ω入力**(`impedance_control: false`。DHO800/900の入力は1MΩ固定)

### 6.3 既知の制限

DHO802 / DHO812 は**2ch(+EXT)**だが、プロファイルスキーマは「同一 `match` 内での機種別ch数」を表現できないため `analog_channels: 4` を宣言している。範囲外のCH3/CH4への操作は機器側の拒否に委ねる(モデル別プロファイルを増やすより、実機検証時に判断する)。
