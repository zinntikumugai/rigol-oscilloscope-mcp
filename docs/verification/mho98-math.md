# MHO98 MATH演算(:MATH<n>)実機検証記録

**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**状態:** **未実施**(本文書は手順のスケルトン。実測を取ったら各項目のチェックを埋め、表に実際の応答を書き込む)
**規範:** [tools.md](../tools.md) 11章 / [roadmap.md](../roadmap.md) 2.5.1
**前提:** MHO900-BND適用済み。手順規律は [mho98-afg.md](mho98-afg.md) と同じ(1コマンド → 応答5s timeout → `:SYSTem:ERRor?` → 記録。**沈黙したら空行付き再接続 → `*IDN?` で復旧確認 → エラーキューをドレイン**)
**安全条件:** write項目は全て「現在値取得 → set → readback → finally で復元」。MATHは表示・解析層のみの操作で出力を伴わないが、**検証前にMATH1〜4の現在設定をスナップショットしておくこと**

```bash
RIGOL_TEST_ADDRESS=<実機IP> uv run pytest -m device -k math                          # read-only
RIGOL_TEST_ADDRESS=<実機IP> RIGOL_TEST_ALLOW_WRITE=1 uv run pytest -m device_write -k math
```

実機アドレスは `RIGOL_TEST_ADDRESS` 環境変数でのみ渡す(本文書を含め、リポジトリ内のいかなるファイルにもIP・シリアルを書かない。例示が要るなら TEST-NET の `192.0.2.10` を使う)。

## プローブ順序

**順序に意味がある。** (a) は fail-dangerous(沈黙すると以降のプローブが全て巻き添えになる)ため最初に単独で行い、write系は read-only が全て通ってから着手する。

### (a) 全MATH表示OFFでの `get_math_state` — 沈黙の有無 ☐

**問い:** MATHトレースが無効(表示OFF)の状態で `:MATH<n>:...?` 系のクエリを送ると、SCPIサーバーが沈黙するか。

`:BUS` と同様に「無効時のクエリは送らない」設計へ倒す必要があるかどうかが決まる。実装は `get_math_state` が `:MATH<n>:DISPlay?` を**最初に**読む構造にしてあるので、沈黙するなら同点を短絡点にする局所修正で済む。

手順: 本体で MATH1〜4 の表示を全てOFF → `:MATH1:DISPlay?` を単発で送る → 応答とエラーキューを記録 → 応答があれば `get_math_state()`(全チャンネル)を実行。

| コマンド | 応答 | エラーキュー | 所見 |
|---|---|---|---|
| `:MATH1:DISPlay?` | | | |
| `:MATH1:OPERator?`(表示OFFのまま) | | | |
| `get_math_state()`(全4本) | | | |

判定: ☐ 沈黙しない(現行実装のまま)/ ☐ 沈黙する(→ `:DISPlay?` が false なら以降を送らない短絡を追加)

### (b) `:MATH1:OPERator?` の工場出荷デフォルト ☐

**問い:** OPERator の readback がどのトークンで返るか(短形か長形か)。

プロファイル `math_operators` は長形(`SUBTract` 等)で宣言しており、`_afg_enum` が短形・長形の両方を受理する実装になっている。ガイド3.16.2 の Return Format 記載を**実測で裏取り**する項目。

| コマンド | 応答 | 宣言(長形) | 一致 |
|---|---|---|---|
| `:MATH1:OPERator?` | | `ADD` ほか21種 | |
| `:MATH1:SOURce1?` / `:SOURce2?` | | `CHANnel<n>` / `REF<n>` / `MATH<m>` | |
| `:MATH1:DISPlay?` / `:INVert?` | | `0` / `1` | |

### (c) FFTトレースのプリアンブルとピーク表の実フォーマット ☐

**問い(3つ):**

1. `:WAVeform:SOURce MATH1` + `:WAVeform:PREamble?` の **xincrement は Hz/pt か**(`x_unit: "Hz"` と `note` の文言を確定する)。xorigin は開始周波数か
2. `:MATH1:FFT:SEARch:RES?` の**実際の書式**(`UNIT VRMS` と `DB` の両方で。ガイド3.16.30 の例は `5,6.50125MHz,-32.34dBV`)
3. ピーク表の**行区切りは改行か `;` か**。**LAN transport の `query()` は1行しか読まない**ため、本当に複数行で返る場合は**応答が切り詰められる**(実装が明示している未解決リスク)。切り詰められるなら `query()` 側に複数行読みを足すか、`search_num` を1に制限する等の対処が要る

手順: **前面パネルでFFTを設定**(MATH1 = FFT、入力CH1、ピーク探索ON)してから、以下を read-only で送る。

