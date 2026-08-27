# MHO98 MATH演算(:MATH<n>)実機検証記録

**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**実施日:** 2026-08-27
**状態:** **実施済み**((a)〜(d)・(f) は実測で確定。**(e) 表示OFF中の書き込み無視quirkのみ未実施**)
**規範:** [tools.md](../tools.md) 11章 / [roadmap.md](../roadmap.md) 2.5.1
**前提:** MHO900-BND適用済み。手順規律は [mho98-afg.md](mho98-afg.md) と同じ(1コマンド → 応答5s timeout → `:SYSTem:ERRor?` → 記録。**沈黙したら空行付き再接続 → `*IDN?` で復旧確認 → エラーキューをドレイン**)
**安全条件:** write項目は全て「現在値取得 → set → readback → finally で復元」。MATHは表示・解析層のみの操作で出力を伴わない

```bash
RIGOL_TEST_ADDRESS=<実機IP> uv run pytest -m device -k math                          # read-only
RIGOL_TEST_ADDRESS=<実機IP> RIGOL_TEST_ALLOW_WRITE=1 uv run pytest -m device_write -k math
```

実機アドレスは `RIGOL_TEST_ADDRESS` 環境変数でのみ渡す(本文書を含め、リポジトリ内のいかなるファイルにもIP・シリアルを書かない。例示が要るなら TEST-NET の `192.0.2.10` を使う)。

## 結論(実装への影響)

実測で**3件の実装バグ**が判明し、いずれも修正済み。詳細は各節に記す。

1. **ピーク表は複数行応答**(改行区切り + 末尾に終端の空行)。`query()` は1行しか読まないため、読み残しが受信バッファに居座り**以降の全クエリがdesync**していた → `Transport.query_lines()` を追加し、ピーク表だけをその経路で読む
2. **ピーク表の振幅にSI接頭辞が付く**(`851.6mVrms`)。逐語保持では**1000倍ずれる** → 周波数列と同じ接頭辞換算を振幅列にも適用(`dB` 系の先頭 `d` は接頭辞ではないため除外)
3. **FFTプリアンブルのx軸は当初の想定と違う**。xincrement は Hz/pt ではなく **GHz/pt**(× 1e9 で Hz 刻み)、xorigin は**時間軸の値が残る**(開始周波数ではない)→ `capture_waveform` の返却キーを `frequency_step_hz` / `frequency_start_hz` に置き換え、時間軸前提のキーはFFTトレースでは返さない

## プローブ結果

### (a) 全MATH表示OFFでの `get_math_state` — 沈黙の有無 ☑ 沈黙しない

MATHチャンネルの表示がOFFでも `:MATH<n>:...?` 系のクエリは**正常に応答する**(MATH1〜4の全チャンネルで確認)。`:BUS` のような「無効時はクエリを送らない」短絡は**不要**。

| コマンド | 応答 | 所見 |
|---|---|---|
| `:MATH<n>:DISPlay?`(n=1..4、表示OFF) | `0` | 4本とも即応。沈黙なし |
| `:MATH1:OPERator?`(表示OFFのまま) | `ADD` | 沈黙なし |
| `get_math_state()`(全4本) | 正常終了 | 表示OFFでも共通5項目 + 演算子依存項目が読める |

判定: ☑ **沈黙しない**(現行実装のまま。`:DISPlay?` を最初に読む順序も維持)

### (b) `:MATH1:OPERator?` の工場出荷デフォルト ☑

| コマンド | 応答 | 宣言(長形) | 一致 |
|---|---|---|---|
| `:MATH1:OPERator?` | `ADD` | `ADD` ほか21種 | ☑ 短形で返る。`_afg_enum` の短形受理で `"add"` へ正しく写像 |
| `:MATH1:DISPlay?` | `0` | `0` / `1` | ☑ |

### (c) FFTトレースのプリアンブルとピーク表の実フォーマット ☑

#### c-1. プリアンブルのx軸(**当初の想定と異なる**)

`:WAVeform:SOURce MATH1` + 演算子FFT + `:WAVeform:PREamble?` を、表示周波数範囲を変えながら3通り測定(開始周波数はいずれも0)。

