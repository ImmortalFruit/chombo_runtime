"""Delta encoding helpers for compact v0.1 hand records."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


DELETE = {"$delete": True}
LIST_BY_KEY = "$list_by_key"


def _stable_mapping_key(before: Any, after: Any) -> str | None:
    """Return a stable identity field for two lists of mapping objects."""
    if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
        return None
    for key in ("seat",):
        if not all(isinstance(item, Mapping) and key in item for item in (*before, *after)):
            continue
        before_ids = [item[key] for item in before]
        after_ids = [item[key] for item in after]
        if (
            len(before_ids) == len(before)
            and len(after_ids) == len(after)
            and before_ids == after_ids
            and len(set(before_ids)) == len(before_ids)
        ):
            return key
    return None


def state_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Return a recursive merge delta; arrays are replaced only when changed."""
    delta: dict[str, Any] = {}
    for key in before.keys() - after.keys():
        delta[key] = deepcopy(DELETE)
    for key, value in after.items():
        if key not in before:
            delta[key] = deepcopy(value)
        elif isinstance(before[key], Mapping) and isinstance(value, Mapping):
            nested = state_delta(before[key], value)
            if nested:
                delta[key] = nested
        elif (identity := _stable_mapping_key(before[key], value)) is not None:
            item_deltas = {
                str(after_item[identity]): nested
                for before_item, after_item in zip(before[key], value)
                if (nested := state_delta(before_item, after_item))
            }
            if item_deltas:
                delta[key] = {LIST_BY_KEY: {"key": identity, "items": item_deltas}}
        elif before[key] != value:
            delta[key] = deepcopy(value)
    return delta


def apply_state_delta(before: Mapping[str, Any], delta: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a delta without mutating either input."""
    result = deepcopy(dict(before))
    _apply_state_delta_owned(result, delta)
    return result


def _apply_state_delta_owned(result: dict[str, Any], delta: Mapping[str, Any]) -> None:
    """Apply a delta to an already isolated copy without copying it again."""
    for key, value in delta.items():
        if value == DELETE:
            result.pop(key, None)
        elif (
            isinstance(value, Mapping)
            and set(value) == {LIST_BY_KEY}
            and isinstance(result.get(key), list)
        ):
            patch = value[LIST_BY_KEY]
            identity = patch.get("key") if isinstance(patch, Mapping) else None
            items = patch.get("items") if isinstance(patch, Mapping) else None
            if not isinstance(identity, str) or not isinstance(items, Mapping):
                raise ValueError("invalid keyed-list delta")
            updated = []
            seen = set()
            for item in result[key]:
                item_id = str(item.get(identity)) if isinstance(item, Mapping) else None
                if item_id in items:
                    if not isinstance(item, dict):
                        item = dict(item)
                    _apply_state_delta_owned(item, items[item_id])
                    updated.append(item)
                    seen.add(item_id)
                else:
                    updated.append(item)
            if seen != set(items):
                raise ValueError("keyed-list delta refers to an unknown item")
            result[key] = updated
        elif isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            child = result[key]
            if not isinstance(child, dict):
                child = dict(child)
                result[key] = child
            _apply_state_delta_owned(child, value)
        else:
            result[key] = deepcopy(value)


def changed_hands(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep only seats whose concealed hand changed."""
    return {
        seat: deepcopy(tiles)
        for seat, tiles in after.items()
        if before.get(seat) != tiles
    }
