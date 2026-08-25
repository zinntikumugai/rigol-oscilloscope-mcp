# ドキュメント一覧

`rigol-oscilloscope-mcp` — Rigol製SCPI対応オシロスコープをLLMから操作するMCPサーバー(Python / mise + uv)のドキュメント。

| 文書 | 内容 | 位置づけ |
|---|---|---|
| [Requirements.md](Requirements.md) | 要件定義書 v1.0。目的、コンセプト、対象範囲、アーキテクチャ、安全要件、動作原則、設定、配布・ホスト統合、開発フェーズと受入基準 | 規範(コア) |
| [tools.md](tools.md) | MCP Toolカタログ。各Toolの引数・返却・操作クラス・導入フェーズ | 規範(詳細) |
| [device-profiles.md](device-profiles.md) | 機種プロファイル仕様。3層解決(verified / family / generic)と、検証済みMHO98プロファイル(quirk集) | 規範(詳細) |
| [roadmap.md](roadmap.md) | 今後の対応予定(MVP対象外)。Phase 3/4の機能、Claudeプラグイン化、方針未定の検討事項 | 予定・記録 |
| [phase0-results.md](phase0-results.md) | Phase 0 実機SCPI検証結果(MHO98, LAN :5555)。各文書が引用する実測エビデンス | エビデンス |

## 読む順序の目安

1. **全体像を知りたい** → [Requirements.md](Requirements.md) の1〜3章(目的・コンセプト・対象範囲)
2. **Toolを設計・実装する** → [tools.md](tools.md) と [Requirements.md](Requirements.md) 6章(安全要件)・7章(動作原則)
3. **機種対応を追加する** → [device-profiles.md](device-profiles.md) と [phase0-results.md](phase0-results.md)
4. **MVP後の計画を知りたい** → [roadmap.md](roadmap.md)
