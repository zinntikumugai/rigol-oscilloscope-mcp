# MHO98 カーソル・カウンタ・電圧計・ヒストグラム(M2)実機検証記録

**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**実施日:** 2026-08-27
**状態:** **実施済み**(SCPIレベルのプローブと、実装コードのE2Eを両方実施。**残件2件は「未解決の観測事実」として6章に記録**)
**規範:** [tools.md](../tools.md) 12章 / [roadmap.md](../roadmap.md) 2.5.2
**前提:** MHO900-BND適用済み。手順規律は [mho98-math.md](mho98-math.md) と同じ(1コマンド → 応答5s timeout → `:SYSTem:ERRor?` → 記録。**沈黙したら空行付き再接続 → `*IDN?` で復旧確認 → エラーキューをドレイン**)
**安全条件:** write項目は全て「現在値取得 → set → readback → finally で復元」。カーソル・カウンタ・電圧計・ヒストグラムは**表示・統計層のみ**の操作で、取り込み条件にも出力にも触れない

```bash
RIGOL_TEST_ADDRESS=<実機IP> uv run pytest -m device -k "cursor or meter or histogram"
RIGOL_TEST_ADDRESS=<実機IP> RIGOL_TEST_ALLOW_WRITE=1 uv run pytest -m device_write -k "cursor or meter or histogram"
```

実機アドレスは `RIGOL_TEST_ADDRESS` 環境変数でのみ渡す(本文書を含め、リポジトリ内のいかなるファイルにもIP・シリアルを書かない。例示が要るなら TEST-NET の `192.0.2.10` を使う)。

## 結論(実装への影響)

実測で**3件の実装バグ**が判明し、いずれも修正済み。加えて**ロードマップの記述2件の誤り**が実装前に判明した(下記「ロードマップ訂正」)。

1. **無効な電圧計の `:DVM:CURRent?` は空応答**(`''`)を返す。`parse_nr3` に渡すと `SCPI_ERROR` になり、**無効な計を読むという普通の操作が機器故障に見えて**いた → `:DVM:ENABle?` を先に1本読んで短絡し、無効なら現在値を問い合わせず `value: null` を返す(`get_math_config` と同じ条件付き読み取り)
2. **ヒストグラム無効時の `:HISTogram:STATistics:RESult?` は `[]` を返しつつエラーキューに `-200,"Command execute failed"` を積む**(沈黙はしない)。**共有状態であるエラーキューが汚れ、次の無関係な書き込みの set → エラーキュー確認に化けて出て**いた → `:HISTogram:ENABle?` を先読みして統計クエリ自体を送らず、`raw: ""` + `warnings` を返す
3. **統計応答には終端の空行が無い**(1行・改行1個で終わる)。FFTピーク表([mho98-math.md](mho98-math.md) c-2)と同じ `query_lines()`(空行まで読む)で読むと**実機ではタイムアウトまで固まる** → この応答は `query()` で1行だけ読む

**ロードマップ訂正**([roadmap.md](../roadmap.md) 2.5 の表を修正済み):

- **カウンタの現在値は `:VALue` ではない。** `:COUNter` サブシステムに `:VALue` は**存在しない**(実在するのは `:COUNter:CURRent?`)。`:MEASure:COUNter:VALue?` はガイド3.17の**別サブシステム**であり、取り違えたまま実装していれば**未定義ヘッダのクエリ1発でSCPIサーバー全体が沈黙**していた(AGENTS.mdルール2)
- **ヒストグラム統計は「統計テキスト」(`[["92","1",…]]` 形式)ではない。** その書式は `:MEASure:HISTogram:STATistics:RESult?`(3.17.32)のもので、`:HISTogram:STATistics:RESult?`(3.11.9)の実応答は**機器自身がラベルを持つ1行**である(下記2章)

## 1. 無効状態での応答(3系統とも)

すべての機能を無効にした状態(=**機器の通常の休息状態**)での実測。実装が最初に踏むのはこの状態である。

| コマンド | 応答 | 所見 |
|---|---|---|
| `:COUNter:ENABle?` | `0` | 沈黙なし |
| `:COUNter:CURRent?` | `'0'` | 無効でも数値を返す(空応答ではない) |
| `:DVM:ENABle?` | `0` | 沈黙なし |
| `:DVM:CURRent?` | `''` | **空応答**。`parse_nr3` が `SCPI_ERROR` を投げる |
| `:HISTogram:ENABle?` | `0` | 沈黙なし |
| `:HISTogram:STATistics:RESult?` | `'[]'` | 空リストを返す |
| `:SYSTem:ERRor?`(上記の直後) | `-200,"Command execute failed"` | **統計クエリがエラーキューを汚す** |

含意:

