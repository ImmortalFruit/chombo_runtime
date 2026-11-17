# Chombo v0.5.2 Python Developer Preview

既存EngineをPythonから直接利用するための開発者向け手順。安定SDK・APIの後方互換保証は提供しない。新しい麻雀規則、戦術、オンライン対戦、RMU新決勝の計算は追加しない。

## 環境と起動

Python 3.9以降。手牌画像を描画する場合はPillowも必要。Codex desktopではworkspace dependenciesが返すPython実行ファイルを使用する。通常のEngine計算にはAPI key・外部MCP server・麻雀サービスのアカウントは不要。

展開したRuntime Bundleのrootを作業ディレクトリとする。以下を `preview.py` に保存し、`<python> preview.py` として実行する。開発repositoryの `tests/` や `docs/learning/` をPython pathに追加しない。

## 一局の計算と記録

```python
from pathlib import Path
from uuid import uuid4
from engine.rule_inputs import RuleSelection, load_selected_profile
from engine.hand_orchestrator import run_autonomous_hand
from engine.canonical_record import development_producer, hand_record_document, write_record

root = Path.cwd()
profile = load_selected_profile(
    RuleSelection(alias="m-league-official-2026-09-08"), route="hand"
)
run = run_autonomous_hand(0, profile)
print(run.log.result.end_reason.value, run.decision_count)
producer = development_producer(root)
record = hand_record_document(
    run.log, app_version=producer["app_version"],
    build_revision=producer["build_revision"], engine_version=producer["engine_version"],
    source_session_id=str(uuid4())
)
write_record(record, root / "outputs/preview-hand.json", allow_evaluation=True)
```

上例のproducerは既存v0.3 Canonical Record契約を解決する。アプリやartifactの版からrecord schema・producer版を推測して変更しない。生成物は `EVALUATION` であり、勝手に `RELEASED` へ変更しない。`write_record` は検証・atomic replace・file/directory fsyncを行う。出力先に既存ファイルがある場合は置き換わるため、保存先は呼出側で選ぶ。

## 半荘と複数対局

```python
from engine.match_orchestrator import run_autonomous_match
from engine.rule_inputs import RuleSelection, load_selected_profile

profile = load_selected_profile(
    RuleSelection(alias="m-league-official-2026-09-08"), route="match"
)
for seed in (0, 17):
    run = run_autonomous_match(seed, profile)
    print(seed, len(run.hand_logs), run.final_points)
    # 必要な保存・集約をここで行い、不要になったrunは保持し続けない。
```

一局・半荘は一度のPython呼出で完了し、一手ごとにCodexへ操作を要求しない。複数対局も同じPython内で順次実行できる。既定は全員Agent、normal方策、演出delayなし。戦術的に最善な打牌を保証するものではない。

seedを明示し、profile ID・snapshot・方策・Python版・revisionも保存する。同じseedのwarm値はcacheに既知の局面を含む。未知seedの対局が同じ速度になるとは限らない。計算cacheには上限なしのものがあり、長時間常駐・大量対局のメモリ上限は未保証。必要に応じ呼出側でworkerの寿命を区切る。

## Replayとローカル画面

```python
from pathlib import Path
from engine.canonical_record import read_record
from engine.navigator import ReplaySession

record = read_record(Path("outputs/preview-hand.json"), allow_evaluation=True)
replay = ReplaySession("preview-hand.json", record)
print(replay.metadata())
replay.seek(1)
hand = replay.current_hand()  # 呼出側が変更しても内部cacheを変更しないcopy
```

ローカル画面は共通Nuxt buildを使用する。ソースcheckoutでは `ui/` で `npm ci`、`npm run generate` を先に実行する。Bundleでは検証済み成果物を同梱するためNodeは不要。build不在・破損時は `UI_BUILD_UNAVAILABLE` で停止し、旧画面へ切り替えない。

`<python> -m engine.navigator --allow-evaluation-records` で起動し、表示されたlocalhost URLを開く。既定のAgent演出delayは1秒で、直接Engine呼出の性能値とは分ける。`ReplaySession` は既存の検証済み文書を受ける境界であり、信頼できないJSONを直接渡さず `read_record` を使う。低層APIを公開サービスの認証・開示制御の代用にしない。

## 停止・エラー

規則未対応、snapshot不一致、入力不足、決定数・局数上限超過を成功結果として扱わない。`run_autonomous_hand(..., max_decisions=1)` や `run_autonomous_match(..., max_hands=1)` は完了前に上限へ達すると構造化例外で停止する。途中結果を補修して続行したり、別規則へfallbackしたりしない。

`RuleSelection(alias="unknown-profile")` は解決を拒否する。未知版・破損牌譜・未資格の規則を推測して移行しない。対局・牌譜の権限や処理回復は既存Navigatorの契約を利用する。

性能値は実測した機械・Python・profile・seed・revisionに限る。Runtime Bundleには開発用ベンチマークや教材・評価文書を同梱しない。

牌譜の実装依存は現在のhashを照合する。同値性を検証した手牌解析実装の旧hash一組のみ、同梱の明示対応表で受理する。元牌譜は変更せず、未知hashや次版の実装へこの扱いを推測して広げない。
