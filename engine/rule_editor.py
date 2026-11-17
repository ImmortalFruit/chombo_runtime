"""Server-side model and accessible HTML shell for the v0.3 rule editor.

The editor never interprets rule prose. It exposes values already present in the
approved profile catalog and sends all writes and confirmation through DraftStore.
"""
from __future__ import annotations

from engine.knowledge_store import concept_document

import copy
import json
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from engine.rule_drafts import DraftStore, RuleProfileError, canonical, new_draft


CATEGORIES = (
    ("開始", ("match.start.", "match.structure", "tile.red_five.")),
    ("局進行", ("response.", "riichi.", "kan.", "dora.", "hand.abortive_draws")),
    ("局精算", ("win.", "scoring.", "settlement.", "yaku.", "hand.special.")),
    ("次局遷移", ("transition.",)),
    ("終了", ("match.end.",)),
    ("最終ポイント", ("match.final_points",)),
)

DIMENSION_LABELS = {
    "match.start.procedure": "対局開始手順", "match.start.points": "配給原点",
    "match.structure": "対局構成", "tile.red_five.composition": "赤ドラ設定",
    "dora.profile": "ドラの種類", "response.call.priority": "打牌応答の優先順位",
    "response.post_call_discard_restriction": "食い替え禁止", "kan.count_limit": "槓の回数制限",
    "kan.profile_boundaries": "責任払い・包", "kan.last_draw_restrictions": "海底牌・河底牌の行為制限",
    "riichi.score_eligibility": "立直の宣言資格", "riichi.post_establishment_kan": "立直後の暗槓",
    "riichi.first_turn_and_ippatsu": "一発・ダブル立直", "win.multiple_ron_resolution": "同時ロン（多家和）",
    "hand.abortive_draws": "途中流局", "hand.special.nagashi_mangan": "流し満貫",
    "yaku.ordinary_catalog_and_values": "通常役と翻数", "yaku.yakuman_catalog_and_values": "役満と倍数",
    "scoring.fu": "符計算", "scoring.engine_profile": "点数計算方式",
    "scoring.limit_and_rounding": "満貫以上・端数処理", "settlement.honba": "本場の精算",
    "settlement.riichi_deposit": "立直供託の精算", "settlement.exhaustive_draw_tenpai": "流局時の聴牌料",
    "transition.dealer": "親の継続・交代", "transition.honba": "本場数の更新",
    "transition.riichi_deposit": "立直供託の持越し", "transition.round_and_seat_winds": "局番号と場の進行",
    "match.end.scheduled": "終了条件", "match.end.bankruptcy": "飛び終了",
    "match.end.extension": "試合の延長", "match.end.final_deposit": "終局時の供託",
    "match.final_points": "最終競技ポイント",
}

HIDDEN_EDITOR_DIMENSIONS = {
    "match.start.procedure",  # Navigator owns the deterministic setup procedure.
    "kan.count_limit",        # Derived from the approved abortive-draw profile.
    "response.call.priority", # Fixed by the currently approved profiles.
    "riichi.first_turn_and_ippatsu", # Internal establishment conditions are profile-fixed.
    "scoring.engine_profile", # Derived from the approved limit-and-rounding value.
    "scoring.fu",             # Fixed by the selected approved ruleset.
    "settlement.riichi_deposit", # Fixed identically by every approved ruleset.
    "transition.riichi_deposit", # Derived from the hand-end deposit settlement.
    "transition.round_and_seat_winds", # Progression is fixed by the selected approved ruleset.
}

# Only implementation-derived dimensions may follow an exact known profile.
# Visible settings retain their current values, including unchanged initial
# values: choosing a reference or editing another setting is not permission to
# silently replace them with a different profile's defaults.
PROFILE_SYNCHRONIZED_DIMENSIONS = HIDDEN_EDITOR_DIMENSIONS

