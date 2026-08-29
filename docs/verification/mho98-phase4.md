# MHO98 Phase 4 実機検証記録

**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**前提:** バンドルライセンス MHO900-BND 未適用の状態で実施(標準搭載機能のみを使用)。未ライセンス状態のプローブ記録は [mho98-unlicensed.md](mho98-unlicensed.md)

## 1. configure_decode(PR B、2026-08-25)

### read-onlyスモーク

`RIGOL_TEST_ADDRESS=<実機IP> uv run pytest -m device` → **10 passed**(既存9件+`test_installed_options_answer`)。

### write検証

`RIGOL_TEST_ADDRESS=<実機IP> RIGOL_TEST_ALLOW_WRITE=1 uv run pytest -m device_write -k decode` → **PASS**

- `tests/device/test_write.py::test_configure_decode_uart_set_and_readback`: バス1の現在設定を `get_decode_config` でスナップショット → UART設定(CH2ソース、baud等)を `configure_decode` で書き込み → readbackで applied 値を確認 → finally で全設定を復元。復元漏れなし
- `:MODE → :FORMat → プロトコル設定 → :DISPlay → :EVENt` の送信順で全コマンドがエラーキューを汚さず受理された

## 2. get_decode_result(PR C、2026-08-25)

### イベントテーブル形式のピン留め(実機実測)

`:BUS1:DATA?` のTMCブロックペイロード(MODE=RS232、FORMat=HEX):

```
RS232
Time,Tx/Rx,Data,Error,
```

- 1行目 = プロトコルトークン、2行目 = ヘッダ行(**RS232は4列**: Time / Tx/Rx / Data / Error、各行は末尾カンマ付き)。ガイド記載のPARallel例と同一構造
- 列名正規化の実測確認: `Tx/Rx` → `tx_rx`(非英数字は `_` へ)、`Time` → `time_s`
- イベント0件の場合はヘッダ行のみ(2行・28バイト)が返る

### 実イベント行の取得(CH1プローブ補償信号をUARTデコード)

信号源: CH1のプローブ補償出力(1 kHz方形波)。`configure_decode(bus=1, uart, rx_source=CH1, baud 9600, rx_threshold 1.5V)` → RUN 1.5秒 → STOP → `get_decode_result`:

```json
{"columns": ["time_s", "tx_rx", "data", "error"],
 "events": [
   {"time_s": -0.0004998, "tx_rx": "RX", "data": "F0", "error": ""},
   {"time_s": 0.0005002, "tx_rx": "", "data": "", "error": ""}],
 "event_count": 2}
```

- 500 µsのLow区間 = start bit + 下位4bit Low → LSBファーストで `0xF0`(理論どおり)。工学接尾辞付き時刻(`-499.8us` 等)が `time_s` の float へ正しく変換された
- **実機の癖**: データ列が空のイベント行が混ざる(2行目)。パーサーは空セルをそのまま保持する(行を捨てない)
- 検証は snapshot → configure → RUN/STOP → 取得 → finally 復元のパターンで実施し、復元一致を確認(RESTORE OK)

### スイート実行

- read-onlyスイート(`-m device`): **11 passed**(`test_get_decode_result_parses_or_warns` 含む)

## 3. ライセンス(MHO900-BND)適用後の再検証(2026-08-26)

ユーザーが本体でバンドルライセンスを手動適用・再起動した後、`installed_options()`(`get_capabilities` と同経路)で再照会した。

### オプション状態の before / after

| オプション | 未適用([mho98-unlicensed.md](mho98-unlicensed.md))| 適用後 |
|---|---|---|
| bundle (BND) | 0 | **1** |
| afg_100mhz / audio_i2s / can_fd / flexray / mil_std_1553 | 0 | **1**(バンドルの内容どおり) |
| afg_50mhz / memory_500mpts | 1(工場出荷) | 1 |
| bandwidth_350_to_500 / _350_to_800 / _500_to_800 | 0 | 0(帯域アップグレードはバンドル対象外=妥当) |

→ オプション照会の実装がライセンス適用を正しく反映することを確認。**ファームウェア再起動後も照会コマンド・トークンの挙動に変化なし**(00.01.00)。

### `:BUS1:CAN:FDBaud?`(オプションゲート済みニモニック)の before / after

| 時点 | 応答 | エラーキュー |
|---|---|---|
| 未適用(2026-08-25、初回) | `1000000` | `-222,"Data out of range"` |
| 未適用(2026-08-26、再測定) | `1000000` | `0,"No error"` |
| **適用後(2026-08-26)** | `1000000` | `0,"No error"` |

- 適用前後とも**沈黙しない**(mho98-unlicensed.md 4章の結論を維持: 送信前ライセンスゲートは不要)
- ただし未適用時のエラーキュー挙動には**揺らぎがある**(-222が積まれる場合と積まれない場合を観測)。エラーキューだけで「未ライセンス」を判定するのは不可で、判定には `:SYSTem:OPTion:STATus?` を使うこと

### スイート実行

