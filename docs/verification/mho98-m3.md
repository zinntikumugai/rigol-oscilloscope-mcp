# MHO98 リファレンス波形(M3)実機検証記録

**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**実施日:** 2026-08-28
**状態:** **実施済み**(SCPIレベルのプローブ・実装コードのE2E・実機テストのpytest実行を全て実施。**未解決の観測事実2件は6章**)
**規範:** [tools.md](../tools.md) 13章(M3の完了記録は [roadmap.md](../roadmap.md) 末尾のコメント 2.5.3。未解決の残件は同 6章)
**前提:** MHO900-BND適用済み。手順規律は [mho98-math.md](mho98-math.md) / [mho98-m2.md](mho98-m2.md) と同じ(1コマンド → 応答5s timeout → `:SYSTem:ERRor?` → 記録。**沈黙したら空行付き再接続 → `*IDN?` で復旧確認 → エラーキューをドレイン**)
**安全条件:** write項目は全て「現在値取得 → set → readback → finally で復元」。リファレンス波形は**表示・比較層のみ**の操作で、取り込み条件にも出力にも触れない。**ただし `:REFerence:SAVE` だけは不可逆**(枠の内容が失われる)ため、保存を伴うテストは追加の環境変数 `RIGOL_TEST_ALLOW_REF_SAVE=1` でゲートし、**未使用と分かっている枠(REF10)**へのみ実行した

```bash
RIGOL_TEST_ADDRESS=<実機IP> uv run pytest -m device -k reference
RIGOL_TEST_ADDRESS=<実機IP> RIGOL_TEST_ALLOW_WRITE=1 uv run pytest -m device_write -k reference
RIGOL_TEST_ADDRESS=<実機IP> RIGOL_TEST_ALLOW_WRITE=1 RIGOL_TEST_ALLOW_REF_SAVE=1 \
  uv run pytest -m device_write -k reference        # 保存(不可逆)を含む
```

実機アドレスは `RIGOL_TEST_ADDRESS` 環境変数でのみ渡す(本文書を含め、リポジトリ内のいかなるファイルにもIP・シリアルを書かない。例示が要るなら TEST-NET の `192.0.2.10` を使う)。

## 結論(実装への影響)

**実装バグ1件が判明し修正済み。それが本検証の最大の収穫である(4章)。**

- **`:REFerence:COLor?` の緑は、ガイドが書く `GRE` ではなく `GREE` が返る。** 列挙値の照合が短形式・長形式の2形しか受理していなかったため、**工場出荷状態(枠4・枠9が緑)の実機で `get_reference_state()` が丸ごと落ちていた**。共有の列挙マッチャを前置一致ベースに変更して修正し、**同時に `TIMe` / `TIMeout` の短形式衝突という潜在バグ1件も塞いだ**(4章)

併せて確定した事項:

- **ガイド3.20.2 の Remark「現在有効なチャンネルのみソースに選べる」はこのファームでは成り立たない**(2章)。ホスト側で表示状態を検証してはならない(正当な操作を塞ぐことになる)
- **ラベルは引用符なしで返る**(`TESTLBL`)。**全10枠を舐める読みでもエラーキューは終始 `0,"No error"`**(沈黙なし)

## 1. 工場出荷状態の全10枠(read-only)

`get_reference_state()`(= 1枠あたり6クエリ × 10枠 = 60クエリ)の実測値。**逐語**で記録する。

```
REF1  source=CHAN1 vscale=5.000000E-2 voffset=0.000000 color=ORAN label=REF1
REF2  source=CHAN1 vscale=5.000000E-2 voffset=0.000000 color=RED  label=REF2
REF3  source=CHAN1 vscale=5.000000E-2 voffset=0.000000 color=BLUE label=REF3
REF4  source=CHAN1 vscale=5.000000E-2 voffset=0.000000 color=GREE label=REF4
REF5  source=CHAN1 vscale=5.000000E-2 voffset=0.000000 color=GRAY label=REF5
REF6  source=CHAN1 vscale=5.000000E-2 voffset=0.000000 color=ORAN label=REF6
REF7  source=CHAN1 vscale=5.000000E-2 voffset=0.000000 color=RED  label=REF7
REF8  source=CHAN1 vscale=5.000000E-2 voffset=0.000000 color=BLUE label=REF8
REF9  source=CHAN1 vscale=5.000000E-2 voffset=0.000000 color=GREE label=REF9
REF10 source=CHAN1 vscale=5.000000E-2 voffset=0.000000 color=GRAY label=REF10

:REFerence:LABel:ENABle? -> 0
読み取り後のエラーキュー: 0,"No error"
```

読み取れること:

