# MHO98 測定の全面開放(M4)/ トリガ種別の開放(M5)実機検証記録

**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**実施日:** 2026-08-29
**状態:** **実施済み**(read-only 19 passed / write 25 passed + **トリガ16種の全数往復検証**。**実装バグ6件を発見し修正済み**)
**規範:** [tools.md](../tools.md) 14章(M4)/ 15章(M5)
**前提:** MHO900-BND適用済み。手順規律は [mho98-math.md](mho98-math.md) / [mho98-m2.md](mho98-m2.md) と同じ(1コマンド → 応答5s timeout → `:SYSTem:ERRor?` → 記録)
**安全条件:** write項目は全て「現在値取得 → set → readback → finally で復元」。50Ω・autoset・factory reset は一切行っていない。プローブで変更した設定(しきい値・振幅算出方式・統計の有効化・Resultビュー)は**最後に作業前の状態へ戻したことを読み出しで確認済み**

## 結論(実装への影響)

実測で**実装バグ6件**が判明し、いずれも修正済み(テスト付き)。加えて**機器の実用上の限界を1つ**と**ガイドの誤植を1つ**確定した。

M4(測定):

1. **`:MEASure:SETup:MAX/MID/MIN` に小数形を送ると黙って上限へ張り付く** — エラーは積まれない。最も危険な種類の不具合
2. **`:MEASure:AMP:TYPE` は長形 `MANual` が拒否され、短形 `MAN` が通る**
3. **同時に有効化できる測定項目数に実用上の限界がある**(16項目以上で収束しない)

M5(トリガ):

4. **`get_trigger_position` が `NameError`** — 存在しないヘルパ名を呼んでいた(ユニットテストの穴)
5. **`pattern` / `duration` の `pattern` は ch別のリスト**を取る。単一の列挙値として実装していたため両種別が丸ごと使えなかった
6. **`:TRIGger:LIN:DATA?` は2進のビットマスク文字列を返す**。整数として実装していたため LIN トリガが読めなかった

**V-9(`:TRIGger:WINDows:SLOPe` の綴り)も決着**: ガイドの Range欄 `RFALI` は誤植で、実機は `-222` で拒否する。

**検証の教訓:** 最初は write スイート(edge / pulse / i2c の3種)だけを回して「実機検証済み」と判断しかけた。**16種すべてを実際に送って初めてバグ3件(上記4〜6)が出た。** 項目表が同じ機構でも、種別ごとの応答形式は実際に送らないと分からない。

---

## 1. しきい値は整数形で送らないと黙って壊れる(最重要)

`configure_measurement(threshold_max=88.0, ...)` が `:MEASure:SETup:MAX 88.0` を送り、**エラーキューは `0,"No error"` のまま値が 100 に張り付いた**。連鎖して MID=99 / MIN=98 になる(上限側から順に詰められる)。

| 送信 | 読み戻し | エラーキュー |
|---|---|---|
| `:MEASure:SETup:MAX 88.0` | `1.000000E+02` | `0,"No error"` |
| `:MEASure:SETup:MAX 88` | `8.800000E+01` | `0,"No error"` |

ガイド3.17.9-11 の `<value>` の型は **Integer**。percentモードでは小数形を機器が誤解釈する。

**absoluteモードでは小数が必要**(電圧なので分解能が要る)ことも確認した:

| モード | 送信 | 読み戻し |
|---|---|---|
| ABSolute | `:MEASure:SETup:MAX 0.5` | `5.000000E-01` |
| ABSolute | `:MEASure:SETup:MID 0.25` | `2.500000E-01` |
| ABSolute | `:MEASure:SETup:MAX 1` | `1.000000E+00` |

**absoluteモードは整数形も受理する**ため、対処は「**整数値のときだけ小数点を落とす**」で両モードに効く。`driver/scope.py` の項目種別 `"threshold"` として実装した。

## 2. `:MEASure:AMP:TYPE` は短形のみ

| 送信 | エラーキュー |
|---|---|
| `:MEASure:AMP:TYPE MANual` | **`-222,"Data out of range"`** |
| `:MEASure:AMP:TYPE MAN` | `0,"No error"` |
| `:MEASure:AMP:TYPE AUTO` | `0,"No error"` |

`VAVerage` が不可で `VAVG` が通るのと同じ癖。プロファイルの `measure_amp_types` を `manual: MAN` に変更した。

**`:MEASure:AMP:MANual:TOP` / `BASE` は長形を受理する**(`MAXMin` / `HISTogram` とも `0,"No error"`)。短形が要るのは `AMP:TYPE` だけ。

## 3. 同時に有効化できる測定項目数の限界

`:MEASure:ITEM?` はクエリ形でも項目をResultビューへ追加する(既知)。**同時に有効化した項目が多いと、一部が番兵値(±9.9E37)を返す。**

`:MEASure:DELete` で空にしてから項目数を変えて3回ずつ測定した結果(番兵になった項目数):

