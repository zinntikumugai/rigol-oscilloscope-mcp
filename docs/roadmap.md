# 今後の対応予定(MVP対象外)

**対象文書:** [Requirements.md](Requirements.md) 3.3 / 10.3 / 12章 の詳細
**位置づけ:** 本文書は規範(要件)ではなく予定・検討事項の記録。着手時に要件へ昇格させる

MVP(Phase 1 + 2 = Read Only + Basic Control)完了後に対応する機能と、判断を保留している検討事項をここに残す。旧要件定義 v0.1 に含まれていた将来機能の詳細も本文書へ移管した。

---

## 1. Phase 3 — Measurement Assistant(完了・要件へ昇格)

**同梱スキルで実現し完了**(2026-08-25)。信号種別10種の推奨設定表・ワークフロー・安全プロンプトは `skills/measurement-workflows/SKILL.md`、プラグイン構成は [Requirements.md](Requirements.md) 10.3、受入基準は同 11.3 を参照。

- サーバー側Tool `recommend_setup`([tools.md](tools.md) 8章)は**実装せず据え置き**。スキルで精度不足が実証された場合のフォールバックとして仕様のみ残す

## 2. Phase 4 — 機器の高度機能

### 2.1 シリアルプロトコルデコード(設定・結果取得とも完了・要件へ昇格)

**標準搭載6種(UART/RS232、I²C、SPI、CAN、LIN、パラレル)の設定Tool `configure_decode` と結果取得Tool `get_decode_result` を実装済み**([tools.md](tools.md) 6章、要件は [Requirements.md](Requirements.md) 3.2)。プロトコル別引数は `settings` オブジェクトで受け、対応表は機種プロファイルの `decode_protocols` と `driver/decode.py` が持つ。

残件:

- **イベントテーブルの列構成は観測しながら固めていく**: `:BUS<n>:DATA?` の列はプロトコル依存でガイドに記載が無く、実装はヘッダ行をそのまま採用する(スキーマを持たない)。実機で観測できた列構成は [verification/mho98-phase4.md](verification/mho98-phase4.md) に追記していく(RS232の `Time,Tx/Rx,Data,Error,` は実測済み)
- **バス無効時の `:DATA?` 挙動は未確認**: 現状は送信前に早期returnしているため実害はないが、観測できたら記録する
- **パラレルの `:BUS<n>:PARallel:WIDTh 1` が実機で拒否される**: 実機writeテストの復元fixtureがスナップショットを書き戻す際に `-200,"Command execute failed"` を受け、`test_configure_decode_uart_set_and_readback` がteardownでERRORになる(2026-08-27の実機実行で観測。M1/M2とは無関係 → [verification/mho98-m2.md](verification/mho98-m2.md) 7.1)。`WIDTh` の値域・他パラメータとの結合制約(現在のパラレル設定でこの幅が許されるか)を実機で切り分ける
- **オプション必須プロトコルは延期**: I2S、FlexRay、MIL-STD-1553、CAN-FD。検証機はMHO900-BND適用済み(2026-08-26、[verification/mho98-phase4.md](verification/mho98-phase4.md) 3章)のため実機検証の障害はなくなった。実ニーズが出たら着手する
- **将来ゲートは送信前に不要**: オプション必須ニモニックは沈黙せず値を返すと実測済み(ライセンス適用前後とも)のため、既存の「set → エラーキュー確認 → read-back」で機器自身のエラーを検出できる(実測根拠: [verification/mho98-unlicensed.md](verification/mho98-unlicensed.md) 4章)。ただし未ライセンス時のエラーキュー挙動には揺らぎがある(`-222` が積まれる場合と積まれない場合を観測)ため、未ライセンス判定は `:SYSTem:OPTion:STATus?` で行うこと
- `:BUS` コアはDHO/MHO共通と見られる(ガイド比較)。DHO実機を検証できたらファミリプロファイルへ引き上げる([device-profiles.md](device-profiles.md) 2.2)

### 2.2 Logic Analyzer

- D0〜D15のON/OFF、Threshold設定、Logic Capture、プロトコルデコード連携
- ロジックプローブの物理接続はMCPから確認できないため、`requires_physical_confirmation` の対象とする

### 2.3 Function / Arbitrary Waveform Generator (AFG)(完了)

