# MVP 実機検証結果(MHO98)

- 日付: 2026-08-25
- 機器: RIGOL MHO98, firmware 00.01.00(serial withheld), LAN SCPI :5555 で接続
- 対象: MVP(Phase 1 Read Only + Phase 2 Basic Control)
- 実行スイート:
  - read-only 9件 — `tests/device/test_readonly.py`(`RIGOL_TEST_ADDRESS` ゲート)
  - write 9件 — `tests/device/test_write.py`(`RIGOL_TEST_ADDRESS` + `RIGOL_TEST_ALLOW_WRITE=1` の二重ゲート)
- 結果: **全18件PASS。書き込みスイートの復元漏れゼロ**

Phase 0 の検証結果は [mho98-phase0.md](mho98-phase0.md)。本文書は MCP サーバー本体(サービス層まで含む)を実機に当てた記録である。

---

## 1. Read Only(9件 PASS)

### 1.1 接続・識別

- `identify`: `*IDN?` から `MHO98` を解決し、プロファイルは **mho98 / confidence=verified**
- 接続時のエラーキューdrainが効いており、接続直後の `:SYSTem:ERRor?` は `0,"No error"`(前セッションの残留なし)

### 1.2 状態取得

| 操作 | 実測 |
|---|---|
| `get_state`(全4セクション) | **1.500 s**(約39クエリ) |
| `get_state(sections=["trigger"])` | **0.267 s** |

`sections` 絞り込みの効果(約5.6倍)を実機で確認。全取得が1秒を超えることは Requirements 8.1 の想定どおり。

### 1.3 測定

- 10項目すべてを機器が受理: `frequency` / `period` / `vpp` / `vmax` / `vmin` / `vavg`(`VAVG`)/ `rms`(`VRMS`)/ `duty`(`PDUTy`)/ `rise_time`(`RTIMe`)/ `fall_time`(`FTIMe`)
  - Phase 0 で修正した `VAVG` ニモニックを含め、全項目でタイムアウト・エラーキュー汚染なし
- **9.9E37 sentinel の扱いを実機で確認**: 信号が画面外へクリップしている状態では `vpp` / `vmax` が `9.9E37` を返し、実装は値 `None` + `quality="unknown"` + warning として返した
  - 切り分け済み: 同時取得した波形1000点のうち500点が `raw=255`(レール値)であり、機器が測定不能と判断した状況が実データと一致する。**実装の判定は正しい**(機器の異常でもパースの誤りでもない)

### 1.4 波形

- 取得点数 1000点(screen モード)
- 実効サンプルレート **200 kSa/s**(`:ACQuire:SRATe?` は 2 MSa/s を返すが、画面データは間引かれている。Phase 0 と同じ挙動)
- プリアンブルからの電圧変換 `volts = (raw - yorigin - yreference) * yincrement` を実測値で検証済み

### 1.5 スクリーンショット

| 形式 | サイズ |
|---|---|
| PNG(機器出力をそのまま保存) | 109,020 B |
| JPEG(Pillowで変換) | 99,962 B |

### 1.6 レイテンシ

`*IDN?` の連続実行: **min 27.8 ms / median 29.3 ms / max 29.6 ms**。Phase 0 の実測(30–40 ms、負荷時 0.9–3.0 s)より安定しており、ばらつきは機器側の負荷状況に依存する。

---

## 2. Write(9件 PASS・復元漏れゼロ)

対象チャンネルは CH2(CH1 はプローブ補正信号の観測に使用)。すべて **set → read-back一致 → 復元** で実施し、各操作の所要時間は **0.40〜0.73 s** の範囲に収まった。

