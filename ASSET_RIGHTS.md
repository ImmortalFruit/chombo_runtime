---
record_version: 1
approval_status: APPROVED
last_verified: 2026-11-11
---

# Chombo 配布素材権利台帳

## 判定

この台帳は配布候補に含まれる画像・音声を列挙し、確認済み事実と未解決事項を分離する。プロジェクト所有者による制作来歴と公開ライセンスの確認、および最終ファイルのhash検査を2026-09-05に完了し、配布対象素材を `APPROVED` とした。

2026-11-11、所有者の明示確認により、A-05の既存アイコン2種類を元ロゴと同じCC BY 4.0として追記した。
画像の変更や公開は行っていない。2026-09-05の既存素材の承認記録はそのまま保持する。

## 共通境界

- 配布対象は本書の一覧に記載したファイルだけである。
- Nuxtがhash付きfilenameで収録する同一copyは、SHA-256で本書の正本へ対応付け、同じlicense・帰属を保持する。
- `assets/generated/`、`local-assets/`、生成スクリプト、フォント、旧素材、作業中間物はRelease Bundleへ含めない。
- 生成AIまたはCodexを制作手段に使ったという事実だけで、各法域における著作権成立を保証しない。本書は制作経緯、第三者依存、配布許可の内部記録であり、法律意見ではない。
- 配布対象メディアはプロジェクト所有者の2026-09-05の承認、およびA-05に記録した2026-11-11の追加確認によりCC BY 4.0で提供する。帰属表示は「Chombo project contributors」とし、本書をライセンス表示として同梱する。
- Chomboの名称・ロゴに関する商標権その他の標識上の権利は許諾しない。CC BY 4.0による著作権上の許諾は、公式性、提携、承認を表示する権利を与えない。

## A-01 Chomboロゴ

| 項目 | 記録 |
|---|---|
| 開発元／Bundle配布先 | `docs/assets/chombo-logo.png` ／ `assets/chombo-logo.png` |
| SHA-256 | `498cbfd64a314641adfe22d05994881965cbdf4a17cae38c484290f631442040` |
| リポジトリ初出 | 2026-08-08 18:22:56 +0900、commit `bddef18152fa096c76822acb5cf47bf420783d67` |
| 技術情報 | 1,254×1,254px、RGB PNG、alphaなし、埋め込みカラープロファイルなし |
| 制作申告 | プロジェクト所有者が定義した「チョンボ」という名称と、熟練した日本麻雀プレイヤーを支援する長期目標をもとに、簡潔なロゴを作るようChatGPTへ明示的に依頼し、ChatGPTの画像生成機能で生成 |
| 改訂 | 初回生成後、字幕・タグライン2行を削除し、図形エンブレムと大きな `chombo` ワードマークだけを残すよう所有者が指示。その改訂後画像を採用 |
| 導入経路 | 2026-08-08 18:21:01 +0900、ユーザーがローカルPNGをCodex会話へ添付。同18:22:00、Codexが画素加工せず `docs/assets/chombo-logo.png` へコピー。その会話内に画像生成処理はない |
| 監査資料 | ローカル保存済みCodex session `rollout-2026-08-08T15-26-36-019fe00d-50d7-74b3-b2da-eb093fdd2813.jsonl` の09:21:01Z〜09:22:00Z。生成元ChatGPT会話そのものではなく、リポジトリ導入経路だけを証明する |
| 第三者入力 | 2026-09-05に所有者が提示した完全な制作来歴では、入力は所有者による上記の文章上のコンセプトと改訂指示であり、第三者画像・素材の使用申告はない |
| 公開ライセンス | CC BY 4.0。商標権その他の標識上の権利は非許諾 |
| 状態 | `APPROVED` |

## A-02 麻雀牌38点

配布ディレクトリは `skills/render-hand/assets/mahjong-tiles/`。対象は次の38点である。

