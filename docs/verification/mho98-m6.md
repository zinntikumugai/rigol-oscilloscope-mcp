# MHO98 M6(取り込み制御と同期)実機検証記録

**対象:** RIGOL MHO98(ファームウェア 00.01.00、LAN SCPI :5555)。IP・シリアルは記録しない
**手順規律:** [mho98-unlicensed.md](mho98-unlicensed.md) と同じ(1コマンド → 応答/エラーキュー確認 → 記録)
**参照ガイド:** MHO98 Programming Guide PGA45102-1110(2026-02-26版、対象ソフトウェア 00.01.00)

## 1. 取得モード(issue #52、2026-11-25)

### 1.1 読み取りプローブ

| コマンド | 応答 | エラーキュー |
|---|---|---|
| `:ACQuire:TYPE?` | `NORM` | No error |
| `:ACQuire:AVERages?` | `2` | No error |
| `:ACQuire:BITS?` | `14` | No error |
| `:ACQuire:MDEPth?` | `1.0000E+04` | No error |

応答は全て短形。どのモードでも `:BITS?` は読める。

### 1.2 書き込みプローブ(最後に元の値へ戻した)

| 状態 | コマンド | エラーキュー | 読み戻し |
|---|---|---|---|
| NORM | `:ACQuire:BITS 14`(現在値と同じ) | **`-200,"Command execute failed"`** | — |
| NORM | `:ACQuire:AVERages 4` | No error | `4` |
| AVER | `:ACQuire:AVERages 100` | **`-200,"Command execute failed"`** | (変化なし) |
| AVER | `:ACQuire:AVERages 128` | No error | `128` |
| AVER | `:ACQuire:AVERages 65536` | No error | `65536` |
| AVER | `:ACQuire:BITS 14` | **`-200`** | — |
| HRES | `:ACQuire:BITS 16` | No error | `16` |
| HRES | `:ACQuire:BITS 14` | No error | `14` |
| HRES | `:ACQuire:AVERages 4` | No error | `4` |
| PEAK | `:ACQuire:BITS 14` | **`-200`** | — |

### 1.3 ガイドとの食い違い(実装へ反映済み)

1. **平均回数の非2のべき乗は切り下げではなく `-200` で拒否される。** ガイド3.3.1 の Remarks「2のべき乗でない値は、それより小さい最も近い2のべき乗が自動入力される(9 → 8)」は前面パネルのテンキー入力の話で、SCPIには当てはまらない。→ `configure_acquisition` は送信前に `INVALID_PARAMETER` で弾く
2. **`:ACQuire:BITS` は high_resolution モードでしか書けない**(現在値と同じ値でも `-200`)。ガイド3.3.5 は「高分解能モードの分解能」と書くだけで、他モードで書き込みを拒否するとは書いていない。→ 送信前に有効モードを判定して弾く(`mode` 未指定なら `:ACQuire:TYPE?` を1回だけ読む)。送信順は `TYPE → AVERages → BITS` で固定
3. 平均回数は **average 以外のモードでも書き込める**(NORM / HRES で受理)。設定は保持され、average へ切り替えたときに効く

### 1.4 スイート実行

- read-only(`-m device tests/device/test_readonly.py`): **20 passed, 1 skipped**(`test_acquisition_config_answers` を含む)
- write(`-m device_write -k acquisition`): **7 passed**。4モードの往復、平均回数65536の往復、high_resolution + 16bit の往復。全テストで復元漏れゼロ(復元は「high_resolution で分解能を戻す → 元のモードと平均回数を戻す」の2段)
