---
name: generate-autonomous-hand
description: Chombo四家を自走させて一局を完了し、再現可能なChombo形式の牌譜JSONを生成する。自走対局、四家シミュレーション、またはseed指定の牌譜生成を求められたときに使用する。
---

# 四家自走牌譜を生成

プロジェクトルートで次を実行する。

```bash
python3 skills/generate-autonomous-hand/scripts/generate_hand_log.py --rule-alias <selected-alias> \
  [--seed <seed>] [--output <output.json>]
```

- seedが未指定なら、起動時のUnix timestampをミリ秒単位で取得して使用する。自動生成時に固定値 `0` を補わない。
- ユーザーが再現用seedを明示した場合は、その整数を変更せず使用する。
- 出力先が未指定なら `outputs/autonomous-hand-seed-<seed>.json` を使用する。
- 既存ファイルは上書きしない。ユーザーが同じパスへの置換を明示した場合だけ `--force` を付ける。
- 成功時は、出力された要約から絶対パス、seed、終局理由、和了者、最終点、イベント数、判断数を案内する。
- 生成後の再生も求められた場合は `replay-mahjong-hand` を使用する。

スクリプトは選択済みprofileの共通検証器、既存の四席Controller、一局Orchestrator、原子的牌譜Writerを呼び出す薄い境界である。Skill側へ麻雀規則や代替方策を実装しない。

現在の生成範囲は一局だけであり、複数局、半荘、最終順位、戦術強度評価は扱わない。

- 規則の選択は必須。選択済みの登録aliasを `--rule-alias` に渡す。固定の既定値へ戻さない。公式版の明示参照または独自確定版は `--rule-ref <reference.json>` を使い、独自版には `--rule-store <store-directory>` も渡す。未選択の場合は利用者に選択を求める。未対応・未確定・版不一致は起動前に拒否される。
