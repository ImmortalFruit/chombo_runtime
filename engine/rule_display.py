"""Immutable three-format projection for confirmed rule snapshots (U3-03).

Presentation never resolves a value again and never calls a scoring boundary.  A
single validated snapshot is copied first, then projected as summary, all axes,
or a comparison with its immediate base.
"""

from __future__ import annotations

import copy
import html
import json

from engine.rule_drafts import DraftStore, canonical, confirmed_ref, new_draft, validate_ref
from engine.rule_editor import CATEGORIES
from engine.rule_profiles import RuleProfileError


DISPLAY_FORMATS = ("概要", "全項目", "基準との差分")

DIMENSION_LABELS = {
    "match.start.procedure": "対局開始手順",
    "match.start.points": "開始点",
    "match.structure": "対局構成",
    "tile.red_five.composition": "赤牌構成",
    "dora.profile": "ドラ",
    "response.call.priority": "鳴き・和了の優先順",
    "response.post_call_discard_restriction": "喰い替え制限",
    "kan.count_limit": "槓の回数上限",
    "kan.profile_boundaries": "槓の成立条件",
    "kan.last_draw_restrictions": "最終巡の槓制限",
    "riichi.score_eligibility": "立直の持ち点条件",
    "riichi.post_establishment_kan": "立直後の槓",
    "riichi.first_turn_and_ippatsu": "一巡目立直・一発",
    "win.multiple_ron_resolution": "複数ロン",
    "hand.abortive_draws": "途中流局",
    "hand.special.nagashi_mangan": "流し満貫",
    "yaku.ordinary_catalog_and_values": "通常役と翻数",
    "yaku.yakuman_catalog_and_values": "役満",
    "scoring.fu": "符計算",
    "scoring.engine_profile": "採点方式",
    "scoring.limit_and_rounding": "上限・点数丸め",
    "settlement.honba": "本場支払",
    "settlement.riichi_deposit": "供託支払",
    "settlement.exhaustive_draw_tenpai": "流局時聴牌料",
    "transition.dealer": "親の継続",
    "transition.honba": "本場の遷移",
    "transition.riichi_deposit": "供託の遷移",
    "transition.round_and_seat_winds": "局・場風・自風の遷移",
    "match.end.scheduled": "予定終局",
    "match.end.bankruptcy": "飛び終了",
    "match.end.extension": "延長戦",
    "match.end.final_deposit": "終局時供託",
    "match.final_points": "最終ポイント",
}

SUMMARY_GROUPS = (
    ("開始", ("match.start.points",)),
    ("赤牌", ("tile.red_five.composition",)),
    ("主な採点", ("scoring.engine_profile", "scoring.limit_and_rounding")),
    ("終了条件", ("match.end.scheduled", "match.end.bankruptcy", "match.end.extension")),
    ("最終ポイント", ("match.end.final_deposit", "match.final_points")),
)

FINAL_POINT_FIELDS = (
    ("返し点", ("return_points",)),
    ("素点の換算単位", ("raw_points_unit",)),
    ("順位点", ("official_rank_points", "rank_points", "rank_points_by_positive_player_count")),
    ("オカ・ウマへの組込み", ("oka", "uma", "oka_included_in_first_place_points")),
    ("同点配分", ("tie_rule", "tied_first_oka")),
    ("残供託", ("final_deposit",)),
    ("精度・端数処理", ("precision_points", "sub_1000_rounding", "remainder_priority")),
)


def _category(dimension_id: str) -> str:
    for label, prefixes in CATEGORIES:
        if any(dimension_id.startswith(prefix) for prefix in prefixes):
            return label
    raise RuleProfileError("RULE_PROFILE_INCOMPLETE", {"dimension_id": dimension_id})


def _official_name(entry: dict, profile: dict) -> str:
    aliases = entry.get("aliases", ())
    return next((alias for alias in reversed(aliases) if any(ord(char) > 127 for char in alias)),
                profile["profile_id"])