- **`:DVM:CURRent?` の空応答は「壊れている」ではなく「値が無い」**。`_readout()` で空応答を `None` に落とす共通処理を入れた(番兵値 ±9.9E37 と同じ扱い)。`parse_nr3` 側は緩めない — 他の全経路(設定値のread-back等)では解釈できない応答が本当に異常だからである
- **`:HISTogram:STATistics:RESult?` は応答自体は返すがエラーキューを汚す**のが厄介で、沈黙よりも発見が遅れる。無効時は**クエリを送らない**のが唯一の正解(送ってしまうと、後続の無関係な `set_and_verify` がそのエラーを拾って `SCPI_ERROR` になる)

## 2. 有効状態での応答(ヒストグラムはCH2・vertical)

| コマンド | 応答 | 所見 |
|---|---|---|
| `:HISTogram:STATistics:RESult?` | 223バイト / **改行1個**(単一行・終端の空行なし) | `query_lines()` は使えない |
| `:HISTogram:HEIGht?` | `2` | 1〜4の整数 |
| `:HISTogram:RANGe:LEFT?` / `:RIGHt?` | `-4.000000E-4` / `4.000000E-4` | 秒 |
| `:HISTogram:RANGe:TOP?` / `:BOTTom?` | `2.000000E0` / `-1.000000E0` | V |
| `:DVM:CURRent?`(有効・CH2・DC) | `4.827000E-3` | 有効なら普通のNR3 |
| `:COUNter:NDIGits?` | `4` | 3〜6の整数 |
| `:COUNter:TOTalize:ENABle?` | `0` | |

統計応答の中身(改行を入れて表記。実際は1行):

```
[Sum:30.37khits, Peaks:234hits, Max:1.562V, Min:-999.9mV, Pk_Pk:2.562V, Mean:265.1mV,
 Median:281.2mV, Mode:1.421V, Bin width:15.62mV, Sigma:6.159mV,
 meanPlusSigma:0.581421, meanPlus2Sigma:1.000000, meanPlus3Sigma:1.000000]
```

判定: ☐ ガイド引用の `[["92","1",…]]` 形式 / ☑ **機器自身がラベルを持つ `[Label:Value, …]` の1行**

含意:

- ラベル集合はガイドに逐語の一覧が無く、ファームウェア・種別(horizontal / vertical)で増減しうる。したがって**パーサはラベル駆動**とし、`raw`(生応答)を必ず返して、解釈できない項目は `warnings` に積むだけの fail-open にした
- **SI接頭辞が付く**(`30.37khits` / `-999.9mV` / `15.62mV`)。基本単位へ換算して `stats`(数値)+ `<キー>_unit` に分ける(`sum=30370.0` / `sum_unit="hits"`)
- **`meanPlusSigma` 系は無単位**(`0.581421` 等)。`_unit` キーを付けない。値の定義はガイドに記載が無く、0〜1の比率に見えるが**意味は確定していない**
- ラベルの正規化は `Bin width` → `bin_width` / `Pk_Pk` → `pk_pk` / `meanPlus2Sigma` → `mean_plus2_sigma`(小文字/数字の直後の大文字の前に `_` を挿し、英数字以外を `_` へ潰す)

## 3. カーソルのドリフト(trackモードの設定値は動く)

**この観測が `configure_cursor` の `changed` 判定の設計を決めた。**

`TRACk` モードで `CAX` を `-6.000000000000E-04` に固定し、約0.8秒間隔で4回読んだ結果:

| 読み | `:CURSor:TRACk:CAY?` | `:CURSor:TRACk:AYValue?` |
|---|---|---|
| 1回目 | `3.748000E-1` | `3.584E-01` |
| 2回目 | `3.701333E-1` | `3.725E-01` |
| 3回目 | `4.005333E-1` | `3.959E-01` |
| 4回目 | `4.214667E-1` | `3.795E-01` |

`MANual` モードで同じことをすると、`CAY` は `3.000000E0` を2回とも**同値**(動かない)。

読み取り書式(いずれもNR3。単位は付かない):

```
XDELta? -> 1.200E-03     YDELta?  -> 1.018E+00     IXDelta? -> 8.333E+02 (Hz)
BXValue? -> 6.000E-04    BYValue? -> 1.440E+00
```

含意:

- **trackモードでは「設定」であるはずの `CAY` / `CBY` が波形に追従して動く**(カーソルがソース波形を追いかけるモードなので当然だが、`changed`(before ≠ after)判定にそのまま使うと**何も変えていなくても毎回 true** になる)→ `configure_cursor` の `changed` は**trackモードのYを比較対象から外す**
- **`:CAY?`(設定側)と `:AYValue?`(読み値側)は桁数も値も一致しない**(1回目: `3.748000E-1` vs `3.584E-01`)。同じ量の2つの表現ではなく、**サンプル時点が違う別々の読み**である。カーソルが実際に何を指しているかを見るなら `get_cursor_measurement`(読み値側)を使う
- 読み値は7項目とも単位なしのNR3で返る。SI単位はホスト側がキー名で持つ(`xdelta_s` / `ydelta_v` / `ixdelta_hz`)

