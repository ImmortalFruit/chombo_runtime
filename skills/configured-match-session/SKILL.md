---
name: configured-match-session
description: 指定した局・本場・供託・人間およびChombo三家の持ち点から、ローカルのテスト用半荘セッションを直接開始する。終局条件や結果画面などの再現テスト、将来機能の検証に使う。
---

# 条件指定した半荘セッションを開始

ユーザーから次の値を受け取り、プロジェクトルートでテスト用セッションを起動する。

- 場風：`east`または`south`
- 局番号：1〜4
- 本場、供託本数
- 人間一名とChombo 1〜3の持ち点
- 任意の再現用seed

```bash
<workspace-python> -m engine.match_app --rule-alias <selected-alias> \
  --start-round <east|south> --start-hand <1-4> \
  --honba <本場> --riichi-deposits <供託本数> \
  --human-score <人間点数> \
  --chombo-scores <Chombo1点数> <Chombo2点数> <Chombo3点数> \
  [--seed <seed>] --port <port>
```

- 最初にCodex workspace dependenciesを読み込み、返されたPython実行ファイルを`<workspace-python>`に使う。
- 指定点は表示上の席風ではなく、固定参加者である人間、Chombo 1、2、3へ対応させる。局開始時の席順が変わってもこの対応を維持する。
- 供託は点数ではなく1000点棒の本数で渡す。全員の持ち点と供託の整合性を勝手に補正しない。
- これはテスト専用の開始経路であり、通常の半荘開始や対局規則として扱わない。
- 起動後は表示されたlocalhost URLを開き、中央表示と4席の点数が指定条件に一致することを確認する。

今回の南4局条件は、人間6,200点、Chombo三家16,700／28,900／48,200点、0本場・供託なしとして指定する。

- 規則の選択は必須。選択済みの登録aliasを `--rule-alias` に渡す。固定の既定値へ戻さない。公式版の明示参照または独自確定版は `--rule-ref <reference.json>` を使い、独自版には `--rule-store <store-directory>` も渡す。未選択の場合は利用者に選択を求める。未対応・未確定・版不一致は起動前に拒否される。