- 萬子: `manzu-1.png`、`manzu-2.png`、`manzu-3.png`、`manzu-4.png`、`manzu-5.png`、`manzu-6.png`、`manzu-7.png`、`manzu-8.png`、`manzu-9.png`、`aka-manzu-5.png`
- 筒子: `pinzu-1.png`、`pinzu-2.png`、`pinzu-3.png`、`pinzu-4.png`、`pinzu-5.png`、`pinzu-6.png`、`pinzu-7.png`、`pinzu-8.png`、`pinzu-9.png`、`aka-pinzu-5.png`
- 索子: `souzu-1.png`、`souzu-2.png`、`souzu-3.png`、`souzu-4.png`、`souzu-5.png`、`souzu-6.png`、`souzu-7.png`、`souzu-8.png`、`souzu-9.png`、`aka-souzu-5.png`
- 字牌: `ton.png`、`nan.png`、`sha.png`、`pei.png`、`haku.png`、`hatsu.png`、`chun.png`
- 伏牌: `facedown.png`

| 項目 | 記録 |
|---|---|
| 制作日 | 2026-09-04〜2026-09-05 |
| 制作主体・手段 | 本プロジェクト向けにCodexで新規制作。決定論的なPillow生成とCodex組み込みImageGen生成原本を併用 |
| 生成正本 | `scripts/generate_mahjong_tile_assets.py` |
| 生成原本 | `assets/generated/chombo-tile-base-source.png`、`chombo-one-sou-source.png`、`chombo-pinzu-head-source.png`、`chombo-souzu-bamboo-source.png` |
| 第三者依存 | 萬子・字牌の生成時のみYuji Bokuを使用。フォントはSIL Open Font License 1.1で、runtimeには非同梱 |
| 旧牌の扱い | 一般的な牌の配置・比率を比較資料として参照。画素のコピー、トレース、切り抜き、再サンプリングは不使用という制作記録 |
| 公開ライセンス | CC BY 4.0 |
| 状態 | `APPROVED` |

## A-03 点棒2点

| 配布ファイル | SHA-256 | 制作・第三者依存 | 状態 |
|---|---|---|---|
| `assets/point-sticks/riichi-stick-100.png` | `33f2b5cf6754fac337027774020888aa109d23df3fa56f3fe5ff39685190b5f9` | 2026-09-05、Pillow基本図形のみ。第三者素材・生成AI入力なし | `APPROVED` |
| `assets/point-sticks/riichi-stick-1000.png` | `c2fd748331e56a1dd79a8113ab2f5e519188d95b7f4a2bf0d78a885dec7f35e4` | 2026-09-05、Pillow基本図形のみ。第三者素材・生成AI入力なし | `APPROVED` |

生成正本は `scripts/generate_point_stick_assets.py`。公開ライセンスはCC BY 4.0である。

## A-04 配布用音声9点

プロジェクト所有者の2026-09-04および2026-09-05の申告により、Chombo開発中にCodexで作成された音声として記録する。作業中間物27点と配布版17点は2026-09-03 16:51:14 +0900、commit `7b1d69e2e18fded1cd702af6c40954f1b09caa0b` で同時に追加された。全配布版を2026-09-05に検査し、mono、22.05kHz、16-bit PCM WAVであることを確認した。

2026-09-05にCodexの保存済みローカル実行履歴を対象ファイル名で限定検索し、生成経路を確定した。

- 人間発声8点: macOSの`/usr/bin/say`、システム音声`Kyoko`で日本語発声をAIFFへ生成し、`afconvert`でWAV化。チー、ポン、カン、ロンはrate 235、リーチとツモはrate 185の強調指定、テンパイはrate 205、ノーテンはrate 195。
- Chombo声5点: Python標準ライブラリによる波形・ノイズ・周波数変調の決定論的合成。`chombo-call.wav`と`chombo-win.wav`は雀声風波形、`chombo-riichi.wav`、`chombo-tenpai.wav`、`chombo-noten.wav`は用途別チャープ。
- 卓・通知音4点: Python標準ライブラリによる決定論的合成。`tile-discard.wav`、`tile-collision.wav`、`response-hint.wav`、`hand-topple.wav`。
- 最終整形: `afconvert`でmono、22.05kHz、16-bit PCM WAVへ変換。外部録音ファイルや第三者サンプルを入力したコマンドは該当履歴にない。

