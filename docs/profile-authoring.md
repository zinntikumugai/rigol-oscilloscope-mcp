# プロファイル作成と動作確認のガイドライン

**対象読者:** 新しいRIGOL機種のプロファイルを追加したい人、既存のguideプロファイルを実機検証してverifiedへ昇格させたい人。
**関連文書:** [device-profiles.md](device-profiles.md)(スキーマ規範)/ [compatibility.md](compatibility.md)(対応状況とガイドURL)/ [e2e-check-prompt.md](e2e-check-prompt.md)(LLMによる総合動作確認)

## 0. 前提と安全原則

1. **未検証のSCPIニモニックを実機へ送らない**(AGENTS.md ルール2)。RIGOL機は未定義ヘッダ・不正パラメータのクエリ1発でSCPIサーバー全体が沈黙し得る(実測: MHO98。再接続では回復せず空行1本で回復)。プロファイルに宣言する値は必ず「公式プログラミングガイドの逐語記載」か「実機プローブの実測」を根拠にする
2. **confidence はエビデンスの段階**: `verified`(実機検証記録あり)> `family`(同系実機検証)> `guide`(ガイド逐語解読のみ・quirk未確認)> `generic`(フォールバック)。エビデンスを超えたconfidenceを名乗らない
3. **実機のIPアドレス・シリアルをリポジトリへ書かない**(`tests/test_ip_guard.py` がlint。例示は TEST-NET `192.0.2.x`)
4. **実機で実行禁止**: 50Ω設定・autosetの実行・factory reset(confirmフローはFakeScopeテストで担保)

## 1. 公式プログラミングガイドの入手と解読

- 各シリーズのガイドURLは [compatibility.md](compatibility.md) に集約してある。新シリーズは `rigol.com/dam/.../program-guide/`、`download.rigol.com`、batronix等のミラーを探す(403はブラウザ系User-Agentで回避できることがある)
- **RIGOLのPDFはテキスト抽出できない難読化が多い**。実績のある解読法:
  - glyph-subset CIDフォント: content stream中の4桁hexのglyph idを `chr(gid + 26)`(標準Macintoshグリフ順)で復号。`gid == 3` はスペース
  - Syntax/Exampleブロックは追加で**Caesar +3**(例: `7&#65;RQlpbq` → `:AUToset`)
  - 旧世代PDFは空パスワード暗号化のみ(pypdfの `decrypt('')` で可)
- **抽出チェックリスト**(プロファイル1本に必要な最小集合):
  - [ ] モデルラインナップ(型番・ch数・帯域)→ `match` 正規表現と `analog_channels`
  - [ ] IEEE488.2章のコマンド集合(**`*OPT?` が無いことの確認** — 全シリーズ非搭載が実績)
  - [ ] `:AUToset` か `:AUToscale` か(`autoset_command`)
  - [ ] `:DISPlay:DATA?` の引数形式と既定フォーマット(`screenshot_command`)
  - [ ] `:MEASure:ITEM` の項目トークン10種の綴り(`measurement_items`)
  - [ ] Resultビュークリア(`:MEASure:DELete` / `:CLEar` / `:CLEar ITEMn|ALL`)(`measurement_clear`)
  - [ ] `:CHANnel<n>:BWLimit` の値集合(`bwlimit_on`)
  - [ ] `:CHANnel<n>:PROBe` の許容値リスト(`limits.probe_ratio`)
  - [ ] `:WAVeform:PREamble?` のフィールド構成とyreference(BYTE時)
  - [ ] `:CHANnel<n>:IMPedance` の有無(`impedance_control` / `impedance_50ohm`)
  - [ ] `:SYSTem:OPTion:*` の有無と `<type>` リスト(宣言する場合のみ。**リスト外トークンは沈黙する実績あり**)
  - [ ] `:BUS<n>` / `:SOURce` の系統(将来のデコード/AFG対応の下調べ。初回スコープでは未宣言でよい)

## 2. プロファイルYAMLの作成

`profiles/data/dho1000.yaml` を手本にする(guideプロファイルの実例)。

