# MHO98 測定Resultビュークリア 実機検証記録(issue #16)

**実施日:** 2026-08-26
**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**手順規律:** [mho98-unlicensed.md](mho98-unlicensed.md) と同じ(1コマンド → 応答/エラーキュー確認 → 記録)

## プローブ結果

| コマンド | 応答 / エラーキュー | 所見 |
|---|---|---|
| `:MEASure:ITEM? FREQuency,CHANnel1` | `1.0000E+03` / No error | クエリ形でも項目がResultビューへ追加される(蓄積の原因) |
| **`:MEASure:DELete`** | No error | **ガイド3.17.3記載のMHO900正式コマンド。正常受理** |
| `:MEASure:ITEM? VPP,CHANnel1`(再追加) | `3.1107E+00` / No error | |
| **`:MEASure:CLEar`** | **No error** | DHO800/900系の同義ニモニック。**MHO98実機は両方受理する**(沈黙しない) |

## 採用判断

- dialect値はMHO900プログラミングガイドに記載のある **`:MEASure:DELete`** を採用(`measurement_clear`、mho98.yaml)
- `:MEASure:CLEar` も実機で受理されることを記録(ファミリ互換の実測証拠。将来DHOプロファイルは `CLEar` を宣言する — ガイド上の正式名がファミリで分岐するため)
- 部分クリア(項目/チャンネル指定)の構文はどちらのガイドにも存在しない(全消しのみ)

## スイート実行

- write検証(`-m device_write -k clear`): `test_clear_measurements` — measure で項目追加 → `clear_measurements` → エラーキューclean を確認(結果は下記PR参照)
