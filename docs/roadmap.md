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
- ~~**パラレルの `:BUS<n>:PARallel:WIDTh` が実機で拒否される問題**~~ → **解消(2026-08-28)**: 原因は「データソースが User のときのみ有効」というガイド3.4.10.4 の Remark([verification/mho98-phase4.md](verification/mho98-phase4.md) 5章)。`settings.parallel` に `bus`(ガイド3.4.10.1 逐語の11トークン)を追加し、**送信順を「表の並び」に固定**して `bus` が `bus_width` より先に届くようにした。あわせて `:PARallel:BITX` + `:PARallel:SOURce` の対を `bit_sources`(添字=ビット番号のリスト)として公開している([tools.md](tools.md) 6章)。`bus="user"` との結合はホスト側では検証せず機器の自己申告に任せる(既定方針)。実機writeテストの復元fixtureは `bus_width` / `bit_sources` を「Userへ入れて書き戻す → 本来の `bus` へ戻す」の2段で扱い、**復元が完全になった**([verification/mho98-m2.md](verification/mho98-m2.md) 7.1 の残件も解消)
- **オプション必須プロトコルは延期**: I2S、FlexRay、MIL-STD-1553、CAN-FD。検証機はMHO900-BND適用済み(2026-08-26、[verification/mho98-phase4.md](verification/mho98-phase4.md) 3章)のため実機検証の障害はなくなった。実ニーズが出たら着手する
- **将来ゲートは送信前に不要**: オプション必須ニモニックは沈黙せず値を返すと実測済み(ライセンス適用前後とも)のため、既存の「set → エラーキュー確認 → read-back」で機器自身のエラーを検出できる(実測根拠: [verification/mho98-unlicensed.md](verification/mho98-unlicensed.md) 4章)。ただし未ライセンス時のエラーキュー挙動には揺らぎがある(`-222` が積まれる場合と積まれない場合を観測)ため、未ライセンス判定は `:SYSTem:OPTion:STATus?` で行うこと
- `:BUS` コアはDHO/MHO共通と見られる(ガイド比較)。DHO実機を検証できたらファミリプロファイルへ引き上げる([device-profiles.md](device-profiles.md) 2.2)。**ただし I2C のアドレス指定(`:IIC:ADDBits` ↔ `:IIC:ADDRess`、意味も違う)と LIN の `:BAUD`(DHOには無い)は方言差がある** — [2.6.6](#266-機種横断で判明した方言差プロファイル拡充の材料)

### 2.2 Logic Analyzer

- D0〜D15のON/OFF、Threshold設定、Logic Capture、プロトコルデコード連携
- ロジックプローブの物理接続はMCPから確認できないため、`requires_physical_confirmation` の対象とする
**2026-08-28の再評価: 読めないのは「生サンプル列」だけで、機能そのものは孤立していない。**

- **デジタル波形の点列転送は不可** — `:WAVeform:SOURce` の値域はガイド3.28.1に `{CHANnel1-4|MATH1-4}` と逐語で書かれており D0-D15 を含まない。`:LA` 配下(3.13)も `ENABle` / `ACTive` / `AUTosort` / `DIGital:ENABle` / `DIGital:LABel` / `POD<n>:DISPlay` / `POD<n>:THReshold` / `SIZE` の8コマンドのみで `DATA?` 相当が無い
- **一方、D0〜D15 を「ソース」として受理するコマンドは既存実装の各所にある。** `:MEASure:ITEM`(3.17.2)の Remarks は逐語で *"After the logic probe is connected, the available sources also include digital channels (D0-D15)."* と書いており、`configure_decode`(`:BUS<n>:PARallel:SOURce` / `:RS232:TX`・`RX` / `:IIC:SCLK`・`SDA` / `:SPI` 各ソース / `:CAN:SOURce` / `:LIN:SOURce`)・`measure`・`configure_meter`(`:COUNter:SOURce`)・`configure_math`(`:MATH<n>:LSOurce1/2`)・`configure_reference` が該当する。**結果はデコード表・測定値としてテキスト/数値で読める**
- **とくに `bus_width > 4` のパラレルデコードはアナログ4chでは成立せず、`:LA` の有効化が事実上の前提になっている。** 2.1 で露出済みの `bus_width`(`driver/decode.py` は 1〜16 を受理、プリセットに `d15_d0`)を実際に使うには `:LA:ENABle` / `:LA:DIGital:ENABle` / `:LA:POD<n>:THReshold` が要る
- したがって**格下げの根拠は無い**。残る不確定はロジックプローブの物理接続確認のみで、`:SYSTem:MODules?` で判定できる可能性がある([2.6.5](#265-実機検証で先に潰す不明項目) の V-2)
- 機種差: DHO900系は `Digital Channel Commands` として搭載するが、**DHO800系とDHO1000/4000系は非搭載**

### 2.3 Function / Arbitrary Waveform Generator (AFG)(完了)

MHO98は2ch・100 MHz・1 GSa/s のAFGを搭載する。実測根拠は [verification/mho98-afg.md](verification/mho98-afg.md)。

- **設定 `configure_afg` と状態取得 `get_afg_state` は実装済み**([tools.md](tools.md) 7章)。**出力状態には一切触れない**ため SAFE_WRITE / READ_ONLY。方言は機種プロファイルの `afg_prefix` / `afg_waveforms` / `afg_impedances` が持つ
- **出力制御 `enable_afg` / `disable_afg` も実装済み**(PR-AFG2)。**出力ONのみ DANGEROUS_WRITE**(confirmトークン必須。トークンはチャンネル単位)で、DUTへ信号を注入する操作であるため物理確認の促しをリスク文言とTool descriptionの双方に置く。**出力OFFは承認を要求しない**(緊急停止をブロックしないため SAFE_WRITE)
- capabilitiesの `afg_channels` で機種差を表現する(**範囲外の `:SOURce3` は実機のSCPIサーバーを沈黙させる**ため、番号検証は送信前に必須)
- **変調(AM/FM/PM、`configure_afg` の `modulation` 引数)・ARBファイル選択(`:LOAD:ARBitrary`、`arb_file` 引数)・位相同期(`:PHASe:SYNChronize`、新Tool `sync_afg_phase`)は実装済み**([tools.md](tools.md) 7章)。方言は機種プロファイルの `afg_mod_types` / `afg_mod_waveforms` が持つ(mho98のみ宣言、DHO800/900系は非宣言)
- 残件(恒久スキップ): `:PERiod` / `:VOLTage:HIGH`・`:LOW`(`frequency_hz` / `amplitude_vpp` + `offset_v` で表現可能な別表現のため不要)、DHO800/900ファミリの番号なし `:SOURce`(DGモジュール。別方言のため未宣言。実機検証できたら着手)
- **ループバックFFT・フィードバック検査・全13波形の実機検査は完了**(2026-08-26。[verification/mho98-afg.md](verification/mho98-afg.md) 5章)。**issue #13(AFGのフィードバック検証)はこの検査で満たされており、残件は無い — クローズしてよい**

### 2.4 ホスト側高度解析

オシロ本体でなくMCPホスト側(Python)で波形データを解析する構成。

- **統計(`stats`)とFFT(`fft`)は `analyze_waveform` として実装済み**([tools.md](tools.md) 5章)。生データを返さず要約数値だけを返す方針で、入力は `capture_waveform` と同じ波形取得経路(`service/waveform.py` の `read_samples`)を共有する
- 未実装の候補: THD、Jitter、Overshoot / Undershoot、Ringing、Noise、Signal Integrity。実測で必要性が出た時点で `analyses` の値を増やす形で追加する(Tool追加ではなく引数追加で済ませる)
- NumPy/SciPy: stdlibのみのradix-2 FFTで実装済み(131k点で約0.15秒、既定上限10万点なら0.1秒台)。解析が重くなったらoptional extras化を再検討する

### 2.5 内蔵機能の棚卸しと対応予定(2026-08-27)

MHO900 Programming Guide 3章の全28サブシステムを棚卸しし、未実装の内蔵機能から**対応予定6機能**を確定した(棚卸し時点の実装済み: Root / :CHANnel / :TIMebase / :TRIGger基本 / :MEASure / :WAVeform / :BUS / :SOURce / :DISPlay:DATA? / :SYSTem)。

**その6機能は M1 / M2 / M3 で全て実装・実機検証まで完了した**(2026-08-28)。以降の追加は下表「見送り」からの再検討か、実機で新たに判明した残件(2.5.1〜2.5.3 の各「残件」)になる。

**対応予定6機能(当初の優先度順。全て実装済み):**

| Phase | 機能 | ガイド節 | 方針 |
|---|---|---|---|
| M1 | **:MATH<n>** 演算(加減乗除・FFT・微積分・フィルタ・論理) | 3.16 | **実装済み**(下記 2.5.1) |
| M2 | **:CURSor** カーソル測定 | 3.8 | **実装済み**(下記 2.5.2。manual/track。XYはモードのみ受理) |
| M2 | **:COUNter** 周波数カウンタ | 3.7 | **実装済み**(enable/source/mode + `:COUNter:CURRent?` 読み) |
| M2 | **:DVM** 電圧計 | 3.10 | **実装済み**(同上。`:DVM:CURRent?`) |
| M2 | **:HISTogram** ヒストグラム | 3.11 | **実装済み**(`:STATistics:RESult?` は `[Label:Value, …]` の1行) |
| M3 | **:REFerence** リファレンス波形 | 3.20 | **実装済み**(下記 2.5.3。保存→比較ワークフロー。**REF波形の読み出しは不可**と確定 — `:WAVeform:SOURce` の値域外。MATHの減算で代替する) |

**見送り(再検討の条件つき):**

| 機能 | ガイド節 | 見送り理由 / 再検討の条件 |
|---|---|---|
| :MASK Pass/Fail試験 | 3.15 | 長時間自動試験の実ニーズが出たら(FAILed/PASSed/TOTal件数取得は有用) |
| :SEARch / :NAVigate | 3.22/3.23 | イベント検索の実ニーズが出たら |
| :RECord 波形録画/再生 | 3.19 | 状態機械が複雑。フレームナビゲーション需要が出たら |
| :BODeplot ボード線図 | 3.5 | ライセンスオプション+AFG連携が前提。ニーズが出たら |
| :QUICk / :LAN / :SAVE | 3.18/3.14/3.21 | ボタン割当・ネット設定・本体保存はMCPから触る価値薄 |

(:LA は 2.2 に既載のため本表から除外)

> **この見送り表は 2026-08-28 に一部改訂された。** `:SEARch` と `:MASK` は「AI調査効率」の観点で**「高」へ格上げ**、`:NAVigate` は `:SEARch` と分離して見送り維持、`:RECord` は保留(実機検証待ち)。根拠は [2.6.7](#268-25見送り表への訂正)。

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
2. **実機テストの既知の失敗2件(M1/M2とは無関係・再診断不要)** — (a) `test_configure_decode_uart_set_and_readback` の復元fixtureが `:BUS1:PARallel:WIDTh 1` で `-200,"Command execute failed"` になる件は **2.1 で解消済み**(`bus` の公開と2段復元)、(b) `test_audit_log_records_every_write` は監査ログに `configure_afg` を要求するが AFG writeテストが安全ガード「AFG出力がONです」で自らskipするためFAILED(**機器のAFG出力がONである間だけ**再現する環境依存の失敗)
3. **カウンタ無効時の `:COUNter:TOTalize:ENABle OFF` が `-200,"Command execute failed"`** — 現在値と同じ値を書いても拒否される。送信順は `enabled` が先頭なので通常の `configure_meter(enabled=True, totalize_enabled=…)` では踏まないが、**無効なカウンタへ `totalize_enabled` だけを送る呼び出し**は失敗する。実機テストの復元fixtureもこれを踏むため、復元では例外を握り潰す扱いにしてある(理由はテスト内の日本語コメント)

### 2.5.3 M3: リファレンス波形(実装完了・実機検証済み)

**Tool 2本を実装済み**([tools.md](tools.md) 13章): `configure_reference`(SAFE_WRITE)/ `get_reference_state`(READ_ONLY)。登録Tool数は36 → **38**。

- **`configure_reference`**: 枠1〜10、`source`(CH / MATH / D0-D15)、垂直(`scale` / `offset_v`)、`color`、`label`、`label_display`、それに一発動作の `save` / `reset`。**送信順は `reset` → 設定 → `save`** に固定した。`reset` を先に置くのは「既定へ戻す」が同じ呼び出しの `scale` / `offset_v` を後から潰さないため、`save` を最後に置くのは保存が**その時点のソースの波形**を焼き込む操作でソース選択が先に届いている必要があるため
- **`get_reference_state`**: `ref` 指定で1枠、省略で全10枠(1枠6クエリ・全枠60クエリ)。**枠にデータが入っているかは返せない**(機器に問い合わせるコマンドが無い)
- **枠番号はコマンド引数**(`:REFerence:VSCale 10,0.5`)で、`:MATH<n>` / `:BUS<n>` のようにニモニックへ埋め込む形ではない。接頭辞にファミリ分岐の余地が無いため `math_prefix` / `afg_prefix` に相当する方言キーは作らず、`ref_channels` の宣言の不在をそのままゲートにしている
- **プロファイル宣言**: capabilities の `ref_channels` は**M1で宣言済み**のものをそのまま使い(MATHソースの `REF<n>` 範囲検証と共用)、dialect に `reference_colors` を追加した([device-profiles.md](device-profiles.md) 2.1 / 2.2)
- **`save` は不可逆**: 枠の内容は上書きされ、元に戻す手段も「入っているか」を問い合わせる手段も無い。confirmトークンの対象にはしていない(取り込みにも出力にも触れないため操作クラスは SAFE_WRITE)が、**Tool description と 13章の双方に「送る前に人間へ確認せよ」と明記**した。実機writeテストでも保存だけは追加の環境変数 `RIGOL_TEST_ALLOW_REF_SAVE=1` でゲートしている

**当初の表の記述に対する決着:**

- **「REF波形の読み出し可否は要確認」→ 読み出しは不可と確定。** `:WAVeform:SOURce` の値域はガイド3.28.1に `{CHANnel1-4|MATH1-4}` と逐語で書かれており `REF<n>` を含まない。代替として **MATHの減算経由**(`configure_math(operator="subtract", source1="CH1", source2="REF1")` → `capture_waveform("MATH1")`)を13章に実用ワークフローとして記載した。`:MATH<n>:SOURce` は `REF1`〜`REF10` を受理する(ガイド3.16.3)

**意図的にスキップ(ガイド3.20にあるが実装しない):**

- **`:REFerence:CURRent`** — **前面パネルで「今どの枠を操作対象にしているか」という選択状態**を設定するコマンド。`:REFerence` の他のコマンドは**全て枠番号を明示的な引数で取る**ため、この選択状態に依存するものが1つも無い。MCPから触っても本体UIのフォーカスが動くだけで、設定にも表示にも波形にも影響しない

**実機検証(2026-08-28、MHO98 / fw 00.01.00。記録: [verification/mho98-m3.md](verification/mho98-m3.md)):**

**実装バグ1件が判明し修正済み(テスト付き)。これがM3最大の収穫である:**

- **`:REFerence:COLor?` の緑はガイド記載の `GRE` ではなく `GREE` が返る。** 列挙値の照合が短形式・長形式の2形しか受理していなかったため、**工場出荷状態(枠4・枠9が緑)の実機で `get_reference_state` が丸ごと落ちていた**。SCPI規格上、機器は短形式以上・長形式以下の**任意の略形**で応答してよく、ガイドの Return Format 欄はあてにならない → **共有の列挙マッチャを「短形式の長さ以上の前置一致を受理、候補が複数なら推測せず `SCPI_ERROR`」に変更**した。これは decode / AFG / MATH / cursor / counter / meter / histogram / reference の**全列挙が通る共通経路**で、**同時に潜在バグ1件を塞いだ** — 旧実装の逐語dictでは、別々の値の短形/長形が同じトークンになる表(例: `TIMe` と `TIMeout` はどちらも短形式 `TIM`)で**後勝ちで片方が黙って消えて**いた。現行の全テーブルを走査した限り実際に衝突する組は無く**現時点では発火していない**が、集合表現+曖昧なら `SCPI_ERROR` に変えて将来の追加に備えた

併せて確定した事項:

- **ガイドの Remark「現在有効なチャンネルのみソースに選べる」はこのファームでは成り立たない**(CH4の表示をOFFにしてから `:REFerence:SOURce 1,CHANnel4` を送るとエラー無しで受理され `CHAN4` が読み戻る)→ ホスト側では表示状態を検証しない
- **工場出荷状態の全10枠は `source=CHAN1` / `vscale=5.000000E-2` / `voffset=0.000000` で同一**、色は ORAN → RED → BLUE → GREE → GRAY の**5色を巡回**(枠 n は (n−1) mod 5 の色)、ラベルは `REF<n>`、`:LABel:ENABle?` は `0`
- **ラベルは引用符なしで返る**(`TESTLBL`)。**全10枠の読み取りでエラーキューは終始 `0,"No error"`**(沈黙なし)

**実機テストのpytest実行も完了**(`-m device`: **18 passed**、`-m device_write`: リファレンスの往復 + reset順序の**2件 passed**、`RIGOL_TEST_ALLOW_REF_SAVE=1` を足すと保存を含む**3件 passed**。監査ログに `configure_reference` の記録を確認)。

**残件(未解決):**

1. **保存済み波形の無い枠では `:REFerence:RESet` が効かないように見える** — 一度も `:SAVE` していない枠1に `VSCale 1,0.5` / `VOFFset 1,0.2` を書いてから `:RESet 1` を送ったところ、**エラーは積まれないまま値も戻らなかった**(既定の `5.000000E-2` / `0.000000` ではなく書いた値のまま)。**観測は1件で、保存の有無が条件だと確定したわけではない**。フェイク機器(`testing/`)にはこの挙動をモデル化していない(条件が不確定なため)。呼び出し側は `applied`(read-back値)を見る前提なので実装側の対処は不要
2. **枠にデータが入っているかを知る手段が無い** — 機器にクエリが存在しないため、`save` の不可逆性を利用者に判断させるしかない。ガイド3.20章を再読しても該当コマンドは見当たらない。**本体の画面を撮る(`capture_screenshot`)以外の確認手段は現状ない**

### 2.6 AI調査効率による優先度の再編(2026-08-28)

**位置づけ:** §2.5 の棚卸しは「機器の内蔵機能を上から順に潰す」順で優先度を付けていた。本節はそれを**「AIエージェントが回路調査を自力で進められるか」の一点**で付け直したもの。§2.5 の判断を一部覆す(2.6.7)。

**やり直した棚卸しの方法:** 公式プログラミングガイド3本(MHO900 / DHO800-900 / DHO1000-4000)のPDFしおりから3章の全コマンド索引を機械生成し、全サブシステムを現行38 Toolと1行ずつ突き合わせた。規模は MHO900 = **28サブシステム / 686コマンド**、DHO800/900 = 28 / 591、DHO1000/4000 = 29 / 634。突き合わせ結果は118行の対応表になり、**実装済み20 / 部分実装6 / 未実装87 / 意図的スキップ5**(機能単位)。

**最大の発見: 穴は未実装サブシステムではなく、実装済みサブシステムの内側にあった。**

| 実装済みに見えるサブシステム | 実態 |
|---|---|
| **`:TRIGger`** | トリガ種別**20種中EDGEの1種のみ**。PULSe / TIMeout / DURation / RUNT / SHOLd / PATTern / DELay / NEDGe / WINDows / SLOPe / VIDeo と、シリアルバストリガ8種(RS232/IIC/SPI/CAN/LIN/FLEXray/IIS/M1553)が全て未実装 |
| **`:MEASure`** | `:ITEM` の**41トークン中10のみ**プロファイル宣言。加えて `:MEASure:STATistic`(統計)、`:MEASure:AREA`(測定区間の限定)、**`:MEASure:COUNter`(3.17.25-27)と `:MEASure:HISTogram`(3.17.31-32)が丸ごと未実装**。後2者は実装済みの `:COUNter`(3.7)/ `:HISTogram`(3.11)とは**別サブシステム**なので取り違えないこと([2.5.2](#252-m2-カーソル周波数カウンタ電圧計ヒストグラム実装完了実機検証済み) の訂正記録) |
| **`:ACQuire`** | `:SRATe?` / `:MDEPth` の**読みのみ**。取得モード(Normal / Peak Detect / Average / High Resolution)・平均回数・ADC分解能の設定が未実装 |
| **`:SYSTem`** | `:ERRor?` / `:OPTion:STATus?` のみ。**`:DATE` / `:TIME` が未実装(issue #10)** |

この4つは**新Toolをほとんど増やさず既存Toolの引数追加で届く**。一方、§2.5 で「見送り」にした `:SEARch` / `:MASK` は新Toolが要るが、**AIが画面を見ずに異常を数え上げられる**という他に代えがたい価値がある(2.6.7)。

#### 2.6.1 優先度「高」— 測る → 捕まえる → 見つける

**層1「測る」— AIが読み取れる数値を増やす**

| 機能 | 代表SCPI | AIにとって何が嬉しいか | 実装コスト |
|---|---|---|---|
| **測定項目の全面拡張(41項目中10 → 全項目)** | `:MEASure:ITEM` の未宣言31トークン | オーバーシュート(`OVERshoot`)・プリシュート(`PREShoot`)・面積(`MARea` / `MPARea`)・スルーレート(`PSLewrate` / `NSLewrate`)・位相/遅延の8項目・パルス幅(`PWIDth` / `NWIDth`)が**測れない**ため(立上り/立下り時間 `RTIMe` / `FTIMe` は宣言済み)、今は波形を全点転送してホスト側で計算するしかない。プロファイル宣言を増やすだけで転送ゼロの数値測定になる。<br>**内訳: 時間系 5/10・振幅系 5/15・面積スルーレート系 0/4・位相遅延系 0/8・カウント系 0/4。後ろの3群は丸ごと未実装**なので、そこから足すのが最も効率がよい | **小**(`measurement_items` への宣言追加 + `measure` の値域拡大) |
| **測定統計の取得** | `:MEASure:STATistic:ITEM`(有効化)/ `:MEASure:STATistic:ITEM? <type>,<item>` | 最大/最小/現在値/平均/**標準偏差**(`DEViation`)/回数(`CNT`)が取れる。「たまに出る異常」の存在証明が波形転送ゼロで済み、目視のレンジ確認をジッタ・電圧変動の数値判定に置き換えられる。**`<type>` は1つずつ指定する形式で、5種欲しければ5クエリ**(いずれも科学表記の単一値でパースは容易) | **中**。下記の注意を参照 |

> **`measure` への引数追加では済まない。** 統計を読む前に **set 形 `:MEASure:STATistic:ITEM <item>` で統計機能を有効化する必要があり、これは書き込み**である。`measure` は `TOOL_CLASSES` で **READ_ONLY** なので、READ_ONLY Toolに書き込みを混ぜることになり AGENTS.mdルール5 に反する。**有効化(SAFE_WRITE)と読み取り(READ_ONLY)を別Toolに分ける**前提で見積もること。
| **測定区間の限定** | `:MEASure:AREA`(MAIN/ZOOM/CURSor)+ `:MEASure:CREGion:*` | 「この区間だけ測る」がSCPIだけで指定でき、関心外のノイズを測定値から除外できる | **中**(新Tool 1本 または `measure` の引数追加) |

> **`:MEASure:AREA ZOOM` は遅延掃引の有効化が前提**(ガイド3.17.19 Remarks: *"only when you enable the delayed sweep function first, can Zoom be enabled"*)。遅延掃引(`:TIMebase:DELay:*`)は下記「低」に置いているので、**ZOOM を使うなら先にそちらが要る**。`CURSor` は実装済みの `configure_cursor` と連動する。
>
> **「関心区間だけを扱う」手段が3つあるので混同しないこと。** ①**`:MEASure:AREA`** = **測定値**を区間で絞る(本行。優先度「高」)。②**`:WAVeform:STARt` / `:STOP`** = **転送する波形点**を絞る(**実装済み**。`max_points` として露出)。③**遅延掃引(`:TIMebase:DELay:*`)** = **画面表示**を拡大する(優先度「低」。①②で足りるため)。AIが数値を得たいなら①、波形データを絞りたいなら②で、③は人間が画面を見るための機能。
| **測定しきい値・振幅算出方式の設定** | `:MEASure:SETup:MAX/MID/MIN`、`:MEASure:THReshold:*` | 立上り時間やパルス幅の測定値が**なぜその値なのか**をAIが制御できる。層1の他項目の精度前提 | **中** |
| **取得モードの制御** | `:ACQuire:TYPE` / `:AVERages` / `:BITS` | ノイズが乗った信号にAverage、見逃したくない過渡にPeak Detect、分解能が要るならHigh Resolution を**AIが自分で選べる**。今は選べない | **小**(`configure_timebase` か新規 `configure_acquisition`) |

> **`:ACQuire:BITS`(ADC分解能)は機種差がある。** MHO900 と DHO1000/4000 には在るが、**DHO800/900 には無い**(代わりに `:ACQuire:HRESolution`)。**方言キーで宣言し、不在ならゲートすること**(AGENTS.mdルール2)。`:ACQuire:TYPE` / `:AVERages` / `:MDEPth` / `:SRATe?` は3系統とも共通。

**層2「捕まえる」— 目的の現象を機器側で捕捉させる**

現行は EDGE トリガのみ。以下はいずれも「人間なら波形を眺めて探す作業」を機器のハードウェアに肩代わりさせるもので、**AIの調査効率に最も効く**。

| トリガ種別 | 代表SCPI | 捕まえられる現象 |
|---|---|---|
| **パルス幅** | `:TRIGger:PULSe` | グリッチ、規定幅を外れたパルス |
| **タイムアウト** | `:TRIGger:TIMeout` | 信号が来なくなった(無応答・ハングアップ) |
| **デュレーション** | `:TRIGger:DURation` | 状態が想定より長引く / 短すぎる |
| **セットアップ&ホールド** | `:TRIGger:SHOLd` | デジタルタイミング違反そのもの |
| **ラント** | `:TRIGger:RUNT` | 振幅が足りないパルス(反射・ノイズ由来の信号品質異常) |
| **ディレイ** | `:TRIGger:DELay` | 2信号のエッジ間時間差が範囲外(伝搬遅延異常) |
| **パターン** | `:TRIGger:PATTern` | 複数chの論理状態の特定の組み合わせ |
| **シリアルバス5種** | `:TRIGger:RS232` / `:IIC` / `:SPI` / `:CAN` / `:LIN` | エラーフレーム、特定アドレス/データ。**既存のデコード実装(`configure_decode`)と対になる** |

実装コストは**中**。トリガ種別ごとの引数差は `configure_decode` の `settings` オブジェクトと同じ構造で吸収でき、**先例がある**(`driver/decode.py` の変換表方式)。`configure_trigger` の `type` 引数を拡張する形で新Toolを増やさずに済む見込み。

**ただしデコードと違い、トリガは「種別を切り替えると読み書きすべきサブツリーが変わる」。** [2.5.2](#252-m2-カーソル周波数カウンタ電圧計ヒストグラム実装完了実機検証済み) のカーソルで確立した原則がそのまま効く — **`get_trigger` は `:TRIGger:MODE?` を1本先読みしてから読む先を決め、サブツリー違いの指定は送信前に拒否する**。着手時にこの構造を再発見しないよう先に書いておく。

**層3「見つける」— AIが自力で異常を数え上げる**

| 機能 | 代表SCPI | AIにとって何が嬉しいか | 実装コスト |
|---|---|---|---|
| **イベント検索** | `:SEARch:COUNt?` / `:SEARch:VALue? <n>` / `:SEARch:EVENt` | `COUNt?` が**整数**、`VALue?` が**各イベントの発生時刻**を返す。件数だけループでクエリすれば**全異常の時刻リストが画面遷移なしに作れる**。人間なら画面をスクロールして探す作業が、AIには数値ループになる。<br>**制約: 検索条件は EDGE と PULSe の2種のみ**(シリアルバスのデータ値検索はガイド3.22に無い)。バス上の特定データを狙うのは層2のシリアルバストリガの役割 | **中**(新Tool 2本。値域が狭く状態機械も持たない) |
| **Pass/Fail試験** | `:MASK:FAILed?` / `:PASSed?` / `:TOTal?` / `:MASK:CREate` | 合否件数が**整数で返る**。ポーリングだけで長時間の無人監視が成立する。マスク生成は「現在表示波形 ± 許容幅(X/Y、div単位)」の3コマンドで完結し値域も狭い | **中**(新Tool 2本) |
| **動作完了の同期** | `*OPC?` | **現状は「取り込みが終わったか」を知る手段が `:TRIGger:STATus?` のポーリングしか無く、設定変更の完了は待てていない**(送って次を送るだけ)。`*OPC?` があれば `:SINGle` 実行後や重い設定変更後の完了を機器自身に申告させられ、待ち時間の当て推量が消える | **小**(内部利用。Tool追加不要)。※ `*WAI` はガイドに「互換性のためだけのno-op」と明記があり使わない |

**フェーズ割り(M1〜M3 の命名を継ぐ)**

| Phase | 内容 | 主な対象 |
|---|---|---|
| **M4** | **測定の全面開放** — `:MEASure:ITEM` の残り31項目 + 統計 + 測定区間の限定 + しきい値等の前提設定 | 層1 |
| **M5** | **トリガ種別の開放** — パルス幅 → タイムアウト/デュレーション → ラント/セットアップ&ホールド/ディレイ/パターン → シリアルバス5種 | 層2 |
| **M6** | **取り込み制御と同期** — `:ACQuire` の設定、`*OPC?`、`:TFORce`、トリガホールドオフ | 層1の一部 + 中 |
| **M7** | **自動監視**([2.5](#25-内蔵機能の棚卸しと対応予定2026-08-27) の見送りを覆した枠)— `:MASK` の合否件数、`:SEARch` のヒット件数と時刻 | 層3 |
| **M8** | **証拠の時刻とメタ情報** — 機器日時の**取得**(issue #10。設定は低優先のまま除外)、`:SYSTem:RAMount?` / `:MODules?`、オートセットの事前可否確認 | 層3 + 中 |

**各フェーズの着手前に潰すべき検証項目**([2.6.5](#265-実機検証で先に潰す不明項目)):

| Phase | 先に潰す項目 |
|---|---|
| **M4** | **V-5**(`:MEASure:STATistic:ITEM?` の応答形式)。測定項目の追加そのものは新ニモニックを送らないため検証不要 |
| **M5** | なし(トリガは既存の `:TRIGger` サブツリー。ただし初送信は読みから) |
| **M6** | **V-6**(ズーム中の `:WAVeform:DATA?`)— 遅延掃引を入れる場合のみ |
| **M7** | **V-4**(`:SEARch:VALue?` の応答形式) |
| **M8** | **V-3**(`:SYSTem:DATE?` / `:TIME?` の書式)、**V-2**(`:SYSTem:MODules?` でLA搭載を判定できるか) |
| (保留) | **V-1**(`:RECord` のフレーム波形)/ **V-7**(`:BODeplot`)/ **V-8**(`:NAVigate`)— フェーズに紐付かない。結果次第で優先度が動く |

**M4を先頭に置く理由:** 実装コストが**プロファイルYAMLへの追記が主**で、**沈黙リスクが最も低い**(既に通っている `:MEASure:ITEM` の値域を広げるだけで、新しいニモニックを1本も送らない)。それでいてAIが取れる数値証拠は4倍以上に増える。

**issue #10(スクリーンショットの時刻)**

`:SYSTem:DATE`(ガイド3.24.4)/ `:SYSTem:TIME`(3.24.5)が設定・クエリの双方に対応して実在する。**取得と設定で優先度を分ける:**

| 操作 | 優先度 | 操作クラス | 判断 |
|---|---|---|---|
| **時刻の取得** | **高** | READ_ONLY | スクリーンショットはAIが人間へ渡す唯一の視覚証拠。撮影時にホスト時刻と機器時刻を突き合わせ、ずれていれば返却に警告を添えられる。**読むだけなので安全**、コストも小さい |
| **時刻の設定** | **低** | SAFE_WRITE | 機器のグローバル状態の書き換え。ずれを検出して人間へ伝えれば目的は達せられ、**AIが勝手に機器の時計を直すのは越権**。人間が明示的に求めたときだけでよい |

実装案(第一候補): **新Toolを作らず `capture_screenshot` の返却に `device_time` と `time_skew_s` を足す**。issue #10 の「実行前に簡単に時刻同期チェックをしておきたい」に直接答える形になる。

**機種差に注意:** MHO900 と DHO1000/4000 には在るが、**DHO800/900 にはコマンド自体が無い**(コマンドリファレンスに存在しない)。方言キーで宣言し、不在ならゲートすること。返却書式は **`:SYSTem:DATE?` はカンマ区切りと Return Format に明記**(例 `2017,10,17`)。**揺れているのは `:SYSTem:TIME` だけ** — set はカンマ区切り(`:SYSTem:TIME 16,10,17`)なのに query の例はコロン区切り(`16:10:17`)で、Return Format 本文に区切り文字の記載が無い。**実機確認が必要**(2.6.5 の V-3)。

#### 2.6.2 優先度「中」

先に「高」を入れると価値が出る前提設定・補助機能。単独では調査能力が増えない。

| 機能 | 代表SCPI | 位置づけ |
|---|---|---|
| 測定の前提設定(2ソース指定・Vtop/Vbase算出方式・振幅アルゴリズム) | `:MEASure:SOURce`、`:MEASure:SETup:*` | 層1の位相/遅延・振幅系測定の精度を決める。「高」の項目を入れてから |
| メモリ深さの設定(現在は読みのみ) | `:ACQuire:MDEPth` | 取得点数を目的に合わせられる |
| トリガホールドオフ / トリガ位置取得 | `:TRIGger:HOLDoff` / `:TRIGger:POSition?` | 繰り返し波形の安定捕捉、波形内のトリガ点の数値特定 |
| 残りの標準トリガ種別(スロープ / ウィンドウ / Nthエッジ) | `:TRIGger:SLOPe` / `:WINDows` / `:NEDGe` | 用途が層2の8種より狭い |
| オプション必須のシリアルトリガ(FlexRay / I2S) | `:TRIGger:FLEXray` / `:TRIGger:IIS` | ライセンス必須のため採点軸4で1段下げ。デコード側(2.1のオプション4種)と対で着手するのが自然 |
| 強制トリガ | `:TFORce` | トリガ待ちで固まったときAIが自力で打開できる |
| オートセットの事前確認・挙動制御 | `:AUToset:LOCK` / `:ENAble` / `:OPENch` / `:KEEPcoup` | 実行前に許可を確認でき、意図しないch無効化を防げる。**autoset は実機実行禁止(AGENTS.mdルール4)のため、実機検証は読み取りに限る** |
| 設定の一括保存・復元 | `:SYSTem:SETup` | 「変更前の状態を保存して後で戻す」= 実機writeテストの安全弁になり得る |
| 遅延掃引(ズーム) | `:TIMebase:DELay:*` | 拡大自体はできる。ただし区間の再取得目的なら既存の `:WAVeform:STARt`/`:STOP` の方が直接的(2.6.5 の V-6 次第) |
| **`:MEASure` 系統の周波数カウンタ・ヒストグラム** | `:MEASure:COUNter:VALue?`(3.17.27)/ `:MEASure:HISTogram:STATistics:RESult?`(3.17.32) | **実装済みの `:COUNter`(3.7)/ `:HISTogram`(3.11)とは別サブシステム**([2.5.2](#252-m2-カーソル周波数カウンタ電圧計ヒストグラム実装完了実機検証済み) の訂正記録を参照)。数値が取れるためAI価値自体は高いが、既存Toolと機能が重なるため中に置く。**取り違えると未定義ヘッダを送る事故になる**ので、着手時は節番号で区別すること |
| オプションのライセンス投入 | `:SYSTem:OPTion:INSTall` / `:UNINstall` | 照会(`:STATus?`)は実装済み。投入は人間の作業 |

#### 2.6.3 低優先で固定するもの

**判定基準は一貫して「AIが読める結果が返るか」。** 設定はできるが結果が数値・テキストで返らない機能は、実装しても画面キャプチャ頼みになるため下げる。

| 機能 | 下げる理由 |
|---|---|
| ~~`:LA`(ロジックアナライザ)~~ | **この行は撤回した。** 当初「結果が読めない」として下げたが、読めないのは**生サンプル列だけ**で、D0-D15 は `:MEASure:ITEM` / `:BUS<n>` 各ソース / `:COUNter:SOURce` / `:MATH<n>:LSOurce` のソースとして受理され、結果はデコード表・測定値として読める。さらに 2.1 で露出済みの `bus_width > 4` のパラレルデコードは `:LA` の有効化が前提。詳細は [2.2](#22-logic-analyzer) |
| **`:BODeplot`** | ゲイン/位相カーブを数値で読むクエリが見当たらない(`GAINcurve:ENABle` / `PHASEcurve:ENABle` は画面表示のon/offフラグ)。オプション必須でもある |
| **`:NAVigate`** | 移動系コマンド(NEXT/BACK/STARt/END/PLAY)が**全て Return Format N/A**。移動先の時刻もイベント番号も返らないためAIには使えない。イベント間移動は `:SEARch:EVENt`(移動 + 現在位置のクエリが1コマンドで完結)で代替できる |
| ビデオトリガ(`:TRIGger:VIDeo`)/ MIL-STD-1553トリガ(`:TRIGger:M1553`) | 前者は映像信号に用途が限定的、後者は航空機バス向けでライセンス必須。**対象ユーザが極端に狭い** |
| `:DISPlay` の装飾系 / `:QUICk` / `:LAN` / `:SAVE` | 画面装飾・本体UIのボタン割当・ネットワーク設定・**機器ストレージへのファイル保存**。証拠はホスト側に置く方が扱いやすく、保存先規約(実行ディレクトリ基準の許可ルート)の外でもある |
| 時刻の**設定** | **この行だけ物差しが違う。** 他は「AIが読める結果が返らないから低」だが、これは結果が返るかどうかではなく **AIが機器のグローバル状態を勝手に書き換えるのは越権**という判断で低い。ずれの検出(取得)は「高」に置いてある。人間が明示的に求めたときだけ実装すればよい |

#### 2.6.4 対象外(MHO98実機で検証できないもの)

優先度の高低を付けない。DHO実機が用意できたら再評価する(issue #19)。

| 機能 | 機種 | 備考 |
|---|---|---|
| **`:POWer`(電源解析、DHO1000/4000ガイド 3.19)** | **DHO4000のみ**(DHO1000は非対応)。オプションライセンス必須 | `:POWer:QUALity:STATistics:RESult?` が**基準周波数・実効電圧・実効電流・有効電力・皮相電力・無効電力・力率・位相角・インピーダンス・電圧クレストファクタ・電流クレストファクタの11指標**を、各々 Current / Average / Maximum / Minimum / Deviation / Count の統計付きの表で**1クエリ返す**(返り値は `"88.273mV"` のような**単位付き文字列**でSI接頭辞の換算が要る)。`:POWer:RIPPle:STATistics:RESult?` はDC出力リプルの6統計。**通常なら人間の手計算か外部パワーメータが要るものが数値で取れる**ため、DHO4000の実機が入手できた場合の価値は高い |
| `VARiance`(分散)の測定 | 両DHOのみ(MHO98の `:MEASure:ITEM` に無い) | DHOは42トークン、MHO98は41トークン |
| `:TIMebase:XY:Z`(輝度変調入力) | DHO系 | XYモード自体が未実装のため実害なし |

#### 2.6.5 実機検証で先に潰す「不明」項目

ガイドに記載が無く、**その1本で上の優先度が書き換わる**もの。順序は「優先度表への影響 ÷ 検証コスト」。**read-only(`-m device`)で確認できるのは V-3 と、V-2 の `:SYSTem:MODules?` 部分のみ。** V-1 / V-4 / V-5 / V-6 は**前提設定の書き込みが要る**(V-1=録画の実行、V-4=`:SEARch:STATe ON` とマークテーブルの生成、V-5=統計機能の有効化、V-6=遅延掃引の有効化)。これらは `-m device_write`(`RIGOL_TEST_ALLOW_WRITE=1`)側で、**現在値取得 → set → readback → finallyで復元** のパターンで実施すること(AGENTS.mdルール4)。

**ただし V-1 / V-2 は未送信のニモニックを含むため、`docs/profile-authoring.md` §4 の「1コマンド送信 → 応答 → `:SYSTem:ERRor?` → 記録」を1つずつ守ること**(AGENTS.mdルール2)。

| # | 確認すること | なぜ最初に潰すか |
|---|---|---|
| **V-1** | **録画した特定フレームを `:WAVeform:DATA?` で吸い出せるか**(ガイド3.19・3.28とも記載なし) | YESなら `:RECord` は「高」へ跳ね上がる(**長時間の間欠不具合を後から数値解析できる**)。NOなら見送り確定。フレーム数・現在フレーム・タイムスタンプは読めることが確定済みなので、残る不確定はこの1点だけ。**確認には録画の実行(`:RECord:WRECord:ENABle` → 録画 → 再生位置指定)が要るため read-only ではない。録画停止・状態復元の手順を先に用意すること** |
| **V-2** | **デジタル波形(D0〜D15)を数値で吸い出す手段があるか**。併せて `:SYSTem:MODules?` でプローブ搭載を判定できるか | `:LA` 全体の評価がこれで決まる。無ければ 2.6.3 の格下げが確定 |
| **V-3** | **`:SYSTem:TIME?` の区切り文字**(カンマかコロンか。`:DATE?` はカンマ区切りとガイドに明記があるため対象外) | issue #10 のパーサ設計に直結。**唯一の純 read-only 項目**でクエリ1本 |
| **V-4** | `:SEARch:VALue?` の応答形式とイベント番号の対応 | 層3「イベント検索」の実装可否の前提。全イベント時刻の一括収集ループが成立するか |
| **V-5** | `:MEASure:STATistic:ITEM?` の応答形式(複数値の区切り) | 層1「測定統計」のパーサ設計。ガイドに区切り文字の記載が無い |
| **V-6** | 遅延掃引(ズーム)中に `:WAVeform:DATA?` が何を返すか | 拡大区間を返すなら「怪しい箇所だけ再取得」が成立。返さないなら既存の `:STARt`/`:STOP` で足りる |
| **V-7** | `:BODeplot` の結果を数値で読むクエリが本当に無いか | 無いなら 2.6.3 の判定が確定。オプション必須のため後回しでよい |
| **V-8** | `:NAVigate` の移動が `:SEARch:EVENt?` に反映されるか | 反映されるなら評価が少し上がるが、`:SEARch:EVENt` で代替できるため影響は小さい |

#### 2.6.6 機種横断で判明した方言差(プロファイル拡充の材料)

DHOプロファイル(`confidence: guide`)を実機検証で昇格させる際に必要になるもの。**現時点では実機が無いため宣言しない**(キーの不在がそのままゲートになる)。

> **機種対応の正は [compatibility.md](compatibility.md) と `profiles/data/*.yaml`。** 本表は**今回のガイド解読で新たに判明した分の作業メモ**であり、恒久記録ではない。両者が食い違ったら compatibility.md 側が正しいものとして扱い、本表を直すこと(二重管理を避けるため、実機検証で確定した内容は compatibility.md へ移すこと)。

| 項目 | MHO900 | DHO800/900 | DHO1000/4000 |
|---|---|---|---|
| **I2Cアドレス指定** | `:BUS<n>:IIC:ADDBits {7\|8\|10}`(ビット幅) | `:BUS<n>:IIC:ADDRess {NORMal\|RW}`(R/Wビットを数えるか) | 同左 |
| **`:SYSTem:DATE` / `:TIME`** | あり | **無し** | あり |
| **`:SYSTem:OPTion:*`** | あり | **丸ごと無し**(`get_capabilities` が機能しない) | **あり**(3.26.9〜3.26.11。`:OPTion:VALid?` のみ無い) |
| **測定区間の限定** | `:MEASure:AREA` は3機種共通。**`:MEASure:CREGion:*` は MHO900 のみ** | `:CREGion` 無し | `:CREGion` 無し |
| **ADC分解能** | `:ACQuire:BITS` | **無し**(`:ACQuire:HRESolution` が別綴りで在る) | `:ACQuire:BITS` あり |
| **`:CHANnel<n>:IMPedance`** | あり(50Ω/1MΩ) | **無し**(ガイド全文に該当なし) | **コマンドは在る**(3.9.8)。機器側は DHO1000 が1MΩのみ、DHO4000 が `{OMEG\|FIFTy}` 対応([compatibility.md](compatibility.md)) |
| **AFG(`:SOURce`)** | `:SOURce<n>`(2ch) | 番号なし `:SOURce`。`PERiod` / `PHASe:SYNChronize` / `VOLTage:HIGH/LOW` / `IMPedance` / `LOAD:ARBitrary` が無い | **AFG自体が無い** |
| **`:LA` / デジタルch** | `:LA` | `Digital Channel Commands`(**DHO900系のみ。DHO800系は非搭載**) | **無し** |
| スクリーンショット | ガイドは `:DISPlay:DATA? [<type>]`、`<type>`=`{BMP\|PNG\|JPG}`・**既定BMP**。ただし**MHO98実機は引数なしでPNGを返す**(ガイドと実機の食い違い。実測: [verification/mho98-phase0.md](verification/mho98-phase0.md)) | **ガイド上は同一**(既定BMPのため PNG が欲しければ明示する) | **ガイド上は同一** |
| 測定クリア | `:MEASure:DELete` | `:MEASure:CLEar` | `:MEASure:CLEar` |
| トリガ種別数 | 20 | 17(FLEXray / IIS / M1553 が無い) | 20(綴りも一致) |

**LINデコードの `:BAUD`(`:BUS<n>:LIN:BAUD`)は両DHO系列とも存在しない** — MHO900 の 3.4.15.4 のみ。DHO の `:BUS<n>:LIN` 配下は `PARity` / `SOURce` / `STANdard` の3つ(`:TRIGger:LIN:BAUD` は**別サブシステム**なので混同しないこと)。`driver/decode.py` は LIN の `baud_bps` で `:BAUD` を送るため、**DHOプロファイルでは `baud_bps` 項目自体を無効化する必要がある**。未定義ヘッダで沈黙する既知の事故モードに直撃する箇所。

#### 2.6.7 本節全体にかかる安全注記

**本節に挙げたニモニックのうち、実機で送信して応答を確認したものは1つも無い**(実装済み機能を除く)。すべて公式プログラミングガイドの記載に基づく**候補**であり、「**ガイド記載あり・実機未検証**」の段階にある。着手時は次を守ること:

1. 新コマンドは**機種プロファイルへの宣言とセット**で追加する(AGENTS.mdルール2)
2. **未実装サブシステム(`:SEARch` / `:MASK` / `:RECord` / `:LA`)への初送信は読み(`?`)から入る。** `docs/profile-authoring.md` §4 の「1コマンド送信 → 応答 → `:SYSTem:ERRor?` → 記録」を1つずつ
3. **ガイドの Return Format 欄はあてにならない** — M3 で `:REFerence:COLor?` がガイド記載の `GRE` ではなく `GREE` を返した実測がある([2.5.3](#253-m3-リファレンス波形実装完了実機検証済み))。列挙値は必ず共有マッチャを通す
4. `*OPT?` は全シリーズで未定義ヘッダ。オプション照会は `:SYSTem:OPTion:STATus?` + ガイド記載トークンのみ

#### 2.6.8 §2.5「見送り」表への訂正

§2.5 の見送り判断は「実ニーズが出たら」を条件にしていたが、**AI調査効率の観点では2件が誤りだった**。ニーズ待ちではなく、AIが自力で調査を進めるための基盤能力である。

| 機能 | §2.5 の判断 | 本節の判断 | 覆す根拠 |
|---|---|---|---|
| **`:SEARch`** | 見送り(イベント検索の実ニーズが出たら) | **「高」へ格上げ** | `:SEARch:COUNt?` が整数、`:SEARch:VALue? <n>` が各イベントの発生時刻を返す。**画面を見ずに全異常の時刻リストが作れる**。人間なら画面をスクロールする作業がAIには数値ループになるため、「ニーズが出てから」ではなく調査の土台になる |
| **`:MASK`** | 見送り(長時間自動試験の実ニーズが出たら。件数取得は有用) | **「高」へ格上げ** | §2.5 自身が「有用」と認めている。**AIは疲れないので長時間監視こそ得意領域**。マスク生成が3コマンドで完結し値域も狭く、実装コストが低い |
| `:NAVigate` | 見送り(`:SEARch` と同枠) | **見送りを維持**(2.6.3) | 移動系コマンドが全て Return Format N/A。`:SEARch` とは分けて扱うべきで、同じ行に置いていたのが誤り |
| `:RECord` | 見送り(状態機械が複雑) | **保留**(V-1 の結果次第) | フレーム数・現在フレーム・タイムスタンプは読めることが判明。残る不確定は「フレーム別の波形を吸い出せるか」の1点のみで、YESなら評価が反転する |
| `:BODeplot` / `:QUICk` / `:LAN` / `:SAVE` | 見送り | **維持**(2.6.3) | 結果が数値で返らない / 本体UI・ネットワーク・ストレージ操作 |


## 3. プラグイン化(完了・要件へ昇格)

**完了**(2026-08-25)。Claude(`.claude-plugin/plugin.json`)・Codex(`.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json`)の両プラグインとして実装し、[Requirements.md](Requirements.md) 10.3 へ昇格した。スキル(`skills/measurement-workflows/SKILL.md`、Agent Skillsオープン標準)とMCP起動定義は両ホストで共有。旧v0.1のスキル素材(UART測定・Unknown Signal探索・安全プロンプト・反復上限)はすべてスキル本文へ吸収済み。

**残タスク: Codex CLIでの実動作確認**(公式ドキュメント準拠で作成、実CLI未確認)。確認対象は Requirements.md 10.3 の未検証事項2点(`mcpServers` の相対パス指定、マーケットプレイスsource `path: "./"`)と、`codex plugin marketplace add zinntikumugai/rigol-oscilloscope-mcp` → install → スキル発見・MCPサーバー起動の通し。なおCodexはMCPサーバー単体なら `config.toml`([Requirements.md](Requirements.md) 10.2)、スキル単体なら `~/.agents/skills/` へのコピーでもプラグインなしで利用できる。

## 4. 機種プロファイルの拡充

- MHO98で未検証の項目の実機確認([device-profiles.md](device-profiles.md) 3.1、[verification/mho98-mvp.md](verification/mho98-mvp.md) 4章): 50Ωニモニック、autoset書き込み(ニモニック :AUToset はサブツリーの読み取りプローブで確認済み — mho98-autoset.md。実行自体が未検証)、RAWモード波形、limits境界値(RUN/STOP/SINGleはMVPで実機確認済み)
- **USB(USBTMC)接続の実機検証** — ユニットテスト(`tests/test_usb_transport.py`、PyVISAのフェイク)は通っているが実機未検証。VISAリソース文字列の推奨形式もここで確定させる
- **表示OFFチャンネルへの書き込みが無視される件への対策検討**([verification/mho98-mvp.md](verification/mho98-mvp.md) 3.3): 表示OFFのCHへ `:SCALe` / `:OFFSet` を送るとエラーなく無視される。`configure_channel` で自動的に `enabled=True` にするか、requested/applied の不一致を警告として返すに留めるか、要検討(暗黙に表示をONにするのは利用者の画面を勝手に変える副作用でもある)
- MHO98以外の対応機種の追加: **DHO800/900/1000/4000系をガイドベースプロファイルとして追加済み**(`dho800.yaml` / `dho900.yaml` / `dho1000.yaml` / `dho4000.yaml`、信頼度 `guide` = 公式プログラミングガイドの逐語解読のみで実機未検証。[device-profiles.md](device-profiles.md) 6章)。実機が用意でき次第、(1) quirk・limitsを実測して `verified` へ昇格、(2) 現在スコープ外のデコード / AFG / LA / オプション照会の宣言を追加する(issue #19)。**ガイド解読で判明している方言差の一覧は [2.6.6](#266-機種横断で判明した方言差プロファイル拡充の材料)**(I2Cアドレス指定の意味差、`:SYSTem:DATE`/`:TIME` と `:SYSTem:OPTion:*` の不在、AFG系統の違い、DHO800系のLA非搭載など)。他機種(DS/MSO系など)は引き続き実機が用意でき次第
- ファミリプロファイルの括り出し(同系2機種以上の検証が揃った段階で)
- **issue #24(2chモデルが `analog_channels: 4` として解決される問題)の解法候補: `:SYSTem:RAMount?`(ガイド3.24.8)。** 機器自身が実アナログch数を申告するため、**プロファイルへ機種内分岐を足さずに実行時解決できる**(宣言値より実測値を優先する)。DHO実機が無くても設計を決められる点が利点。ただし**外部トリガ(EXT)入力が DHO802 / DHO812 専用**である点は `:RAMount?` では分からず、別途プロファイル宣言が要る。併せて `:SYSTem:MODules?`(3.24.15)でLA搭載の有無を実行時判定できる可能性がある([2.6.5](#265-実機検証で先に潰す不明項目) の V-2 で確認する)。**いずれも実機未検証**
- **DHO800/900 を `verified` へ昇格させる際の障害: `get_capabilities` が依存する `:SYSTem:OPTion:STATus?` が DHO800/900 のコマンドリファレンスに存在しない**(代替手段もガイド上見つからない)。オプション照会をゲートするか、機種によっては返せないことを許容する設計判断が要る([2.6.6](#266-機種横断で判明した方言差プロファイル拡充の材料))
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