MHO98は2ch・100 MHz・1 GSa/s のAFGを搭載する。実測根拠は [verification/mho98-afg.md](verification/mho98-afg.md)。

- **設定 `configure_afg` と状態取得 `get_afg_state` は実装済み**([tools.md](tools.md) 7章)。**出力状態には一切触れない**ため SAFE_WRITE / READ_ONLY。方言は機種プロファイルの `afg_prefix` / `afg_waveforms` / `afg_impedances` が持つ
- **出力制御 `enable_afg` / `disable_afg` も実装済み**(PR-AFG2)。**出力ONのみ DANGEROUS_WRITE**(confirmトークン必須。トークンはチャンネル単位)で、DUTへ信号を注入する操作であるため物理確認の促しをリスク文言とTool descriptionの双方に置く。**出力OFFは承認を要求しない**(緊急停止をブロックしないため SAFE_WRITE)
- capabilitiesの `afg_channels` で機種差を表現する(**範囲外の `:SOURce3` は実機のSCPIサーバーを沈黙させる**ため、番号検証は送信前に必須)
- **変調(AM/FM/PM、`configure_afg` の `modulation` 引数)・ARBファイル選択(`:LOAD:ARBitrary`、`arb_file` 引数)・位相同期(`:PHASe:SYNChronize`、新Tool `sync_afg_phase`)は実装済み**([tools.md](tools.md) 7章)。方言は機種プロファイルの `afg_mod_types` / `afg_mod_waveforms` が持つ(mho98のみ宣言、DHO800/900系は非宣言)
- 残件(恒久スキップ): `:PERiod` / `:VOLTage:HIGH`・`:LOW`(`frequency_hz` / `amplitude_vpp` + `offset_v` で表現可能な別表現のため不要)、DHO800/900ファミリの番号なし `:SOURce`(DGモジュール。別方言のため未宣言。実機検証できたら着手)
- **ループバックFFT・フィードバック検査・全13波形の実機検査は完了**(2026-08-26。[verification/mho98-afg.md](verification/mho98-afg.md) 5章)

### 2.4 ホスト側高度解析

オシロ本体でなくMCPホスト側(Python)で波形データを解析する構成。

- **統計(`stats`)とFFT(`fft`)は `analyze_waveform` として実装済み**([tools.md](tools.md) 5章)。生データを返さず要約数値だけを返す方針で、入力は `capture_waveform` と同じ波形取得経路(`service/waveform.py` の `read_samples`)を共有する
- 未実装の候補: THD、Jitter、Overshoot / Undershoot、Ringing、Noise、Signal Integrity。実測で必要性が出た時点で `analyses` の値を増やす形で追加する(Tool追加ではなく引数追加で済ませる)
- NumPy/SciPy: stdlibのみのradix-2 FFTで実装済み(131k点で約0.15秒、既定上限10万点なら0.1秒台)。解析が重くなったらoptional extras化を再検討する

### 2.5 内蔵機能の棚卸しと対応予定(2026-08-27)

MHO900 Programming Guide 3章の全28サブシステムを棚卸しし、未実装の内蔵機能から**対応予定6機能**を確定した(実装済み: Root / :CHANnel / :TIMebase / :TRIGger基本 / :MEASure / :WAVeform / :BUS / :SOURce / :DISPlay:DATA? / :SYSTem)。

**対応予定(優先度順):**

| Phase | 機能 | ガイド節 | 方針 |
|---|---|---|---|
| M1 | **:MATH<n>** 演算(加減乗除・FFT・微積分・フィルタ・論理) | 3.16 | **実装済み**(下記 2.5.1) |
| M2 | **:CURSor** カーソル測定 | 3.8 | **実装済み**(下記 2.5.2。manual/track。XYはモードのみ受理) |
| M2 | **:COUNter** 周波数カウンタ | 3.7 | **実装済み**(enable/source/mode + `:COUNter:CURRent?` 読み) |
| M2 | **:DVM** 電圧計 | 3.10 | **実装済み**(同上。`:DVM:CURRent?`) |
| M2 | **:HISTogram** ヒストグラム | 3.11 | **実装済み**(`:STATistics:RESult?` は `[Label:Value, …]` の1行) |
| M3 | **:REFerence** リファレンス波形 | 3.20 | 保存→比較ワークフロー。REF波形の読み出し可否は要確認 |