## 4. 実装コードのE2E(全て成功・状態は復元済み)

SCPIレベルではなく**出荷する実装**を通した確認。

無効状態:

```
get_histogram_result()      -> {'raw': '', 'warnings': ['the histogram is disabled, ...']}
get_meter_value("dvm")      -> value=None / unit='V'          (修正前は SCPI_ERROR)
直後の configure_histogram(height=3) -> {'height': 3}          ← キュー汚染の回帰確認
```

3行目が重要で、**「無効時に統計を読む」→「直後に無関係な設定を書く」が通ること**が、1章のエラーキュー汚染に対する回帰確認になっている。

カーソル(trackモード):

```
{'mode': 'track', 'ax_s': -0.0006, 'ay_v': 0.3631, 'bx_s': 0.0006, 'by_v': 1.393,
 'xdelta_s': 0.0012, 'ydelta_v': 1.03, 'ixdelta_hz': 833.3}
```

ヒストグラム統計:

```
stats = {'sum': 16880.0, 'sum_unit': 'hits', 'max': 1.531, 'max_unit': 'V',
         'min': -0.9999, 'min_unit': 'V', ...}       warnings なし
```

一連の操作後のエラーキュー: `0,"No error"`。

## 5. カウンタのTotalize書き込み(無効時に拒否される)

| 状態 | 書き込み | エラーキュー | 所見 |
|---|---|---|---|
| `:COUNter:ENABle? -> 0`(無効) | `:COUNter:TOTalize:ENABle OFF`(**現在値と同値**) | `-200,"Command execute failed"` | 値が変わらなくても拒否される |

含意:

- `configure_meter` の送信順は `enabled` → `source` → `mode` → `digits` → `totalize_enabled` なので、**`configure_meter(enabled=True, totalize_enabled=…)` という通常の呼び方では踏まない**(有効化が先に届く)
- 踏むのは「**無効なカウンタへ `totalize_enabled` だけを送る**」呼び出しだけである。ホスト側の事前チェックは追加しない(モードとの結合制約と同じく機器のエラーキューに委ねる方針 — [tools.md](../tools.md) 12章)
- **実機writeテストの復元fixtureはこれを踏む**(スナップショットの全キーを書き戻すため)。プローブ用スクリプトが実際にここで失敗した。復元では `totalize_enabled` の失敗を握り潰す扱いにしてある(`tests/device/test_write.py` の該当コメント参照)

## 6. 未解決(次に試すこと)

### 6.1 `:COUNter:CURRent?` が有効化しても `0` を返す

生信号が載っているチャンネルでカウンタを有効化し、2秒の整定待ちを置いても `:COUNter:CURRent?` は `0` のままだった(`:COUNter:ENABle?` は `1`、`:COUNter:SOURce?` は当該チャンネル、`:COUNter:MODE?` は `FREQ`)。

未特定の要因(次に試す順):

1. **ゲート時間** — カウンタが1回の測定に使う時間の設定がガイドにあるか再確認する。2秒の待ちで足りているかも含めて
2. **トリガ要件** — トリガがかかって取り込みが走っている状態(`:TRIGger:STATus?` が `TD` / `AUTO`)での再測定。停止中は数えない可能性がある
3. **ソース条件** — 別のアナログch、およびデジタルch(`D0`-`D15`)との比較。信号レベル・しきい値がカウンタ側の判定基準を満たしていない可能性

実装側は現状のままで問題ない(値をそのまま返すだけで、`0` を異常として扱ってはいない)。原因が判明したら `get_meter_value` の説明に条件を書き足す。

### 6.2 実機テストのpytest経由での実行

本文書の記録はすべて手動プローブと実装コードの直接呼び出しによるもの。`tests/device/test_readonly.py` / `tests/device/test_write.py` に追加したM2ケース**自体の実機実行は未実施**。

## 未実施・今後の予定

- **6.1 のカウンタ現在値** — 唯一の機能的な未解決事項
- **6.2 のM2実機テストのpytest実行**(テストは追加済み・未実行)
- `:CURSor:XY:*` はXY水平時間軸の対応が前提のためM2スコープ外(`mode="xy"` の受理のみ)
- DHO800/900系の `:CURSor` / `:COUNter` / `:DVM` / `:HISTogram` 宣言はM2スコープ外(別ガイドの逐語解読が未了。[device-profiles.md](../device-profiles.md) 6.2)