- read-onlyスイート(`-m device`): **11 passed**(適用後。`test_installed_options_answer` は「boolであること」のみを検証する設計のため適用前後どちらでもパス)

## 4. 未実施・今後の予定

- RS232以外(I2C/SPI/CAN/LIN/Parallel)のイベントテーブル列構成は該当信号源を接続した際に本書へピン留めする(パーサーはスキーマ非依存のためコード変更不要)
- ライセンス解放済みとなったオプション機能(CAN-FD / FlexRay / I2S / MIL-STD-1553デコード、AFG 100MHz)の対応は roadmap 2章の対象(着手時に要件へ昇格)

## 5. パラレルデコードの `:PARallel:WIDTh` が拒否される原因(2026-08-28)

**長らく残っていた「復元fixtureが `:BUS1:PARallel:WIDTh 1` で `-200,"Command execute failed"` を受ける」問題([mho98-m2.md](mho98-m2.md) 7.1、[roadmap.md](../roadmap.md) 6章)の原因を切り分けた。**

実測(状態を変えながら全て試行。毎回エラーキューを空にしてから1コマンド):

| 送信 | 状態 | エラーキュー |
|---|---|---|
| `:BUS1:PARallel:WIDTh 1` / `2` / `4` / `8` / `16` | バス無効 | 全て `-200,"Command execute failed"` |
| 同上 | `:BUS1:MODE PAR` に設定後 | 全て `-200` |
| 同上 | さらに `:BUS1:DISPlay ON` | 全て `-200` |
| `:BUS1:PARallel:ENDian MSB` / `LSB` | 同じ状態 | **どちらも成功**(read-back一致、エラーなし) |

**値の問題でも、バスの有効/無効の問題でもない。** 兄弟項目の `:ENDian` が同じ状態で問題なく往復することが、経路そのものは生きていることを示している。

**原因はガイド自身の Remark にあった(3.4.10.4):** `:BUS<n>:PARallel:WIDTh` は **データソースが User に設定されているときのみ有効**(`:BUS<n>:PARallel:BUS USER`)。今回の実機はその条件を満たしていなかったため、どの値を送っても実行が拒否されていた。

### 前提の確認(データソースを User にしてからの再試行)

エラーキューを毎回空にしてから1コマンドずつ送った結果:

| 送信 | 結果 |
|---|---|
| `:BUS1:PARallel:BUS USER` | 成功(read-back `USER`) |
| `:BUS1:PARallel:WIDTh 2` / `4` / `1` | **全て成功**(read-back 一致) |
| `:BUS1:PARallel:BITX 1` | 成功 |
| `:BUS1:PARallel:SOURce CHANnel2` | 成功(read-back `CHAN2`) |
| `:BUS1:PARallel:BUS CHANnel1` → `:BUS1:PARallel:WIDTh 2` | `-200,"Command execute failed"`(前提を外すと再び拒否) |

**ガイドの Remark どおりで、条件は `:PARallel:BUS USER` ただ1つ。** 拒否は沈黙を伴わず、エラーキューに積まれるだけである。

### 実装(修正済み・[roadmap.md](../roadmap.md) 6章)

- **`settings.parallel` に `bus` を追加した。** 値域はガイド3.4.10.1 逐語の11トークン(`d7_d0` / `d15_d8` / `d15_d0` / `d0_d7` / `d8_d15` / `d0_d15` / `ch1`〜`ch4` / `user`。デジタルグループは**先に書かれた側がMSB**)。プロトコル名 → ニモニックの対応と違い、この値域は機種依存の方言ではないので `driver/decode.py` の変換表が持つ(プロファイルの追加宣言は無し)
- **`configure_decode` の送信順を「表の並び」に固定した**(従来は呼び出し側の `settings` のキー順だった)。表では `bus` が `bus_width` より前にあるので、`{"bus": "user", "bus_width": 4}` が**1回の呼び出しで**通る
- **`:PARallel:BITX` + `:PARallel:SOURce` の対を `bit_sources` として公開した。** 添字=ビット番号のリストで、ドライバが `bus_width` の後に `:BITX <i>` → `:SOURce <src>` を1ビットずつ送る。**読み取りにも同じ走査が要る**(`:SOURce?` は選択中ビットのぶんしか返さない)。読み取りが書き込みを伴う唯一の経路なので走査はopt-inにし(`get_decode_config(bus, include_bit_sources=True)`)、`changed` の判定に必要な `configure_decode` の前後スナップショットだけが要求する。既定の読み取りは `bit_sources` を返さず `:BITX` を1本も送らない
- **`bus="user"` との結合はホスト側では検証しない**(機器が `-200` で自己申告する経路に任せる。M1/M2/M3 と同じ方針)
- FakeScope も **User 以外では `:WIDTh` / `:BITX` / `:SOURce` を `-200` で拒否する**ようにした(沈黙はしない)。この穴を最初に取り逃した回帰網がこれで埋まる
- 実機writeテストの復元fixtureは `bus_width` / `bit_sources` を「Userへ入れて書き戻す → 本来の `bus` へ戻す」の2段で扱う。**復元対象から外していた `bus_width` が復元できるようになった**
