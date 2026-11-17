---
name: quick-game
description: Chombo三家と人間一家によるローカルのリアルタイム一局対局を開始する。短い一局、クイックゲーム、または従来の一局対局を求められたときに使用する。
---

# 一局対局を開始

プロジェクトルートで次を実行し、表示されたlocalhost URLをCodex内ブラウザで開く。

```bash
<workspace-python> -m engine.hand_app play --rule-alias <selected-alias> [--seed <seed>] --port <port> --log <output.json>
```

- 最初にCodex workspace dependenciesを読み込み、返されたPython実行ファイルを`<workspace-python>`に使う。
- seed未指定時はコマンド側の時刻seedを使う。再現用seedが指定された場合だけ、その整数を渡す。
- port未指定時は`8765`、牌譜出力先未指定時は既存ファイルと衝突しない`outputs/`配下を使う。
- サーバーの終了を待たず、起動出力のURLと人間席を確認してブラウザを開く。
- 終局時に一局牌譜が保存される。合法性や席割当はEngineへ委ね、他家の秘密情報を公開しない。

一局だけを実行する。半荘には`hanchan-session`を使う。

- 規則の選択は必須。選択済みの登録aliasを `--rule-alias` に渡す。固定の既定値へ戻さない。公式版の明示参照または独自確定版は `--rule-ref <reference.json>` を使い、独自版には `--rule-store <store-directory>` も渡す。未選択の場合は利用者に選択を求める。未対応・未確定・版不一致は起動前に拒否される。
