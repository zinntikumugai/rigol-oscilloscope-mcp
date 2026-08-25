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

## 2. 未実施・今後の予定

- `get_decode_result`(イベントテーブル)の実機検証は PR C で実施(実UART信号源が必要。RS232/I2C/SPI等の列構成をここにピン留めする)
- ライセンス適用後の再検証(options全True化、`:BUS1:CAN:FDBaud?` のbefore/after)は Phase 4 完了時に本書へ追記
