"""Read SWEX/SW account exports into the rule engine's rune shape.

The account export stores runes in two places: the top-level ``runes`` list
for unequipped inventory and each unit's nested ``runes`` list for equipped
inventory.  Both use effect-id arrays, so normalization lives here instead
of being duplicated by the web preview and scanner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

SET_NAMES = {
    1: "Energy", 2: "Guard", 3: "Swift", 4: "Blade", 5: "Rage",
    6: "Focus", 7: "Endure", 8: "Fatal", 10: "Despair", 11: "Vampire",
    13: "Violent", 14: "Nemesis", 15: "Will", 16: "Shield",
    17: "Revenge", 18: "Destroy", 19: "Fight", 20: "Determination",
    21: "Enhance", 22: "Accuracy", 23: "Tolerance", 24: "Seal",
    25: "Intangible",
}
QUALITY_NAMES = {1: "Common", 2: "Magic", 3: "Rare", 4: "Hero", 5: "Legend"}


def _quality(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("rank", raw.get("quality"))
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return value if isinstance(value, str) else None
    # Ancient rune ranks are encoded as 14/15, while normal runes use 4/5.
    return QUALITY_NAMES.get(rank % 10, raw.get("quality"))


def _stat(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    result: dict[str, Any] = {"type": value[0], "value": value[1]}
    if len(value) > 3 and value[3]:
        result["grind"] = value[3]
    if len(value) > 2 and value[2]:
        result["gemmed"] = bool(value[2])
    return result


def normalize_account_rune(raw: Mapping[str, Any], *, locked_ids: set[int] | None = None) -> dict[str, Any]:
    """Normalize one exported rune while retaining its source identifiers."""
    if not isinstance(raw, Mapping):
        raise ValueError("rune must be an object")
    rune_id = raw.get("rune_id")
    class_value = raw.get("class", raw.get("stars", 6))
    try:
        class_number = int(class_value)
    except (TypeError, ValueError):
        class_number = 6
    stars = class_number - 10 if class_number >= 10 else class_number
    primary = _stat(raw.get("pri_eff"))
    if primary is None:
        raise ValueError(f"rune {rune_id or '?'} has no primary stat")
    substats = [_stat(value) for value in raw.get("sec_eff", []) or []]
    substats = [value for value in substats if value is not None]
    innate = _stat(raw.get("prefix_eff"))
    locked = bool(raw.get("locked", False)) or bool(rune_id and rune_id in (locked_ids or set()))
    occupied_id = raw.get("occupied_id", 0)
    return {
        "recognized": True,
        "rune_id": rune_id,
        "stars": max(1, min(stars, 6)),
        "ancient": class_number >= 10,
        # rank is the rune quality in SW exports; ancient ranks are 14/15.
        # extra is the innate/ancient marker and must not be used as quality.
        "quality": _quality(raw),
        "set": SET_NAMES.get(raw.get("set_id"), raw.get("set_id")),
        "set_id": raw.get("set_id"),
        "slot": raw.get("slot_no", raw.get("slot")),
        "level": raw.get("upgrade_curr", raw.get("level", 0)),
        "main_stat": primary,
        "innate": innate,
        "substats": substats,
        "equipped": bool(occupied_id),
        "locked": locked,
        "source": dict(raw),
    }


def iter_account_runes(account: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Yield top-level and nested unit runes, without assuming one export layout."""
    for rune in account.get("runes", []) or []:
        if isinstance(rune, Mapping):
            yield rune
    for unit in account.get("unit_list", []) or []:
        if not isinstance(unit, Mapping):
            continue
        for rune in unit.get("runes", []) or []:
            if isinstance(rune, Mapping):
                yield rune


def load_account_runes(path: str | Path) -> list[dict[str, Any]]:
    """Load and normalize an account export, deduplicating by rune id."""
    source = Path(path)
    account = json.loads(source.read_text(encoding="utf-8"))
    locked_ids = {
        int(item["rune_id"])
        for item in account.get("rune_lock_list", []) or []
        if isinstance(item, Mapping) and str(item.get("rune_id", "")).isdigit()
    }
    result: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for raw in iter_account_runes(account):
        rune_id = raw.get("rune_id")
        if rune_id in seen:
            continue
        seen.add(rune_id)
        result.append(normalize_account_rune(raw, locked_ids=locked_ids))
    return result
