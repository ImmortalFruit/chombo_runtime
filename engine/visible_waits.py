"""Replay-only wait summaries computed from qualified hand and win engines."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import json
from typing import Any

from engine.game_state import SEATS
from engine.hand_completion import analyze_hand, standard_wait_tiles_with_fixed_melds
from engine.winning import analyze_win


def _wait_judgments(
    hand_before: str,
    concealed_notation: str,
    fixed_tiles: str,
    fixed_meld_count: int,
    context_json: str,
    rule_profile_id: str,
    rule_snapshot_id: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    context = json.loads(context_json)
    if fixed_meld_count:
        waits = standard_wait_tiles_with_fixed_melds(
            concealed_notation, fixed_meld_count, fixed_tiles
        )
    else:
        waits = tuple(
            item["tile"] for item in analyze_hand(hand_before).get("waits", [])
        )
    judgments = []
    for tile_type in waits:
        result = analyze_win(hand_before, tile_type, "ron", context=context)
        reasons = []
        if result["furiten_status"]["active"]:
            reasons.append("furiten")
        if result["failed_stage"] == "yaku_requirement":
            reasons.append("no_yaku")
        judgments.append((tile_type, tuple(reasons)))
    return tuple(judgments)


def _notation(tiles: Sequence[Mapping[str, Any]]) -> str:
    grouped = {suit: [] for suit in "mpsz"}
    for tile in tiles:
        tile_type = str(tile.get("tile_type", ""))
        if len(tile_type) != 2 or tile_type[1] not in grouped:
            continue
        rank = "0" if tile.get("is_red") else tile_type[0]
        grouped[tile_type[1]].append(rank)
    return "".join(
        "".join(sorted(grouped[suit])) + suit
        for suit in "mpsz"
        if grouped[suit]
    )


def _player(authoritative_state: Mapping[str, Any], seat: str) -> Mapping[str, Any] | None:
    players = authoritative_state.get("players", [])
    if not isinstance(players, Sequence) or isinstance(players, (str, bytes)):
        return None
    return next(
        (item for item in players if isinstance(item, Mapping) and item.get("seat") == seat),
        None,
    )


def _visible_counts(public_state: Mapping[str, Any], player: Mapping[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for tile in player.get("concealed_hand", []):
        if isinstance(tile, Mapping):
            counts[str(tile.get("tile_type", ""))] += 1
    seats = public_state.get("seats", {})
    if isinstance(seats, Mapping):
        for details in seats.values():
            if not isinstance(details, Mapping):
                continue
            for tile in details.get("river", []):
                if isinstance(tile, Mapping):
                    counts[str(tile.get("tile_type", ""))] += 1
            for meld in details.get("melds", []):
                if not isinstance(meld, Mapping):
                    continue
                for tile in meld.get("tiles", []):
                    if isinstance(tile, Mapping):
                        counts[str(tile.get("tile_type", ""))] += 1
    for tile in public_state.get("dora_indicators", []):
        if isinstance(tile, Mapping):
            counts[str(tile.get("tile_type", ""))] += 1
    counts.pop("", None)
    return counts


def all_open_remaining_counts(
    public_state: Mapping[str, Any], authoritative_state: Mapping[str, Any]
) -> dict[str, int]:
    """Return remaining copies after every concealed hand is treated as visible."""
    counts: Counter[str] = Counter()
    players = authoritative_state.get("players", [])
    if isinstance(players, Sequence) and not isinstance(players, (str, bytes)):
        for player in players:
            if not isinstance(player, Mapping):
                continue
            for tile in player.get("concealed_hand", []):
                if isinstance(tile, Mapping):
                    counts[str(tile.get("tile_type", ""))] += 1
    seats = public_state.get("seats", {})
    if isinstance(seats, Mapping):
        for details in seats.values():
            if not isinstance(details, Mapping):
                continue
            for tile in details.get("river", []):
                if isinstance(tile, Mapping):
                    counts[str(tile.get("tile_type", ""))] += 1
            for meld in details.get("melds", []):
                if isinstance(meld, Mapping):
                    for tile in meld.get("tiles", []):
                        if isinstance(tile, Mapping):
                            counts[str(tile.get("tile_type", ""))] += 1
    for tile in public_state.get("dora_indicators", []):
        if isinstance(tile, Mapping):
            counts[str(tile.get("tile_type", ""))] += 1
    return {tile_type: max(0, 4 - count) for tile_type, count in counts.items()}


def calculate_visible_waits(
    public_state: Mapping[str, Any],
    authoritative_state: Mapping[str, Any],
    seat: str,
    *,
    rule_profile_id: str,
    rule_snapshot_id: str,
) -> list[dict[str, Any]]:
    """Return ron-oriented waits and normally visible remaining-copy counts."""
    player = _player(authoritative_state, seat)
    if player is None:
        return []
    concealed = [tile for tile in player.get("concealed_hand", []) if isinstance(tile, Mapping)]
    melds = [meld for meld in player.get("melds", []) if isinstance(meld, Mapping)]
    equivalent = [*concealed]
    for meld in melds:
        equivalent.extend(
            tile for tile in meld.get("tiles", [])[:3] if isinstance(tile, Mapping)
        )
    if len(equivalent) != 13:
        return []

    dealer = str(public_state.get("dealer", "east"))
    round_wind = str(public_state.get("round_wind", "east"))
    seat_names = [item.value for item in SEATS]
    if seat not in seat_names or dealer not in seat_names or round_wind not in seat_names:
        return []
    seat_wind = f"{(seat_names.index(seat) - seat_names.index(dealer)) % 4 + 1}z"
    fixed_melds = [
        {
            "tiles": [str(tile.get("tile_type", "")) for tile in meld.get("tiles", [])],
            "open": meld.get("kind") != "concealed_kan",
            "included_in_hand": True,
        }
        for meld in melds
    ]
    furiten = str(player.get("furiten", "none"))
    context = {
        "player": seat,
        "concealed": all(meld.get("kind") == "concealed_kan" for meld in melds),
        "prevailing_wind": f"{seat_names.index(round_wind) + 1}z",
        "seat_wind": seat_wind,
        "winning_event": "ordinary",
        # A replay frame immediately after the declaration discard still uses
        # `declared`; unless the discard is won/called away, it becomes riichi
        # before the next draw.  Show that accepted wait as yaku-bearing now.
        "riichi_established": player.get("riichi") in {"declared", "established"},
        "double_riichi_established": False,
        "ippatsu_right_active": bool(player.get("ippatsu_active")),
        "quad_count": sum("kan" in str(meld.get("kind", "")) for meld in melds),
        "concealed_triplet_or_quad_count": sum(
            meld.get("kind") == "concealed_kan" for meld in melds
        ),
        "fixed_melds": fixed_melds,
        "own_discards": [
            str(tile.get("tile_type", ""))
            for tile in player.get("river", [])
            if isinstance(tile, Mapping)
        ],
        "same_turn_missed_win": furiten == "temporary",
        "riichi_missed_win": furiten == "riichi",
        "declared": True,
    }
    hand_before = _notation(equivalent)
    concealed_notation = _notation(concealed)
    fixed_tiles = _notation([
        tile
        for meld in melds
        for tile in meld.get("tiles", [])
        if isinstance(tile, Mapping)
    ])
    judgments = _wait_judgments(
        hand_before,
        concealed_notation,
        fixed_tiles,
        len(melds),
        json.dumps(context, sort_keys=True, separators=(",", ":")),
        rule_profile_id,
        rule_snapshot_id,
    )
    visible = _visible_counts(public_state, player)
    summaries = []
    for tile_type, base_reasons in judgments:
        remaining = max(0, 4 - visible[tile_type])
        reasons = list(base_reasons)
        if remaining == 0:
            reasons.append("exhausted")
        summaries.append(
            {
                "tile_type": tile_type,
                "remaining_count": remaining,
                "dimmed": bool(reasons),
                "reasons": reasons,
            }
        )
    return summaries


def visible_waits_for_seat(
    public_state: Mapping[str, Any],
    authoritative_state: Mapping[str, Any],
    seat: str,
    *,
    observer_seat: str | None = None,
    rule_profile_id: str | None = None,
    rule_snapshot_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return waits through the shared observer- and ruleset-scoped boundary."""
    from engine.local_calculations import observer_visible_waits

    profile_id = rule_profile_id or str(authoritative_state.get("rule_profile_id", ""))
    snapshot_id = rule_snapshot_id or str(authoritative_state.get("rule_snapshot_id", ""))
    return observer_visible_waits(
        public_state, authoritative_state, seat,
        observer_seat=observer_seat or seat,
        rule_profile_id=profile_id,
        rule_snapshot_id=snapshot_id,
    )