| コマンド | 応答 | 所見 |
|---|---|---|
| `:MATH1:OPERator?` | | `FFT` を確認 |
| `:MATH1:FFT:SOURce?` / `:WINDow?` / `:UNIT?` / `:MODE?` | | |
| `:MATH1:FFT:FREQuency:STARt?` / `:END?` | | |
| `:WAVeform:SOURce MATH1` → `:WAVeform:SOURce?` | | 受理されるか |
| `:WAVeform:PREamble?` | | **xincrement / xorigin の単位判定** |
| `:MATH1:FFT:SEARch:ENABle?` / `:NUM?` / `:ORDer?` | | |
| `:MATH1:FFT:SEARch:RES?`(`UNIT VRMS`) | | 生バイト列をそのまま記録(区切り文字の判定) |
| `:MATH1:FFT:SEARch:RES?`(`UNIT DB`) | | 振幅単位の末尾トークン(`dBV` 等) |

判定: ☐ 1行 / ☐ `;` 区切りの1行 / ☐ 複数行(**要 `query()` 対応**)

### (d) `configure_math` の往復(write) ☐

「現在値取得 → set → readback → finally 復元」パターン。`tests/device/test_write.py` へ追加する。

- スナップショット: `get_math_state(1)`
- 書き込み: `configure_math(1, display=True, operator="add", source1="CH1", source2="CH2")` → `applied` を確認
- 続けて FFT 経路: `configure_math(1, operator="fft", fft={"source": "CH1", "window": "hanning", "unit": "vrms"})`
- finally: スナップショットの値で全項目を復元し、`display` を元へ戻す

| 項目 | requested | applied | エラーキュー | 所見 |
|---|---|---|---|---|
| operator | | | | |
| source1 / source2 | | | | |
| fft.window / fft.unit | | | | |
| scale / offset_v | | | | クランプ・スナップの有無 |

### (e) 表示OFF中の書き込み無視quirk ☐

**問い:** 表示OFFのMATHトレースへ `:SCALe` 等を書くと、AFGの `MOD:STATe` OFF(→ [mho98-afg.md](mho98-afg.md) 6章)や表示OFFチャンネルの `:SCALe`(→ [mho98-mvp.md](mho98-mvp.md) 3.3)と同じく**エラーなく無視される**か。

現行実装は「表示ONを最初・OFFを最後」に送る順序でこれを緩和しているが、quirkが実在するなら AFG式の**送信前拒否**(パラメータのみ指定 + 表示OFF は `INVALID_PARAMETER`)を足す判断材料になる。

手順: MATH1 表示OFF → `:MATH1:SCALe <値>` → エラーキュー → readback → 表示ON → 同じ値を書いて readback 比較 → 復元。

| 状態 | 書き込み | エラーキュー | readback | 無視されたか |
|---|---|---|---|---|
| 表示OFF | `:MATH1:SCALe` | | | |
| 表示ON | `:MATH1:SCALe`(同値) | | | |

判定: ☐ quirkなし(現行の送信順のみで十分)/ ☐ quirkあり(→ 送信前拒否を追加)

### (f) FFT + ピーク表 + `capture_waveform("MATH1")` のE2E ☐

`configure_math` で FFT を組み、`get_math_state` でピーク表を読み、`capture_waveform("MATH1")` でスペクトルの点列を取るまでの通し。既知の信号(AFGループバック、またはプローブ補償出力)を入力にして**ピーク周波数が既知値と一致するか**を確認する。

- 入力: 既知の周波数の信号(結線は [mho98-afg.md](mho98-afg.md) 5章のループバック構成が使える)
- `get_math_state(1)["peaks"]` の `frequency_hz` が既知値と一致するか(パーサの周波数サフィックス換算の裏取り)
- `capture_waveform("MATH1")` の `x_unit` / `sample_interval_s` / `time_origin_s` が (c) の判定と整合するか
- `analyze_waveform("MATH1")` が `INVALID_PARAMETER` で拒否されるか(設計どおりの拒否)
- 非FFTのMATH(例: `operator="add"`)では `capture_waveform` が従来どおり時間軸で返るか

| 確認 | 期待 | 実測 |
|---|---|---|
| `peaks[0].frequency_hz` | 入力の既知周波数 | |
| `capture_waveform` の `x_unit` | `"Hz"` | |
| `effective_sample_rate_sa_per_s` | **返らない** | |
| `analyze_waveform("MATH1")` | `INVALID_PARAMETER` | |
| 非FFT MATH の `capture_waveform` | 時間軸・`x_unit` なし | |

## 実装への含意

(実測後に記入。(c) と (e) の結果は実装変更を伴う可能性がある)

## 未実施・今後の予定

- 上記 (a)〜(f) の全項目
- DHO800/900系の `:MATH<n>` 宣言はM1スコープ外(別ガイドの逐語解読が未了。[device-profiles.md](../device-profiles.md) 6.2)
