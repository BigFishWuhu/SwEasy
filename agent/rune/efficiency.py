"""SWEX-compatible rune efficiency calculation.

The formula and the base-value tables mirror the public Rune Card SWEX plugin
(v1.2.1).  Rune data is deliberately accepted in both the SWEX array format
and the friendlier JSON object format used by the local web editor.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

ALGORITHM_VERSION = "swex-rune-card-1.2.1"

# SWEX effect ids.  The names are also accepted by normalize_stat_type().
STAT_IDS = {
    "HP": 1,
    "HP_FLAT": 1,
    "HP%": 2,
    "HP_PERCENT": 2,
    "ATK": 3,
    "ATK_FLAT": 3,
    "ATK%": 4,
    "ATK_PERCENT": 4,
    "DEF": 5,
    "DEF_FLAT": 5,
    "DEF%": 6,
    "DEF_PERCENT": 6,
    "SPD": 8,
    "CR": 9,
    "CRATE": 9,
    "CRIT_RATE": 9,
    "CD": 10,
    "CDMG": 10,
    "CRIT_DMG": 10,
    "RES": 11,
    "ACC": 12,
}
STAT_NAMES = {
    1: "HP",
    2: "HP%",
    3: "ATK",
    4: "ATK%",
    5: "DEF",
    6: "DEF%",
    8: "SPD",
    9: "CR",
    10: "CD",
    11: "RES",
    12: "ACC",
}

# Six-star main-stat maxima from the SWEX plugin.  The inner mapping is
# keyed by rune star count (1..6); flat and percentage stats are intentionally
# kept together because the effect id disambiguates them.
MAIN_MAX = {
    1: {1: 804, 2: 1092, 3: 1380, 4: 1704, 5: 2088, 6: 2448},
    2: {1: 18, 2: 20, 3: 38, 4: 43, 5: 51, 6: 63},
    3: {1: 54, 2: 74, 3: 93, 4: 113, 5: 135, 6: 160},
    4: {1: 18, 2: 20, 3: 38, 4: 43, 5: 51, 6: 63},
    5: {1: 54, 2: 74, 3: 93, 4: 113, 5: 135, 6: 160},
    6: {1: 18, 2: 20, 3: 38, 4: 43, 5: 51, 6: 63},
    8: {1: 18, 2: 19, 3: 25, 4: 30, 5: 39, 6: 42},
    9: {1: 18, 2: 20, 3: 37, 4: 41, 5: 47, 6: 58},
    10: {1: 20, 2: 37, 3: 43, 4: 58, 5: 65, 6: 80},
    11: {1: 18, 2: 20, 3: 38, 4: 44, 5: 51, 6: 64},
    12: {1: 18, 2: 20, 3: 38, 4: 44, 5: 51, 6: 64},
}

SUBSTAT_MAX = {
    1: 1875,
    2: 40,
    3: 100,
    4: 40,
    5: 100,
    6: 40,
    8: 30,
    9: 30,
    10: 35,
    11: 40,
    12: 40,
}

# SWEX compares both normal and ancient rune rolls against the regular
# six-star substat maxima above. It then exposes separate Hero and Legend
# grind projections using the ordinary grindstone caps.
HERO_GRIND_MAX = {1: 450, 2: 7, 3: 25, 4: 7, 5: 25, 6: 7, 8: 4}
LEGEND_GRIND_MAX = {1: 550, 2: 10, 3: 30, 4: 10, 5: 30, 6: 10, 8: 5}
# Backward-compatible export used by older callers.
GRIND_MAX = LEGEND_GRIND_MAX
FLAT_SUBSTAT_TYPES = {1, 3, 5}


def normalize_stat_type(value: Any) -> int:
    """Return a SWEX effect id or raise ValueError for an unknown stat."""

    if isinstance(value, bool):
        raise ValueError("boolean is not a stat type")
    if isinstance(value, int):
        if value in STAT_NAMES:
            return value
        raise ValueError(f"unknown stat id: {value}")
    text = str(value).strip().upper().replace(" ", "_")
    if text.isdigit():
        return normalize_stat_type(int(text))
    try:
        return STAT_IDS[text]
    except KeyError as exc:
        raise ValueError(f"unknown stat type: {value}") from exc


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _stars(rune: Mapping[str, Any]) -> int:
    value = rune.get("stars", rune.get("class", 6))
    stars = int(_number(value, 6) or 6)
    if stars >= 10:
        stars -= 10
    return max(1, min(stars, 6))


def is_ancient(rune: Mapping[str, Any]) -> bool:
    value = rune.get("ancient")
    if value is not None:
        return bool(value)
    rune_class = _number(rune.get("class"))
    return bool(rune_class is not None and rune_class >= 10)


def _stat_parts(stat: Any) -> tuple[int, float, float, bool]:
    """Normalize a SWEX array or object stat.

    Returns ``(type, value, grind, gemmed)``.  In SWEX arrays the shape is
    ``[type, value, gemmed, grind]``; in the editor JSON it is an object.
    """

    if isinstance(stat, Mapping):
        stat_type = normalize_stat_type(stat.get("type", stat.get("stat")))
        value = _number(stat.get("value"), 0) or 0
        grind = _number(stat.get("grind", stat.get("enchanted", 0)), 0) or 0
        gemmed = bool(stat.get("gemmed", stat.get("swapped", False)))
        return stat_type, value, grind, gemmed
    if not isinstance(stat, Sequence) or isinstance(stat, (str, bytes)) or len(stat) < 2:
        raise ValueError(f"invalid stat: {stat!r}")
    stat_type = normalize_stat_type(stat[0])
    value = _number(stat[1], 0) or 0
    gemmed = bool(stat[2]) if len(stat) > 2 else False
    grind = _number(stat[3], 0) if len(stat) > 3 else 0
    return stat_type, value, grind or 0, gemmed


def _main_stat(rune: Mapping[str, Any]) -> tuple[int, float]:
    main = rune.get("main_stat", rune.get("pri_eff"))
    if isinstance(main, Mapping):
        return normalize_stat_type(main.get("type", main.get("stat"))), _number(main.get("value"), 0) or 0
    if isinstance(main, Sequence) and not isinstance(main, (str, bytes)) and len(main) >= 2:
        return normalize_stat_type(main[0]), _number(main[1], 0) or 0
    raise ValueError("missing main_stat")


def _substats(rune: Mapping[str, Any]) -> list[tuple[int, float, float, bool]]:
    raw = rune.get("substats", rune.get("sec_eff", [])) or []
    return [_stat_parts(stat) for stat in raw]


def _innate(rune: Mapping[str, Any]) -> tuple[int, float] | None:
    raw = rune.get("innate", rune.get("prefix_eff"))
    if raw in (None, [], 0):
        return None
    if isinstance(raw, Mapping):
        stat_type = raw.get("type", raw.get("stat"))
        value = raw.get("value")
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) >= 2:
        stat_type, value = raw[0], raw[1]
    else:
        return None
    try:
        return normalize_stat_type(stat_type), _number(value, 0) or 0
    except ValueError:
        return None


def calculate_efficiency(rune: Mapping[str, Any], to_fixed: int = 2) -> dict[str, float | str]:
    """Calculate SWEX and flat-penalized practical efficiency values.

    The four original fields remain byte-for-byte compatible with SWEX Rune
    Card. Practical efficiency is an additional filtering metric that counts
    flat HP/ATK/DEF substats and their grind potential at 50% weight. Main
    stats are never penalized because flat mains are mandatory on slots 1/3/5.
    """

    if not isinstance(rune, Mapping):
        raise ValueError("rune must be an object")
    stars = _stars(rune)
    main_type, main_value = _main_stat(rune)
    main_table = MAIN_MAX.get(main_type)
    if not main_table:
        raise ValueError(f"unsupported main stat: {main_type}")
    # SWEX's Rune Card implementation uses the theoretical main-stat maximum
    # for the rune's star tier, divided by the six-star maximum.  This is
    # intentionally independent of the current upgrade value: it measures the
    # rune's quality ceiling, while substats carry their actual rolled values.
    ratio = MAIN_MAX[main_type][stars] / MAIN_MAX[main_type][6]
    practical_ratio = ratio

    hero_grind_potential = 0.0
    legend_grind_potential = 0.0
    practical_hero_grind_potential = 0.0
    practical_legend_grind_potential = 0.0
    for stat_type, value, grind, _gemmed in _substats(rune):
        denominator = SUBSTAT_MAX.get(stat_type)
        if not denominator:
            raise ValueError(f"unsupported sub stat: {stat_type}")
        weight = 0.5 if stat_type in FLAT_SUBSTAT_TYPES else 1.0
        contribution = (value + grind) / denominator
        ratio += contribution
        practical_ratio += contribution * weight
        if stat_type in HERO_GRIND_MAX:
            hero_gain = max(HERO_GRIND_MAX[stat_type] - grind, 0) / denominator
            legend_gain = max(LEGEND_GRIND_MAX[stat_type] - grind, 0) / denominator
            hero_grind_potential += hero_gain
            legend_grind_potential += legend_gain
            practical_hero_grind_potential += hero_gain * weight
            practical_legend_grind_potential += legend_gain * weight

    innate = _innate(rune)
    if innate:
        stat_type, value = innate
        denominator = SUBSTAT_MAX.get(stat_type)
        if denominator:
            ratio += value / denominator
            weight = 0.5 if stat_type in FLAT_SUBSTAT_TYPES else 1.0
            practical_ratio += value / denominator * weight

    current = ratio / 2.8 * 100
    practical_current = practical_ratio / 2.8 * 100
    level_value = _number(rune.get("level", rune.get("upgrade_curr")), 0) or 0
    remaining_procs = max(math.ceil((12 - level_value) / 3.0), 0)
    max_potential = current + ((remaining_procs * 0.2) / 2.8) * 100
    # This is deliberately optimistic for unfinished runes: a remaining proc
    # may land on a percentage or combat stat worth the full SWEX roll value.
    practical_max = practical_current + ((remaining_procs * 0.2) / 2.8) * 100
    max_hero_grind = max_potential + (hero_grind_potential / 2.8) * 100
    max_legend_grind = max_potential + (legend_grind_potential / 2.8) * 100
    practical_max_hero_grind = practical_max + (practical_hero_grind_potential / 2.8) * 100
    practical_max_legend_grind = practical_max + (practical_legend_grind_potential / 2.8) * 100
    return {
        "algorithm": ALGORITHM_VERSION,
        "current": round(current, to_fixed),
        "max": round(max_potential, to_fixed),
        "maxHeroGrind": round(max_hero_grind, to_fixed),
        "maxLegendGrind": round(max_legend_grind, to_fixed),
        # Existing rules and saved sort settings use this field. In SWEX terms
        # it is the Legend Grinded value.
        "maxWithGrind": round(max_legend_grind, to_fixed),
        "practicalCurrent": round(practical_current, to_fixed),
        "practicalMax": round(practical_max, to_fixed),
        "practicalMaxHeroGrind": round(practical_max_hero_grind, to_fixed),
        "practicalMaxLegendGrind": round(practical_max_legend_grind, to_fixed),
        "practicalMaxWithGrind": round(practical_max_legend_grind, to_fixed),
    }


def stat_name(value: Any) -> str:
    """Return the canonical short name used by web rules."""

    return STAT_NAMES[normalize_stat_type(value)]
