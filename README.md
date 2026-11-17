<p align="center">
  <img src="assets/chombo-logo.png" alt="Chomboのロゴ" width="360">
</p>

# Chombo v0.5.2

Chomboは、日本麻雀の学習・手牌解析・点数計算・ローカル対局・牌譜再生を支援するエージェントです。明示的に追加され、確認済みとなった知識とEngineの計算結果を使います。未学習の知識や未対応の規則は推測で補いません。

## 利用できる機能

- 牌の簡略表記から手牌図を生成
- 向聴数・聴牌・待ち・受け入れの解析
- 適用規則、符・翻・和了方法・親子・本場に基づく点数計算
- 利用者一人とChombo三家による一局対局・東南戦
- Chombo四家による一局の自動対局と牌譜保存
- 確認済みの項目だけを使った独自規則の作成・検証・確定
- 保存点からの復帰と、対応するChombo形式の牌譜再生
- 閲覧権限に応じた、固定された対局中・対局後の局面への質問
- 明示入力からの順位・目標ポイント条件計算

公式の累積総合順位規則は未提供です。規則が不足する計算は停止します。RMU新決勝は確認済みの知識説明に限り、専用計算や対局は提供しません。

## 配布物

WindowsとmacOSで共通のPython Runtime Bundleを使います。Engine、10 Skill（麻雀機能9種とCodex用の不具合報告）、知識包、表示資産、ライセンスを含みます。必要なPythonソースは含まれますが、別のソースコードZIPは用意しません。

教材原稿全体、評価試験、学習履歴、開発テスト、計画文書、ビルド用スクリプトは同梱しません。`release-manifest.json` に元の開発revisionと各ファイルのSHA-256を記録します。

## 導入前の準備

- Python 3.9以降
- 手牌図を生成するためのPillow
- Codex経由で使う場合はローカルpluginを利用できるCodexデスクトップ

配布案内で指定されたZIPと対応するchecksumを取得し、照合後に新しいフォルダーへ展開してください。単体RuntimeにCodex・Node.js・Gitは不要です。Codex経由では、ワークスペース依存ランタイムを読み込み、返されたPythonの絶対パスを使います。

## CodexのSkillとして使う

配布物全体を保持してください。`skills/` だけの移動やコピーではEngine・知識を参照できません。既存の `.codex-plugin/plugin.json` が `./skills/` を指定しているため、元の配置を維持してローカルmarketplaceから登録できます。

marketplaceルートの `.agents/plugins/marketplace.json` で、`plugins[].source.path` を完全なBundleへの `./` で始まる相対パスにします。パスはmarketplaceルート内を指定してください。登録例は次のとおりです。

```text
codex plugin marketplace add <marketplaceルートの絶対パス>
```

デスクトップのPlugins画面でローカルsourceを選び、Chomboをインストール・有効化します。ホストは管理用cacheへコピーします。元配置とcacheを区別し、更新後は説明文とSkill一覧が新しくなっていることを確認してください。

公式導入手順: https://developers.openai.com/plugins/build/plugins#install-a-local-plugin-manually

| Skill ID | 用途 |
|---|---|
| `render-hand` | 手牌図の生成 |
| `analyze-hand-completion` | 手牌解析 |
| `calculate-mahjong-score` | 点数計算 |
| `quick-game` | 一局対局 |
| `hanchan-session` | 半荘対局 |
| `configured-match-session` | 条件指定の検証用対局 |
| `generate-autonomous-hand` | Chombo四家の自動対局 |
| `replay-mahjong-hand` | 指定牌譜の再生 |
| `calculate-rank-conditions` | 順位・ポイント条件計算 |
| `report-chombo-bug` | Codex経由の不具合報告（内容確認後に送信） |

```text
$analyze-hand-completion で 123456m789p5567s を解析してください。
$quick-game で、使用する規則を確認して一局を開始してください。
$replay-mahjong-hand で、指定したChombo形式の牌譜を再生してください。
```

旧版の牌譜を別途用意する必要はありません。指定牌譜の形式・完全性・規則・来歴を検証し、対応する場合だけ開きます。評価用牌譜には明示許可が必要です。形式、版、hashや `baseline_status` を書き換えて読み込ませないでください。

## 単体Runtimeとして使う

利用者が用意したPythonとPillowを使います。同じ実行ファイルで `import PIL` が成功することを確認し、展開先の `engine/` と `release-manifest.json` があるフォルダーから起動します。Windows PowerShellでは、引用符で囲んだ実行ファイルの前に `&` を付けます。

```powershell
$chomboPython = 'C:\PATH\TO\python.exe'
& $chomboPython -B -c 'import sys; import PIL; print(sys.version); print(PIL.__version__)'
& $chomboPython -B -m engine.runtime serve --port 0
```

macOSでも選択したPythonの絶対パスを使います。

```sh
CHOMBO_PYTHON=/absolute/path/to/python3
"$CHOMBO_PYTHON" -B -c 'import sys; import PIL; print(sys.version); print(PIL.__version__)'
"$CHOMBO_PYTHON" -B -m engine.runtime serve --port 0
```