| 項目数 | 1回目 | 2回目 | 3回目 |
|---|---|---|---|
| 4 | 0 | 0 | 0 |
| 8 | 1 | 0 | 0 |
| 10 | 2 | 0 | 0 |
| 11 | 0 | 0 | 0 |
| 12 | 3 | 0 | 0 |
| **16** | 2 | **5** | **7** |
| **20** | 7 | **5** | **4** |

**12項目以下なら1巡目に数件出ても2巡目で収束する。16項目以上は収束せず、番兵になる項目は毎回変わる。** 33項目では毎回4〜9件が番兵だった。

単独で問い合わせれば必ず正常値が返る(番兵になった `VTOP` / `VAVG` / `ACRMs` を個別に読んで確認済み)ので、**項目そのものが測れないのではなく、機器が同時に更新し続けられる項目数に限りがある**。

**対処:** `service/measurement.py` に `RELIABLE_MEASUREMENT_BATCH = 12` を置き、超えた要求には警告を添える(拒否はしない — 呼び出し側の要求を勝手に狭めない)。

## 4. `get_trigger_position` の `NameError`

`:TRIGger:POSition?` を読む実装が、存在しないモジュール関数 `_readout` を呼んでいた(正しくはインスタンスメソッド `self._readout`)。**ユニットテストが1件も無かったため実機で初めて発覚した。** 回帰テストを追加済み。実機の応答は `0.0`。

## 5. 測定項目41種の実測(read-only、CH1のプローブ補償信号)

全41項目が応答した。代表値:

| 項目 | 値 | 項目 | 値 |
|---|---|---|---|
| frequency | 1000.0 Hz | vpp | 3.2475 V |
| rise_time | 1.2 µs | vamp | 3.0549 V |
| pulse_width_pos | 500 µs | overshoot | 0.031686(比率) |
| duty | 0.5(比率) | area | 3.0652e-3 V·s |
| time_at_vmax | -956.8 µs | slew_rate_pos | 1.7457e6 V/s |
| pulses_pos | 2 | edges_neg | 2 |

**2ソース項目(遅延・位相)8種**も CH1/CH2 で全て応答した(`delay_rise_rise` = -26.8 µs、`phase_rise_rise` = -9.288°)。`:MEASure:ITEM? RRDelay,CHANnel1,CHANnel2` のインライン2ソース指定が実機で通ることを確認。

所要時間は33項目で 4.817 秒(1項目あたり 0.146 秒)。**一度Resultビューに載った項目の再読は 0.03 秒**と4倍以上速い。

## 6. 測定統計(V-5 の決着)

**ガイドの記述どおり、`:MEASure:STATistic:ITEM? <type>,<item>` は科学表記の単一値を返す。** 区切り文字は存在しない(`<type>` を1つずつ指定する形式のため)。

| 送信 | 応答 |
|---|---|
| `:MEASure:STATistic:ITEM? MAXimum,VPP` | `6.1773E+00` |
| `:MEASure:STATistic:ITEM? DEViation,VPP` | `6.1941E-04` |
| `:MEASure:STATistic:ITEM? CNT,VPP` | `3.0000E+00` |

Tool経由の実測(有効化 → 読み出し):

```
vpp:       maximum=3.0921 minimum=3.0477 current=3.0757 average=3.0678 deviation=0.011666 count=19
frequency: maximum=9058.0 minimum=8912.7 current=9025.3 average=8991.6 deviation=37.84   count=25
```

**これがM4の主眼**だった「波形を1点も転送せずにばらつきを数値で示す」がそのまま成立している。

## 7. トリガ種別の切り替え(M5)

| 操作 | 結果 |
|---|---|
| `configure_trigger(type="pulse", settings={"when":"less","upper_width_s":1e-6})` | `applied={'type':'pulse','when':'less','upper_width_s':1e-06}` |
| `configure_trigger(type="i2c", settings={"when":"nack","address_bits":7,"address":42})` | `applied={'type':'i2c','when':'nack','address_bits':7.0,'address':42}` |
| 復元(元の `edge` へ) | `{'type':'edge','source':'CH1','level_v':0.18,'slope':'rising',...}` **復元漏れゼロ** |
| `:TRIGger:POSition?` | `0.0` |

`get_trigger` が `:TRIGger:MODE?` を先読みして**現在の種別のサブツリーだけ**を返すことを実機で確認した(edge時の `settings` は `source` / `level_v` / `slope` の3項目のみ)。

**ガイドの罠(MODEトークンとサブツリー名の不一致、`WINDow`/`:WINDows` と `SETup`/`:SHOLd`)は、いずれもユニットテストで固定済み。実機での送信は今回 pulse と i2c のみ**(他の14種は未送信)。

## 8. トリガ16種の全数往復検証

`configure_trigger` で種別を切り替え、種別固有の設定を書いて `get_trigger` で読み戻す、を16種すべてに対して実施した。**最後に元の種別・設定へ復元し、復元漏れゼロを確認済み。**

| 種別 | 結果 |
|---|---|
| edge / pulse / slope / timeout / runt / window / setup_hold / nth_edge | 往復OK |
| **pattern / duration** | **初回FAIL** → ch別リストとして実装し直して往復OK |
| **lin** | **初回FAIL** → DATA を2進マスクとして読むよう直して往復OK |
| uart / i2c / spi / can | 往復OK |
| delay | 往復OK。ただし `upper_time_s` は 9e-06 → 8.999999e-06 に量子化される(**機器の分解能。requested / applied の差として正しく現れている**) |