- **ソース・垂直スケール・垂直オフセットは全10枠で同一**(`CHAN1` / `5.000000E-2` / `0.000000`)。枠ごとの初期差は色とラベルだけである
- **色は5色を巡回する**: ORAN → RED → BLUE → GREE → GRAY。**枠 n の色は (n−1) mod 5** で決まる(枠6以降が枠1〜5の繰り返し)。したがって**工場出荷状態でも枠4と枠9は緑**であり、これが4章のバグを「未操作の実機で必ず踏む」ものにしていた
- ラベルの初期値は枠番号そのもの(`REF<n>`)、ラベル表示はOFF
- **全枠問い合わせは安全**: 10枠すべてが正常応答し、沈黙も応答のずれも起きず、エラーキューは終始 `0,"No error"`。`get_reference_state()`(ref省略)の集約読みはそのまま使える

## 2. 枠1への書き込み往復(各書き込みの前にエラーキューを空にした)

| 送信 | 結果 | read-back | 所見 |
|---|---|---|---|
| `:REFerence:COLor 1,GREen` | OK | `GREE` | **ガイド3.20.7 は `GRE` と記載**(4章) |
| `:REFerence:COLor 1,ORANge` | OK | `ORAN` | |
| `:REFerence:LABel:CONTent 1,TESTLBL` | OK | `TESTLBL` | **引用符は付かない** |
| `:REFerence:VSCale 1,0.5` | OK | `5.000000E-1` | NR3 |
| `:REFerence:VOFFset 1,0.2` | OK | `2.000000E-1` | NR3 |
| `:REFerence:SOURce 1,CHANnel1` | OK | `CHAN1` | |
| `:REFerence:SOURce 1,CHANnel4`(**CH4の表示をOFFにしてから**) | OK | `CHAN4` | **ガイドのRemarksは不成立**(下記) |
| `:REFerence:RESet 1` | OK(エラーなし) | `5.000000E-1` / `2.000000E-1` のまま | **既定へ戻らない**(6.1) |

復元後の REF1 は1章の採取値と**完全に一致**した。

含意:

- **ガイド3.20.2 の Remark「Only the enabled channel can be selected as the source」は、このファームウェアでは成り立たない。** CH4の表示をOFFにした状態で `:REFerence:SOURce 1,CHANnel4` を送るとエラー無しで受理され、`CHAN4` が読み戻る。→ **ホスト側では表示状態を検証しない**(検証を入れると、機器が受理する正当な操作を塞ぐうえ、判定のために毎回 `:CHANnel<n>:DISPlay?` を1本余計に読むことになる)
- **ラベルは引用符なしで往復する**。実装も引用符を付けずに埋め込むが、そのぶん `;` や空白が入るとコマンドが壊れるため、**送信前にホワイトリスト(英数字と `_` `.` `+` `-`)で検証する**
- `:RESet` の挙動は 6.1 へ

## 3. 送信順(`reset` → 設定 → `save`)

`configure_reference` は1回の呼び出しの中で **`reset` を設定より前、`save` を設定より後**に送る。理由はそれぞれ:

- **`reset` が後だと同じ呼び出しの `scale` / `offset_v` を捨てる**。`:REFerence:RESet` は垂直スケール/オフセットを既定へ戻す**設定**であり、利用者が明示した値より前に置くのが正しい
- **`save` が前だとソース選択が間に合わない**。`:REFerence:SAVE` は「その時点のソースの波形を焼き込む」操作なので、`source` を含む設定が先に届いている必要がある

実機テスト `test_configure_reference_reset_runs_before_the_settings` は、`reset=True` と `scale` / `offset_v` を**同じ呼び出しで**指定したとき、read-back が**指定した値**になる(既定へ戻らない)ことを確認している。

## 4. `GREE` — ガイドの Return Format は当てにならない(実装バグ1件・潜在バグ1件)

**本検証の中心的な発見。**

ガイド3.20.7 は `:REFerence:COLor?` の返却を **`GRE`**(緑)と書いている。**実機が返すのは `GREE`** である。

問題の広がり方:

1. 実装の列挙マッチャは、プロファイルの対応表(`green: GREen`)から**短形式 `GRE` と長形式 `GREEN` の2形だけ**を受理していた
2. `GREE` はそのどちらでもないため `SCPI_ERROR` になる
3. **工場出荷状態の枠4・枠9は緑**(1章)。したがって `get_reference_state()`(全枠読み)は**未操作の実機で必ず落ちる**

SCPI規格上、機器は**短形式以上・長形式以下の任意の略形**で応答してよい。ガイドの Return Format 欄はその一例を書いているにすぎず、**規範として当てにできない**。

**修正**(`driver/decode.py` の `_enum`。decode / AFG / MATH / cursor / counter / meter / histogram / reference の**全列挙が通る共通経路**):

1. 短形式・長形式との**完全一致を最優先**する(規範の2形は必ず一意に読む)
2. 完全一致が無ければ**前置一致**で探し、**短形式の長さ以上**の略形なら受理する(`GREE` → `green`)
3. **候補が2個以上なら黙って片方を選ばず `SCPI_ERROR`** にする。読み値はそのまま書き戻される値であり、推測で確定させると誤設定になる