| `:MATH1:FFT:FREQuency:END?` | points | xincrement | xorigin |
|---|---|---|---|
| `1.825000E+4`(18250 Hz) | 1000 | `1.825e-08` | `-0.0025` |
| `3.650000E+4`(36500 Hz) | 1000 | `3.65e-08` | `-0.0025` |
| `1.000000E+5`(100000 Hz) | 1000 | `1e-07` | `-0.0025` |

読み取れる関係(3通り全てで厳密に一致):

- **周波数刻み[Hz] = xincrement × 1e9**(機器は刻みを GHz 単位で報告している)
- **points × 刻み = 設定した表示終端周波数**
- **xorigin は開始周波数ではない**。同一機器のアナログchの参照値は `xincrement=5.000000E-6, xorigin=-2.500000E-3` であり、FFT時の `-0.0025` は**時間軸の値がそのまま残っている**だけ

→ 実装は `sample_interval_s` / `time_origin_s` を返すのをやめ、`frequency_step_hz`(= xincrement × 1e9)と `frequency_start_hz`(`:MATH<n>:FFT:FREQuency:STARt?` から読む)を返す。`yorigin` は動的(実測 `-87`)で、実装は既にライブ値を使っている。

`:WAVeform:STARt` / `:STOP` は**ソースが MATH1 でも受理される**(`STOP 16` → プリアンブル points 16、ブロックヘッダ `#9000000016`)。`max_points` はMATHトレースでも効く。

#### c-2. ピーク表の書式(**複数行**)

`:MATH1:FFT:SEARch:RES?` の生バイト列(ピーク5本、`UNIT DB`):

```
b'1,9.09061kHz,-1.373dBV\n2,27.0239kHz,-20.45dBV\n3,45.0798kHz,-29.34dBV\n4,63.0150kHz,-35.15dBV\n5,117.998Hz,-38.98dBV\n\n'
```

`UNIT VRMS` に切り替えた同条件:

```
b'1,9.09294kHz,851.6mVrms\n2,27.0220kHz,94.68mVrms\n3,45.0827kHz,34.15mVrms\n4,63.0178kHz,17.47mVrms\n5,129.047Hz,14.51mVrms\n\n'
```

ピークが見つからないとき(探索OFF含む)の応答は **`b'\n'`(空行1本)**。

判定: ☐ 1行 / ☐ `;` 区切りの1行 / ☑ **複数行**(改行区切り、`;` は一切現れない。**末尾に終端の空行が1本つく**)

含意:

- `query()` は1行しか読まないため、**先頭行しか取れず残り4行+空行が受信バッファに残る**。以降のクエリは前問の応答を読み続け、セッション全体がdesyncする(下の「観測した障害」参照)→ `Transport.query_lines()` を追加(LAN / USB / Fake の3実装 + Protocol)
- 振幅列に**SI接頭辞が付く**(`851.6mVrms` = 0.8516 Vrms)。逐語保持では1000倍ずれる → 周波数列と同じ換算を適用。ただし `dBV` / `dBm` の先頭 `d` は**デシ接頭辞ではない**ため、dB系は換算対象から明示的に外す
- 周波数列のサフィックスは `kHz` / `Hz` を実測(既存の Hz/kHz/MHz/GHz 表でカバー済み)

#### 観測した障害: 読み残しによる `ConnectionResetError`

プローブ中に1度、`get_math_config()` が `:MATH1:FFT:SEARch:EXCursion?`(単独では正常に応答するクエリ)で `ConnectionResetError` を起こしてセッションが落ちた。原因は**このピーク表の読み残し**で、`RES?` の2行目以降が受信バッファに残ったまま後続クエリが進み、応答と質問の対応が全てずれた結果、機器側が接続を切った。

- **回復手順:** 再接続すれば復旧する(`LanTransport.open()` が接続時に空行を1本送り、`ConnectionManager.require_scope()` が再接続とエラーキューのdrainを行う)。未定義ヘッダによる**沈黙**とは別の現象で、機器がwedgeしたわけではない
- **恒久対策:** ピーク表を `query_lines()` で終端の空行まで読み切る(読み残しを作らない)。FakeScopeも実機と同じ書式(行 + 終端の空行、探索OFF時は空行1本)を返すようにし、`FakeTransport.query()` も実機同様「1行だけ読む」意味論に揃えて、同種の実装ミスがユニットテストで露見するようにした