def confirmed_rule_snapshot(store: DraftStore, ref: dict) -> dict:
    """Validate and copy one fixed official or custom-confirmed display source."""
    validate_ref(ref)
    approval, _, required, _, profiles, _ = store.validator.catalogs()
    del approval
    dimensions, lineage = store.validator.resolve(
        ref, store.lookup, profiles, confirmed_root=ref["kind"] == "CUSTOM_CONFIRMED"
    )
    if set(dimensions) != required:
        raise RuleProfileError("RULE_PROFILE_INCOMPLETE", {})

    if ref["kind"] == "OFFICIAL":
        match = next(((entry, profile) for entry, profile in profiles if entry["ref"] == ref), None)
        if match is None:
            raise RuleProfileError("REFERENCE_MISSING", {"ref": ref})
        entry, profile = match
        availability = store.validator.validate(new_draft(ref, "display availability"), store.lookup)
        support = availability["execution_support"].get("display", {})
        if availability["status"] != "VALIDATED" or support.get("status") != "SUPPORTED":
            raise RuleProfileError("UNSUPPORTED_RULE_VALUE", {"route": "display"})
        name = _official_name(entry, profile)
        version = profile["ruleset_version"]
        base_ref = copy.deepcopy(ref)
        parent_dimensions = copy.deepcopy(dimensions)
        current_origin = None
    else:
        # execution_profile performs the route capability check and a second
        # validation of this exact immutable reference.
        store.execution_profile(ref, route="display")
        document = store.lookup(ref)
        if document is None or confirmed_ref(document) != ref:
            raise RuleProfileError("REFERENCE_MISSING", {"ref": ref})
        source_draft, source_validation = store.load(
            document["source_draft"]["draft_id"], document["source_draft"]["revision"]
        )
        if source_validation["input_digest"] != document["source_draft"]["input_digest"]:
            raise RuleProfileError("SNAPSHOT_MISMATCH", {"reason": "SOURCE_DRAFT_MISMATCH"})
        name = source_draft["display_name"]
        version = document["version"]
        base_ref = copy.deepcopy(document["base_ref"])
        parent_dimensions, _ = store.validator.resolve(base_ref, store.lookup, profiles)
        current_origin = copy.deepcopy(document["source_draft"])
        current_origin.pop("input_digest")

    axes = []
    for dimension_id in sorted(required):
        item = dimensions[dimension_id]
        parent = parent_dimensions[dimension_id]
        explicit = current_origin is not None and item["origin_ref"] == current_origin
        equal = canonical(item["value"]) == canonical(parent["value"])
        axes.append({
            "dimension_id": dimension_id,
            "label": DIMENSION_LABELS[dimension_id],
            "category": _category(dimension_id),
            "value": copy.deepcopy(item["value"]),
            "parent_value": copy.deepcopy(parent["value"]),
            "mode": "OVERRIDE" if explicit else ("OFFICIAL" if ref["kind"] == "OFFICIAL" else "INHERIT"),
            "difference": "EXPLICIT_SAME_VALUE_OVERRIDE" if explicit and equal else
                          "VALUE_CHANGED" if not equal else "UNCHANGED",
            "origin_ref": copy.deepcopy(item["origin_ref"]),
            "concept_ids": copy.deepcopy(item["concept_ids"]),
        })

    return {
        "name": name,
        "version": version,
        "ref": copy.deepcopy(ref),
        "snapshot_id": ref["snapshot_id"],
        "base_ref": base_ref,
        "lineage": copy.deepcopy(lineage),
        "availability": "DISPLAY_SUPPORTED",
        "axes": axes,
    }


def _final_points(axes_by_id: dict[str, dict]) -> dict:
    value = axes_by_id["match.final_points"]["value"]
    rows = []
    for label, keys in FINAL_POINT_FIELDS:
        present = {key: copy.deepcopy(value[key]) for key in keys if key in value}
        if label == "残供託":
            present["match.end.final_deposit"] = copy.deepcopy(
                axes_by_id["match.end.final_deposit"]["value"]
            )
        rows.append({"label": label, "status": "AVAILABLE" if present else "DISPLAY_UNSUPPORTED",
                     "values": present})
    return {
        "rows": rows,
        "final_points_value": copy.deepcopy(value),
        "final_deposit_value": copy.deepcopy(axes_by_id["match.end.final_deposit"]["value"]),
    }