既定の保存先はWindowsが `%LOCALAPPDATA%\Chombo\data`、macOSが `~/Library/Application Support/Chombo/data` です。検証や複数環境では、別途案内された専用config/dataを指定し、起動・状態確認・停止で同じ指定を使ってください。

起動出力のREADYに表示されたローカルURLを開きます。起動ターミナルは開いたままにし、停止には別ターミナルで同じPython・展開先・設定を使います。別ターミナルでもPythonの変数を設定し直してください。ブラウザーを閉じてもRuntimeは停止しません。

Windows PowerShell:

```powershell
$chomboPython = 'C:\PATH\TO\python.exe'
& $chomboPython -B -m engine.runtime status
& $chomboPython -B -m engine.runtime stop
```

macOS:

```sh
CHOMBO_PYTHON=/absolute/path/to/python3
"$CHOMBO_PYTHON" -B -m engine.runtime status
"$CHOMBO_PYTHON" -B -m engine.runtime stop
```

同じdataを使うRuntimeは一つだけ起動できます。ポート競合や書込み不能では起動を停止します。更新時はRuntimeを停止し、新しいBundleを別フォルダーへ展開して外部dataを保持します。

対局、保存点、牌譜再生、規則編集、手牌解析、固定局面の質問、順位・ポイント条件の入力画面を利用できます。条件計算画面は明示入力を使い、対局・牌譜からの自動抽出は行いません。HTTP仕様は `/api/v1/openapi.json`、Codex向けの参照確認は別の `/api/conversation/openapi.json` です。

## 任意の外部LLM接続

### BYOKチャットはデモ・評価中です

v0.5.2では外部APIへの接続と、限られた定型応答を確認するデモとして提供します。質問を自由入力できても、学習済みConceptを横断した知識検索や、質問に応じた本格的な会話は未対応です。現在の知識応答は向聴・受け入れ・形だけの聴牌の3教材を対象とし、局面では許可された既存の解析結果・公開情報を使います。

Conceptに基づく知識の接続と自然な説明・会話はv0.5.3以降の実装対象です。デモでもAPI利用料は利用者の負担です。接続確認の成功を、一般会話や知識連携の完成とは扱わないでください。Codex経由の既存Skill・知識参照は別の経路です。

OpenAI・Anthropic・GeminiへのBYOK接続は任意です。基本機能とCodex経由の利用には、追加のChombo用APIキーは不要です。

使う場合はローカルの設定画面へキーを入力します。キーはブラウザーセッションに対応するRuntimeのメモリー上に保持し、ディスクやブラウザー保存領域には保存しません。削除・期限切れ・Runtime再起動で失効します。

外部へ送る内容をpreviewで確認し、送信ごとに許可してください。再試行にも新しい許可が必要です。API利用料はキー所有者がproviderへ支払います。利用可能モデルや課金はChatGPTの契約とは別に確認してください。

実providerへの正常応答・費用・遅延は未認定です。利用上限により停止した試行は成功扱いにしません。自動検定の代替通信結果も実API成功とは区別します。本版の対応環境・未検証項目は配布案内に従い、公開Alphaの認定済みとは扱わないでください。

## 対応範囲と制限

四人打ち日本麻雀の一局・東南戦を、対応する公式規則または確定した独自規則で実行します。保存点、規則表示、改変検出付き牌譜、閲覧権限を守った局面会話、確認済みの特殊終局、処理中断からの回復を提供します。

四家自動対局の観戦では表示待ちを省略でき、人間参加時は対局の表示間隔を保持します。既存牌譜は明示された互換情報だけで判定し、未知の実装hashを推測で受け入れません。

東風戦の開始、オンライン対戦、外部牌譜形式の変換、未知版や破損状態の推測修復、強い戦術判断は提供しません。Python向けの既存入口はありますが、安定した公開SDKや将来の互換性は保証しません。

## 既知の不具合

2026-11-17時点の既存報告とWindows受入結果を整理しています。次の9件は未解決として扱います。各検証から全OS・全入口へ影響を拡張しておらず、この候補での全件再現確認も未完了です。