**見送り(再検討の条件つき):**

| 機能 | ガイド節 | 見送り理由 / 再検討の条件 |
|---|---|---|
| :MASK Pass/Fail試験 | 3.15 | 長時間自動試験の実ニーズが出たら(FAILed/PASSed/TOTal件数取得は有用) |
| :SEARch / :NAVigate | 3.22/3.23 | イベント検索の実ニーズが出たら |
| :RECord 波形録画/再生 | 3.19 | 状態機械が複雑。フレームナビゲーション需要が出たら |
| :BODeplot ボード線図 | 3.5 | ライセンスオプション+AFG連携が前提。ニーズが出たら |
| :QUICk / :LAN / :SAVE | 3.18/3.14/3.21 | ボタン割当・ネット設定・本体保存はMCPから触る価値薄 |

(:LA は 2.2 に既載のため本表から除外)

### 2.5.1 M1: MATH演算(実装完了・実機検証完了)

**`configure_math`(SAFE_WRITE)と `get_math_state`(READ_ONLY)を実装済み**([tools.md](tools.md) 11章)。ホスト側FFT(`analyze_waveform`)との棲み分けは当初の想定どおり — 機上FFTは「画面に出る(人間が確認できる)」「ピーク表がテキストで取れる(波形転送ゼロ)」点で別価値がある。

- **`configure_math`**: 演算子21種(加減乗除・論理4種・FFT・微積分系・デジタルフィルタ4種・AXB)、算術ソース(`source1` / `source2`)と論理ソース(`lsource1` / `lsource2`)、垂直(`scale` / `offset_v` / `invert`)、`fft` サブ辞書(入力ch・窓・単位・モード・平均回数・縦軸・表示周波数範囲・ピーク探索6項目)、`filter` サブ辞書(種別・W1・W2)。**送信順は表示ONが先頭・OFFが末尾**に固定(表示 OFF→ON 遷移で機器が縦軸を再計算するquirk対策。実測根拠は下記「実機検証」)。検証は全て送信前で、不正が1つでもあれば1コマンドも送らない
- **`get_math_state`**: 演算子に応じた条件付き読み取り(未検証サブツリーを突かない)。`:MATH<n>:FFT:SEARch:RES?` のピーク表は当初方針どおり**Toolを増やさず**返却の `peaks` に含める(解釈できない行は `raw` + `peak_warnings` で fail-open)
- **波形取得**: 当初の「`source` 引数拡張」ではなく**既存の `channel` 引数を拡張**する形で実現した(引数を増やさない)。`capture_waveform` / `analyze_waveform` が `"MATH1"`〜`"MATH4"` を受理し、`read_samples` 経路をそのまま流用する。FFT演算のトレースは `x_unit: "Hz"` / `frequency_step_hz` / `frequency_start_hz` を返して時間軸前提のキーを省き、`analyze_waveform` では**取得前に `INVALID_PARAMETER` で拒否**する(横軸が周波数のトレースに時間軸統計・ホスト側FFTは意味を持たないため)
- **プロファイル宣言**: capabilities に `math_channels` / `ref_channels`、dialect に `math_operators` / `math_fft_windows` / `math_fft_units` / `math_fft_modes` / `math_fft_search_orders` / `math_filter_types`(`mho98.yaml` のみ。[device-profiles.md](device-profiles.md) 2.1 / 2.2)。`:MATH<n>` はファミリ分岐の実例が無いため `math_prefix` 方言は作らず、`math_channels` の宣言の不在をそのままゲートにしている
- **`:WAVeform:SOURce MATH<n>` の可否は解決済み**: ガイド3.28.1に `{CHANnel1-4|MATH1-4}` と逐語で記載があり(`NORMal` モード限定 = 既定で使用しているモード)、M1最大のリスクだった「取得不能なら画面キャプチャ頼み」は回避できた

**意図的にスキップ(ガイド3.16にあるが実装しない):**

- `:FFT:HSCale` / `:FFT:HCENter` — `fft.freq_start_hz` / `fft.freq_end_hz` で表現できる別表現(AFGの `:PERiod` 恒久スキップと同じ原則)
- `GRID` / `EXPand` / `RESet` / `WAVetype` / `SENSitivity` / `DISTance` / `THReshold`(論理演算のしきい値)/ `WINDow:TITLe?` / `LABel:SHOW` / `DISMode` — 画面装飾・本体UI寄りの項目で、MCPから触る価値が薄い