| # | 検証項目 | 結果 |
|---|---|---|
| 1 | CH2 `scale_v_per_div = 3.0` | verbatim適用(1-2-5スナップなし。Phase 0 の再確認) |
| 2 | CH2 `offset_v`(現在値 + 1div) | set → read-back一致 → 復元 |
| 3 | CH2 `coupling`(DC ⇔ AC) | set → read-back一致 → 復元 |
| 4 | CH2 `probe_ratio`(1 ⇔ 10) | set → read-back一致 → 復元。V/div の読み値が連動して変わることも確認 |
| 5 | Timebase `scale_s_per_div = 3e-4` + `position_s` | ともに set → read-back一致 → 復元 |
| 6 | Trigger `level_v` / `slope` / `sweep_mode` | すべて set → read-back一致 → 復元 |
| 7 | `run` / `stop` / `single` | すべて実行でき、取込状態が追随(下記 3.4 のポーリングが必要) |
| 8 | CH2 `scale_v_per_div = 3.3`(中間値) | **丸めなしでそのまま適用**。1-2-5どころか任意の細かいステップを受理する |
| 9 | 監査ログ | 40行すべてがJSONLとして妥当。`tool` / `requested` / `before` / `after` / `result` が揃う |

CH ON/OFF(`enabled`)は、垂直軸の書き込み検証の前提として実機で ON → 検証 → 元の状態へ復元、という形で通っている(3.3 参照)。

---

## 3. 新たに判明した実機仕様(重要)

### 3.1 未定義ヘッダのクエリでSCPIサーバー全体が沈黙する

未定義のヘッダを持つクエリ(例: `:MEASure:VPP?` — 正しくは `:MEASure:ITEM? VPP`)を **1回送るだけ**で、機器のSCPIサーバーが以後いっさい応答しなくなる。

- TCP接続そのものは生き続け、`connect` も成功する。それでいて `*IDN?` すら返らない
- クライアントプロセスの再起動・TCP再接続でも回復しない(機器側の状態)
- **空行 `\n` を1本送ると即座に回復する**

対策として `LanTransport.open()` が接続直後に空行を1本送る実装を導入済み(`src/rigol_oscilloscope_mcp/transport/lan.py`)。健全な機器に対しては無害で、仮にエラーになっても接続シーケンスのエラーキューdrainで掃除される。

Requirements 7.2「未確認ニモニックの送信禁止」の重要度は、Phase 0 時点の想定(タイムアウト1回ぶんのコスト)より **一段高い**。一度の事故で機器が使用不能になる。

### 3.2 プリアンブルの `yorigin` は設定依存の動的値

Phase 0 では `yorigin=0` を観測したが、これはオフセット 0 V のときの値だった。CH offset を -0.064 V にすると `yorigin = -9.0` になる。

```text
yorigin = offset_v / yincrement
```

つまり `yorigin` は固定値として扱ってはならず、必ずプリアンブルから読むこと(実装はそうしている)。

### 3.3 表示OFFのチャンネルへの垂直軸書き込みは無言で無視される

`:CHANnel<n>:DISPlay OFF` の状態で `:CHANnel<n>:SCALe` / `:OFFSet` を送ると、**エラーキューにも何も積まれないまま、設定が適用されない**。

- 一方で `:CHANnel<n>:COUPling` / `:PROBe` は表示OFFでも適用される
- read-back(`applied`)で検出できる: requested と applied が食い違う

実機writeスイートは、この挙動を踏まえて「垂直軸の検証前に `enabled=True` を先に送り、復元は最後に `enabled` を戻す」という順序にしてある。`configure_channel` 側で自動的に表示をONにするかは要検討([roadmap.md](../roadmap.md) 5章)。

### 3.4 `:RUN` 直後の `:TRIGger:STATus?` は約0.2秒 STOP のまま

`:RUN` 送信直後の1回目の `:TRIGger:STATus?` はまだ `STOP` を返し、約0.2秒後に `TD` へ変わる(機器側の再アーム待ち)。取込状態は1回のクエリで判定せず、ポーリングすること。

---

## 4. 未検証のまま残る項目

MVP完了時点で実機確認できていない項目。[roadmap.md](../roadmap.md) 5章に引き継ぐ。

- 50Ω入力インピーダンス(`FIFT`)— 耐圧が低く誤接続時に機器を壊すため、意図的に実機へ送っていない(FakeScopeでの検証のみ)
- `autoset`(`:AUToscale`)の書き込み — 利用者の設定を破壊するため実機未実施(FakeScopeでの検証のみ)
- factory reset
- RAWモード波形のダウンロード(チャンク処理・上限)
- **USB(USBTMC)接続 — 実機未検証。**ユニットテスト(`tests/test_usb_transport.py`、PyVISAのフェイク)のみで担保している
- パラメータlimitsの境界値収集