AppleのmacOS Software License Agreement「Voices; Live Captions」は、System Voicesによる制作を個人的・非商用利用に限定し、営利・非営利を問わず公開共有・公開・再配布を許可していない。このためKyoko由来の人間発声8点は2026-09-05に削除した。参照: [macOS Tahoe Software License Agreement](https://www.apple.com/legal/sla/docs/macOSTahoe.pdf)。

現行配布対象は独自合成9点だけであり、人間とAgentの行為へ同じChombo音を割り当てる。プロジェクト所有者が承認したCC BY 4.0を9点すべてに適用する。

| 配布ファイル | SHA-256 | 用途 |
|---|---|---|
| `assets/sounds/chombo-call.wav` | `97b72f8ce3c2cb6ba68c9423bcd759e4a3f16d6d98434c7e87d3dc0b08e6f870` | Chomboのチー・ポン・カン |
| `assets/sounds/chombo-noten.wav` | `aa475eb3d35bb87cb07bd37357e18562c014cfbe71bb2a29aad95a9defe279ff` | Chomboのノーテン |
| `assets/sounds/chombo-riichi.wav` | `c55c7b3c3f0bac2108de1cf7e3b92c371c2bcb8a78c12784cfea4b55a45b3dc2` | Chomboの立直 |
| `assets/sounds/chombo-tenpai.wav` | `f84cf08190b06ff4a41201699a0f9e2e5c1a27ec7a57d917a76df5b5f4ce2d25` | Chomboの聴牌 |
| `assets/sounds/chombo-win.wav` | `38299da88fa14670ceacb4e6cf595d36c7f653f434c67d3f1303fd6288a05ada` | Chomboのロン・ツモ |
| `assets/sounds/hand-topple.wav` | `2272387ce90b77595413a1b9b724994ea4252f4b448dcca9292578e69e18cf0c` | 和了成立 |
| `assets/sounds/response-hint.wav` | `3c61c06c5fca53f9dd8436349afc03970301422d5e2310556369820777ba545b` | 応答可能通知 |
| `assets/sounds/tile-collision.wav` | `999455df571eebd6d07da8622f2899cbf1b7a0661561a738b61e745624b3e8cf` | 鳴き成立時の牌音 |
| `assets/sounds/tile-discard.wav` | `9b356a3758050a0def9c0838901814c55e74b711b1e46de12172b0b6cc3c34c8` | 打牌音 |

## A-05 既存アイコン2種類

承認者はChombo project owner。承認日2026-11-11。両画像を元ロゴと同じCC BY 4.0で提供することを明示確認した。
帰属はChombo project contributors。商標その他の標識上の権利は許諾しない。

| 開発正本 | Runtime内のcopy | SHA-256 | 状態 |
|---|---|---|---|
| `docs/assets/chombo-icon.png` | `assets/chombo-icon.png` とNuxtのhash付き同一copy | `e3c07025b21665d429a189889914983484536ef6b8967177f7c450f8e2d2f79d` | `APPROVED` |
| `ui/app/assets/chombo-dialogue-icon-monochrome.png` | `ui/.output/public/_nuxt/chombo-dialogue-icon-monochrome.<hash>.png` | `24873ca68b0b17ca2b08059441535a9029cfb262200683ff976f976031932830` | `APPROVED` |

既存iconの導入はprivate開発commit `2692263b796a7e9fa7e0bd64aa4004aa9701c443`、
白黒会話iconの導入は `63e596dd91e58ab0c79453bb1e403918a051bbd9`。
会話iconのImageGen生成・元icon参照・白黒化はprivate開発文書
`docs/operations/v0.5.1-dialogue-ui-implementation.md` の「ボタン画像」に記録されている。
今回の確認では画像bytesを変更していない。hash付きbuild copyは元画像と同一digestで対応付ける。
未収録の作業用カラー画像などへ、本承認を自動で拡張しない。

## 承認記録

- 承認者: Chombo project owner
- 承認日: 2026-09-05
- 適用条件: 配布対象メディアはCC BY 4.0。Chomboの名称・ロゴに関する商標権その他の標識上の権利は許諾しない。
- 検査: 一覧記載の最終ファイルhash、Release Bundleのallowlist、展開後smoke testを確認済み。