**実機検証(2026-08-27、MHO98 / fw 00.01.00。記録: [verification/mho98-math.md](verification/mho98-math.md)):**

実測で**実装バグ3件**が判明し、いずれも修正済み(テスト付き):

1. **`:FFT:SEARch:RES?` は複数行応答**(改行区切り + 末尾に終端の空行。`;` は現れない)。`query()` が1行しか読まないため読み残しが以降の全クエリをdesyncさせ、実機で `ConnectionResetError` を観測した(再接続で回復)→ `Transport.query_lines()`(LAN / USB / Fake + Protocol)を追加し、ピーク表だけをその経路で読む
2. **ピーク表の振幅にSI接頭辞が付く**(`851.6mVrms`)。逐語保持では値が1000倍ずれていた → 周波数列と同じ換算を振幅列にも適用(`dBV` / `dBm` の先頭 `d` はデシ接頭辞ではないため除外)
3. **FFTプリアンブルのx軸が想定と違った**。xincrement は Hz/pt ではなく **GHz/pt**(`frequency_step_hz` = xincrement × 1e9。表示範囲3通りで `点数 × 刻み = 表示終端周波数` が厳密に一致)、**xorigin は開始周波数ではなく時間軸の値が残る** → `capture_waveform` は `frequency_step_hz` / `frequency_start_hz`(`:FFT:FREQuency:STARt?` から読む)を返し、時間軸前提のキーはFFTトレースでは返さない

併せて確定した事項: **MATH表示OFFでもクエリは沈黙しない**(4チャンネルで確認。`:DISPlay?` 先読みの短絡は不要)/ `:MATH1:OPERator?` のデフォルトは短形 `ADD`(写像は正しい)/ `:WAVeform:STARt` / `:STOP` はMATHソースでも有効(`max_points` が効く)。

**残件: なし**(検証項目 (a)〜(f) を全て実測で確定。実機テストのpytest実行も完了)

最後まで残っていた検証項目 (e)「表示OFF中の書き込み無視quirk」は **quirkなしで決着**した。**MATHには表示OFF中の書き込み無視は存在しない**(表示OFFでも書き込みは受理され read-back も一致する)ため、**AFG式の送信前拒否は追加しない**。代わりに別のquirkが見つかった: **表示を OFF → ON に戻した瞬間、機器が縦軸を再計算して書いた値を捨てる**(`scale=0.5` を表示OFFで書く → read-back `0.5` → 表示ON → `0.9446667`。エラーキューは `0,"No error"`)。再現性あり・再計算値は信号依存で、表示ONのまま放置しても値は動かない(4回読んで毎回同値)ため**遷移をトリガとする再計算**である。

含意は2つ:

- **現行の送信順(表示ONが先頭・OFFが末尾)はこの挙動に対しても正しい。** `configure_math(display=True, scale=X)` は表示ONを先に送るので、再計算が `X` の書き込みより**前**に起き `X` が生き残る。**この順序を後から「単純化」してはならない**
- **呼び出し側への注意:** MATH表示OFFの状態で書いた `scale` / `offset_v` は表示をONに戻した時点で破棄される。`display=True` と**同じ呼び出しで指定する**のが確実。`changed` 判定への影響は無い(連続ドリフトが無いため除外不要 — trackカーソルのYとは事情が違う)

追加した実機テストのpytest実行も完了した(`-m device`: 17 passed / 22 skipped、`-m device_write`: 16 passed / 1 failed / 1 error / 4 skipped。**M1ケースは全てPASS**。failed / error の2件はM1/M2と無関係な既知の事象 → 2.5.2 の残件2)。

### 2.5.2 M2: カーソル・周波数カウンタ・電圧計・ヒストグラム(実装完了・実機検証済み)

**Tool 6本を実装済み**([tools.md](tools.md) 12章): `configure_cursor` / `get_cursor_measurement` / `configure_meter` / `get_meter_value` / `configure_histogram` / `get_histogram_result`(いずれも SAFE_WRITE + READ_ONLY の対)。登録Tool数は30 → **36**。

