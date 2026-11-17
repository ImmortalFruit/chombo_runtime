---
name: calculate-mahjong-score
description: 資格済みの点数計算Engineを使い、明示された符・翻・役満単位、ロン・ツモ、親子、本場、ルールプロファイルから日本麻雀の点数と支払内訳を決定する。通常点、本場込み点数、切り上げ満貫、13翻以上、複数候補の高点法、明示指定された青天井の符・翻からの点数を計算するときに使用する。
---

# 麻雀点数計算

`scripts/calculate_score.py` を実行し、EngineのJSON結果を提示する。Skill内へ点数規則を複製しない。

## 単一候補

符、翻、和了方法、親子、プロファイルを明示する。

```bash
python3 skills/calculate-mahjong-score/scripts/calculate_score.py \
  --fu 30 --han 4 --method ron --non-dealer \
  --profile general_red_kazoe_yakuman --honba 1
```

役満は役満単位を渡す。一般役の符・翻へ換算しない。

```bash
python3 skills/calculate-mahjong-score/scripts/calculate_score.py \
  --yakuman-units 2 --method tsumo --dealer --profile m_league_2026
```

## 高点法

全有効分解と和了牌割当から得た採点候補をJSON配列で渡す。候補を目視で間引かない。

公開候補の必須fieldは `fu`・`han`・`method`・`dealer`・`profile_id`、任意fieldは `candidate_id`・`honba_count`・`yakuman_units` のみとする。`dealer` は真偽値、数値fieldは0以上の整数、`method` は `ron` または `tsumo`、`profile_id` は登録済みの文字列を指定する。`candidate_id` 省略時は入力位置の文字列IDを付ける。

余剰fieldは採点前に拒否する。内部用 `profile_override`・`snapshot_id_override` を公開JSONで受け付けず、規則や出典を黙って差し替えない。信頼済みの解決済み独自規則による内部Python呼出しは、この公開候補入力とは区別する。

```bash
python3 skills/calculate-mahjong-score/scripts/calculate_score.py \
  --candidates-json '[{"candidate_id":"a","fu":30,"han":4,"method":"ron","dealer":false,"profile_id":"general_red_kazoe_yakuman"},{"candidate_id":"b","fu":60,"han":3,"method":"ron","dealer":false,"profile_id":"general_red_kazoe_yakuman"}]'
```

## 必須手順

1. Engineの資格済みプロファイルsnapshotと参照Conceptがすべて `QUALIFIED` であることを実行時に確認する。不整合なら計算しない。
2. 青天井の明示指定がある場合は、下記「青天井の数値計算」の入力確認へ進み、確定済み数値を使用する。青天井以外で13枚相当の手牌から計算する場合は、先に `analyze-hand-completion` Skillで全待ち・全分解を列挙する。
3. 通常の手牌計算では、和了資格、成立役、ドラ、役満、最終符をそれぞれのEngine出力から確定する。
4. プロファイルを明示し、本Skillへ数値入力を渡す。
5. `base_payments`、`honba_payments`、`winner_total_receipt` を分離したまま説明する。
6. 本場がある場合、結論では `score_declaration` を先に示す。ロンは `5200は5500`、子ツモは `1300・2600は1400・2700`、親ツモは `4000は4200オール` の形を使う。

## 青天井の数値計算

ユーザーが青天井計算を明示した場合に限り、`aotenjo_numeric_v1` を選択する。通常の競技プロファイルから自動で切り替えない。資格済み Concept は `concepts/aotenjo.json`、補足教材は `docs/learning/ja-JP/supplements/aotenjo.md` を参照する。

```bash
python3 skills/calculate-mahjong-score/scripts/calculate_score.py \
  --fu 40 --han 4 --method ron --non-dealer --profile aotenjo_numeric_v1
```

この入力は子ロン10,300点となる。確定済みの一般役・ドラの翻数と最終符を入力し、親子・ロン・ツモの支払内訳を得る。個別役満の翻数換算と本場は未対応のため、役満単位や正の本場数を入力しない。手牌から青天井固有の複合役や符を推定しない。

青天井の入力確認と結果表示は次の順序で行う。

1. 青天井の明示指定と、確定済みの符・一般役とドラの翻数・親子・和了方式を確認する。欠けた値や青天井固有の役・符を推定しない。
2. 個別役満や正の本場数が指定されていれば未対応の理由を説明する。役満を13翻へ換算したり、本場を0へ落としたりして入力を書き換えない。
3. 専用プロファイルを渡して実行し、`profile.profile_id` と `score_class` が青天井の結果であることを確認する。
4. `score_declaration` に加え、青天井を適用したこと、支払者別の `base_payments`、`winner_total_receipt` を示す。ツモの各支払いと受取総額を取り違えない。
5. 一般役とドラの合計が13翻以上でも、確定済みの数値として扱う。個別役満の換算と混同しない。

## 境界

- 現在のEngineは成立役、ドラ、符の構成要素を推測しない。
- 責任払い、多家和の特例適用、供託、流局、親継続、次局遷移は計算しない。
- 未登録プロファイルや、プロファイルで未承認の13翻以上を既定値で補わない。
- 資格済みプロファイルsnapshotの検証はConcept状態を変更しない。


### 配布版Runtimeの知識参照

Runtimeでは原稿の論理パスを知識包内に保持する。選択したPythonで
`-B -m engine.knowledge_cli --list --document concepts/aotenjo.json` を実行し、
必要なIDだけ `-B -m engine.knowledge_cli --concept <concept_id>` で参照する。
返却された資格・来歴・依存を確認し、未資格・欠落・破損時は説明を停止する。
教材は同梱allowlistにあるものだけ `--lesson <lesson_id>` で参照可能であり、
未同梱の補足教材を利用済みとして扱わない。
