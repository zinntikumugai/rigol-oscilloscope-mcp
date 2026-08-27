# ドキュメント一覧

`rigol-oscilloscope-mcp` — Rigol製SCPI対応オシロスコープをLLMから操作するMCPサーバー(Python / mise + uv)のドキュメント。

| 文書 | 内容 | 位置づけ |
|---|---|---|
| [Requirements.md](Requirements.md) | 要件定義書 v1.0。目的、コンセプト、対象範囲、アーキテクチャ、安全要件、動作原則、設定、配布・ホスト統合、開発フェーズと受入基準 | 規範(コア) |
| [tools.md](tools.md) | MCP Toolカタログ。各Toolの引数・返却・操作クラス・導入フェーズ | 規範(詳細) |
| [device-profiles.md](device-profiles.md) | 機種プロファイル仕様。3層解決(verified / family / generic)と、検証済みMHO98プロファイル(quirk集) | 規範(詳細) |
| [compatibility.md](compatibility.md) | 機種対応表。RIGOL各シリーズの実装状態×機器側操作性(ガイドURL付き) | 記録 |
| [profile-authoring.md](profile-authoring.md) | プロファイル作成と動作確認のガイドライン(ガイド解読→YAML→テスト→実機プローブ→昇格) | 手順 |
| [e2e-check-prompt.md](e2e-check-prompt.md) | LLMによる総合動作確認プロンプト(別セッションに貼って通し確認) | 手順 |
| [roadmap.md](roadmap.md) | 今後の対応予定。Phase 4の機能、機種プロファイル拡充、方針未定の検討事項(Phase 3・プラグイン化は完了し要件へ昇格済み) | 予定・記録 |
| [verification/mho98-phase0.md](verification/mho98-phase0.md) | Phase 0 実機SCPI検証結果(MHO98, LAN :5555)。各文書が引用する実測エビデンス | エビデンス |
| [verification/mho98-mvp.md](verification/mho98-mvp.md) | MVP(Phase 1 + 2)実機検証結果(MHO98)。read-only 9件 + write 9件のPASS記録と、新たに判明した実機仕様 | エビデンス |
| [verification/mho98-phase4.md](verification/mho98-phase4.md) | Phase 4(シリアルデコード)実機検証結果。イベントテーブルの列構成の実測記録を含む | エビデンス |
| [verification/mho98-afg.md](verification/mho98-afg.md) | AFG(信号発生)実機検証結果。全13波形・変調・ループバックFFTの記録 | エビデンス |
| [verification/mho98-math.md](verification/mho98-math.md) | M1(MATH演算)実機検証結果。FFTピーク表の複数行応答・プリアンブルのx軸・表示OFF→ONでの再計算quirk | エビデンス |
| [verification/mho98-m2.md](verification/mho98-m2.md) | M2(カーソル/カウンタ/DVM/ヒストグラム)実機検証結果。無効時のエラーキュー汚染と統計書式 | エビデンス |
| [verification/mho98-m3.md](verification/mho98-m3.md) | M3(リファレンス波形)実機検証結果。工場出荷状態の全10枠と、ガイド記載と違う色応答(`GREE`)による列挙照合の修正 | エビデンス |
| [verification/mho98-unlicensed.md](verification/mho98-unlicensed.md) | 未ライセンス状態でのオプション照会とデコードの挙動(`:SYSTem:OPTion:STATus?`) | エビデンス |
| [verification/mho98-autoset.md](verification/mho98-autoset.md) | オートセットアップのニモニック実在確認(読み取りプローブのみ。実行自体は未検証) | エビデンス |
| [verification/mho98-measure-clear.md](verification/mho98-measure-clear.md) | 測定項目クリア(`:MEASure:DELete`)のニモニック分岐の実測 | エビデンス |

## 読む順序の目安

1. **全体像を知りたい** → [Requirements.md](Requirements.md) の1〜3章(目的・コンセプト・対象範囲)
2. **Toolを設計・実装する** → [tools.md](tools.md) と [Requirements.md](Requirements.md) 6章(安全要件)・7章(動作原則)
3. **機種対応を追加する** → [device-profiles.md](device-profiles.md) と [verification/mho98-phase0.md](verification/mho98-phase0.md) / [verification/mho98-mvp.md](verification/mho98-mvp.md)
4. **MVP後の計画を知りたい** → [roadmap.md](roadmap.md)
