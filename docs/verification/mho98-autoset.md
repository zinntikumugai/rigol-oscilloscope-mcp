# MHO98 オートセットアップ ニモニック検証記録

**実施日:** 2026-08-26
**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**背景:** 機種対応表調査(issue #19)の過程で、ドライバがハードコードしていた `:AUToscale` が**MHO900ガイドに存在しない**ことが判明した(潜在バグ)。autoset書き込みは実機実行禁止(AGENTS.md ルール4)のため一度も送信されておらず、発火していなかった。

## ガイド上の事実(MHO900 Programming Guide)

- **3.2.1 `:AUToset`**: オートセットアップの正式コマンド(引数なし)。「AUTO機能が無効の場合このコマンドは無効」
- **3.24.22 `:SYSTem:AUToscale <bool>`**: AUTOキー(Auto メニュー)の有効/無効スイッチ。**オートセットアップの実行コマンドではない**
- 3.1 Root Commands は `:CLEar` / `:RUN` / `:STOP` / `:SINGle` / `:TFORce` のみ(AUTo系はルートに無い)
- DHO800/900ガイドも同様に `:AUToset`(3.2.1)で、`:AUToscale` 実行コマンドは無い

→ 裸の `:AUToscale` は未定義ヘッダであり、実機へ送ればSCPIサーバーが沈黙するところだった。

## 実機プローブ(読み取りのみ。autoset実行はしていない)

| コマンド | 応答 | エラーキュー |
|---|---|---|
| `:AUToset:ENAble?` | 1 | No error |
| `:SYSTem:AUToscale?` | 1 | No error |

→ `:AUToset` サブツリーの実在を、autoset を実行せずクエリ形で確認した(実行コマンド `:AUToset` 自体の書き込みは引き続き実機実行禁止のため未検証。confirmフローはFakeScopeテストで担保)。

## 対応

- dialect キー `autoset_command` を新設し、mho98.yaml に `":AUToset"` を宣言(ニモニックは世代で分岐: 旧世代DS1000Z等は `:AUToscale`)
- rigol-generic には宣言しない(不在=ゲート。未知機種のautosetは `UNSUPPORTED_FEATURE` になる — 世代不明の機種へ推測ニモニックを送らない)
- FakeScope は `:AUToset` を受理し、旧 `:AUToscale` は沈黙(誤送信の回帰をテストで検出)
