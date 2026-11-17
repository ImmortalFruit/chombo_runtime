---
name: calculate-rank-conditions
description: 確認済みの知識と条件計算Engineを使い、指定された入力や固定局面から一半荘・累積順位の達成条件を計算する。大会・リーグの目標ポイントは、明示された単純加算の条件だけを扱う。確率、期待値、押し引き、推奨打牌は扱わない。
---

# 順位・ポイント条件計算

`scripts/calculate_conditions.py` を実行し、検証済みの
順位要求には `CONDITION_RESPONSE`、ポイント要求には共有APIと同じ構造化結果を提示する。
Skill内へ点数・精算・順位・候補生成規則を複製しない。

目標順位Nは「N位以上」（順位数値N以下）を意味する。すでに上位の場合の順位維持も達成候補に含め、ちょうどN位へ下がることを要求しない。厳密上回り／同点到達可は目標順位の境界へ適用し、公式順位を書き換えない。

## 明示入力

`INPUT_CONSTRAINED_COMEBACK` の要求は `position_source.state` に完全な
`CONDITION_STATE` を含める。Rule Profileのファイルも明示する。

```bash
<workspace-python> skills/calculate-rank-conditions/scripts/calculate_conditions.py \
  --request request.json \
  --match-profile engine/profiles/m_league_official_rules_2026_09_08_content_snapshot.json
```

## 固定局面参照

`CURRENT_HAND_COMEBACK` の要求では、参照が固定する状態と
`CONDITION_SUPPORT_REF` を同時に渡す。IDだけから現在局面を推測しない。

```bash
<workspace-python> skills/calculate-rank-conditions/scripts/calculate_conditions.py \
  --request request.json --state state.json --support-ref support-ref.json \
  --match-profile engine/profiles/m_league_official_rules_2026_09_08_content_snapshot.json
```

累積順位は上記に `--aggregate-profile aggregate-profile.json` を加える。総合順位profileを
一半荘Rule Profileから推測しない。

## 大会・リーグの目標ポイント

`condition.points_query` と同じ構造化入力を `--point-input` へ渡す。
`--request`、`--state`、`--support-ref`、`--aggregate-profile` と混在させない。
閉じたfield一覧は `engine.application_schema.INPUTS['condition.points_query']`
またはRuntime OpenAPIの同名操作で確認する。

```bash
<workspace-python> skills/calculate-rank-conditions/scripts/calculate_conditions.py \
  --point-input points.json \
  --match-profile engine/profiles/m_league_official_rules_2026_09_08_content_snapshot.json
```

対象者・目標ポイント・比較 `GT`（厳密に上回る）／`GTE`（同点でも可）と、
`aggregation_policy:EXACT_SUM_NO_EXTRA_ADJUSTMENTS` を明示する。
四人の `aggregate_points` は今回半荘を含まない調整済み累積値とし、十進文字列で渡す。
入力した一局で半荘が終了する候補だけについて、半荘最終ポイントを一度加算する。
追加の丸め・罰則・倍率・後続局探索は扱わない。大会の正式な規則への適合を推測しない。
新規2 Conceptが未資格なら候補生成前に停止する。
順位条件の要求文書に、ポイント条件用の項目を追加しない。

## 必須境界

1. 文書は厳格loaderへそのまま渡し、hash不一致、未知版、余剰fieldを修復しない。
2. 要求が参照するConceptは実行時に `concepts/` の `QUALIFIED` 状態を確認する。
3. `match_rule_ref` と明示したRule Profile snapshotを一致させる。別profileへ自動変更しない。
4. `ACHIEVABLE` は順方向検証済み候補、`UNREACHABLE` は完全探索証拠、
   `INDETERMINATE_LIMIT` は部分候補と打切り理由を区別して表示する。
5. `INPUT_INCOMPLETE`、`INVALID_REQUEST`、`INVALID_STATE`、`SNAPSHOT_MISMATCH`、
   `RULES_INCOMPLETE`、`KNOWLEDGE_UNQUALIFIED`、`LIMIT_INVALID` を候補なしの停止結果として扱う。
6. 最小条件は発生確率や推奨度ではない。候補順を並べ替えず、計算前後の点数・順位、適用規則、
   根拠を応答から辿れる形で提示する。

## 境界

- 牌姿、他家の意思、発生確率、期待値、押し引き、推奨打牌を推測しない。
- 固定した状態、Rule Profile、support ref、Replay、checkpointを変更しない。
- Liveの自家制限、Replay grant、Navigator統合は後続の専用境界で扱う。本Skill単独で権限を補完しない。


### 配布版Runtimeの知識参照

Runtimeでは原稿の論理パスを知識包内に保持する。選択したPythonで
`-B -m engine.knowledge_cli --list` を実行し、
必要なIDだけ `-B -m engine.knowledge_cli --concept <concept_id>` で参照する。
返却された資格・来歴・依存を確認し、未資格・欠落・破損時は説明を停止する。
教材は同梱allowlistにあるものだけ `--lesson <lesson_id>` で参照可能であり、
未同梱の補足教材を利用済みとして扱わない。