| 対象 | 事象・影響 | 当面の対処 |
|---|---|---|
| 全手牌公開Replayの待ち表示 | 他家の公開手牌が残枚数・枯れ待ちの暗転へ反映されない（#277）。 | 全公開時の表示枚数を検討の根拠にせず、公開牌の所在を別途確認してください。 |
| 観戦半荘の対局結果 | 「閉じる」を押しても結果ダイアログが残る（#278）。 | 終了済みの画面はタブを閉じてください。Runtimeの停止は別途必要です。 |
| Replayの局面スライダー | 左右矢印キー1回で2イベント移動する（#275）。 | 1イベントずつの移動には「戻る」「進む」ボタンを使ってください。 |
| 一局対局の複数候補チー | 定期更新で途中の牌選択が解除され、選択を完了しづらい（#269）。 | 該当画面での確実な回避策は未確認です。選択できなければ対局を中断してください。 |
| 順位条件CLI | `score_bounds` を指定すると未処理例外が発生する（#268）。 | 範囲指定を必要とする計算は利用を控えてください。条件を省いた結果を同じ問題の答えとして使わないでください。 |
| ポイント条件CLI | `rule_ref` のruleset ID・版が不一致でも受理される場合がある（#273）。 | 入力の参照全項目と指定profileの一致を実行前に確認してください。成功応答だけでは版の一致を保証できません。 |
| 自走生成CLI | 存在しない `--rule-ref` ファイルで未処理例外が発生する（#272）。 | 参照ファイルの存在と読取可否を確認してください。失敗時に牌譜は生成されません。 |
| 和了資格CLI | contextの `own_discards: null` で未処理例外が発生する（#270）。 | 実際の自己捨て牌を配列で渡してください。捨て牌がない場合だけ空配列を使います。 |
| 点数計算CLIの高点法 | 本場数の異なる候補を混ぜると、本場込み額で比較して最高通常点や同点候補を失う（#267）。 | 異なる本場の候補を一括比較しないでください。同一局面・同一本場の実対局での発生は未確認です。 |

Issue番号は開発リポジトリでの管理番号です。閲覧権限がなくても、問い合わせ時にこの番号を添えていただけます。

Windowsの長い保存パスで復元に失敗する問題はv0.5.2で修正しました。更新前に保存データのバックアップを保持してください。過去のZIPは自動更新されないため、使用中の版を確認してください。

v0.5.2では、連荘Replayの局選択に本場が表示されず候補が同名になる問題（#276）を修正しています。局名は「東2局 0本場」の形式になります。既存のインストールや過去のZIPには自動反映されません。

### 未検証項目

WindowsとmacOSの単体Runtime・Codex Pluginは、受入記録に示す環境と範囲で確認しています。clean VM・別CPU・新しいCodexチャットでのSkill自動選択、人によるスクリーンリーダー確認、実providerの正常応答・費用・遅延は未認定です。Python・Pillowのすべての版や組合せを保証しません。以前の候補での成功を、別のZIPの認定へ読み替えないでください。これらは上記の既知の実装不具合とは区別します。

## 知識包

`knowledge/chombo.ckp` に確認済みConceptと、選択された教材・索引・検証情報を収録します。論理パスと既存のライセンスは保持します。

```sh
<selected-python> -B -m engine.knowledge_cli --list
<selected-python> -B -m engine.knowledge_cli --concept <concept_id>
<selected-python> -B -m engine.knowledge_cli --lesson <lesson_id>
```

欠落・破損・未対応形式なら停止します。追加キーやネット接続は不要です。圧縮・可逆難読化は秘密保持や抽出不能を保証するものではありません。

## AIによる実装について

コード、テスト、実行設定、技術文書は、人間の指示のもとOpenAI Codexを用いてAIが作成しました。所有者が目的・知識・資料を提供し、結果と利用範囲を確認しています。ロゴなどのメディア来歴は `ASSET_RIGHTS.md` を参照してください。

## ライセンスと依存資産

コードと一般文書はApache License 2.0、構造化された麻雀知識・表示データ・配布メディアはCC BY 4.0です。範囲・帰属・来歴は `LICENSE_SCOPE.md`、`NOTICE`、`ASSET_RIGHTS.md`、`LICENSES/CC-BY-4.0.txt` を参照してください。Chomboの名称・ロゴの商標権は許諾しません。複製・改変・再配布・商用利用に新しい制限を加えるものではありません。

Web依存資産の一覧とライセンス本文は `ui/.output/public/_nuxt/third-party-notices.json` にあります。元の著作権・ライセンス条件は引き続き適用されます。

## エラー・不具合の問い合わせ

不具合は [GitHubで起票](https://github.com/ImmortalFruit/chombo_runtime/issues/new)、または
[chombo.feedback@gmail.com](mailto:chombo.feedback@gmail.com) へお問い合わせください。
GitHubにアクセスできない場合はメールをご利用ください。
版、OS/CPU、再現手順、期待した結果と実際の結果、エラーコードを記載し、
APIキー・control token・個人情報や会話全体を送らないでください。
GitHub報告のメール自動転送と、エラー時の自動送信は行いません。

Codex利用時は `$report-chombo-bug` で報告文を作成し、内容と送信先を確認してから、
接続済みGitHubまたはメール機能で送信できます。接続や権限がなければ下書きと手動手順を返します。
単体Runtimeでは上記の窓口へ手動でご連絡ください。
v0.5.2には報告Skillを収録しています。v0.5.1以前の配布物には含まれていません。

## 文書の更新

最終確認日: 2026-11-17。v0.5.2向けの説明整備。

このREADMEの正本は開発リポジトリの `distribution/README.md` です。導入・機能・制限の変更と同じ作業で更新します。正式資材では対応版を確認し、過去の公開ZIPを後から書き換えません。

Pythonでの利用例と制限は [Python Developer Preview](PYTHON_DEVELOPER_PREVIEW.md) を参照してください。