- **`configure_cursor` / `get_cursor_measurement`**: モード(`off` / `manual` / `track` / `xy`)、manual専用の `type` / `source`、track専用の `source1` / `source2`、位置4点(`ax` / `ay` は秒・V)。**位置とソースは「今のモードのサブツリー」に属する**ため、`mode` 省略時は `:CURSor:MODE?` を1本読んで書き込み先を決め、サブツリー違いの指定は送信前に拒否する。読み値(A/B位置・ΔX・ΔY・1/ΔX)は別Toolで、`off` / `xy` では `mode` だけを返す(非活性のサブツリーを突かない)
- **`configure_meter` / `get_meter_value`**: カウンタと電圧計は「有効化 + ソース + モード + 現在値1本」で**同形**のため、Toolを2対に分けず `kind` 引数1つで切り替えた(当初の「実装コスト極小」の見立てどおり)。現在値は単位がモード依存なので、`value` に `unit`(`Hz` / `s` / `counts` / `V`)と設定一式を必ず添えて返す
- **`configure_histogram` / `get_histogram_result`**: 有効化・種別・ソース・表示高さ・集計ウィンドウ4値 + `reset`。統計は `raw`(生応答)を常に返し、`stats` に**機器自身のラベル**を正規化したキー → SI換算済みの数値(+ `<キー>_unit`)を載せる
- **プロファイル宣言**: capabilities に `cursor` / `frequency_counter` / `dvm` / `histogram`、dialect に `cursor_modes` / `cursor_types` / `counter_modes` / `dvm_modes` / `histogram_types`(`mho98.yaml` のみ。[device-profiles.md](device-profiles.md) 2.1 / 2.2)。M1と同じく**キーの不在がそのままゲート**

**当初の表の記述に対する訂正(実装時に判明):**

1. **カウンタの現在値は `:VALue` ではない。** `:COUNter` サブシステムに `:VALue` というニモニックは**存在しない**(実在するのは `:COUNter:CURRent?`。`:MEASure:COUNter:VALue?` は3.17の**別サブシステム**で、混同したもの)。**未定義ヘッダはクエリ1発でMHO98のSCPIサーバー全体を沈黙させる**(AGENTS.mdルール2)ため、この取り違えを実装まで持ち込んでいたら実機を止めていた。電圧計も同じく `:DVM:CURRent?`
2. **ヒストグラム統計は「統計テキスト」ではない。** 当初表が想定していたガイド引用の `[["92","1",…]]`(引用符付き入れ子リスト)は `:MEASure:HISTogram:STATistics:RESult?`(3.17.32)の書式で、`:HISTogram:STATistics:RESult?`(3.11.9)の実応答は**機器自身がラベルを持つ1行** `[Sum:30.37khits, Peaks:234hits, Max:1.562V, …, meanPlus3Sigma:1.000000]` だった(実測223バイト・改行1個・**終端の空行なし**)。したがってパーサはラベル駆動で、`query_lines()` ではなく `query()` で読む

**意図的にスキップ(ガイド3.7 / 3.8 / 3.10 / 3.11 にあるが実装しない):**

- `:CURSor:MANual:VUNit` — **ガイド本文がページ欠落で値域が不明**。確認できていないトークンを実機に送らない(AGENTS.mdルール2)
- `:CURSor:MANual:TUNit` — 値が `{SECond}` の**1つしかなく**、設定させる意味が無い
- `:CURSor:XY:*` — XY水平時間軸の対応が前提。`mode="xy"` は機器が持つ正当なモードなので受理するが、位置サブツリーは公開しない
- `:CURSor:MEASure:INDicator` — 画面のインジケータ表示のみで、測定値には影響しない
- `:HISTogram:SAVE:CSV` — **機器のストレージへファイルを書く**操作。本サーバーの保存先規約(実行ディレクトリ基準の許可ルート)の外にあり、操作クラスも別建てになる

**実機検証(2026-08-27、MHO98 / fw 00.01.00。記録: [verification/mho98-m2.md](verification/mho98-m2.md)):**

実測で**実装バグ3件**が判明し、いずれも修正済み(テスト付き。詳細は検証記録):