VALUE_FIELD_LABELS = {
    "points_per_player": "持ち点", "honba": "本場", "riichi_deposit_points": "供託点",
    "format": "対局形式", "prevailing_wind": "場風", "hand_number": "局数",
    "red": "赤牌", "ura": "裏ドラ", "kan": "槓ドラ", "kan_ura": "槓裏ドラ",
    "minimum_points": "必要点数", "maximum_winners": "最大和了者数", "priority": "優先順位",
    "points_per_honba": "1本場の点数", "points_per_declaration": "立直棒の点数",
    "return_points": "返し点", "raw_points_unit": "素点単位", "precision_points": "表示単位",
    "payment_rounding_unit": "支払丸め単位", "maximum_multiple_yakuman": "役満上限",
    "open_tanyao": "喰いタン", "aotenjou": "青天井", "counted_yakuman": "数え役満",
    "agari_yame": "アガリ止め", "tenpai_yame": "聴牌止め", "sudden_death": "サドンデス",
    "bankruptcy_end": "飛びを採用", "negative_score_ends_match": "持ち点が0点未満で終了",
    "zero_score_ends_match": "持ち点が0点で終了",
    "zero_or_negative_score_ends_match": "持ち点が0点以下で終了",
    "uma": "順位点", "oka": "オカ", "official_rank_points": "公式順位点",
    "total_points": "場の聴牌料", "threshold_points": "基準点", "catalog_size": "役数",
    "win": "和了時の処理", "exhaustive_draw": "流局時の処理",
    "qualified_win": "有効な和了時の処理", "resolution_mode": "供託の処理",
    "unallocated_table_balance": "未配分の供託を卓上に残す",
    "scheduled_final_hand": "規定最終局",
    "single_top": "単独首位の処理", "tied_top": "同点首位の処理",
    "three_way_remainder_priority": "3人同点時の端数優先順位",
    "tie_rule": "同点時の順位決定", "final_deposit": "終局時の供託",
    "oka_included_in_first_place_points": "オカ",
    "sub_1000_rounding": "1,000点未満の端数処理",
    "dealer_win": "規定最終局・親和了時", "non_dealer_win": "子の和了時",
    "dealer_tenpai": "規定最終局・親聴牌時", "dealer_noten": "規定最終局・親ノーテン時",
    "dealer_continues": "規定最終局・親連荘時", "dealer_changes": "規定最終局・親交代時",
    "dealer_continues_on_win": "親の和了時に連荘",
    "dealer_continues_on_exhaustive_draw_when_tenpai": "荒牌流局・親聴牌時に連荘",
    "dealer_changes_on_exhaustive_draw_when_noten": "荒牌流局・親ノーテン時に親交代",
    "exhaustive_draw_and_dealer_changes": "荒牌流局・親交代時",
    "qualification": "成立条件", "payment": "支払い方法", "method": "処理方式",
    "condition": "適用条件", "priority": "優先順位", "maximum_winners": "最大和了者数",
    "declaration_window": "宣言可能時期", "acceptance_timing": "成立時期",
    "fifth_kan_allowed": "5回目の槓", "established_kans_max": "成立できる槓の上限",
    "ron_payer": "放銃者の支払い", "dealer_pays": "親の支払い",
    "each_non_dealer_pays": "各子の支払い", "each_other_non_dealer_pays": "他の各子の支払い",
    "total": "合計", "han_each": "1枚あたりの翻数", "standard": "本ドラ",
    "activation": "有効化条件", "end_condition": "終了条件", "maximum_final_hand": "最終局",
    "adopted": "採用",
    "kan_on_last_draw": "最後の自摸で槓を認める", "riichi_on_last_draw": "最後の自摸で立直を認める",
    "calls_on_last_discard": "最後の打牌で鳴きを認める",
    "qualified_ron_on_last_discard": "最後の打牌でロンを認める",
    "same_tile": "現物の食い替えを禁止",
    "other_end_of_called_sequence": "筋食い替えを禁止",
    "prohibit_discard_completing_called_meld_with_used_group": "食い替えを禁止",
    "concealed_kan_only": "暗槓を認める",
    "drawn_tile_must_be_used": "自摸牌を使った暗槓のみ認める",
    "meld_interpretations_must_not_change": "面子構成の解釈が変わらない",
    "waits_must_be_unchanged": "待ちが変わらない",
    "yaku_treatment_must_not_change": "役の扱いが変わらない",
    "hand_composition_must_be_unchanged": "手牌構成が変わらない",
    "yaku_must_not_decrease": "役が減少しない",
    "drawn_tile_only": "自摸牌を使った暗槓のみ認める",
    "meld_type_count_must_not_decrease": "面子構成の候補数が減らない",
    "all_wait_shapes_must_be_unchanged": "すべての待ち形が変わらない",
    "yaku_change_allowed": "役の変化を認める",
}

