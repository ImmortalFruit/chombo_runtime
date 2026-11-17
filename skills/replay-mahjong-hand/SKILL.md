---
name: replay-mahjong-hand
description: 指定されたChombo形式の牌譜JSONを検証し、閲覧権限に応じたローカルの牌譜再生画面で開く。牌譜の読み込み、閲覧、再生、または局面の前後移動を求められたときに使用する。
---

# 指定牌譜を再生

最初に指定パスが読み取り可能なファイルであることを確認する。プロジェクトルートで次を実行し、表示されたlocalhost URLをCodex内ブラウザで開く。

```bash
<workspace-python> -m engine.hand_app replay <hand-log.json> --port <port> --authorized-replay
```

- 最初にワークスペース依存ランタイムを読み込み、返されたPythonの絶対パスを `<workspace-python>` に使う。
- 対応可否は牌譜内の形式・完全性・規則の検証結果に従う。旧版の牌譜を使うよう誘導せず、形式やhashを変更して読み込ませない。
- `EVALUATION` の牌譜は利用者が許可した場合だけ `--evaluation-record` を付ける。拒否された場合に自動で追加しない。
- portが未指定なら `8765` を使用し、使用中なら未使用のlocalhost portへ変更する。
- コマンドはReplayサーバーとして動き続ける。起動出力を確認したら、プロセスの終了を待たずにブラウザを開く。
- 既定は牌譜検討に必要な全四家の手牌を表示する権限付きReplay Projectionとし、`--authorized-replay` を明示して起動する。
- ユーザーが特定の自席視点を指定した場合だけ、`--human-seat east|south|west|north` を付ける。
- ユーザーが手牌非公開の公開観戦を明示した場合だけ、`--public-replay` を付ける。
- JSONの検証またはProjectionが失敗した場合、Viewerを迂回して正本用秘密情報をブラウザへ渡さない。

Viewerでは保存済みイベントの開始、前進、後退、任意位置への移動を行える。Skill側でイベントを補完、修正、並べ替えたり、外部形式をChombo形式の牌譜として読み込んだりしない。