1. **無効な電圧計の `:DVM:CURRent?` は空応答**を返す。そのままパースすると `SCPI_ERROR`(機器故障に見える)になっていた → `:ENABle?` を先読みして短絡し `value: null` を返す
2. **ヒストグラム無効時の `:HISTogram:STATistics:RESult?` は `[]` を返しつつエラーキューに `-200` を積む**(沈黙はしない)。共有状態が汚れ、**次の無関係な書き込みのエラーキュー確認に化けて出て**いた → `:ENABle?` 先読みで統計クエリ自体を送らない
3. **統計応答に終端の空行が無い**。FFTピーク表と同じ `query_lines()` で読むと実機ではタイムアウトまで固まる → `query()` で1行読む

併せて確定した事項: trackモードでは**設定側の `CAY` / `CBY` も波形に追従して動く**(0.8秒間隔の4回読みで毎回変化)ため、`configure_cursor` の `changed` 判定はtrackモードのYを比較対象から外している / manualモードの `CAY` は動かない / 設定側の `:CAY?` と読み値の `:AYValue?` は**サンプル時点が違うため桁数も値も一致しない**。

**実機テストのpytest実行も完了**(`-m device`: 17 passed / 22 skipped、`-m device_write`: 16 passed / 1 failed / 1 error / 4 skipped。**M2ケースは全てPASS**・復元確認済み)。カーソルの往復は算術的にも整合した(`ax=-0.0002 s` / `bx=0.0002 s` → `xdelta_s=0.0004` / `ixdelta_hz=2500.0` で 1/ΔX が厳密一致)。ヒストグラム統計の**13ラベルとその並びは2回の独立した測定で同一**であり、`[Label:Value, …]` 1行という書式判定が再現した。

**残件(未解決):**

1. **有効化したカウンタの現在値が読めない(応答が run 間で一定しない)** — 生信号の載ったチャンネルでカウンタを有効化し2秒待っても `:COUNter:CURRent?` は `0` のままだった。**pytest実行時の追試では同条件で `value: None`**(空応答または番兵値 ±9.9E37)が返り、生ソケットでの `'0'` と食い違った。ゲート時間・整定時間・トリガ要件・ソース条件のいずれかが未特定。**実装側の対処は不要**(`0` も `None` もそのまま返すだけで、非ゼロを仮定した分岐はコードのどこにも無い)。次に試すこと: 整定時間を延ばす / ゲート時間設定の有無をガイドで再確認 / トリガがかかっている状態での再測定 / 別ソース(D0-D15含む)での比較
2. **実機テストの既知の失敗2件(M1/M2とは無関係・再診断不要)** — (a) `test_configure_decode_uart_set_and_readback` は復元fixtureが `:BUS1:PARallel:WIDTh 1` を送って `-200,"Command execute failed"` になりteardownでERROR(デコード経路はM1/M2で未変更 → 2.1の残件で追跡)、(b) `test_audit_log_records_every_write` は監査ログに `configure_afg` を要求するが AFG writeテストが安全ガード「AFG出力がONです」で自らskipするためFAILED(**機器のAFG出力がONである間だけ**再現する環境依存の失敗)
3. **カウンタ無効時の `:COUNter:TOTalize:ENABle OFF` が `-200,"Command execute failed"`** — 現在値と同じ値を書いても拒否される。送信順は `enabled` が先頭なので通常の `configure_meter(enabled=True, totalize_enabled=…)` では踏まないが、**無効なカウンタへ `totalize_enabled` だけを送る呼び出し**は失敗する。実機テストの復元fixtureもこれを踏むため、復元では例外を握り潰す扱いにしてある(理由はテスト内の日本語コメント)

## 3. プラグイン化(完了・要件へ昇格)

**完了**(2026-08-25)。Claude(`.claude-plugin/plugin.json`)・Codex(`.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json`)の両プラグインとして実装し、[Requirements.md](Requirements.md) 10.3 へ昇格した。スキル(`skills/measurement-workflows/SKILL.md`、Agent Skillsオープン標準)とMCP起動定義は両ホストで共有。旧v0.1のスキル素材(UART測定・Unknown Signal探索・安全プロンプト・反復上限)はすべてスキル本文へ吸収済み。

**残タスク: Codex CLIでの実動作確認**(公式ドキュメント準拠で作成、実CLI未確認)。確認対象は Requirements.md 10.3 の未検証事項2点(`mcpServers` の相対パス指定、マーケットプレイスsource `path: "./"`)と、`codex plugin marketplace add zinntikumugai/rigol-oscilloscope-mcp` → install → スキル発見・MCPサーバー起動の通し。なおCodexはMCPサーバー単体なら `config.toml`([Requirements.md](Requirements.md) 10.2)、スキル単体なら `~/.agents/skills/` へのコピーでもプラグインなしで利用できる。