VALUE_NAMES = {
    "east_south": "東南戦", "east": "東", "south": "南", "west": "西", "north": "北",
    "sanbaiman": "三倍満", "baiman": "倍満", "counted_yakuman": "数え役満",
    "sanbaiman_cap": "三倍満止まり", "average_occupied_rank_points": "同順位の順位点を平均",
    "closer_to_starting_dealer": "起家に近い順", "split_equally_to_0.1": "0.1単位で均等配分",
    "approved_winner_takes_all": "承認された和了者が全額取得", "carry_over": "次局へ持越し",
    "carry_entire_pool_forward": "全額を次局へ持越し", "transfer_to_approved_winner": "承認された和了者へ移す",
    "AWARD": "1位者が取得（同点時は分配）", "LEAVE_ON_TABLE": "卓上に残す",
    "FIRST_PLACE_TAKES_ALL": "1位者が全額取得（同点時も1名）", "south_4": "南4局",
    "continue_same_hand": "連荘", "end_match": "対局終了",
    "end": "対局終了", "continue": "連荘",
    "end_if_any_player_at_or_above_30000_else_advance": "返し点以上の者がいれば終了、いなければ延長",
    "end_if_dealer_is_top_and_at_or_above_30000_else_continue": "親が首位かつ返し点以上なら終了、その他は連荘",
    "transfer_entire_pool_to_top_player": "首位者へ全額移す",
    "east_1_seat_order": "起家に近い順", "first_place_takes_all": "1位者が全額取得",
    "profile_defined_top_resolution": "終局時の供託設定に従う",
    "leave_on_table": "卓上に残す", "none": "端数処理なし",
    "split_pool_among_tied_top_players": "同点首位者で分配",
    "increment_by_one": "1本場増やす", "reset_to_zero": "0本場に戻す",
    "remain_zero": "0本場のまま",
}


def _field_display_name(path: str) -> str:
    key = path.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
    if key.isdigit():
        return f"{int(key) + 1}番目"
    return VALUE_FIELD_LABELS.get(key, key.replace("_", "・"))


def _approved_value_label(value: Any, ordinal: int) -> str:
    """Give an approved JSON value a concise Japanese UI label, never a source-rule name."""
    def scalar(item: Any) -> str:
        if type(item) is bool:
            return "有効" if item else "無効"
        if item is None:
            return "設定なし"
        if type(item) in (int, float):
            return f"{item:,}"
        return VALUE_NAMES.get(str(item), str(item))

    fields = _flatten(value)
    if len(fields) == 1 and fields[0]["path"] == "/":
        return scalar(fields[0]["value"])
    parts = []
    for field in fields[:4]:
        key = field["path"].rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
        name = VALUE_FIELD_LABELS.get(key, f"設定{len(parts) + 1}")
        parts.append(f"{name}：{scalar(field['value'])}")
    suffix = f"・ほか{len(fields) - 4}項目" if len(fields) > 4 else ""
    return "／".join(parts) + suffix if parts else f"承認値{ordinal}"


def _qualified_yaku_names() -> tuple[list[dict], list[str]]:
    """Read display-only role names and han from qualified Concept sources."""
    concept_root = Path(__file__).resolve().parents[1] / "concepts"
    ordinary = []
    for filename in ("basic-yaku.json", "ordinary-yaku.json"):
        document = concept_document(concept_root, filename)
        if document.get("status") == "QUALIFIED":
            ordinary.extend({
                "name": item["display_term_ja"],
                "han": item.get("basic_han", item.get("basic_han_per_satisfied_role")),
                "open_han": item.get("open_han"),
            } for item in document.get("concepts", []) if item.get("display_term_ja"))
    ordinary = sorted(
        (item for item in ordinary if item["han"] in (1, 2, 3, 6)),
        key=lambda item: (item["han"], item["name"]),
    )
    document = concept_document(concept_root, "yakuman.json")
    yakuman = []
    if document.get("status") == "QUALIFIED":
        catalog = next(
            (item for item in document.get("concepts", [])
             if item.get("concept_id") == "game.hand.yaku.yakuman.catalog"),
            {},
        )
        yakuman = [item["name_ja"] for item in catalog.get("roles", []) if item.get("name_ja")]
    return ordinary, yakuman