### (d) `configure_math` の往復(write)☑

SCPIレベルでの往復は上記プローブで確認済み。

| 項目 | 実測 | 所見 |
|---|---|---|
| `:MATH1:OPERator FFT` → `?` | 受理 → `FFT` | エラーキュー無し |
| `:MATH1:FFT:FREQuency:END`(18250 / 36500 / 100000) | 3値とも受理 → readback一致 | クランプ・スナップ無し(verbatim適用) |
| `:MATH1:FFT:UNIT`(`DB` ⇄ `VRMS`) | 受理 → ピーク表の単位列が追随 | |
| `:MATH1:FFT:SEARch:EXCursion?` | `1.8` 相当を返す | 単独では正常(desync時のみ失敗した) |

自動テスト `tests/device/test_write.py::test_configure_math_round_trip`(現在値取得 → 算術演算set → FFTset → readback → finally復元)を追加した。**このテスト自体の実機実行は未実施。**

### (e) 表示OFF中の書き込み無視quirk ☐ **未実施**

**問い:** 表示OFFのMATHトレースへ `:SCALe` 等を書くと、AFGの `MOD:STATe` OFF(→ [mho98-afg.md](mho98-afg.md) 6章)や表示OFFチャンネルの `:SCALe`(→ [mho98-mvp.md](mho98-mvp.md) 3.3)と同じく**エラーなく無視される**か。

現行実装は「表示ONを最初・OFFを最後」に送る順序でこれを緩和しているが、quirkが実在するなら AFG式の**送信前拒否**(パラメータのみ指定 + 表示OFF は `INVALID_PARAMETER`)を足す判断材料になる。

手順: MATH1 表示OFF → `:MATH1:SCALe <値>` → エラーキュー → readback → 表示ON → 同じ値を書いて readback 比較 → 復元。

| 状態 | 書き込み | エラーキュー | readback | 無視されたか |
|---|---|---|---|---|
| 表示OFF | `:MATH1:SCALe` | | | |
| 表示ON | `:MATH1:SCALe`(同値) | | | |

判定: ☐ quirkなし(現行の送信順のみで十分)/ ☐ quirkあり(→ 送信前拒否を追加)

### (f) FFT + ピーク表 + `capture_waveform("MATH1")` のE2E ☑

| 確認 | 期待 | 実測 |
|---|---|---|
| `peaks[n].frequency_hz` | 入力信号の周波数 | ☑ `9.09kHz` を基本波として高調波列(27.0 / 45.1 / 63.0 kHz)。サフィックス換算は正しい |
| `peaks[n].amplitude_unit` | 接頭辞を外した単位 | ☑ 修正後 `Vrms` / `dBV`(修正前は `mVrms` を逐語保持し値が1000倍ずれていた) |
| `capture_waveform` の `x_unit` | `"Hz"` | ☑ |
| `frequency_step_hz` | xincrement × 1e9 | ☑ 3通りの表示範囲すべてで points × 刻み = 終端周波数 |
| `frequency_start_hz` | `:FFT:FREQuency:STARt?` の値 | ☑ プリアンブルの xorigin では代用できない(時間軸の値が残る) |
| `effective_sample_rate_sa_per_s` | **返らない** | ☑ |
| `:WAVeform:STARt` / `:STOP` | MATHソースでも有効 | ☑ `STOP 16` → points 16 |
| 非FFT MATH の `capture_waveform` | 時間軸・`x_unit` なし | ☑ 既定の `ADD` で従来どおり |

## 未実施・今後の予定

- **(e) 表示OFF中の書き込み無視quirk** — 唯一の残件。結果次第で送信前拒否を追加する
- `tests/device/test_write.py::test_configure_math_round_trip` の実機実行(テストは追加済み・未実行)
- DHO800/900系の `:MATH<n>` 宣言はM1スコープ外(別ガイドの逐語解読が未了。[device-profiles.md](../device-profiles.md) 6.2)