### 8.1 pattern / duration は ch別のリスト

```
:TRIGger:PATTern:PATTern?        -> 'H,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X,X'   (24個)
:TRIGger:PATTern:PATTern H,L,X,X -> 0,"No error"
:TRIGger:PATTern:PATTern?        -> 'H,L,X,X,X,...'                                      (先頭4個が反映)
:TRIGger:DURation:TYPE?          -> 'L,X,X,X'                                            (4個)
:TRIGger:DURation:TYPE L,X,H,L   -> 0,"No error"
```

ガイド3.27.12.1 / 3.27.13.2 の構文は `<pch1>[,<pch2>[,<pch3>[,<pch4>]]]` で CH1〜CH4 を一度に指定する。**書けるのはアナログch分だけだが、読み戻しは24個返る**(デジタル/MATH分と思われる)。実装はアナログch数までに切っている。

### 8.2 `:TRIGger:LIN:DATA?` は2進マスク

```
:TRIGger:LIN:DATA?      -> 'XXXXXXXX'   (未設定。X = don't care)
:TRIGger:LIN:DATA 100   -> 0,"No error"
:TRIGger:LIN:DATA?      -> '01100100'   (= 100 の2進表現)
:TRIGger:CAN:DATA?      -> '0'          (他プロトコルは素の整数)
```

ガイド3.27.24.9 の Return Format は「0 から 2^64-1 の整数を返す」だが**実機は違う**。全て0/1なら整数へ、`X` を含むならマスク文字列のまま返す実装にした。**この表現の違いは LIN だけ**で、CAN / SPI / I2C / RS232 の `:DATA?` は素の整数を返す。

### 8.3 種別を切り替えるとトリガレベルが伝播する

window トリガの `level_a_v`(0.48)を触ったあと edge へ戻したところ、`:TRIGger:EDGE:LEVel?` が元の 0.18 ではなく **0.48** になっていた。復元するときは**元の種別へ戻したうえでレベルも明示的に書き戻す**必要がある(本検証では手動で 0.18 へ戻して確認済み)。

## 9. V-9 決着: `:TRIGger:WINDows:SLOPe` の綴り

| 送信 | エラーキュー | 読み戻し |
|---|---|---|
| `:TRIGger:WINDows:SLOPe RFALl` | `0,"No error"` | `RFAL` |
| `:TRIGger:WINDows:SLOPe RFALI` | **`-222,"Data out of range"`** | `RFAL`(変わらず) |

**ガイド3.27.16.2 の Range欄 `RFALI`(大文字I)は誤植。** Remarks欄の `RFALl`(小文字L)が正しく、EDGE / TIMeout と同じ綴り。`trigger_window_slopes` に `either: RFALl` を宣言した。

## 10. 測定区間(カーソル領域)と `area="zoom"`

```
:MEASure:AREA CURSor          -> 0,"No error"
:MEASure:CREGion:CAX -0.0001  -> 0,"No error" / 読み戻し '-1.000000E-4'
:MEASure:CREGion:CBX 0.0001   -> 0,"No error" / 読み戻し '1.000000E-4'
:MEASure:CREGion:CABX?        -> '0'
:MEASure:AREA ZOOM            -> 0,"No error"     ← エラーを積まない
:MEASure:AREA?                -> 'CURS'           ← **値が変わっていない**
```

**`area="zoom"` は遅延掃引が無効だと無言で無視される**(ガイド3.17.19 Remarks のとおりだが、拒否ではなく無視)。`applied` が要求値と一致しないことで呼び出し側は検出できる。

## 11. 追加で判明した機器の挙動(Copilotレビュー対応時の再検証)

**`:HISTogram:RESet` の直後、ヒットが1つも溜まっていないと統計のシグマ由来の項目が `*****` になる。**

```
[Sum:0hits, Peaks:0hits, Max:0V, ..., Sigma:0V, meanPlusSigma:*****, meanPlus2Sigma:*****, meanPlus3Sigma:*****]
```

数値ではないためパーサは fail-open で `warnings` に載せる(設計どおり)。ヒットが溜まっていれば `meanPlus2Sigma:1.000000` のように数値で返る。

実機テストは**ヒット数0のときだけ警告を許す**形に直した(ヒットがあるのに警告が出たら異常として落とす)。単独実行では毎回2000超のヒットが溜まるため通るが、スイート全体では reset 直後に読むタイミングになり得る。

## 未実施・今後の予定

- **オプション必須のトリガ3種**(FlexRay / I2S / MIL-STD-1553)は未実装・未検証。ライセンスは適用済みなので実機検証の障害は無い
- **`VIDeo` トリガ**は優先度「低」のため未実装
- CURRbit / CODE(データのビット単位マスク編集)は未対応。LIN の `:DATA?` がマスクを返すことが分かったので、着手するならこの機構と合わせて設計する
- `area="zoom"` の実動作は遅延掃引(`:TIMebase:DELay:*`)の実装後に再検証する