def _category(dimension_id: str) -> str:
    for label, prefixes in CATEGORIES:
        if any(dimension_id.startswith(prefix) for prefix in prefixes):
            return label
    raise RuleProfileError("RULE_PROFILE_INCOMPLETE", {"dimension_id": dimension_id})


def _flatten(value: Any, pointer: str = "") -> list[dict]:
    """Split structured values into semantic JSON-pointer fields for the UI."""
    if isinstance(value, dict):
        fields = []
        for key, child in value.items():
            fields.extend(_flatten(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1")))
        return fields
    if isinstance(value, list):
        fields = []
        for index, child in enumerate(value):
            fields.extend(_flatten(child, pointer + "/" + str(index)))
        return fields
    return [{"path": pointer or "/", "value": copy.deepcopy(value), "type": type(value).__name__}]


def _browser_number_shape_matches(value: Any, exemplar: Any) -> bool:
    """Match JSON shapes while allowing browsers to collapse integral floats."""
    if isinstance(value, bool) or isinstance(exemplar, bool):
        return type(value) is type(exemplar)
    if type(value) in (int, float) and type(exemplar) in (int, float):
        return True
    if isinstance(value, dict) and isinstance(exemplar, dict):
        return set(value) == set(exemplar) and all(
            _browser_number_shape_matches(value[key], exemplar[key]) for key in value
        )
    if isinstance(value, list) and isinstance(exemplar, list):
        return len(value) == len(exemplar) and all(
            _browser_number_shape_matches(child, expected)
            for child, expected in zip(value, exemplar)
        )
    return type(value) is type(exemplar)


def _restore_browser_number_types(value: Any, exemplar: Any) -> Any:
    if isinstance(value, dict):
        return {key: _restore_browser_number_types(value[key], exemplar[key]) for key in value}
    if isinstance(value, list):
        return [_restore_browser_number_types(child, expected)
                for child, expected in zip(value, exemplar)]
    if type(exemplar) is float and type(value) is int:
        return float(value)
    return copy.deepcopy(value)


def _changed_dimensions(old: dict, new: dict) -> list[str]:
    keys = set(old.get("overrides", ())) | set(new.get("overrides", ()))
    return sorted(key for key in keys if old.get("overrides", {}).get(key) != new.get("overrides", {}).get(key))


@dataclass
class RuleEditorSession:
    store: DraftStore
    draft: dict
    persisted_revision: int
    actor_id: str
    dirty: bool = False
    validation: dict | None = None

    @classmethod
    def create(cls, store: DraftStore, base_ref: dict, display_name: str, *, actor_id: str):
        return cls(store, new_draft(base_ref, display_name), 0, actor_id, True)

    @classmethod
    def open(cls, store: DraftStore, draft_id: str, *, actor_id: str):
        draft, validation = store.load(draft_id)
        return cls(store, draft, draft["revision"], actor_id, False, validation)

    def _touch(self):
        self.dirty = True
        self.validation = None

    def rename(self, display_name: str):
        if not isinstance(display_name, str) or not display_name.strip():
            raise RuleProfileError("MALFORMED_DRAFT", {"reason": "EMPTY_TEXT"})
        self.draft["display_name"] = display_name.strip()
        self._touch()

    def set_override(self, dimension_id: str, value: Any, *, request_summary: str):
        _, _, required, _, profiles, _ = self.store.validator.catalogs()
        if dimension_id not in required or not isinstance(request_summary, str) or not request_summary.strip():
            raise RuleProfileError("MALFORMED_DRAFT", {"reason": "EDITOR_INPUT"})
        if dimension_id == "settlement.honba":
            points = value.get("points_per_honba") if isinstance(value, dict) else None
            if isinstance(points, bool) or not isinstance(points, int) or points < 0 or points % 300:
                raise RuleProfileError(
                    "MALFORMED_DRAFT",
                    {"reason": "HONBA_POINTS_MUST_BE_NON_NEGATIVE_MULTIPLE_OF_300"},
                )
            value = {
                "points_per_honba": points,
                "ron_payer": "discarder" if points else "none",
                "tsumo_points_per_non_winner": points // 3,
            }
        if dimension_id == "settlement.exhaustive_draw_tenpai":
            total = value.get("total_points") if isinstance(value, dict) else None
            if (isinstance(total, bool) or not isinstance(total, int) or total < 0
                    or (total != 0 and total % 3000)):
                raise RuleProfileError(
                    "MALFORMED_DRAFT",
                    {"reason": "TENPAI_TOTAL_MUST_BE_ZERO_OR_MULTIPLE_OF_3000"},
                )
            approved = next((profile["dimensions"][dimension_id]["value"] for _, profile in profiles
                if _browser_number_shape_matches(value, profile["dimensions"][dimension_id]["value"])
                and value == profile["dimensions"][dimension_id]["value"]), None)
            # A complete approved value must round-trip unchanged. Inserting an
            # absent optional field would change its approved combination hash.
            value = copy.deepcopy(approved) if approved is not None else {
                "total_points": total, "formal_tenpai": True,
                "zero_or_four_tenpai_transfer": 0,
            }
        if dimension_id == "transition.dealer":
            tenpai_continues = (
                value.get("dealer_continues_on_exhaustive_draw_when_tenpai")
                if isinstance(value, dict) else None
            )
            if type(tenpai_continues) is not bool:
                raise RuleProfileError("MALFORMED_DRAFT", {"reason": "DEALER_TENPAI_RULE_REQUIRED"})
            value = {
                "dealer_continues_on_win": True,
                "dealer_continues_on_exhaustive_draw_when_tenpai": tenpai_continues,
                "dealer_changes_on_exhaustive_draw_when_noten": True,
            }
        if (dimension_id == "match.final_points" and isinstance(value, dict)
                and "oka_included_in_first_place_points" in value):
            start_override = self.draft["overrides"].get("match.start.points", {}).get("value")
            if not isinstance(start_override, dict):
                parent, _ = self.store.validator.resolve(
                    self.draft["base_ref"], self.store.lookup, profiles
                )
                start_override = parent["match.start.points"]["value"]
            starting = start_override.get("points_per_player")
            returning = value.get("return_points")
            unit = value.get("raw_points_unit")
            if not all(type(item) is int and item > 0 for item in (starting, returning, unit)):
                raise RuleProfileError("MALFORMED_DRAFT", {"reason": "OKA_INPUT_REQUIRED"})
            oka_points = (returning - starting) * 4 / unit
            value = copy.deepcopy(value)
            value["oka_included_in_first_place_points"] = (
                int(oka_points) if oka_points.is_integer() else oka_points
            )
        exemplars = [profile["dimensions"][dimension_id]["value"] for _, profile in profiles]
        exemplar = next((item for item in exemplars if _browser_number_shape_matches(value, item)), None)
        if exemplar is not None:
            value = _restore_browser_number_types(value, exemplar)
        provenance_id = "ui-" + str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self.draft["provenance"][provenance_id] = {
            "kind": "USER_REQUEST", "actor_id": self.actor_id,
            "recorded_at": now, "request_summary": request_summary.strip(),
        }
        self.draft["overrides"][dimension_id] = {
            "value": copy.deepcopy(value), "provenance_id": provenance_id,
        }
        if dimension_id == "scoring.limit_and_rounding":
            matching_profile = next(
                (profile for _, profile in profiles
                 if canonical(profile["dimensions"][dimension_id]["value"]) == canonical(value)),
                None,
            )
            if matching_profile is not None:
                self.draft["overrides"]["scoring.engine_profile"] = {
                    "value": copy.deepcopy(
                        matching_profile["dimensions"]["scoring.engine_profile"]["value"]
                    ),
                    "provenance_id": provenance_id,
                }
        self.draft["updated_at"] = now
        self._touch()

    def unset_override(self, dimension_id: str):
        if dimension_id not in self.store.validator.catalogs()[2]:
            raise RuleProfileError("MALFORMED_DRAFT", {"reason": "EDITOR_INPUT"})
        self.draft["overrides"].pop(dimension_id, None)
        if dimension_id == "scoring.limit_and_rounding":
            self.draft["overrides"].pop("scoring.engine_profile", None)
        self.draft["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._touch()

    def synchronize_fixed_dimensions(self, *, request_summary: str) -> str | None:
        """Align derived axes without overwriting explicitly edited rule settings."""
        _, _, required, _, profiles, _ = self.store.validator.catalogs()
        parent, _ = self.store.validator.resolve(
            self.draft["base_ref"], self.store.lookup, profiles
        )
        values = {key: copy.deepcopy(item["value"]) for key, item in parent.items()}
        values.update({
            key: copy.deepcopy(item["value"])
            for key, item in self.draft["overrides"].items()
        })
        editable = required - PROFILE_SYNCHRONIZED_DIMENSIONS
        matches = [
            profile for _, profile in profiles
            if all(
                canonical(values[key]) == canonical(profile["dimensions"][key]["value"])
                for key in editable
            )
        ]
        if len(matches) != 1:
            return None
        target = matches[0]
        for dimension_id in sorted(PROFILE_SYNCHRONIZED_DIMENSIONS):
            target_value = target["dimensions"][dimension_id]["value"]
            if canonical(values[dimension_id]) != canonical(target_value):
                self.set_override(
                    dimension_id,
                    target_value,
                    request_summary=request_summary,
                )
        return target["profile_id"]

    def switch_base(self, base_ref: dict):
        # Reference validity and existence are deliberately decided by validation.
        self.draft["base_ref"] = copy.deepcopy(base_ref)
        self.draft["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._touch()

    def validate(self) -> dict:
        candidate = copy.deepcopy(self.draft)
        candidate["revision"] = self.persisted_revision + 1
        self.validation = self.store.validator.validate(candidate, self.store.lookup)
        return copy.deepcopy(self.validation)

    def save(self) -> dict:
        candidate = copy.deepcopy(self.draft)
        candidate["revision"] = self.persisted_revision + 1
        try:
            saved, result = self.store.save(candidate, expected_revision=self.persisted_revision)
        except RuleProfileError as error:
            if error.code != "REVISION_CONFLICT":
                raise
            latest, latest_validation = self.store.load(self.draft["draft_id"])
            return {"status": "CONFLICT", "code": error.code, "local_draft": copy.deepcopy(candidate),
                    "latest_draft": latest, "latest_validation": latest_validation,
                    "changed_dimension_ids": _changed_dimensions(self.draft, latest)}
        self.draft, self.persisted_revision = saved, saved["revision"]
        self.validation, self.dirty = result, False
        return {"status": "SAVED", "draft": copy.deepcopy(saved), "validation": copy.deepcopy(result)}

    def confirm(self) -> dict:
        if self.dirty:
            raise RuleProfileError("VALIDATION_STALE", {"reason": "UNSAVED_CHANGES"})
        # Revalidate now; DraftStore verifies this exact result again under its lock.
        self.validation = self.store.validator.validate(self.draft, self.store.lookup)
        return self.store.confirm(
            draft_id=self.draft["draft_id"], revision=self.draft["revision"],
            input_digest=self.validation["input_digest"],
            validation_ref=self.store.validator.validation_ref(self.validation),
        )

    def leave(self, choice: str) -> dict:
        if not self.dirty:
            return {"status": "LEAVE"}
        if choice == "CONTINUE_EDITING":
            return {"status": "STAY"}
        if choice == "DISCARD_AND_LEAVE":
            if self.persisted_revision:
                self.draft, self.validation = self.store.load(self.draft["draft_id"])
            self.dirty = False
            return {"status": "LEAVE"}
        if choice == "SAVE_AND_LEAVE":
            result = self.save()
            return {"status": "LEAVE", **result} if result["status"] == "SAVED" else result
        raise RuleProfileError("MALFORMED_DRAFT", {"reason": "LEAVE_CHOICE"})

    def view_model(self) -> dict:
        result = self.store.validator.validate(self.draft, self.store.lookup)
        resolved = result.get("resolved_preview") or {"dimensions": {}}
        _, _, required, _, profiles, _ = self.store.validator.catalogs()
        registry = json.loads(
            (Path(__file__).with_name("profiles") / "registry.v1.json").read_text(encoding="utf-8")
        )
        display_names = {
            item["profile_id"]: item.get("aliases", [item["profile_id"]])[-1]
            for item in registry["profiles"]
        }
        try:
            parent, _ = self.store.validator.resolve(
                self.draft["base_ref"], self.store.lookup, profiles
            )
        except RuleProfileError:
            parent = {}
        dimensions = []
        for dimension_id in sorted(required):
            item = resolved["dimensions"].get(dimension_id, {})
            options = []
            seen = set()
            for _, profile in profiles:
                value = profile["dimensions"][dimension_id]["value"]
                key = canonical(value)
                source_name = display_names.get(profile["profile_id"], profile["profile_id"])
                if key in seen:
                    next(item for item in options if canonical(item["value"]) == key)["source_names"].append(source_name)
                else:
                    seen.add(key)
                    options.append({"value": copy.deepcopy(value), "fields": _flatten(value),
                                    "source_names": [source_name]})
            for index, option in enumerate(options, 1):
                option["label"] = _approved_value_label(option["value"], index)
            fields = _flatten(item.get("value"))
            option_fields = [_flatten(option["value"]) for option in options]
            for field in fields:
                approved = []
                for flattened in option_fields:
                    match = next((candidate for candidate in flattened if candidate["path"] == field["path"]), None)
                    if match is not None and canonical(match["value"]) not in {canonical(value) for value in approved}:
                        approved.append(copy.deepcopy(match["value"]))
                field["display_name"] = _field_display_name(field["path"])
                field["approved_values"] = approved
            dimensions.append({
                "dimension_id": dimension_id, "category": _category(dimension_id),
                "display_name": DIMENSION_LABELS[dimension_id],
                "mode": "OVERRIDE" if dimension_id in self.draft["overrides"] else "INHERIT",
                "parent_value": copy.deepcopy(parent.get(dimension_id, {}).get("value")),
                "effective_value": copy.deepcopy(item.get("value")), "fields": fields,
                "options": options,
                "issues": [issue for issue in result["issues"] if dimension_id in issue["dimension_ids"]],
            })
        return {"draft_id": self.draft["draft_id"], "custom_ruleset_id": self.draft["custom_ruleset_id"],
                "display_name": self.draft["display_name"],
                "base_ref": copy.deepcopy(self.draft["base_ref"]), "revision": self.draft["revision"],
                "persisted_revision": self.persisted_revision, "dirty": self.dirty,
                "status": result["status"], "execution_support": result["execution_support"],
                "issues": result["issues"], "categories": [label for label, _ in CATEGORIES],
                "dimensions": dimensions}


def rule_management_model(store: DraftStore) -> dict:
    """List official, Draft, and confirmed rules with honest route availability."""
    approval, _, _, _, profiles, _ = store.validator.catalogs()
    registry = json.loads(
        (Path(__file__).with_name("profiles") / "registry.v1.json").read_text(encoding="utf-8")
    )
    display_names = {
        item["profile_id"]: item.get("aliases", [item["profile_id"]])[-1]
        for item in registry["profiles"]
    }
    aliases = {item["profile_id"]: item["aliases"][0] for item in registry["profiles"]}
    official = []
    for entry, profile in profiles:
        check = store.validator.validate(new_draft(entry["ref"], "availability"), store.lookup)
        official.append({"kind": "OFFICIAL", "name": display_names.get(profile["profile_id"], profile["profile_id"]),
                         "version": profile["ruleset_version"], "ref": copy.deepcopy(entry["ref"]),
                         "selector_alias": aliases[profile["profile_id"]],
                         "points_per_player": profile["dimensions"]["match.start.points"]["value"]["points_per_player"],
                         "state": "CONFIRMED", "read_only": True,
                         "execution_support": check["execution_support"]})
    drafts = []
    for draft in store.list_drafts():
        check = store.validator.validate(draft, store.lookup)
        drafts.append({"kind": "CUSTOM", "name": draft["display_name"], "version": draft["revision"],
                       "draft_id": draft["draft_id"], "custom_ruleset_id": draft["custom_ruleset_id"],
                       "state": "EDITING", "read_only": False,
                       "execution_support": check["execution_support"]})
    confirmed = []
    for document in store.list_confirmed():
        confirmed.append({"kind": "CUSTOM", "name": document["profile_id"],
                          "version": document["version"],
                          "points_per_player": document["dimensions"]["match.start.points"]["value"]["points_per_player"],
                          "custom_ruleset_id": document["custom_ruleset_id"], "ref": {
                              "kind": "CUSTOM_CONFIRMED", "ruleset_id": document["custom_ruleset_id"],
                              "ruleset_version": document["version"], "profile_id": document["profile_id"],
                              "snapshot_id": document["snapshot_id"]}, "state": "CONFIRMED",
                          "read_only": True})
    return {"official": official, "drafts": drafts, "confirmed": confirmed}