- `match`: IDNのモデル文字列に対する正規表現。**既存プロファイルと衝突しないこと**(解決テストで固定する)
- `inherits: rigol-generic`(または同一ガイドの姉妹シリーズ)。**別ガイド由来のプロファイルを継承しない** — マージ結果が同じでも出典の追跡性が壊れる
- `confidence: guide`(実機未検証の間)
- **宣言原則**: ガイドに逐語で載っているニモニック・値だけを宣言する。quirk(沈黙挙動・スナップ・NR3形式)は宣言しない(rigol-genericの保守的既定を継承)。実機検証していない機能(デコード/AFG/LA/オプション照会)は**未宣言のまま**にする — キーの不在がそのまま送信ゼロの `UNSUPPORTED_FEATURE` ゲートになる
- 各宣言に**出典コメント必須**(ガイドの出版番号と節番号。例: `# ガイド3.6.1`)。シリーズ内の既知制限(2chモデル等)もコメントに残す

## 3. テスト(TDD)

失敗するテストを先に書き、redを確認してからYAMLを置く。`tests/test_profiles.py` の既存パターンに追加する:

1. **解決テスト**: 代表モデル文字列→プロファイル名(`test_resolve_dho_models` のパラメトライズへ追加)。既存機種(MHO98等)が影響を受けないことも同じ表が担保する
2. **ロード/継承テスト**: confidence・capabilities・姉妹シリーズとの差分
3. **in-scope dialectテスト**: 宣言した方言値の固定(`test_dho_profiles_declare_in_scope_dialect` のパラメトライズへ追加)
4. **absence-gateテスト**: 未宣言キーが本当に無いことの回帰(`test_dho_profiles_do_not_declare_unverified_features`)

`mise exec -- uv run pytest` 全green + `git grep "172\.16\."` 空を確認。

## 4. 実機プローブ(実機がある場合)

`docs/verification/mho98-unlicensed.md` の規律に従う:

- **1コマンド送信 → 応答(5秒timeout)→ `:SYSTem:ERRor?` → 記録**、をREPL相当で1つずつ。pytest経由の一括実行でプローブしない(沈黙1発でセッション全体のエビデンスが壊れる)
- **復旧手順を先に構える**: 沈黙 → ソケット再接続(open時空行)→ `*IDN?` 5秒以内応答。ダメなら電源再投入。沈黙はエラーキューに残渣を残すので復旧後にdrainする
- **リスク昇順**で実施し、沈黙の可能性がある1発は最後に(高リスク枠)。read-only → write(現在値取得→set→readback→**finallyで復元**)の順
- 接続先は `RIGOL_TEST_ADDRESS` 環境変数のみで渡す
- 結果は `docs/verification/<機種>-<目的>.md` に記録(送信文字列/生応答/エラーキュー/レイテンシ/要した復旧。IP・シリアル非記載)

## 5. 動作確認

段階順に:

1. **実機なし**: `RIGOL_MCP_FAKE=1 uv run rigol-oscilloscope-mcp` でFakeScope接続のstdio起動。ホスト設定と会話フローの確認
2. **実機read-only**: `RIGOL_TEST_ADDRESS=<実機IP> uv run pytest -m device`(機器の設定を変更しない)
3. **実機write**: `RIGOL_TEST_ADDRESS=<実機IP> RIGOL_TEST_ALLOW_WRITE=1 uv run pytest -m device_write`(全テストが復元付き)
4. **LLMからの総合動作確認**: [e2e-check-prompt.md](e2e-check-prompt.md) のプロンプトを、MCPサーバーを接続した別のLLMセッションに貼って実行させる(接続→設定→測定→解析→デコード→復元の通し)

## 6. 検証記録とconfidence昇格

- guide → verified の条件: (4)のプローブでプロファイル宣言値が全て実機で裏取りされ、(5)の 2〜4 がPASSし、記録が `docs/verification/` に残っていること
- 昇格時の変更: YAMLの `confidence: verified` + 出典コメントに検証記録へのリンク追加 + [compatibility.md](compatibility.md) の該当表(🟡→✅)+ 必要なら未宣言だった機能(デコード等)の段階追加
- PRはAGENTS.mdの作法(featureブランチ、日本語コミット、`uv run pytest` 全green、IPガード空、機器通信に触れた変更は検証記録添付)
