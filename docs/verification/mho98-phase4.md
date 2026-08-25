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

## 3. 未実施・今後の予定

- RS232以外(I2C/SPI/CAN/LIN/Parallel)のイベントテーブル列構成は該当信号源を接続した際に本書へピン留めする(パーサーはスキーマ非依存のためコード変更不要)
- ライセンス適用後の再検証(options全True化、`:BUS1:CAN:FDBaud?` のbefore/after)は Phase 4 完了時に本書へ追記
