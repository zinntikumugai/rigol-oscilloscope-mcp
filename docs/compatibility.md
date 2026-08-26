# 機種対応表

**位置づけ:** RIGOL各シリーズの対応状況の恒久記録(トラッキングは issue #19)。プロファイル仕様は [device-profiles.md](device-profiles.md)、実測エビデンスは [verification/](verification/) を参照。

## 凡例(各表は「実装状態」と「機器側の操作性」の2軸)

**実装状態**(本サーバーのプロファイル/コード):

| 記号 | 意味 |
|---|---|
| ✅ | **実装・定義済み(実機確認済み)** — verifiedプロファイル+実機検証記録あり |
| 🟡 | **実装・定義済み(実機未確認)** — guideプロファイル(公式ガイドの逐語解読のみ) |
| ❌ | **未実装** — プロファイル未宣言。呼んでも送信ゼロで `UNSUPPORTED_FEATURE` |

**機器側の操作性**(SCPI方言):

| 記号 | 意味 |
|---|---|
| ○ | **操作可能** — 方言差なし、またはプロファイルで吸収済み |
| △ | **操作には注意が必要** — 引数・ニモニックの方言差あり(プロファイル対応で吸収可能) |
| × | **操作不可** — 機器に機能が無い、または現行実装と非互換の別系統 |

共通事項: `*OPT?` は全シリーズ非搭載の未定義ヘッダ(送信禁止ガード実装済み)。方言差はすべてサーバー側プロファイルで吸収され、LLMに見えるTool APIは機種非依存。

---

## MHO900シリーズ(MHO98 ほか)

**プロファイル:** `mho98`(confidence: **verified**)— 全27 Tool対応
**ガイド:** [MHO900 Programming Guide](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/MHO900-ProgrammingGuide.pdf)
**実機検証:** [verification/](verification/) 一式(MHO98、firmware 00.01.00)

| 機能 | 実装 | 機器 | 備考 |
|---|---|---|---|
| コア読み取り/設定/取得 | ✅ | ○ | |
| スクリーンショット | ✅ | ○ | `:DISPlay:DATA?`(引数なしでPNG) |
| 測定10種 / Resultビュークリア | ✅ | ○ | クリアは `:MEASure:DELete` |
| Autoset | ✅ | ○ | `:AUToset`(サブツリーを読み取りプローブで確認) |
| シリアルデコード(標準6種) | ✅ | ○ | `:BUS<n>`×4。opt4種(I2S/FlexRay/M1553/CAN-FD)は未実装 |
| オプション照会 | ✅ | ○ | `:SYSTem:OPTion:STATus?` |
| AFG(2ch・13波形) | ✅ | ○ | 出力ONはDANGEROUS_WRITE |
| 入力50Ω | ✅ | ○ | RESTRICTED_WRITE(confirm必須) |
| Logic Analyzer | ❌ | ○ | 機器はD0-D15搭載、サーバー側未実装 |

## DHO800シリーズ(DHO802 / 804 / 812 / 814)

**プロファイル:** `dho800`(confidence: **guide**、v0.1.2〜)
**ガイド:** [DHO800/900 Programming Guide](https://download.rigol.com/en/Manual/Digital%20Oscilloscope/DHO800/DHO800900_ProgrammingGuide_EN.pdf)(PGA39106-1110。**注意: 公式サイトの `/DHO900/` パスの同名ファイルはDHO1000/4000ガイドの誤配置**)

| 機能 | 実装 | 機器 | 備考 |
|---|---|---|---|
| コア読み取り/設定/取得 | 🟡 | ○ | DHO802/812は2ch(範囲外CHは機器側拒否に委ねる) |
| スクリーンショット | 🟡 | ○ | `PNG` 引数必須(既定BMP)— プロファイルで吸収済み |
| 測定10種 / Resultビュークリア | 🟡 | ○ | クリアは `:MEASure:CLEar` — 吸収済み |
| Autoset | 🟡 | ○ | `:AUToset` |
| シリアルデコード | ❌ | △ | `:BUS`コアは共通だがLIN無し(5種)。実機検証後に追加予定 |
| オプション照会 | ❌ | × | 機器にOPTionコマンド自体なし |
| AFG | ❌ | × | DHO800系に内蔵ジェネレータなし |
| 入力50Ω | ❌ | × | 1MΩ固定 |
| Logic Analyzer | ❌ | × | LAなし |

## DHO900シリーズ(DHO914 / 914S / 924 / 924S)

**プロファイル:** `dho900`(confidence: **guide**、v0.1.2〜、dho800を継承)
**ガイド:** DHO800と同一

| 機能 | 実装 | 機器 | 備考 |
|---|---|---|---|
| コア読み取り/設定/取得 | 🟡 | ○ | 全モデル4ch |
| スクリーンショット / 測定 / クリア / Autoset | 🟡 | ○ | dho800と同一(継承) |
| シリアルデコード | ❌ | △ | 標準6種(LIN含む)。実機検証後に追加予定 |
| オプション照会 | ❌ | × | dho800と同じくOPTionなし |
| AFG | ❌ | △ | S型のみ内蔵(番号なし `:SOURce`・1ch・6波形 — MHO900と別方言) |
| 入力50Ω | ❌ | × | 1MΩ固定 |
| Logic Analyzer | ❌ | ○ | 機器はD0-D15搭載、サーバー側未実装 |

## DHO1000シリーズ(DHO1072 / 1074 / 1102 / 1104 / 1202 / 1204)

**プロファイル:** `dho1000`(confidence: **guide**)
**ガイド:** [DHO1000/4000 Programming Guide](https://www.batronix.com/files/Rigol/Oszilloskope/DHO1000/dho10004000_programmingguide_en.pdf)(PGA34101-1110。公式download.rigolは404のためミラー)

| 機能 | 実装 | 機器 | 備考 |
|---|---|---|---|
| コア読み取り/設定/取得 | 🟡 | ○ | DHO1072/1102/1202は2ch(範囲外CHは機器側拒否に委ねる) |
| スクリーンショット | 🟡 | ○ | `PNG` 引数必須(既定BMP)— プロファイルで吸収済み |
| 測定10種 / Resultビュークリア | 🟡 | ○ | クリアは `:MEASure:CLEar`(引数なし) |
| Autoset | 🟡 | ○ | `:AUToset`(ガイド3.6.1) |
| シリアルデコード | ❌ | △ | `:BUS<n>`×4(FlexRay/I2S/M1553非対応)。実機検証後に追加予定 |
| オプション照会 | ❌ | ○ | `:SYSTem:OPTion:STATus?` あり(`<type>` リスト未抽出のため未実装) |
| AFG | ❌ | × | ガイドに `:SOURce` 章なし |
| 入力50Ω | ❌ | × | **50Ω非対応**(1MΩのみ。ガイド3.9.8) |
| Logic Analyzer | ❌ | × | ガイドに `:LA` 章なし |

## DHO4000シリーズ(DHO4204 / 4404 / 4804)

**プロファイル:** `dho4000`(confidence: **guide**、dho1000を継承)
**ガイド:** DHO1000と同一文書

| 機能 | 実装 | 機器 | 備考 |
|---|---|---|---|
| コア/スクショ/測定/クリア/Autoset | 🟡 | ○ | dho1000と同一(継承。全モデル4ch) |
| シリアルデコード | ❌ | △ | `:BUS<n>`×4(FlexRay/I2S/M1553はDHO4000のみオプション対応) |
| オプション照会 | ❌ | ○ | dho1000と同じ |
| AFG | ❌ | × | AFGなし |
| 入力50Ω | 🟡 | ○ | `{OMEG\|FIFTy}` あり — **guideプロファイルで初めて50Ω確認フローが有効**(RESTRICTED_WRITE。50Ω時は垂直感度上限が1 V/divへ低下) |
| Logic Analyzer | ❌ | × | LAなし |

## MSO5000 / MSO7000・DS7000シリーズ(未対応)

**プロファイル:** なし(rigol-genericで読み取りのみ動く見込み)
**ガイド:** [MSO5000](https://www.batronix.com/files/Rigol/Oszilloskope/MSO5000/MSO5000_ProgrammingGuide_EN-V2.0.pdf) / [MSO7000・DS7000](https://res.cloudinary.com/iwh/image/upload/q_auto,g_center/assets/1/26/Rigol_DS7000-MSO7000_-_Programming_Guide.pdf) / [MSO8000(未解読)](https://www.batterfly.com/PDF/RIGOL/mso8000/MSO8000-Series_programming-guide_EN.pdf)

| 機能 | 実装 | 機器 | 備考 |
|---|---|---|---|
| コア読み取り | ❌ | ○ | genericで動く見込み(過渡期方言) |
| スクリーンショット | ❌ | △ | **引数不可・BMPのみ**(genericの `:DISPlay:DATA?` 裸形はそのまま動く可能性あり・要検証) |
| Resultビュークリア | ❌ | △ | `:MEASure:CLEar ITEMn\|ALL` — **引数必須**(方言キーは引数込み文字列で吸収可能) |
| シリアルデコード | ❌ | △ | `:BUS<n>`×4 |
| オプション照会 | ❌ | ○ | `:SYSTem:OPTion:STATus?` あり |
| AFG | ❌ | △ | `[:SOURce[<n>]]:APPLy` 別形式 |
| 入力50Ω | ❌ | MSO5000:× / MSO7000:○ | |

## DS1000Z / MSO1000Zシリーズ(未対応・旧世代)

**プロファイル:** なし(genericで読み取りのみ)
**ガイド:** [DS1000Z/MSO1000Z Programming Guide](https://www.bitsavers.org/test_equipment/rigol/DS1000Z/PGA19109-1110_MSO1000Z_DS1000Z_Series_Digital_Oscilloscope_Programming_Guide_201807.pdf)

| 機能 | 実装 | 機器 | 備考 |
|---|---|---|---|
| コア読み取り | ❌ | ○ | 概ね互換 |
| スクリーンショット | ❌ | △ | `:DISPlay:DATA? [color,invert,format]` 別形式 |
| Resultビュークリア | ❌ | △ | `ITEM1..5\|ALL` 引数必須 |
| シリアルデコード | ❌ | × | **`:DECoder<n>` 別系統**(現行実装と非互換) |
| Autoset | ❌ | △ | 旧世代は `:AUToscale` |
| オプション照会 | ❌ | △ | STATus?なし(INSTall/UNINstallのみ) |

## その他の資料

- 製品ドキュメント一覧(RIGOL公式): https://www.rigol.com/global/services/services/document