**同時に塞いだ潜在バグ:** 旧実装は「トークン → 意味的な値」の**dict**で持っていたため、**別々の値の短形/長形が同じトークンになる表があると、後勝ちで片方が黙って消えて**いた(例: `TIMe` と `TIMeout` を同じ表に持つと短形式がどちらも `TIM` になる)。現在は「トークン → 意味的な値の**集合**」で持ち、衝突したトークンは曖昧として `SCPI_ERROR` にする(黙って誤った値を返さない)。

**現行の全テーブル**(プロファイルの `dialect` 全表 + `driver/decode.py` のハードコード表)を機械的に走査した限り、**実際に衝突する組は1つも無い**(`math_fft_search_orders` の `AMPorder` / `FREQorder` も `counter_modes` も接頭辞が重ならず、SPIの `frame_mode` も `CS` / `TIM` / `TIMEOUT` で衝突しない)。したがってこの潜在バグは**現時点では発火していない** — 3. と集合表現は、将来テーブルが増えたときのための安全側の既定である。

## 5. 実機テストの実行結果(pytest経由・2026-08-28)

| スイート | 結果 |
|---|---|
| `-m device`(read-only) | **18 passed** |
| `-m device_write`(`RIGOL_TEST_ALLOW_WRITE=1`) | リファレンスの往復 + reset順序の**2件 passed** |
| 同上 + `RIGOL_TEST_ALLOW_REF_SAVE=1` | 保存を含む**3件 passed** |

- read-only側は全10枠の読み取り(1章)とエラーキュー確認を含む
- write側は「現在値取得 → set → readback → finally で復元」で、復元後の値が採取値と一致することまで確認済み
- 保存テストは **REF10 へ実行**した。**この枠に入っていた内容は復元不能**であり、そのために環境変数を別立てにしている
- **監査ログに `configure_reference` の記録があることを確認**(before / after が揃っていることもテストが検証している)

## 6. 未解決(観測はあるが原因を確定していない)

### 6.1 保存済み波形の無い枠では `:REFerence:RESet` が効かないように見える

一度も `:REFerence:SAVE` していない枠1に対し:

```
:REFerence:VSCale 1,0.5     -> read-back 5.000000E-1
:REFerence:VOFFset 1,0.2    -> read-back 2.000000E-1
:REFerence:RESet 1          -> エラーキュー 0,"No error"
:REFerence:VSCale? 1        -> 5.000000E-1   (既定の 5.000000E-2 に戻らない)
:REFerence:VOFFset? 1       -> 2.000000E-1   (既定の 0.000000 に戻らない)
```

**エラーは積まれないまま、値も戻らない。** 枠に保存済み波形が無いことが条件だと推測できるが、**観測は1件**であり確定していない(保存済みの枠でも同じかどうかは未確認 — 確認するには枠を1つ潰す必要がある)。

対応:

- **フェイク機器(`testing/`)にはこの挙動をモデル化していない**(条件が不確定なため。確定したら追加する)
- **実装側の対処は不要**。`configure_reference` は常に read-back した `applied` を返すので、呼び出し側が「戻ったはず」と仮定しない限り実害は無い。[tools.md](../tools.md) 13章と `reset_reference` の docstring に「戻ったと仮定せず `applied` を見よ」と明記した

### 6.2 枠にデータが入っているかを知る手段が無い

ガイド3.20章に**該当のクエリが存在しない**。`:REFerence:SAVE` が不可逆であるにもかかわらず、**保存前に「その枠は空か」を確認する方法が機器側に無い**ということである。

- したがって `configure_reference(save=true)` の判断は**利用者にしか下せない**。Tool description と [tools.md](../tools.md) 13章の双方に「送る前に人間へ確認せよ」と明記した
- 現状の確認手段は**本体画面を撮る(`capture_screenshot`)以外に無い**

## 未実施・今後の予定

- **6.1 の `:RESet`** — 保存の有無が条件かどうかの切り分け(枠を1つ潰す覚悟がいるため保留)
- **`:REFerence:CURRent` は意図的にスキップ**(前面パネルの選択状態。他の全コマンドが枠番号を明示的な引数で取るため、依存するものが無い → [roadmap.md](../roadmap.md) 3章「実装しないと決めたもの」)
- **REF波形のデータ取得は不可**と確定済み(`:WAVeform:SOURce` の値域はガイド3.28.1に `{CHANnel1-4|MATH1-4}`)。ホスト側で数値比較したい場合は **MATHの減算経由**(`configure_math(operator="subtract", source1="CH1", source2="REF1")` → `capture_waveform("MATH1")`)を使う。この経路の実機での往復確認は未実施
- DHO800/900系の `:REFerence` 宣言はM3スコープ外(別ガイドの逐語解読が未了。[device-profiles.md](../device-profiles.md) 6.2)