## 4. 機種プロファイルの拡充

- MHO98で未検証の項目の実機確認([device-profiles.md](device-profiles.md) 3.1、[verification/mho98-mvp.md](verification/mho98-mvp.md) 4章): 50Ωニモニック、autoset書き込み(ニモニック :AUToset はサブツリーの読み取りプローブで確認済み — mho98-autoset.md。実行自体が未検証)、RAWモード波形、limits境界値(RUN/STOP/SINGleはMVPで実機確認済み)
- **USB(USBTMC)接続の実機検証** — ユニットテスト(`tests/test_usb_transport.py`、PyVISAのフェイク)は通っているが実機未検証。VISAリソース文字列の推奨形式もここで確定させる
- **表示OFFチャンネルへの書き込みが無視される件への対策検討**([verification/mho98-mvp.md](verification/mho98-mvp.md) 3.3): 表示OFFのCHへ `:SCALe` / `:OFFSet` を送るとエラーなく無視される。`configure_channel` で自動的に `enabled=True` にするか、requested/applied の不一致を警告として返すに留めるか、要検討(暗黙に表示をONにするのは利用者の画面を勝手に変える副作用でもある)
- MHO98以外の対応機種の追加: **DHO800/900系をガイドベースプロファイルとして追加済み**(`dho800.yaml` / `dho900.yaml`、信頼度 `guide` = 公式プログラミングガイドの逐語解読のみで実機未検証。[device-profiles.md](device-profiles.md) 6章)。実機が用意でき次第、(1) quirk・limitsを実測して `verified` へ昇格、(2) 現在スコープ外のデコード / AFG / LA / オプション照会の宣言を追加する(issue #19)。他機種(DS/MSO系など)は引き続き実機が用意でき次第
- ファミリプロファイルの括り出し(同系2機種以上の検証が揃った段階で)
- **`raw_scpi` Tool は未実装**(configの `RIGOL_MCP_RAW_SCPI` は将来用の予約。[tools.md](tools.md) 9章の仕様で実装する際に使用する)

## 5. 検討事項(方針未定)

| 項目 | 現状の判断 | 再検討の条件 |
|---|---|---|
| PyPI公開 | 当面しない(GitHubからuvx起動) | 利用者が増え、バージョン固定・供給の信頼性が必要になったら |
| 複数台同時接続 | 非対象(単一アクティブ接続) | 複数台運用の実ニーズが出たら。全Toolへの `device_id` 波及が必要 |
| 機器自動探索(mDNS / VXI-11 discovery) | 非対象 | 接続先入力の手間が問題になったら |
| ネットワークMCP(HTTP/SSE、認証、TLS) | 非対象(stdioローカルのみ) | リモート利用の実ニーズが出たら。認証・ACL設計を伴う |
| MCP Resource(`rigol://state` 等) | 見送り(ホスト側サポートが不均一) | 主要ホストのResource対応が安定したら |
| READ_ONLY操作の並列化 | 見送り(全SCPIを直列化) | 複数クライアントからの読み取り需要が出たら |
| Windows対応 | 対象外(macOS / Linux) | 利用者からの要望が出たら。実装は pathlib 等でOS非依存に書いてあるが、動作確認とパス周り(許可ルート検証・ドライブレター・パス区切り)の検証が必要 |
| TMCブロック長の上限cap | なし(最大~1GBを宣言どおり受信) | 悪意ある機器を想定するなら64MB程度のsanity capを `parse_block` に(2026-08のセキュリティ監査ノート。DoSのみで攻撃価値が低いため見送り) |
| 許可ルートからの `/tmp` 除外 | `/tmp` を常に許可(画像のみ書き込み可) | world-readableな場所への保存が問題になったら `tempfile.gettempdir()` のみに絞る(同監査ノート) |
| 配布ピンのSHA化 | gitタグ(`@v0.1.0`)固定 | タグは付け替え可能なため、改ざん耐性を上げるならコミットSHA固定。エコシステム慣行とのバランスで現状はタグ(同監査ノート) |
