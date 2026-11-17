---
name: hanchan-session
description: Chombo三家と人間一家によるローカルのリアルタイム半荘セッションを開始する。半荘、東南戦、または複数局を通した対局を求められたときに使用する。
---

# 半荘セッションを開始

プロジェクトルートで次を実行し、表示されたlocalhost URLをCodex内ブラウザで開く。

```bash
<workspace-python> -m engine.match_app --rule-alias <selected-alias> [--seed <seed>] [--log <match.json>] --port <port>
```

- 最初にCodex workspace dependenciesを読み込み、返されたPython実行ファイルを`<workspace-python>`に使う。
- seed未指定時はコマンド側の時刻seedを使う。再現用seedが指定された場合だけ、その整数を渡す。
- port未指定時は`8765`を使い、使用中なら空いているlocalhost portへ変更する。
- サーバーの終了を待たず、起動出力のURLと現在の人間席を確認してブラウザを開く。
- 局ごとの席風変更後も同じ人間参加者を追跡する。合法性、局間遷移、南四局終局、最終ポイントはEngineへ委ねる。
- 半荘終了時に`--log`で指定した`MatchRecord`を原子的に保存する。省略時は`match.json`を使う。
- 開発版で保存した評価用（`EVALUATION`）牌譜は、`<workspace-python> -m engine.match_app --replay <match.json> --allow-evaluation-records --port <port>`で明示許可して開く。局選択または前局・次局で局を移動し、局内は従来のReplay操作で移動する。
- `--allow-evaluation-records` はReplay専用で、省略時は評価用牌譜を拒否する。`RELEASED` 牌譜では省略できる。保存済みの `baseline_status` やhashを書き換えて再生可能にしない。完全性・規則版・コード来歴の検証と、`--public-replay`／`--human-seat` の観測範囲はこのフラグとは独立に維持する。
- 終局画面のローカル確認が必要な場合だけ、起動URLの`/test/south-4`を開く。このテスト専用経路は南4局0本場・供託なし、Chombo 1〜3を16,700／28,900／48,200点、人間を6,200点としてセッションを置き換える。

一局だけなら`quick-game`を使う。東風戦は専用の完全プロファイルとEngine対応が追加されるまで開始しない。

- 規則の選択は必須。選択済みの登録aliasを `--rule-alias` に渡す。固定の既定値へ戻さない。公式版の明示参照または独自確定版は `--rule-ref <reference.json>` を使い、独自版には `--rule-store <store-directory>` も渡す。未選択の場合は利用者に選択を求める。未対応・未確定・版不一致は起動前に拒否される。