def rule_display_model(snapshot: dict, display_format: str) -> dict:
    """Project one already-copied snapshot without resolution or calculation."""
    if display_format not in DISPLAY_FORMATS:
        raise RuleProfileError("UNSUPPORTED_DISPLAY_FORMAT", {"display_format": display_format})
    axes = copy.deepcopy(snapshot["axes"])
    axes_by_id = {axis["dimension_id"]: axis for axis in axes}
    model = {key: copy.deepcopy(snapshot[key]) for key in
             ("name", "version", "ref", "snapshot_id", "base_ref", "availability")}
    model["display_format"] = display_format
    model["display_formats"] = list(DISPLAY_FORMATS)
    model["final_points"] = _final_points(axes_by_id)
    if display_format == "概要":
        model["cards"] = [
            {"title": title, "axes": [copy.deepcopy(axes_by_id[key]) for key in ids]}
            for title, ids in SUMMARY_GROUPS
        ]
        model["all_items_link"] = "全項目"
    elif display_format == "全項目":
        model["categories"] = [
            {"name": category, "axes": [axis for axis in axes if axis["category"] == category]}
            for category, _ in CATEGORIES
        ]
    else:
        differences = [axis for axis in axes if axis["difference"] != "UNCHANGED"]
        model["difference_count"] = len(differences)
        model["differences"] = differences
        model["zero_difference"] = not differences
    return model


def render_rule_display(model: dict) -> str:
    """Render the selected projection as an accessible in-page rules view."""
    esc = lambda value: html.escape(str(value), quote=True)
    value = lambda item: esc(json.dumps(item, ensure_ascii=False, sort_keys=True))
    options = "".join(
        f'<option value="{esc(name)}"{" selected" if name == model["display_format"] else ""}>{esc(name)}</option>'
        for name in model["display_formats"]
    )
    if model["display_format"] == "概要":
        body = "".join(
            f'<section><h2>{esc(card["title"])}</h2>' + "".join(
                f'<h3>{esc(axis["label"])}</h3><pre>{value(axis["value"])}</pre>' for axis in card["axes"]
            ) + "</section>" for card in model["cards"]
        ) + '<button type="button" data-display-format="全項目">全項目を見る</button>'
    elif model["display_format"] == "全項目":
        rows = "".join(
            f'<tr><th scope="row">{esc(axis["label"])}</th><td>{value(axis["value"])}</td>'
            f'<td>{esc(axis["mode"])}</td><td>{value(axis["origin_ref"])}</td></tr>'
            for category in model["categories"] for axis in category["axes"]
        )
        body = f'<table><caption>全33項目</caption><thead><tr><th>項目</th><th>採用値</th><th>継承／上書き</th><th>出所</th></tr></thead><tbody>{rows}</tbody></table>'
    else:
        if model["zero_difference"]:
            body = '<p class="zero-difference">変更軸は0件です。</p>'
        else:
            body = "".join(
                f'<section><h2>{esc(axis["label"])}</h2><p>{esc(axis["difference"])}</p>'
                f'<div class="comparison"><div><h3>基準版</h3><pre>{value(axis["parent_value"])}</pre></div>'
                f'<div><h3>確定版</h3><pre>{value(axis["value"])}</pre></div></div></section>'
                for axis in model["differences"]
            )
    final_rows = "".join(
        f'<tr><th scope="row">{esc(row["label"])}</th><td>{esc(row["status"])}</td><td>{value(row["values"])}</td></tr>'
        for row in model["final_points"]["rows"]
    )
    return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>ルール — {esc(model['name'])}</title><style>body{{font-family:system-ui,sans-serif;margin:auto;max-width:72rem;padding:1rem}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #809087;padding:.5rem;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.comparison{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}@media(max-width:760px){{.comparison{{display:block}}table{{display:block;overflow-x:auto}}}}</style></head>
<body><a href="#content" class="skip">内容へ</a><header><a href="#navigator">Navigatorへ</a><h1>{esc(model['name'])}</h1><p>版 {esc(model['version'])} · {esc(model['availability'])}</p><details><summary>詳細</summary><code>{esc(model['snapshot_id'])}</code></details><label>表示形式<select name="display-format">{options}</select></label></header>
<main id="content">{body}<section><h2>最終ポイント</h2><table><thead><tr><th>項目</th><th>表示状態</th><th>意味値</th></tr></thead><tbody>{final_rows}</tbody></table></section></main></body></html>'''
