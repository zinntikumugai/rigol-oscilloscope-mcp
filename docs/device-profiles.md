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
   ├─ 2. ファミリプロファイル         (例: DHO900系)     … 信頼度 family
   └─ 3. 汎用Rigolプロファイル        (フォールバック)   … 信頼度 generic
```

- **verified**: 実機検証済みの機種。quirk・制限値がプロファイルに記録されている
- **family**: 同系列機種のプロファイルを適用。ニモニックは概ね互換だが未検証項目あり
- **generic**: 未知のRigol機種。共通性の高いSCPIコマンドのみでベストエフォート動作

解決したプロファイル名と信頼度は `scope_identify` / `get_capabilities` の返却に必ず含め、generic時はdegraded動作であることをLLMへ明示する。

製造者が `RIGOL TECHNOLOGIES` でない機器へ接続した場合は警告を返す(接続自体は拒否しない。返却に `unsupported_vendor: true` を含める)。

## 2. プロファイルの構成要素

プロファイルは以下の3ブロックからなる。ファミリ/汎用プロファイルからの**継承**(差分定義)を可能とする。

### 2.1 capabilities — 機能有無と構成

| 項目 | 例 (MHO98) | 用途 |
|---|---|---|
| `analog_channels` | 4 | channel引数の検証 |
| `digital_channels` | 16 | 将来のLA対応 |
| `afg_channels` | 2 | 将来のAFG対応 |
| `bandwidth_hz` | 実機依存 | 参考情報 |
| `protocol_decode` | true | デコード対応(`configure_decode` の可否) |
| `decode_buses` | 4 | デコードバス本数(`bus` 引数の検証。未宣言ならデコード自体を行わない) |
| `waveform_download` | true | capture_waveform 可否 |
| `screenshot` | true | capture_screenshot 可否 |
| `measurements` | 対応測定項目リスト | measure の項目検証 |
| `impedance_control` | true | `:CHANnel<n>:IMPedance` の可否(問い合わせ含む) |
| `impedance_50ohm` | true | 50Ω設定の可否 |

未対応機能のToolが呼ばれた場合、実機へコマンドを送らず `UNSUPPORTED_FEATURE` を返す(理由は4.2参照)。

### 2.2 dialect / quirks — SCPI方言と実機挙動の癖

| 項目 | 内容 | MHO98での実測値 |
|---|---|---|
| `measurement_items` | 意味的測定名 → SCPIニモニック対応表 | `vavg` → `VAVG`(`VAVerage` は**不可**、無応答+`-222`) |
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

`:SYSTem:OPTion:*` は**MHO900専用**でDHO800/900のガイドには存在しない。したがって `option_query` / `option_types` は `mho98.yaml` にのみ宣言し、`rigol-generic.yaml` には置かない — **キーの不在がそのままゲート**である(4.2の原則の適用例)。`*OPT?` はRigolオシロ全シリーズで未定義ヘッダのため使わない。`<type>` リスト外のトークン(実測: `AUTOA`)でもSCPIサーバーが沈黙するので、`option_types` に載っていないトークンを送ってはならない。実測根拠は [verification/mho98-unlicensed.md](verification/mho98-unlicensed.md)(未ライセンス状態でも `AFG50` / `RLU-05` は `1` を返す)。

`decode_protocols` に載せるのは**標準搭載の6種のみ**。I2S(`IIS`)/ FlexRay / MIL-STD-1553(`M1553`)/ CAN-FD(`:BUS<n>:CAN:FDBaud`)はライセンスオプション必須のため意図的に載せず、**不在がそのままゲート**になる(未宣言のプロトコルは送信前に `UNSUPPORTED_FEATURE`)。実測根拠は [verification/mho98-unlicensed.md](verification/mho98-unlicensed.md) 3章(未ライセンス状態でも RS232/IIC/SPI/CAN/LIN/PAR は全て正常応答)。

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
- ファミリプロファイル(例: DHO900系)は、同系機種の検証結果が2機種以上揃った段階で共通部分を括り出して作成する(先回りで作らない)
- プロファイルの適用結果(名前・信頼度)は監査ログに記録する
