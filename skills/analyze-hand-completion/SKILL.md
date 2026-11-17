---
name: analyze-hand-completion
description: 手牌の形からシャンテン数、受け入れ、聴牌、待ち、完成形を分析し、承認済み範囲では役・振り聴・宣言を統合した和了資格まで決定する。手牌完成、待ち、和了可否、または多家和解決を求められたときに使用する。
---

# 手牌完成分析

`scripts/analyze_hand.py` に13枚または14枚の簡略記法を1引数で渡す。

```bash
python3 skills/analyze-hand-completion/scripts/analyze_hand.py '123456m789p5567s'
```

固定済みの副露・槓子がある標準形では、残る手牌、固定面子数、固定牌を渡す。

```bash
python3 skills/analyze-hand-completion/scripts/analyze_hand.py \
  --fixed-meld-count 1 --fixed-tiles '9999m' '1112345678m'
```

和了判定では、和了前の13枚相当手牌、和了牌、取得方法、承認済み局面状態を渡す。

```bash
python3 skills/analyze-hand-completion/scripts/analyze_hand.py \
  --win-tile 5s --method ron \
  --context-json '{"player":"self","concealed":true,"prevailing_wind":"1z","seat_wind":"2z","winning_event":"ordinary","riichi_established":true,"double_riichi_established":false,"ippatsu_right_active":false,"quad_count":0,"concealed_triplet_or_quad_count":0,"fixed_melds":[],"own_discards":[],"same_turn_missed_win":false,"riichi_missed_win":false,"declared":true}' \
  '123456m789p55s67s'
```

多家和候補は各人を先に和了判定し、その結果をまとめて解決する。プロファイル未指定時はChombo Agent方針の `m_league_head_bump` を使用する。

```bash
python3 skills/analyze-hand-completion/scripts/analyze_hand.py \
  --claimants-json '[{"player":"south","relative_to_discarder":"next_player","can_win":true},{"player":"west","relative_to_discarder":"opposite_player","can_win":true}]'
```

形解析モードは `engine.codex_adapter` → 共通 `Application` → `engine.hand_completion` を呼び、資格確認済みのJSON結果を従来形式で提示する。和了資格・多家和は既存の専用Engine経路を維持する。追加APIキー・外部通信は不要である。Skill内へ麻雀規則を複製しない。

## 入力

- 萬子・筒子・索子は `m`・`p`・`s`、字牌は `z` を使う。
- 赤5の `0` は同じ牌種の5として数える。
- 多家和候補はJSON配列で渡し、各要素に空でない文字列 `player`、profile内の `relative_to_discarder`、真偽値 `can_win` を指定する。人物と相対席は、不適格候補を含めそれぞれ一意とする。重複・欠落・型不正は入力エラーとして拒否し、一意化や資格の再計算は行わない。
- 通常分析では、ちょうど13枚または14枚を指定する。
- 固定面子分析では、固定面子を除いた13枚相当の残り牌を指定する。槓子も面子数では1組として数え、`--fixed-tiles` には物理的な4枚を渡す。
- 和了判定では固定面子を含む13枚相当の手牌を渡し、槓子はその中で3枚相当、`context.fixed_melds[].tiles` では物理4枚で指定する。物理4枚目を待ち候補の上限へ反映し、5枚目の和了牌は待ち集合に含めず `NOT_QUALIFIED` を返す。取得前から物理5枚以上、または固定面子の3枚相当が手牌に不足する入力は拒否する。
- 横向き・伏牌を含む表示記法は直接受け付けない。固定面子は通常の簡略記法へ正規化する。

## 出力

- 対象3形それぞれと全体のシャンテン数
- 完成済みなら該当する完成形（Engine契約ではシャンテン `-1`）
- 13枚の0シャンテンなら聴牌、待ち牌、待ち分類、完成形、全分解、牌種数、手牌基準枚数
- 13枚の1シャンテン以上なら受け入れ、進行後シャンテン、進む完成形、牌種数、手牌基準枚数
- 固定面子分析なら、固定牌の使用枚数も反映した一般形の待ち牌集合
- 和了判定なら、完成形、成立役、振り聴区分、和了資格、宣言結果、局終了処理へ渡す最小結果
- 多家和なら、適用プロファイル、全候補、選ばれた和了者集合

## 必須実行

- 複合形の待ちを問われた場合、見た目による分解だけで回答せず、このSkillで全候補を列挙する。
- 13枚の手牌から得点計算を求められた場合、得点計算を始める前に必ずこのSkillを実行する。Engineが返した待ち牌集合と待ちごとの全分解を、符・役・翻・高点法の候補入力として使用し、目視で選んだ一部の待ちだけを採点しない。
- 得点計算へ渡すときは、`waits[].tile`、`waits[].wait_types`、`waits[].decompositions` を欠落させない。Engine結果が得られない場合は得点を断定せず、待ち列挙を完了できないことを明示する。
- 立直後暗槓の待ち不変性を問われた場合、暗槓前と暗槓後をそれぞれ実行し、待ち集合全体を比較する。
- 固定槓子で4枚使い切った牌を待ちへ含めないよう、固定牌も必ず入力する。
- Skill結果が得られない場合、単一の推測した待ちから暗槓可否を断定しない。

## 境界

通常の手牌解析と固定面子分析は従来どおり形だけを扱う。和了判定モードだけが承認済み役・振り聴・宣言を統合する。得点計算前の実行では待ち・分解候補を後続へ渡すだけであり、このSkill自身は符・点数を計算しない。役満、符、点数、支払い、親の継続、次局遷移、取得後の打牌、何切る、最善打牌、牌効率は判定しない。未承認細則を必要とする役は、承認済み状態入力がない限り推測しない。Skill内へ麻雀規則を複製せず、Engine結果を提示する。
