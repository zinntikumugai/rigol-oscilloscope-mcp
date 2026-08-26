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
| M2 | **:CURSor** カーソル測定 | 3.8 | manual/track/XY。設定+ΔX/ΔY等の読み取り |
| M2 | **:COUNter** 周波数カウンタ | 3.7 | enable/source/mode + `:VALue` 読み。実装コスト極小 |
| M2 | **:DVM** 電圧計 | 3.10 | 同上(4コマンドのみ) |
| M2 | **:HISTogram** ヒストグラム | 3.11 | `:STATistics:RESult` で統計テキスト取得 |
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

### 2.5.1 M1: MATH演算(実装完了・実機検証は残件)

**`configure_math`(SAFE_WRITE)と `get_math_state`(READ_ONLY)を実装済み**([tools.md](tools.md) 11章)。ホスト側FFT(`analyze_waveform`)との棲み分けは当初の想定どおり — 機上FFTは「画面に出る(人間が確認できる)」「ピーク表がテキストで取れる(波形転送ゼロ)」点で別価値がある。

- **`configure_math`**: 演算子21種(加減乗除・論理4種・FFT・微積分系・デジタルフィルタ4種・AXB)、算術ソース(`source1` / `source2`)と論理ソース(`lsource1` / `lsource2`)、垂直(`scale` / `offset_v` / `invert`)、`fft` サブ辞書(入力ch・窓・単位・モード・平均回数・縦軸・表示周波数範囲・ピーク探索6項目)、`filter` サブ辞書(種別・W1・W2)。**送信順は表示ONが先頭・OFFが末尾**に固定(表示OFF中の書き込み無視quirk対策)。検証は全て送信前で、不正が1つでもあれば1コマンドも送らない
- **`get_math_state`**: 演算子に応じた条件付き読み取り(未検証サブツリーを突かない)。`:MATH<n>:FFT:SEARch:RES?` のピーク表は当初方針どおり**Toolを増やさず**返却の `peaks` に含める(解釈できない行は `raw` + `peak_warnings` で fail-open)
- **波形取得**: 当初の「`source` 引数拡張」ではなく**既存の `channel` 引数を拡張**する形で実現した(引数を増やさない)。`capture_waveform` / `analyze_waveform` が `"MATH1"`〜`"MATH4"` を受理し、`read_samples` 経路をそのまま流用する。FFT演算のトレースは `x_unit: "Hz"` を付けて `effective_sample_rate_sa_per_s` を省き、`analyze_waveform` では**取得前に `INVALID_PARAMETER` で拒否**する(横軸が周波数のトレースに時間軸統計・ホスト側FFTは意味を持たないため)
- **プロファイル宣言**: capabilities に `math_channels` / `ref_channels`、dialect に `math_operators` / `math_fft_windows` / `math_fft_units` / `math_fft_modes` / `math_fft_search_orders` / `math_filter_types`(`mho98.yaml` のみ。[device-profiles.md](device-profiles.md) 2.1 / 2.2)。`:MATH<n>` はファミリ分岐の実例が無いため `math_prefix` 方言は作らず、`math_channels` の宣言の不在をそのままゲートにしている
- **`:WAVeform:SOURce MATH<n>` の可否は解決済み**: ガイド3.28.1に `{CHANnel1-4|MATH1-4}` と逐語で記載があり(`NORMal` モード限定 = 既定で使用しているモード)、M1最大のリスクだった「取得不能なら画面キャプチャ頼み」は回避できた

**意図的にスキップ(ガイド3.16にあるが実装しない):**

- `:FFT:HSCale` / `:FFT:HCENter` — `fft.freq_start_hz` / `fft.freq_end_hz` で表現できる別表現(AFGの `:PERiod` 恒久スキップと同じ原則)
- `GRID` / `EXPand` / `RESet` / `WAVetype` / `SENSitivity` / `DISTance` / `THReshold`(論理演算のしきい値)/ `WINDow:TITLe?` / `LABel:SHOW` / `DISMode` — 画面装飾・本体UI寄りの項目で、MCPから触る価値が薄い

**残件(実機検証。記録先は [verification/mho98-math.md](verification/mho98-math.md) — 現時点では手順のみのスケルトンで、実測結果は未記入):**

1. **MATH表示OFF時のクエリ挙動(沈黙の有無)** — fail-dangerousのため最初に確認する。沈黙する場合は `get_math_state` が先頭で読む `:DISPlay?` を短絡点にする
2. `:MATH1:OPERator?` の工場出荷デフォルトreadbackトークン(短形の実測裏取り)
3. **FFTトレースのプリアンブル解釈**(横軸Hz時の xincrement / xorigin)と、`:FFT:SEARch:RES?` の実フォーマット(`UNIT VRMS` / `DB` の両方、行区切りが改行か `;` か)。**LAN transportの `query()` は1行しか読まないため、真に複数行で返る場合は切り詰められる**(実装が明示している未解決リスク)
4. `configure_math` の往復(現在値取得 → set → readback → finally復元)
5. 表示OFF中の書き込み無視quirkの有無 — 結果次第でAFG式の送信前拒否を追加する
6. FFT + ピーク表 + `capture_waveform("MATH1")` のE2E(`x_unit` と `note` の文言を確定する)

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
