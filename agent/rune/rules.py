"""Safe, serializable rune rule engine."""

from __future__ import annotations

import math
from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Mapping

from .efficiency import MAIN_MAX, calculate_efficiency, normalize_stat_type, stat_name
from .json_import import normalize_account_rune

CONFIG_VERSION = 1
VALID_ACTIONS = {"keep", "sell"}
VALID_MATCHES = {"all", "any", "at_least"}
VALID_OPERATORS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in", "contains"}
DEPRECATED_DEFAULT_GROUP_IDS = {
    "keep-rage-fatal-attack-crit",
    # The original rule treated every extreme substat as useful on every set.
    # It is replaced by purpose-specific groups below.
    "keep-single-extreme-substat",
    # Replaced by separate Fight support/damage rules and an Intangible rule.
    # Removing the old broad group is important for persisted SQLite configs:
    # otherwise almost every efficient Fight rune would keep matching it.
    "keep-fight-intangible-versatile-support",
}
SINGLE_EXTREME_REPLACEMENT_IDS = {
    "keep-single-extreme-offense",
    "keep-single-extreme-scaling",
    "keep-single-extreme-resistance",
    "keep-single-extreme-accuracy",
}
REFRESHED_ACCOUNT_GROUP_IDS = {
    "sell-fatal-mismatched-main-no-speed",
    "keep-wind-occult-damage-control",
    "keep-bomber-speed-accuracy",
    "keep-hp-cd-guaranteed-crit",
    "keep-damage-hp-def",
    "keep-slot2-speed-main",
}
TOP_SIXTY_PRESET = "account-tuned-forodo-29649161-v3-progressive-max90"
TOP_SIXTY_EXCEPTIONAL_CURRENT = 91
# Purpose rules already require coherent stats and at least three useful
# enhancement rolls.  During the first-pass cleanup, maxWithGrind is therefore
# only a broad safety floor instead of a second, overly strict elimination
# gate.  Later passes can raise individual archetypes after previewing them.
TOP_SIXTY_EFFICIENCY = {95: 90, 100: 90, 105: 90, 108: 90, 110: 90}
TOP_SIXTY_STATS = {
    10: 9, 12: 11, 15: 13,
    20: 19, 21: 20, 22: 21, 23: 22, 24: 22, 25: 23, 29: 27,
}
TOP_SIXTY_GROUP_NAMES = {
    # IDs remain stable for saved configs and historical scanner logs.
    "keep-slot46-legend-speed-25": "四/六号位传说速度23保留",
    "keep-single-speed-premium-set": "迅速/暴走/意志/无形单条速度20+",
    "keep-single-speed-any-set": "任意套装单条速度22+",
    "keep-key-set-base-speed-20": "关键套装未精炼速度19+",
}
FORBIDDEN_SUBSTATS_BY_SLOT = {
    1: {"DEF", "DEF%"},
    3: {"ATK", "ATK%"},
}
KEY_SPEED_SETS = (
    "Swift", "Violent", "Will", "Despair", "Nemesis", "Revenge",
    "Shield", "Vampire", "Destroy", "Seal", "Intangible",
)
ATTACK_SETS = ("Fatal", "Rage", "Blade")
ATTACK_SLOT6_SPEED_TARGET = 21

# These values are also exposed to the web editor.  Keeping the canonical
# values here prevents OCR (Chinese) labels and user-entered labels from
# becoming subtly different rule values.
QUALITY_RANK = {"Common": 1, "Magic": 2, "Rare": 3, "Hero": 4, "Legend": 5}
QUALITY_ALIASES = {"普通": "Common", "魔法": "Magic", "稀有": "Rare", "英雄": "Hero", "传说": "Legend"}
STAT_ALIASES = {
    "体力": "HP", "攻击": "ATK", "攻击力": "ATK", "防御": "DEF", "防御力": "DEF",
    "攻击%": "ATK%", "防御%": "DEF%", "体力%": "HP%", "攻击速度": "SPD",
    "暴击率": "CR", "暴击伤害": "CD", "效果抵抗": "RES", "效果命中": "ACC",
}
INNATE_MAX_VALUES = {"CR": 6, "CD": 7, "ACC": 8}
ANCIENT_INNATE_MAX_VALUES = {"CR": 7, "CD": 8, "ACC": 10}

# A rune's displayed substat contains its initial roll plus any +3/+6/+9/+12
# enhancement rolls.  These are the inclusive six-star ranges used to infer a
# *minimum* number of enhancement rolls.  Ancient runes have wider ranges, so
# treating them as ordinary runes would overstate how many useful rolls are
# proven by the final value.
SUBSTAT_ROLL_RANGES = {
    "HP": (135, 375), "HP%": (5, 8),
    "ATK": (10, 20), "ATK%": (5, 8),
    "DEF": (10, 20), "DEF%": (5, 8),
    "SPD": (4, 6), "CR": (4, 6), "CD": (4, 7),
    "RES": (4, 8), "ACC": (4, 8),
}
ANCIENT_SUBSTAT_ROLL_RANGES = {
    "HP": (202, 435), "HP%": (6, 10),
    "ATK": (12, 23), "ATK%": (6, 10),
    "DEF": (12, 23), "DEF%": (6, 10),
    "SPD": (5, 7), "CR": (5, 7), "CD": (5, 8),
    "RES": (6, 10), "ACC": (6, 10),
}
INITIAL_SUBSTATS_BY_QUALITY = {
    "Common": 0, "Magic": 1, "Rare": 2, "Hero": 3, "Legend": 4,
}
ROLL_PROFILES = {
    "attackOutput": {"ATK%", "CR", "CD", "SPD"},
    "hpOutput": {"HP%", "CR", "CD", "SPD"},
    "defOutput": {"DEF%", "CR", "CD", "SPD"},
    "scalingOutput": {"HP%", "ATK%", "DEF%", "CR", "CD", "SPD"},
    "guaranteedCrit": {"ATK%", "CD", "SPD"},
    "hpGuaranteedCrit": {"HP%", "ATK%", "DEF%", "CD", "SPD"},
    "speedCritHp": {"HP%", "CR", "CD", "SPD"},
    "bomber": {"ATK%", "ACC", "SPD"},
    "control": {"HP%", "DEF%", "RES", "ACC", "SPD"},
    "tank": {"HP%", "DEF%", "RES", "SPD"},
    "support": {"HP%", "DEF%", "RES", "ACC", "SPD"},
    "damageControl": {"HP%", "ATK%", "DEF%", "CR", "CD", "ACC", "SPD"},
    "versatile": {"HP%", "ATK%", "DEF%", "CR", "CD", "RES", "ACC", "SPD"},
}


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    rule_id: str | None = None
    uncertain: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "uncertain": self.uncertain,
        }


def default_config() -> dict[str, Any]:
    config = {
        "version": CONFIG_VERSION,
        "algorithm": "swex-rune-card-1.2.1",
        "mode": "auto_sell",
        "preset": TOP_SIXTY_PRESET,
        "retention_policy": {
            "basis": "level_gte_12",
            "target_percent": 60,
        },
        "fallback": "sell",
        "groups": [
            {
                "id": "keep-swift-speed-crit-attack",
                "name": "迅速高速暴击攻击胚子",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                    {"field": "set", "op": "eq", "value": "Swift"},
                    {"field": "substats.SPD", "op": "gte", "value": 10},
                    {"field": "substats.CR", "op": "gte", "value": 10},
                    {"field": "substats.ATK%", "op": "gte", "value": 8},
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 72},
                ],
            },
            {
                "id": "keep-swift-violent-speed-accuracy",
                "name": "迅速/暴走高速命中三维",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                    {"field": "set", "op": "in", "value": ["Swift", "Violent"]},
                    {"field": "substats.SPD", "op": "gte", "value": 15},
                    {"field": "substats.ACC", "op": "gte", "value": 15},
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 70},
                ],
            },
            {
                "id": "keep-rage-fatal-attack-crit",
                "name": "激怒/猛攻攻击暴击爆伤",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                    {"field": "set", "op": "in", "value": ["Rage", "Fatal"]},
                    {"field": "substats.ATK%", "op": "gte", "value": 10},
                    {"field": "substats.CR", "op": "gte", "value": 12},
                    {"field": "substats.CD", "op": "gte", "value": 15},
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 72},
                ],
            },
            {
                "id": "keep-blade-will-attack-crit",
                "name": "刀刃/意志攻击暴击爆伤",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                    {"field": "set", "op": "in", "value": ["Blade", "Will"]},
                    {"field": "substats.ATK%", "op": "gte", "value": 10},
                    {"field": "substats.CR", "op": "gte", "value": 12},
                    {"field": "substats.CD", "op": "gte", "value": 15},
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 72},
                ],
            },
            {
                "id": "keep-damage-defense-scaler",
                "name": "防御加成输出三维暴击",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                    {"field": "set", "op": "in", "value": ["Rage", "Fatal", "Blade", "Will"]},
                    {"field": "substats.DEF%", "op": "gte", "value": 10},
                    {"field": "substats.CR", "op": "gte", "value": 10},
                    {"field": "substats.CD", "op": "gte", "value": 15},
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 70},
                ],
            },
            {
                "id": "keep-damage-hp-scaler",
                "name": "体力加成输出三维暴击",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                    {"field": "set", "op": "in", "value": ["Rage", "Fatal", "Blade", "Will"]},
                    {"field": "substats.HP%", "op": "gte", "value": 10},
                    {"field": "substats.CR", "op": "gte", "value": 10},
                    {"field": "substats.CD", "op": "gte", "value": 15},
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 70},
                ],
            },
            {
                "id": "keep-pure-tank-res-speed",
                "name": "纯肉满抗兼顾攻速",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                    {"field": "set", "op": "in", "value": ["Violent", "Will", "Guard", "Energy", "Swift"]},
                    {"field": "substats.RES", "op": "gte", "value": 15},
                    {"field": "substats.SPD", "op": "gte", "value": 10},
                    {"field": "substats.HP%", "op": "gte", "value": 10},
                    {"field": "substats.DEF%", "op": "gte", "value": 10},
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 65},
                ],
            },
            {
                "id": "keep-speed-tank-three-stat",
                "name": "暴走/意志高速三维肉装",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                    {"field": "set", "op": "in", "value": ["Violent", "Will", "Swift"]},
                    {"field": "substats.SPD", "op": "gte", "value": 15},
                    {"field": "substats.HP%", "op": "gte", "value": 10},
                    {"field": "substats.DEF%", "op": "gte", "value": 10},
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 68},
                ],
            },
            {
                "id": "keep-despair-control-speed-accuracy",
                "name": "绝望控制高速命中",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                    {"field": "set", "op": "eq", "value": "Despair"},
                    {"field": "substats.SPD", "op": "gte", "value": 15},
                    {"field": "substats.ACC", "op": "gte", "value": 15},
                    {"field": "substats.HP%", "op": "gte", "value": 10},
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 68},
                ],
            },
            {
                "id": "keep-vampire-destroy-bruiser",
                "name": "吸血/覆灭肉输出",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                    {"field": "set", "op": "in", "value": ["Vampire", "Destroy", "Nemesis"]},
                    {"field": "substats.HP%", "op": "gte", "value": 10},
                    {"field": "substats.ATK%", "op": "gte", "value": 10},
                    {"field": "substats.CR", "op": "gte", "value": 10},
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 68},
                ],
            },
            {
                "id": "keep-six-star-hero-plus",
                "name": "保留六星英雄及以上",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [
                    {"field": "stars", "op": "gte", "value": 6},
                    {"field": "quality", "op": "gte", "value": "Hero"},
                ],
            },
            {
                "id": "sell-low-potential",
                "name": "出售满磨潜力低于 80%",
                "enabled": True,
                "action": "sell",
                "match": "all",
                "rules": [
                    {"field": "efficiency.maxWithGrind", "op": "lt", "value": 80}
                ],
            },
        ],
    }
    config["groups"] = _account_tuned_groups()
    return config


def _account_tuned_groups() -> list[dict[str, Any]]:
    return _relax_groups_for_top_sixty(_strict_account_groups())


def _relax_groups_for_top_sixty(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tune absolute thresholds against this account's finished (+12+) runes.

    Build rules stay directional; only their numeric strength thresholds are
    relaxed. The exceptional-efficiency rule is calibrated after the build
    rules so their union retains approximately 60% of finished runes.
    """
    result = deepcopy(groups)
    for group in result:
        if group.get("id") in TOP_SIXTY_GROUP_NAMES:
            group["name"] = TOP_SIXTY_GROUP_NAMES[group["id"]]
        for section in ("requirements", "rules", "conditions"):
            for condition in group.get(section, []) or []:
                if not isinstance(condition, dict) or condition.get("op") not in {"gte", "gt"}:
                    continue
                field = str(condition.get("field", ""))
                value = condition.get("value")
                if not isinstance(value, (int, float)):
                    continue
                if (
                    group.get("id") in {
                        "keep-attack-slot6-acc-res-speed-target",
                        "sell-attack-slot6-acc-res-speed-below-target",
                    }
                    and field == "baseSubstats.SPD"
                ):
                    continue
                if field.startswith("efficiency."):
                    if group.get("id") == "keep-exceptional-efficiency":
                        condition["value"] = TOP_SIXTY_EXCEPTIONAL_CURRENT
                    else:
                        condition["value"] = TOP_SIXTY_EFFICIENCY.get(value, value)
                elif field.startswith(("buildStats.", "substats.", "baseSubstats.")):
                    condition["value"] = TOP_SIXTY_STATS.get(value, value)
    return result


def _strict_account_groups() -> list[dict[str, Any]]:
    """Use absolute build thresholds tuned for this account's multi-team use."""
    common = [
        {"field": "stars", "op": "gte", "value": 6},
        {"field": "quality", "op": "gte", "value": "Hero"},
    ]

    def group(group_id: str, name: str, requirements: list[dict[str, Any]], rules: list[dict[str, Any]], *, match: str = "all", min_match: int = 1) -> dict[str, Any]:
        result = {
            "id": group_id, "name": name, "enabled": True, "action": "keep",
            "match": match, "requirements": [*common, *requirements], "rules": rules,
        }
        if match == "at_least":
            result["min_match"] = min_match
        return result

    sets = lambda values: {"field": "set", "op": "in", "value": values}
    efficiency = lambda value: {"field": "efficiency.maxWithGrind", "op": "gte", "value": value}
    stat = lambda name, value: {"field": f"buildStats.{name}", "op": "gte", "value": value}
    substat = lambda name, value: {"field": f"substats.{name}", "op": "gte", "value": value}
    useful_rolls = lambda profile: {"field": f"rolls.{profile}", "op": "gte", "value": 3}
    return [
        {
            "id": "sell-rare-quality-immediate",
            "name": "稀有（蓝色）符文立即出售",
            "enabled": True,
            "action": "sell",
            "match": "all",
            "requirements": [],
            "rules": [{"field": "quality", "op": "eq", "value": "Rare"}],
            "priority": 200,
            "terminal": True,
        },
        {
            **group(
                "keep-attack-slot6-acc-res-speed-target",
                "攻击套装六号命中/抵抗主属性速度21+",
                [
                    sets(list(ATTACK_SETS)),
                    {"field": "slot", "op": "eq", "value": 6},
                    {"field": "main_stat.type", "op": "in", "value": ["ACC", "RES"]},
                ],
                [{"field": "baseSubstats.SPD", "op": "gte", "value": ATTACK_SLOT6_SPEED_TARGET}],
            ),
            "priority": 195,
        },
        {
            **group(
                "sell-attack-slot6-acc-res-no-speed",
                "攻击套装六号命中/抵抗主属性无速度立即出售",
                [
                    sets(list(ATTACK_SETS)),
                    {"field": "slot", "op": "eq", "value": 6},
                    {"field": "main_stat.type", "op": "in", "value": ["ACC", "RES"]},
                ],
                [{"field": "hasSubstat.SPD", "op": "eq", "value": False}],
            ),
            "action": "sell",
            "priority": 190,
            "terminal": True,
        },
        {
            **group(
                "sell-attack-slot6-acc-res-speed-below-target",
                "攻击套装六号命中/抵抗主属性速度未达标",
                [
                    sets(list(ATTACK_SETS)),
                    {"field": "slot", "op": "eq", "value": 6},
                    {"field": "main_stat.type", "op": "in", "value": ["ACC", "RES"]},
                    {"field": "hasSubstat.SPD", "op": "eq", "value": True},
                ],
                [{"field": "baseSubstats.SPD", "op": "lt", "value": ATTACK_SLOT6_SPEED_TARGET}],
            ),
            "action": "sell",
            "priority": 190,
        },
        {
            **group(
                "keep-slot46-legend-speed-25",
                "四/六号位传说速度25保留",
                [
                    {"field": "quality", "op": "eq", "value": "Legend"},
                    {"field": "slot", "op": "in", "value": [4, 6]},
                ],
                [substat("SPD", 25)],
            ),
            "priority": 110,
        },
        {
            **group(
                "sell-flat-slot46-main",
                "出售未达标的四/六号位固定主属性",
                [],
                [
                    {"field": "slot", "op": "in", "value": [4, 6]},
                    {"field": "main_stat.type", "op": "in", "value": ["HP", "ATK", "DEF"]},
                ],
            ),
            "action": "sell",
            "priority": 100,
        },
        {
            **group(
                "sell-fatal-mismatched-main-no-speed",
                "猛攻二/四/六号位主属性不适合输出",
                [
                    sets(["Fatal"]),
                    {"field": "slot", "op": "in", "value": [2, 4, 6]},
                ],
                [{"field": "main_stat.type", "op": "in", "value": ["HP", "HP%", "DEF", "DEF%", "ACC", "RES"]}],
            ),
            "action": "sell",
            "priority": 90,
        },
        {
            **group(
                "sell-swift-slot2-tank-main-no-speed",
                "迅速二号位无速度且体防主属性",
                [
                    sets(["Swift"]),
                    {"field": "slot", "op": "eq", "value": 2},
                    {"field": "hasSubstat.SPD", "op": "eq", "value": False},
                ],
                [{"field": "main_stat.type", "op": "in", "value": ["HP", "HP%", "DEF", "DEF%"]}],
            ),
            "action": "sell",
            "priority": 90,
        },
        {
            **group(
                "sell-tank-set-attack-main-no-speed",
                "祝福/守护/忍耐二/四/六号位无速度且攻击主属性",
                [
                    sets(["Energy", "Guard", "Endure"]),
                    {"field": "slot", "op": "in", "value": [2, 4, 6]},
                    {"field": "hasSubstat.SPD", "op": "eq", "value": False},
                ],
                [{"field": "main_stat.type", "op": "in", "value": ["ATK", "ATK%"]}],
            ),
            "action": "sell",
            "priority": 90,
        },
        {
            **group(
                "keep-legend-key-set-reappraisal-slot135",
                "传说迅速/暴走/意志洗炼胚子（1/3/5号位）",
                [
                    {"field": "quality", "op": "eq", "value": "Legend"},
                    sets(["Swift", "Violent", "Will"]),
                    {"field": "slot", "op": "in", "value": [1, 3, 5]},
                ],
                [
                    {"field": "innate.type", "op": "in", "value": ["CR", "CD", "ACC"]},
                    {"field": "innate.maxed", "op": "eq", "value": True},
                ],
            ),
            "priority": 80,
        },
        {
            **group(
                "keep-legend-key-set-reappraisal-slot2-speed",
                "传说迅速/暴走/意志洗炼胚子（2号速度）",
                [
                    {"field": "quality", "op": "eq", "value": "Legend"},
                    sets(["Swift", "Violent", "Will"]),
                    {"field": "slot", "op": "eq", "value": 2},
                    {"field": "main_stat.type", "op": "eq", "value": "SPD"},
                ],
                [
                    {"field": "innate.type", "op": "in", "value": ["CR", "CD", "ACC"]},
                    {"field": "innate.maxed", "op": "eq", "value": True},
                ],
            ),
            "priority": 80,
        },
        {
            **group(
                "keep-legend-key-set-reappraisal-slot46-percent",
                "传说迅速/暴走/意志洗炼胚子（4/6号非固定主属性）",
                [
                    {"field": "quality", "op": "eq", "value": "Legend"},
                    sets(["Swift", "Violent", "Will"]),
                    {"field": "slot", "op": "in", "value": [4, 6]},
                    {"field": "main_stat.type", "op": "not_in", "value": ["HP", "ATK", "DEF"]},
                ],
                [
                    {"field": "innate.type", "op": "in", "value": ["CR", "CD", "ACC"]},
                    {"field": "innate.maxed", "op": "eq", "value": True},
                ],
            ),
            "priority": 80,
        },
        # Finished runes must already be efficient. Grind potential alone can
        # no longer rescue a weak +12/+15 rune; unfinished runes reach this
        # rule only through the staged upgrade path's optimistic roll search.
        group(
            "keep-exceptional-efficiency",
            "任意套装高效率成品",
            [{"field": "level", "op": "gte", "value": 12}],
            [{"field": "efficiency.current", "op": "gte", "value": 94}],
        ),
        group("keep-exceptional-speed", "任意套装高速稀缺胚子", [], [substat("SPD", 22), efficiency(105)]),
        group(
            "keep-single-speed-premium-set",
            "迅速/暴走/意志/无形单条速度21+",
            [sets(["Swift", "Violent", "Will", "Intangible"])],
            [{"field": "baseSubstats.SPD", "op": "gte", "value": 21}],
        ),
        group(
            "keep-single-speed-any-set",
            "任意套装单条速度23+",
            [],
            [{"field": "baseSubstats.SPD", "op": "gte", "value": 23}],
        ),
        group(
            "keep-key-set-base-speed-20",
            "关键套装未精炼速度20+",
            [sets(list(KEY_SPEED_SETS))],
            [{"field": "baseSubstats.SPD", "op": "gte", "value": 20}],
        ),
        group(
            "keep-single-extreme-offense",
            "输出套装单条极品攻击/暴击/爆伤",
            [sets([
                "Fatal", "Rage", "Blade", "Swift", "Violent", "Will",
                "Vampire", "Destroy", "Fight", "Intangible", "Energy",
                "Guard", "Revenge", "Despair",
            ])],
            [
                {"field": "baseSubstats.CR", "op": "gte", "value": 24},
                {"field": "baseSubstats.CD", "op": "gte", "value": 25},
                {"field": "baseSubstats.ATK%", "op": "gte", "value": 29},
            ],
            match="any",
        ),
        group(
            "keep-single-extreme-scaling",
            "体防/半肉套装单条极品体力或防御",
            [sets([
                "Rage", "Blade", "Swift", "Violent", "Will", "Vampire",
                "Destroy", "Energy", "Guard", "Endure", "Despair", "Focus",
                "Accuracy", "Nemesis", "Shield", "Revenge", "Fight",
                "Intangible", "Seal",
            ])],
            [
                {"field": "baseSubstats.HP%", "op": "gte", "value": 29},
                {"field": "baseSubstats.DEF%", "op": "gte", "value": 29},
            ],
            match="any",
        ),
        group(
            "keep-single-extreme-resistance",
            "肉装/泛用套装单条极品抵抗",
            [sets([
                "Swift", "Violent", "Will", "Vampire", "Destroy", "Energy",
                "Guard", "Endure", "Despair", "Nemesis", "Shield", "Revenge",
                "Fight", "Intangible", "Seal",
            ])],
            [{"field": "baseSubstats.RES", "op": "gte", "value": 29}],
        ),
        group(
            "keep-single-extreme-accuracy",
            "控制/泛用套装单条极品命中",
            [sets([
                "Swift", "Violent", "Will", "Despair", "Focus", "Accuracy",
                "Fight", "Intangible", "Seal",
            ])],
            [{"field": "baseSubstats.ACC", "op": "gte", "value": 29}],
        ),
        group("keep-swift-violent-attack-core", "迅速/暴走高速攻击暴击爆伤", [sets(["Swift", "Violent"]), efficiency(105), useful_rolls("attackOutput"), stat("SPD", 15), stat("ATK%", 10)], [stat("CR", 10), stat("CD", 12), {"field": "main_stat.type", "op": "eq", "value": "CR"}, {"field": "main_stat.type", "op": "eq", "value": "CD"}], match="at_least", min_match=1),
        {**group("keep-swift-violent-attack-speed-main", "迅速/暴走二号速度攻击输出", [sets(["Swift", "Violent"]), efficiency(105), useful_rolls("attackOutput"), {"field": "main_stat.type", "op": "eq", "value": "SPD"}, stat("ATK%", 10)], [stat("CR", 10), stat("CD", 12)], match="at_least", min_match=1), "priority": 1},
        {**group("keep-swift-violent-attack-main", "迅速/暴走六号攻击高速输出", [sets(["Swift", "Violent"]), efficiency(105), useful_rolls("attackOutput"), {"field": "main_stat.type", "op": "eq", "value": "ATK%"}, stat("SPD", 15)], [stat("CR", 10), stat("CD", 12)], match="at_least", min_match=1), "priority": 1},
        group("keep-speed-attack-crit", "高速攻击暴击（风天使）", [sets(["Swift", "Violent", "Will", "Rage", "Fatal", "Blade"]), efficiency(105), useful_rolls("attackOutput"), stat("SPD", 15), stat("ATK%", 10), stat("CR", 10)], [stat("CD", 12), stat("HP%", 10), stat("DEF%", 10)], match="at_least", min_match=1),
        group("keep-speed-attack-cd", "高速攻击爆伤（水希里）", [sets(["Swift", "Violent", "Will", "Rage", "Fatal", "Blade"]), efficiency(105), useful_rolls("attackOutput"), stat("SPD", 15), stat("ATK%", 10), stat("CD", 15)], [stat("CR", 10), stat("HP%", 10), stat("DEF%", 10)], match="at_least", min_match=1),
        group("keep-speed-crit-hp", "高速暴击体力（风马）", [sets(["Violent", "Will", "Swift", "Blade"]), efficiency(105), useful_rolls("speedCritHp"), stat("SPD", 15), stat("CR", 15), stat("HP%", 15)], [stat("CD", 12), stat("DEF%", 10), stat("ATK%", 10), stat("RES", 10)], match="at_least", min_match=1),
        group(
            "keep-despair-damage-output",
            "绝望攻击双暴输出",
            [sets(["Despair"]), efficiency(105), useful_rolls("attackOutput")],
            [stat("ATK%", 10), stat("CR", 10), stat("CD", 12), stat("SPD", 10)],
            match="at_least",
            min_match=2,
        ),
        group("keep-wind-occult-damage-control", "风奥类高速输出控制", [sets(["Swift", "Violent", "Despair", "Will", "Nemesis", "Rage"]), efficiency(105), useful_rolls("damageControl"), stat("SPD", 15)], [stat("ATK%", 10), stat("CR", 10), stat("CD", 12), stat("ACC", 10), stat("HP%", 10), stat("DEF%", 10)], match="at_least", min_match=2),
        group("keep-bomber-speed-accuracy", "炸弹流速度命中攻击（不要求暴击）", [sets(["Swift", "Violent", "Focus", "Will", "Despair", "Rage", "Fatal"]), efficiency(100), useful_rolls("bomber"), stat("SPD", 10), stat("ACC", 15)], [stat("ATK%", 10)]),
        group("keep-hp-cd-guaranteed-crit", "体力爆伤必暴击（神兽僧/风大王）", [sets(["Violent", "Will", "Rage", "Blade", "Vampire"]), efficiency(105), useful_rolls("hpGuaranteedCrit"), stat("HP%", 15), stat("CD", 15)], [stat("ATK%", 10), stat("DEF%", 10), stat("SPD", 10)], match="at_least", min_match=1),
        group(
            "keep-guaranteed-crit-damage",
            "必定暴击输出：速度+爆伤+攻击",
            [sets(["Swift", "Rage", "Fatal", "Blade", "Will", "Violent", "Vampire"]), efficiency(105), useful_rolls("guaranteedCrit")],
            [stat("SPD", 10), stat("CD", 15), stat("ATK%", 10)],
        ),
        {
            **group(
            "keep-guaranteed-crit-speed-main",
            "必定暴击输出：二号速度主属性",
            [sets(["Swift", "Rage", "Fatal", "Blade", "Will", "Violent", "Vampire"]), efficiency(105), useful_rolls("guaranteedCrit"), {"field": "slot", "op": "eq", "value": 2}, {"field": "main_stat.type", "op": "eq", "value": "SPD"}],
            [stat("CD", 15), stat("ATK%", 10)],
            ),
            "priority": 1,
        },
        {
            **group(
            "keep-guaranteed-crit-cd-main",
            "必定暴击输出：四号爆伤主属性",
            [sets(["Swift", "Rage", "Fatal", "Blade", "Will", "Violent", "Vampire"]), efficiency(105), useful_rolls("guaranteedCrit"), {"field": "slot", "op": "eq", "value": 4}, {"field": "main_stat.type", "op": "eq", "value": "CD"}],
            [stat("SPD", 10), stat("ATK%", 10)],
            ),
            "priority": 1,
        },
        {
            **group(
            "keep-guaranteed-crit-attack-main",
            "必定暴击输出：六号攻击主属性",
            [sets(["Swift", "Rage", "Fatal", "Blade", "Will", "Violent", "Vampire"]), efficiency(105), useful_rolls("guaranteedCrit"), {"field": "slot", "op": "eq", "value": 6}, {"field": "main_stat.type", "op": "eq", "value": "ATK%"}],
            [stat("SPD", 10), stat("CD", 15)],
            ),
            "priority": 1,
        },
        {
            **group(
                "keep-will-slot2-raid-attack-main",
                "意志二号位攻击主属性（异界突袭输出）",
                [
                    sets(["Will"]),
                    efficiency(100),
                    useful_rolls("attackOutput"),
                    {"field": "slot", "op": "eq", "value": 2},
                    {"field": "main_stat.type", "op": "eq", "value": "ATK%"},
                ],
                [stat("ATK%", 10), stat("CR", 10), stat("CD", 12), stat("SPD", 10)],
                match="at_least",
                min_match=3,
            ),
            "priority": 5,
        },
        {
            **group(
                "keep-will-slot2-raid-tank-main",
                "意志二号位体防主属性（异界突袭前排）",
                [
                    sets(["Will"]),
                    efficiency(100),
                    useful_rolls("support"),
                    {"field": "slot", "op": "eq", "value": 2},
                    {"field": "main_stat.type", "op": "in", "value": ["HP%", "DEF%"]},
                ],
                [stat("HP%", 10), stat("DEF%", 10), stat("RES", 10), stat("SPD", 10), stat("ACC", 10)],
                match="at_least",
                min_match=3,
            ),
            "priority": 5,
        },
        group(
            "keep-fight-support-core",
            "斗志速度三维辅助",
            [sets(["Fight"]), efficiency(105), useful_rolls("support"), stat("SPD", 10)],
            [stat("HP%", 10), stat("DEF%", 10), stat("RES", 10), stat("ACC", 10)],
            match="at_least",
            min_match=1,
        ),
        group(
            "keep-fight-damage-core",
            "斗志攻击输出",
            [sets(["Fight"]), efficiency(105), useful_rolls("attackOutput"), stat("ATK%", 10)],
            [stat("SPD", 10), stat("CR", 10), stat("CD", 12)],
            match="at_least",
            min_match=1,
        ),
        group(
            "keep-intangible-versatile-support",
            "无形百搭补位",
            [sets(["Intangible"]), efficiency(105), useful_rolls("versatile")],
            [stat("SPD", 10), stat("HP%", 10), stat("DEF%", 10), stat("RES", 10), stat("ACC", 10), stat("ATK%", 10), stat("CR", 10), stat("CD", 12)],
            match="at_least",
            min_match=2,
        ),
        group("keep-swift-violent-speed-accuracy", "迅速/暴走/意志高速肉控", [sets(["Swift", "Violent", "Will"]), efficiency(108), useful_rolls("control"), stat("SPD", 15)], [stat("HP%", 10), stat("DEF%", 10), stat("RES", 10), stat("ACC", 10)], match="at_least", min_match=2),
        {
            **group(
                "keep-fatal-attack-core",
                "猛攻攻击输出核心",
                [sets(["Fatal"]), efficiency(108), useful_rolls("attackOutput"), stat("ATK%", 10)],
                [stat("CR", 10), stat("CD", 12), stat("SPD", 10)],
                match="at_least",
                min_match=1,
            ),
            "priority": 10,
        },
        {
            **group(
                "keep-fatal-attack-main-core",
                "猛攻攻击主属性输出核心",
                [sets(["Fatal"]), efficiency(108), useful_rolls("attackOutput"), {"field": "main_stat.type", "op": "eq", "value": "ATK%"}],
                [stat("CR", 10), stat("CD", 12), stat("SPD", 10)],
                match="at_least",
                min_match=2,
            ),
            "priority": 11,
        },
        {
            **group(
                "keep-rage-crit-core",
                "激怒多类型双暴输出核心",
                [sets(["Rage"]), efficiency(108), useful_rolls("scalingOutput")],
                [stat("CR", 10), stat("CD", 12)],
            ),
            "priority": 10,
        },
        {
            **group(
                "keep-rage-crit-main-core",
                "激怒四号暴击主属性输出核心",
                [
                    sets(["Rage"]),
                    efficiency(108),
                    useful_rolls("scalingOutput"),
                    {"field": "slot", "op": "eq", "value": 4},
                    {"field": "main_stat.type", "op": "in", "value": ["CR", "CD"]},
                ],
                [stat("CR", 10), stat("CD", 12), stat("ATK%", 10), stat("DEF%", 10), stat("HP%", 10), stat("SPD", 10)],
                match="at_least",
                min_match=2,
            ),
            "priority": 11,
        },
        group(
            "keep-blade-will-attack-crit",
            "刀刃/意志攻击输出核心",
            [sets(["Blade", "Will"]), efficiency(108), useful_rolls("attackOutput")],
            [stat("ATK%", 10), stat("CR", 10), stat("CD", 12), stat("SPD", 10)],
            match="at_least",
            min_match=2,
        ),
        group("keep-guard-defense-crit-damage", "守护防御加成暴击输出", [sets(["Guard"]), efficiency(105), useful_rolls("defOutput")], [stat("DEF%", 10), stat("CR", 10), stat("CD", 12)], match="at_least", min_match=3),
        group("keep-energy-hp-crit-damage", "祝福体力加成暴击输出", [sets(["Energy"]), efficiency(105), useful_rolls("hpOutput")], [stat("HP%", 10), stat("CR", 10), stat("CD", 12)], match="at_least", min_match=3),
        group("keep-tank-res-speed", "纯肉满抗兼顾攻速", [sets(["Violent", "Will", "Guard", "Energy", "Endure", "Shield", "Nemesis"]), efficiency(105), useful_rolls("tank"), stat("RES", 10)], [stat("HP%", 10), stat("DEF%", 10), stat("SPD", 10)], match="at_least", min_match=2),
        group(
            "keep-damage-hp-def",
            "体力加成暴击/爆伤输出",
            [
                sets(["Violent", "Will", "Rage", "Blade", "Vampire", "Destroy"]),
                efficiency(108),
                useful_rolls("hpOutput"),
                stat("HP%", 10),
            ],
            [stat("CR", 10), stat("CD", 12)],
            match="at_least",
            min_match=1,
        ),
        group(
            "keep-defense-scaler-crit-output",
            "防御加成暴击/爆伤输出",
            [
                sets(["Violent", "Will", "Rage", "Blade", "Vampire", "Destroy"]),
                efficiency(108),
                useful_rolls("defOutput"),
                {"field": "slot", "op": "neq", "value": 1},
                stat("DEF%", 10),
            ],
            [stat("CR", 10), stat("CD", 12)],
            match="at_least",
            min_match=1,
        ),
        group("keep-despair-control", "绝望控制高速命中", [sets(["Despair", "Focus", "Accuracy", "Seal"]), efficiency(105), useful_rolls("control"), stat("SPD", 15), stat("ACC", 15)], [stat("HP%", 10), stat("DEF%", 10), stat("RES", 10)], match="at_least", min_match=1),
        group(
            "keep-slot2-speed-main",
            "二号位速度主属性（实用当前效率80+）",
            [
                {"field": "slot", "op": "eq", "value": 2},
                {"field": "main_stat.type", "op": "eq", "value": "SPD"},
            ],
            [{"field": "efficiency.practicalCurrent", "op": "gte", "value": 80}],
        ),
        group(
            "keep-slot4-crit-main",
            "四号位暴击/爆伤主属性输出胚子",
            [
                {"field": "slot", "op": "eq", "value": 4},
                {"field": "main_stat.type", "op": "in", "value": ["CR", "CD"]},
                efficiency(108),
                useful_rolls("scalingOutput"),
            ],
            [
                substat("SPD", 10), substat("ATK%", 10), substat("HP%", 10),
                substat("DEF%", 10), substat("CR", 10), substat("CD", 12),
            ],
            match="at_least",
            min_match=2,
        ),
        group("keep-ancient-high-quality", "远古高效率", [{"field": "ancient", "op": "eq", "value": True}], [efficiency(110)]),
        group("keep-scarce-sets", "封印/无形高效率", [sets(["Seal", "Intangible"])], [efficiency(105)]),
    ]

    common = [
        {"field": "stars", "op": "gte", "value": 6},
        {"field": "quality", "op": "gte", "value": "Hero"},
    ]

    def keep(group_id: str, name: str, sets: list[str], rules: list[dict[str, Any]], *, min_match: int = 2, extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "id": group_id,
            "name": name,
            "enabled": True,
            "action": "keep",
            "match": "at_least",
            "min_match": min_match,
            "requirements": [
                *common,
                {"field": "set", "op": "in", "value": sets},
                {"field": "efficiency.maxWithGrind", "op": "gte", "value": 95},
                *(extra or []),
            ],
            "rules": rules,
        }

    groups = [
        keep("keep-swift-speed-crit-attack", "迅速高速暴击攻击胚子", ["Swift"], [
            {"field": "substats.CR", "op": "gte", "value": 10},
            {"field": "substats.ATK%", "op": "gte", "value": 10},
            {"field": "substats.CD", "op": "gte", "value": 12},
        ], min_match=1, extra=[{"field": "substats.SPD", "op": "gte", "value": 10}]),
        keep("keep-swift-violent-speed-accuracy", "迅速/暴走高速命中三维", ["Swift", "Violent"], [
            {"field": "substats.ACC", "op": "gte", "value": 10},
            {"field": "substats.HP%", "op": "gte", "value": 10},
            {"field": "substats.DEF%", "op": "gte", "value": 10},
            {"field": "substats.RES", "op": "gte", "value": 10},
        ], min_match=2, extra=[{"field": "substats.SPD", "op": "gte", "value": 10}]),
        keep("keep-rage-fatal-attack-crit", "激怒/猛攻攻击暴击爆伤", ["Rage", "Fatal"], [
            {"field": "substats.ATK%", "op": "gte", "value": 10},
            {"field": "substats.CR", "op": "gte", "value": 10},
            {"field": "substats.CD", "op": "gte", "value": 12},
            {"field": "substats.SPD", "op": "gte", "value": 10},
        ]),
        keep("keep-blade-will-attack-crit", "刀刃/意志攻击暴击爆伤", ["Blade", "Will"], [
            {"field": "substats.ATK%", "op": "gte", "value": 10},
            {"field": "substats.CR", "op": "gte", "value": 10},
            {"field": "substats.CD", "op": "gte", "value": 12},
            {"field": "substats.SPD", "op": "gte", "value": 10},
        ]),
        keep("keep-damage-defense-scaler", "防御加成输出三维暴击", ["Rage", "Fatal", "Blade", "Will", "Violent"], [
            {"field": "substats.DEF%", "op": "gte", "value": 10},
            {"field": "substats.CR", "op": "gte", "value": 10},
            {"field": "substats.CD", "op": "gte", "value": 12},
            {"field": "substats.SPD", "op": "gte", "value": 10},
        ]),
        keep("keep-damage-hp-scaler", "体力加成输出三维暴击", ["Rage", "Fatal", "Blade", "Will", "Violent", "Vampire", "Destroy"], [
            {"field": "substats.HP%", "op": "gte", "value": 10},
            {"field": "substats.CR", "op": "gte", "value": 10},
            {"field": "substats.CD", "op": "gte", "value": 12},
            {"field": "substats.SPD", "op": "gte", "value": 10},
        ]),
        keep("keep-pure-tank-res-speed", "纯肉满抗兼顾攻速", ["Violent", "Will", "Guard", "Energy", "Swift", "Endure"], [
            {"field": "substats.HP%", "op": "gte", "value": 10},
            {"field": "substats.DEF%", "op": "gte", "value": 10},
            {"field": "substats.SPD", "op": "gte", "value": 10},
        ], min_match=2, extra=[{"field": "substats.RES", "op": "gte", "value": 10}]),
        keep("keep-speed-tank-three-stat", "暴走/意志高速三维肉装", ["Violent", "Will", "Swift", "Nemesis", "Shield"], [
            {"field": "substats.HP%", "op": "gte", "value": 10},
            {"field": "substats.DEF%", "op": "gte", "value": 10},
            {"field": "substats.RES", "op": "gte", "value": 10},
            {"field": "substats.ACC", "op": "gte", "value": 10},
        ], min_match=2, extra=[{"field": "substats.SPD", "op": "gte", "value": 10}]),
        keep("keep-despair-control-speed-accuracy", "绝望控制高速命中", ["Despair", "Focus", "Seal"], [
            {"field": "substats.HP%", "op": "gte", "value": 10},
            {"field": "substats.DEF%", "op": "gte", "value": 10},
            {"field": "substats.RES", "op": "gte", "value": 10},
        ], min_match=1, extra=[{"field": "substats.SPD", "op": "gte", "value": 10}, {"field": "substats.ACC", "op": "gte", "value": 10}]),
        keep("keep-vampire-destroy-bruiser", "吸血/覆灭肉输出", ["Vampire", "Destroy", "Nemesis", "Revenge"], [
            {"field": "substats.HP%", "op": "gte", "value": 10},
            {"field": "substats.DEF%", "op": "gte", "value": 10},
            {"field": "substats.ATK%", "op": "gte", "value": 10},
            {"field": "substats.CR", "op": "gte", "value": 10},
            {"field": "substats.CD", "op": "gte", "value": 12},
            {"field": "substats.SPD", "op": "gte", "value": 10},
        ], min_match=3),
        {
            "id": "keep-exceptional-speed",
            "name": "任意套装高速稀缺胚子",
            "enabled": True,
            "action": "keep",
            "match": "all",
            "requirements": common,
            "rules": [
                {"field": "substats.SPD", "op": "gte", "value": 20},
                {"field": "efficiency.maxWithGrind", "op": "gte", "value": 95},
            ],
        },
        {
            "id": "keep-exceptional-efficiency",
            "name": "任意套装高效率胚子",
            "enabled": True,
            "action": "keep",
            "match": "all",
            "requirements": common,
            "rules": [{"field": "efficiency.maxWithGrind", "op": "gte", "value": 110}],
        },
        {
            "id": "keep-slot2-speed-main",
            "name": "二号位速度主属性保护",
            "enabled": True,
            "action": "keep",
            "match": "all",
            "requirements": common,
            "rules": [
                {"field": "slot", "op": "eq", "value": 2},
                {"field": "main_stat.type", "op": "eq", "value": "SPD"},
                {"field": "efficiency.maxWithGrind", "op": "gte", "value": 85},
            ],
        },
        {
            "id": "keep-slot4-crit-main",
            "name": "四号位暴击/爆伤主属性输出胚子",
            "enabled": True,
            "action": "keep",
            "match": "at_least",
            "min_match": 2,
            "requirements": [
                *common,
                {"field": "slot", "op": "eq", "value": 4},
                {"field": "main_stat.type", "op": "in", "value": ["CR", "CD"]},
                {"field": "efficiency.maxWithGrind", "op": "gte", "value": 90},
                {"field": "rolls.scalingOutput", "op": "gte", "value": 3},
            ],
            "rules": [
                {"field": "substats.SPD", "op": "gte", "value": 10},
                {"field": "substats.ATK%", "op": "gte", "value": 10},
                {"field": "substats.HP%", "op": "gte", "value": 10},
                {"field": "substats.DEF%", "op": "gte", "value": 10},
                {"field": "substats.CR", "op": "gte", "value": 10},
                {"field": "substats.CD", "op": "gte", "value": 12},
            ],
        },
        {
            "id": "keep-ancient-hero-plus",
            "name": "远古英雄及以上保护",
            "enabled": True,
            "action": "keep",
            "match": "all",
            "requirements": common,
            "rules": [
                {"field": "ancient", "op": "eq", "value": True},
                {"field": "efficiency.maxWithGrind", "op": "gte", "value": 90},
            ],
        },
        {
            "id": "keep-scarce-sets",
            "name": "封印/无形稀缺套装保护",
            "enabled": True,
            "action": "keep",
            "match": "all",
            "requirements": common,
            "rules": [
                {"field": "set", "op": "in", "value": ["Seal", "Intangible"]},
                {"field": "efficiency.maxWithGrind", "op": "gte", "value": 90},
            ],
        },
        {
            "id": "keep-six-star-hero-plus",
            "name": "保留全部六星英雄及以上（宽松开关）",
            "enabled": False,
            "action": "keep",
            "match": "all",
            "rules": common,
        },
        {
            "id": "sell-low-potential",
            "name": "出售账号后 10% 低潜力符文",
            "enabled": True,
            "action": "sell",
            "match": "all",
            "requirements": common,
            "rules": [{"field": "efficiency.maxWithGrind", "op": "lt", "value": 90}],
        },
    ]
    return groups


def normalize_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade the original flat ``rules`` shape for API/editor consumers."""
    result = dict(config)
    if not isinstance(result.get("groups"), list) and isinstance(result.get("rules"), list):
        result["groups"] = [
            {**rule, "rules": rule.get("rules", rule.get("conditions", []))}
            for rule in result["rules"]
            if isinstance(rule, Mapping)
        ]
        result.pop("rules", None)
    if isinstance(result.get("groups"), list):
        result["groups"] = [
            {**group, "rules": group.get("rules", group.get("conditions", []))}
            for group in result["groups"]
            if isinstance(group, Mapping)
        ]
        for group in result["groups"]:
            group.pop("conditions", None)
    return result


def _migrate_builtin_build_fields(group: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    """Move built-in usage rules from substat-only to total build stats.

    Thresholds and enabled state remain user-controlled. Only fields whose
    current built-in definition uses ``buildStats`` are migrated.
    """
    build_names = {
        str(condition.get("field", "")).split(".", 1)[1]
        for condition in [*source.get("requirements", []), *source.get("rules", [])]
        if isinstance(condition, Mapping)
        and str(condition.get("field", "")).startswith("buildStats.")
    }
    result = deepcopy(group)
    if "priority" in source:
        result["priority"] = source["priority"]
    for key in ("requirements", "rules"):
        migrated = []
        for condition in result.get(key, []) or []:
            if not isinstance(condition, Mapping):
                migrated.append(condition)
                continue
            field = str(condition.get("field", ""))
            name = field.split(".", 1)[1] if field.startswith("substats.") else None
            migrated.append(
                {**condition, "field": f"buildStats.{name}"}
                if name in build_names else condition
            )
        result[key] = migrated
    # These two old groups counted the same main stat twice: once through an
    # explicit main-stat predicate and once through the new build stat.
    if result.get("id") in {"keep-guard-defense-crit-damage", "keep-energy-hp-crit-damage"}:
        result["rules"] = [
            condition for condition in result.get("rules", [])
            if not (
                isinstance(condition, Mapping)
                and condition.get("field") == "main_stat.type"
            )
        ]
    return result


def _migrate_exceptional_efficiency(
    group: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Replace the old grind-potential-only finished-rune rule once."""
    if group.get("id") != "keep-exceptional-efficiency":
        return deepcopy(group)
    fields = {
        str(condition.get("field", ""))
        for condition in group.get("rules", []) or []
        if isinstance(condition, Mapping)
    }
    if "efficiency.maxWithGrind" not in fields or "efficiency.current" in fields:
        return deepcopy(group)
    return {
        **deepcopy(source),
        "enabled": group.get("enabled", True),
    }


def _migrate_blade_will_output(
    group: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Remove tank-only stats from the legacy Blade/Will output rule once."""
    if group.get("id") != "keep-blade-will-attack-crit":
        return deepcopy(group)
    stat_fields = {
        str(condition.get("field", "")).split(".", 1)[-1]
        for condition in group.get("rules", []) or []
        if isinstance(condition, Mapping)
    }
    if stat_fields.isdisjoint({"HP%", "DEF%"}):
        return deepcopy(group)
    return {
        **deepcopy(source),
        "enabled": group.get("enabled", True),
    }


def _migrate_slot2_speed_efficiency(
    group: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Replace the old grind-potential-only slot-2 speed protection once."""
    if group.get("id") != "keep-slot2-speed-main":
        return deepcopy(group)
    fields = {
        str(condition.get("field", ""))
        for condition in [*group.get("requirements", []), *group.get("rules", [])]
        if isinstance(condition, Mapping)
    }
    if "efficiency.maxWithGrind" not in fields:
        return deepcopy(group)
    return {
        **deepcopy(source),
        "enabled": group.get("enabled", True),
    }


def _migrate_slot4_crit_coherence(
    group: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Replace main-stat-only slot-4 protection with a coherent output check."""
    if group.get("id") != "keep-slot4-crit-main":
        return deepcopy(group)
    conditions = [
        condition
        for condition in [*group.get("requirements", []), *group.get("rules", [])]
        if isinstance(condition, Mapping)
    ]
    if any(condition.get("field") == "rolls.scalingOutput" for condition in conditions):
        return deepcopy(group)
    return {
        **deepcopy(source),
        "enabled": group.get("enabled", True),
    }


def _migrate_despair_extreme_offense(
    group: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Allow exceptionally concentrated offensive rolls on Despair runes."""
    if group.get("id") != "keep-single-extreme-offense":
        return deepcopy(group)
    set_conditions = [
        condition
        for condition in group.get("requirements", []) or []
        if isinstance(condition, Mapping) and condition.get("field") == "set"
    ]
    if any(
        "Despair" in (
            condition.get("value", [])
            if isinstance(condition.get("value"), (list, tuple, set))
            else [condition.get("value")]
        )
        for condition in set_conditions
    ):
        return deepcopy(group)
    return {
        **deepcopy(source),
        "enabled": group.get("enabled", True),
    }


def _migrate_reappraisal_innate_stat(
    group: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Require a maximum-roll desirable innate stat for reappraisal groups."""
    if not str(group.get("id", "")).startswith("keep-legend-key-set-reappraisal-"):
        return deepcopy(group)
    conditions = [
        condition
        for condition in [*group.get("requirements", []), *group.get("rules", [])]
        if isinstance(condition, Mapping)
    ]
    if any(condition.get("field") == "innate.maxed" for condition in conditions):
        return deepcopy(group)
    return {**deepcopy(source), "enabled": group.get("enabled", True)}


def _migrate_useful_roll_requirement(
    group: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Add concentration checks to saved built-ins without replacing edits."""
    result = deepcopy(group)
    source_conditions = [
        deepcopy(condition)
        for condition in source.get("requirements", []) or []
        if isinstance(condition, Mapping)
        and str(condition.get("field", "")).startswith("rolls.")
    ]
    if not source_conditions:
        return result
    requirements = result.get("requirements", []) or []
    existing = {
        str(condition.get("field", ""))
        for condition in requirements
        if isinstance(condition, Mapping)
    }
    result["requirements"] = [
        *deepcopy(requirements),
        *(condition for condition in source_conditions if condition.get("field") not in existing),
    ]
    return result


def merge_default_groups(config: Mapping[str, Any], defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Merge built-ins and apply scoped migrations to account-tuned presets."""
    result = normalize_config(config)
    source = normalize_config(defaults or default_config())
    source_by_id = {
        group.get("id"): group
        for group in source.get("groups", [])
        if isinstance(group, Mapping) and group.get("id")
    }
    legacy_extreme_enabled = next(
        (
            group.get("enabled", True)
            for group in result.setdefault("groups", [])
            if isinstance(group, Mapping)
            and group.get("id") == "keep-single-extreme-substat"
        ),
        None,
    )
    groups = [
        group for group in result.setdefault("groups", [])
        if not isinstance(group, Mapping) or group.get("id") not in DEPRECATED_DEFAULT_GROUP_IDS
    ]
    previous_preset = str(result.get("preset", ""))
    migrate_to_top_sixty = (
        previous_preset.startswith("account-tuned-forodo-")
        and previous_preset != TOP_SIXTY_PRESET
    )
    if migrate_to_top_sixty:
        # Explicitly refresh every built-in threshold once while retaining
        # custom groups and each built-in group's enabled state.
        groups = [
            {
                **deepcopy(source_by_id[group.get("id")]),
                "enabled": group.get("enabled", True),
            }
            if isinstance(group, Mapping) and group.get("id") in source_by_id
            else group
            for group in groups
        ]
        result["preset"] = TOP_SIXTY_PRESET
        result["retention_policy"] = deepcopy(source.get("retention_policy", {}))
    elif previous_preset.startswith("account-tuned-"):
        groups = [
            {
                **deepcopy(source_by_id[group.get("id")]),
                "enabled": group.get("enabled", True),
            }
            if isinstance(group, Mapping)
            and group.get("id") in REFRESHED_ACCOUNT_GROUP_IDS
            and group.get("id") in source_by_id
            else group
            for group in groups
        ]
    groups = [
        _migrate_exceptional_efficiency(group, source_by_id[group.get("id")])
        if isinstance(group, Mapping) and group.get("id") == "keep-exceptional-efficiency"
        and group.get("id") in source_by_id
        else group
        for group in groups
    ]
    groups = [
        _migrate_blade_will_output(group, source_by_id[group.get("id")])
        if isinstance(group, Mapping) and group.get("id") == "keep-blade-will-attack-crit"
        and group.get("id") in source_by_id
        else group
        for group in groups
    ]
    groups = [
        _migrate_slot2_speed_efficiency(group, source_by_id[group.get("id")])
        if isinstance(group, Mapping) and group.get("id") == "keep-slot2-speed-main"
        and group.get("id") in source_by_id
        else group
        for group in groups
    ]
    groups = [
        _migrate_slot4_crit_coherence(group, source_by_id[group.get("id")])
        if isinstance(group, Mapping) and group.get("id") == "keep-slot4-crit-main"
        and group.get("id") in source_by_id
        else group
        for group in groups
    ]
    groups = [
        _migrate_despair_extreme_offense(group, source_by_id[group.get("id")])
        if isinstance(group, Mapping) and group.get("id") == "keep-single-extreme-offense"
        and group.get("id") in source_by_id
        else group
        for group in groups
    ]
    groups = [
        _migrate_reappraisal_innate_stat(group, source_by_id[group.get("id")])
        if isinstance(group, Mapping)
        and str(group.get("id", "")).startswith("keep-legend-key-set-reappraisal-")
        and group.get("id") in source_by_id
        else group
        for group in groups
    ]
    groups = [
        _migrate_builtin_build_fields(group, source_by_id[group.get("id")])
        if isinstance(group, Mapping) and group.get("id") in source_by_id
        else group
        for group in groups
    ]
    groups = [
        _migrate_useful_roll_requirement(group, source_by_id[group.get("id")])
        if isinstance(group, Mapping) and group.get("id") in source_by_id
        else group
        for group in groups
    ]
    groups = [
        {**group, "name": TOP_SIXTY_GROUP_NAMES[group.get("id")]}
        if isinstance(group, Mapping) and group.get("id") in TOP_SIXTY_GROUP_NAMES
        else group
        for group in groups
    ]
    result["groups"] = groups
    known = {group.get("id") for group in groups if isinstance(group, Mapping)}
    hp_scaler_enabled = next(
        (
            group.get("enabled", True)
            for group in groups
            if isinstance(group, Mapping) and group.get("id") == "keep-damage-hp-def"
        ),
        None,
    )
    for group in source.get("groups", []):
        if isinstance(group, Mapping) and group.get("id") not in known:
            added = deepcopy(group)
            if (
                group.get("id") in SINGLE_EXTREME_REPLACEMENT_IDS
                and legacy_extreme_enabled is not None
            ):
                added["enabled"] = legacy_extreme_enabled
            if (
                group.get("id") == "keep-defense-scaler-crit-output"
                and hp_scaler_enabled is not None
            ):
                added["enabled"] = hp_scaler_enabled
            groups.append(added)
    return result


def validate_config(config: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(config, Mapping):
        return ["配置必须是对象"]
    if config.get("version") != CONFIG_VERSION:
        errors.append(f"version 必须为 {CONFIG_VERSION}")
    if config.get("fallback", "keep") not in VALID_ACTIONS:
        errors.append("fallback 必须为 keep 或 sell")
    groups = config.get("groups")
    rules = config.get("rules")
    if groups is None and rules is None:
        return errors + ["groups 或 rules 至少需要一个数组"]
    if groups is not None and not isinstance(groups, list):
        errors.append("groups 必须是数组")
    if rules is not None and not isinstance(rules, list):
        errors.append("rules 必须是数组")
    entries = [("groups", item) for item in (groups or [])] + [("rules", item) for item in (rules or [])]
    for index, (collection, rule) in enumerate(entries):
        prefix = f"{collection}[{index}]"
        if not isinstance(rule, Mapping):
            errors.append(f"{prefix} 必须是对象")
            continue
        if not rule.get("id"):
            errors.append(f"{prefix}.id 不能为空")
        if rule.get("action") not in VALID_ACTIONS:
            errors.append(f"{prefix}.action 必须为 keep 或 sell")
        if rule.get("match", "all") not in VALID_MATCHES:
            errors.append(f"{prefix}.match 必须为 all、any 或 at_least")
        if rule.get("match") == "at_least":
            min_match = rule.get("min_match")
            if not isinstance(min_match, int) or min_match < 1:
                errors.append(f"{prefix}.min_match 必须为正整数")
        requirements = rule.get("requirements", [])
        if not isinstance(requirements, list):
            errors.append(f"{prefix}.requirements 必须是数组")
            requirements = []
        condition_key = "rules" if "rules" in rule else "conditions"
        conditions = rule.get(condition_key, [])
        if not isinstance(conditions, list):
            errors.append(f"{prefix}.{condition_key} 必须是数组")
            continue
        if not conditions:
            errors.append(f"{prefix}.rules 不能为空")
        for cidx, condition in enumerate([*requirements, *conditions]):
            source_key = "requirements" if cidx < len(requirements) else condition_key
            source_index = cidx if cidx < len(requirements) else cidx - len(requirements)
            cprefix = f"{prefix}.{source_key}[{source_index}]"
            if not isinstance(condition, Mapping):
                errors.append(f"{cprefix} 必须是对象")
                continue
            if not condition.get("field"):
                errors.append(f"{cprefix}.field 不能为空")
            if condition.get("op") not in VALID_OPERATORS:
                errors.append(f"{cprefix}.op 不支持: {condition.get('op')}")
            value = condition.get("value")
            if value is None or value == "" or value == []:
                errors.append(f"{cprefix}.value 不能为空")
    return errors


def _substat_parts(item: Any) -> tuple[str, float, bool] | None:
    """Return canonical type, ungrinded value and gem marker for a substat."""
    try:
        if isinstance(item, Mapping):
            name = stat_name(item.get("type", item.get("stat")))
            value = float(item.get("value", 0) or 0)
            gemmed = bool(item.get("gemmed", item.get("swapped", False)))
        else:
            name = stat_name(item[0])
            value = float(item[1] or 0)
            gemmed = bool(item[2]) if len(item) > 2 else False
        return name, value, gemmed
    except (ValueError, TypeError, IndexError, KeyError):
        return None


def _completed_enhancement_rolls(rune: Mapping[str, Any]) -> int:
    """Return how many milestone rolls enhanced an existing substat.

    Lower qualities reveal their missing fourth substat at the later
    milestones.  Those reveals are useful attributes, but they are not one of
    the finite enhancement rolls whose concentration this metric measures.
    """
    try:
        # Legacy editor objects did not always include level.  Treat that
        # shape as a finished rune; live scans and account imports always do.
        raw_level = rune.get("level", 12)
        level = max(0, min(12, int(12 if raw_level is None else raw_level)))
    except (TypeError, ValueError):
        return 0
    quality = _canonical_quality(rune.get("quality"))
    initial_count = INITIAL_SUBSTATS_BY_QUALITY.get(str(quality))
    if initial_count is None:
        # Unknown quality must not gain an optimistic concentration score.
        return 0
    return min(level // 3, initial_count)


def _confirmed_useful_rolls(rune: Mapping[str, Any], useful_stats: set[str]) -> int:
    """Infer the minimum number of enhancement rolls spent on useful stats.

    Per-stat values alone can undercount low rolls.  We therefore also use the
    known total number of enhancement opportunities: if irrelevant attributes
    cannot legally absorb all completed rolls, the remainder is forced onto
    useful attributes.  Grind values are stored separately and ignored.
    A gemmed stat does not prove where the replaced stat's old rolls went, so
    it receives no useful credit and may absorb unknown/wasted rolls.
    """
    total_rolls = _completed_enhancement_rolls(rune)
    if total_rolls <= 0:
        return 0
    ranges = ANCIENT_SUBSTAT_ROLL_RANGES if rune.get("ancient") else SUBSTAT_ROLL_RANGES
    useful_min = 0
    irrelevant_max = 0
    for raw in rune.get("substats", []) or []:
        parts = _substat_parts(raw)
        if parts is None:
            continue
        name, value, gemmed = parts
        if gemmed:
            # The original enhanced value was discarded by the gem.  It is
            # therefore safest to treat those rolls as no longer useful.
            irrelevant_max += total_rolls
            continue
        bounds = ranges.get(name)
        if bounds is None or value <= 0:
            continue
        minimum, maximum = bounds
        min_rolls = max(0, math.ceil((value - maximum) / maximum))
        max_rolls = max(0, math.floor(value / minimum) - 1)
        min_rolls = min(min_rolls, total_rolls)
        max_rolls = min(max(max_rolls, min_rolls), total_rolls)
        if name in useful_stats:
            useful_min += min_rolls
        else:
            irrelevant_max += max_rolls

    forced_useful = max(useful_min, total_rolls - irrelevant_max)
    return max(0, min(total_rolls, forced_useful))


def _lookup(rune: Mapping[str, Any], field: str, efficiency: Mapping[str, Any]) -> Any:
    if field.startswith("efficiency."):
        name = field.split(".", 1)[1]
        # Every rule-level efficiency comparison uses practical efficiency:
        # flat HP/ATK/DEF count at 50%, while speed and all percentage/combat
        # stats retain full weight. The original SWEX fields remain available
        # in calculate_efficiency() for display and external compatibility.
        practical_names = {
            "current": "practicalCurrent",
            "max": "practicalMax",
            "maxHeroGrind": "practicalMaxHeroGrind",
            "maxLegendGrind": "practicalMaxLegendGrind",
            "maxWithGrind": "practicalMaxWithGrind",
        }
        practical = practical_names.get(name)
        if practical and practical in efficiency:
            return efficiency.get(practical)
        return efficiency.get(name)
    if field.startswith("rolls."):
        profile = field.split(".", 1)[1]
        if profile in ROLL_PROFILES:
            return _confirmed_useful_rolls(rune, ROLL_PROFILES[profile])
        try:
            return _confirmed_useful_rolls(rune, {stat_name(profile)})
        except (ValueError, KeyError):
            return 0
    if field.startswith("substats."):
        stat = field.split(".", 1)[1]
        target = stat_name(stat)
        for item in rune.get("substats", []) or []:
            try:
                if stat_name(item.get("type", item.get("stat")) if isinstance(item, Mapping) else item[0]) == target:
                    if isinstance(item, Mapping):
                        return item.get("value", 0) + item.get("grind", item.get("enchanted", 0) or 0)
                    return item[1] + (item[3] if len(item) > 3 else 0)
            except (ValueError, TypeError, IndexError):
                continue
        return 0
    if field.startswith("buildStats."):
        stat = field.split(".", 1)[1]
        target = stat_name(stat)
        values = [_lookup(rune, f"substats.{target}", efficiency)]
        innate = rune.get("innate")
        if isinstance(innate, Mapping):
            try:
                if stat_name(innate.get("type", innate.get("stat"))) == target:
                    values.append(float(innate.get("value", 0) or 0))
            except (ValueError, TypeError):
                pass
        main = rune.get("main_stat")
        if isinstance(main, Mapping):
            try:
                main_type = normalize_stat_type(main.get("type", main.get("stat")))
                if stat_name(main_type) == target:
                    stars = max(1, min(6, int(rune.get("stars", 6) or 6)))
                    values.append(max(
                        float(main.get("value", 0) or 0),
                        float(MAIN_MAX.get(main_type, {}).get(stars, 0)),
                    ))
            except (ValueError, TypeError):
                pass
        return max(values, default=0)
    if field.startswith("baseSubstats."):
        stat = field.split(".", 1)[1]
        target = stat_name(stat)
        for item in rune.get("substats", []) or []:
            try:
                if stat_name(item.get("type", item.get("stat")) if isinstance(item, Mapping) else item[0]) == target:
                    return item.get("value", 0) if isinstance(item, Mapping) else item[1]
            except (ValueError, TypeError, IndexError):
                continue
        return 0
    if field.startswith("innate."):
        target = field.split(".", 1)[1]
        if target == "type":
            innate = rune.get("innate")
            return innate.get("type", innate.get("stat")) if isinstance(innate, Mapping) else None
        if target == "maxed":
            innate = rune.get("innate")
            if not isinstance(innate, Mapping):
                return False
            try:
                name = stat_name(innate.get("type", innate.get("stat")))
                value = float(innate.get("value", 0) or 0)
            except (ValueError, KeyError, TypeError):
                return False
            maxima = ANCIENT_INNATE_MAX_VALUES if rune.get("ancient") else INNATE_MAX_VALUES
            maximum = maxima.get(name)
            return maximum is not None and value >= maximum
        try:
            target = stat_name(target)
        except (ValueError, KeyError, TypeError):
            return None
        innate = rune.get("innate")
        if not isinstance(innate, Mapping):
            return 0
        try:
            return float(innate.get("value", 0) or 0) if stat_name(innate.get("type", innate.get("stat"))) == target else 0
        except (ValueError, KeyError, TypeError):
            return 0
    if field.startswith("hasSubstat."):
        return _lookup(rune, "substats." + field.split(".", 1)[1], efficiency) > 0
    value: Any = rune
    for part in field.split("."):
        if isinstance(value, Mapping):
            value = value.get(part)
        else:
            return None
    return value


def _canonical_quality(value: Any) -> Any:
    if isinstance(value, str):
        return QUALITY_ALIASES.get(value, value)
    return value


def _compare(actual: Any, op: str, expected: Any, field: str = "") -> bool:
    if actual is None:
        return False
    if field == "quality":
        actual = _canonical_quality(actual)
        if isinstance(expected, (list, tuple, set)):
            expected = [_canonical_quality(item) for item in expected]
        else:
            expected = _canonical_quality(expected)
        # Quality is ordered, so gte Hero means Hero or Legend rather than a
        # failed string-to-float conversion.
        if op not in {"in", "not_in", "contains", "eq", "neq"}:
            actual = QUALITY_RANK.get(actual, actual)
            expected = QUALITY_RANK.get(expected, expected)
    elif field in {"main_stat.type", "innate.type"}:
        try:
            actual = stat_name(STAT_ALIASES.get(actual, actual) if isinstance(actual, str) else actual)
            if isinstance(expected, (list, tuple, set)):
                expected = [stat_name(STAT_ALIASES.get(value, value) if isinstance(value, str) else value) for value in expected]
            else:
                expected = stat_name(STAT_ALIASES.get(expected, expected) if isinstance(expected, str) else expected)
        except (ValueError, KeyError):
            pass
    if op in {"in", "not_in"}:
        values = expected if isinstance(expected, (list, tuple, set)) else [expected]
        result = actual in values
        return not result if op == "not_in" else result
    if op == "contains":
        try:
            return expected in actual
        except TypeError:
            return False
    try:
        if op == "eq":
            return actual == expected
        if op == "neq":
            return actual != expected
        left, right = float(actual), float(expected)
        return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]
    except (TypeError, ValueError, KeyError):
        return False


def _normalize_rune(rune: Mapping[str, Any]) -> dict[str, Any]:
    if any(key in rune for key in ("pri_eff", "sec_eff", "prefix_eff")):
        result = normalize_account_rune(rune)
    else:
        result = dict(rune)
    if "quality" not in result and "rank" in result:
        result["quality"] = {1: "Common", 2: "Magic", 3: "Rare", 4: "Hero", 5: "Legend"}.get(result["rank"])
    if "stars" not in result and "class" in result:
        value = result["class"]
        result["stars"] = int(value) - 10 if isinstance(value, int) and value >= 10 else value
    result.setdefault("recognized", True)
    result.setdefault("equipped", False)
    result.setdefault("locked", False)
    return result


def _condition_matches(normalized: Mapping[str, Any], condition: Mapping[str, Any], efficiency: Mapping[str, Any]) -> bool:
    field = str(condition.get("field", ""))
    return _compare(
        _lookup(normalized, field, efficiency),
        condition.get("op", "eq"),
        condition.get("value"),
        field,
    )


def _condition_is_structurally_unavailable(normalized: Mapping[str, Any], condition: Mapping[str, Any]) -> bool:
    """Whether a build stat cannot legally occur on this rune position."""
    field = str(condition.get("field", ""))
    if not field.startswith("buildStats."):
        return False
    try:
        target = stat_name(field.split(".", 1)[1])
        slot = int(normalized.get("slot", 0) or 0)
    except (ValueError, TypeError):
        return False
    return target in FORBIDDEN_SUBSTATS_BY_SLOT.get(slot, set())


def _group_conditions(group: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Read both the new group.rules shape and the original conditions shape.

    A group is intentionally just a named collection of field predicates.  A
    future editor can call them rules without changing the evaluator or old
    config files.
    """
    values = group.get("rules", group.get("conditions", [])) or []
    return [value for value in values if isinstance(value, Mapping) and value.get("field")]


def _group_matches(normalized: Mapping[str, Any], group: Mapping[str, Any], efficiency: Mapping[str, Any]) -> bool:
    values = group.get("rules", group.get("conditions", []))
    if not isinstance(values, list) or not values:
        return False
    conditions = _group_conditions(group)
    # Invalid hand-written JSON must never turn into an empty `all` group,
    # which would otherwise match every rune and could cause a broad sale.
    if len(conditions) != len(values):
        return False
    requirements = group.get("requirements", [])
    if not isinstance(requirements, list) or any(not isinstance(value, Mapping) for value in requirements):
        return False
    if not all(
        _condition_is_structurally_unavailable(normalized, condition)
        or _condition_matches(normalized, condition, efficiency)
        for condition in requirements
    ):
        return False
    available = [
        condition for condition in conditions
        if not _condition_is_structurally_unavailable(normalized, condition)
    ]
    if not available:
        return False
    matches = [_condition_matches(normalized, condition, efficiency) for condition in available]
    match = group.get("match", "all")
    if match == "all":
        return all(matches)
    if match == "at_least":
        required = min(int(group.get("min_match", 1)), len(matches))
        return sum(matches) >= required
    return any(matches)


def _iter_rule_groups(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    groups = config.get("groups")
    if isinstance(groups, list):
        valid = [group for group in groups if isinstance(group, Mapping)]

        def priority(group: Mapping[str, Any]) -> int:
            try:
                return int(group.get("priority", 0))
            except (TypeError, ValueError):
                return 0

        # Stable priority ordering lets strict exceptions override generic
        # keep rules even when a newly added default is merged at the end.
        return sorted(valid, key=priority, reverse=True)
    # Version-1 configurations used a flat rules array.  Treat each old rule
    # as a one-group entry so upgrades do not change matching order.
    rules = config.get("rules")
    return [rule for rule in rules if isinstance(rule, Mapping)] if isinstance(rules, list) else []


_FIELD_LABELS = {
    "stars": "星级", "quality": "品质", "set": "套装", "slot": "位置",
    "level": "强化等级", "ancient": "远古", "main_stat.type": "主属性",
    "innate.type": "先天属性类型", "innate.maxed": "先天属性满值",
    "efficiency.current": "规则当前效率（固定值半权）",
    "efficiency.max": "规则 Max 效率（固定值半权）",
    "efficiency.maxHeroGrind": "规则英雄磨炼效率（固定值半权）",
    "efficiency.maxWithGrind": "规则传说磨炼效率（固定值半权）",
    "efficiency.practicalCurrent": "实用当前效率（固定值半权）",
    "efficiency.practicalMax": "实用 Max 效率（固定值半权）",
    "efficiency.practicalMaxHeroGrind": "实用英雄磨炼效率（固定值半权）",
    "efficiency.practicalMaxWithGrind": "实用传说磨炼效率（固定值半权）",
    "rolls.attackOutput": "攻击输出有效强化",
    "rolls.hpOutput": "体力输出有效强化",
    "rolls.defOutput": "防御输出有效强化",
    "rolls.scalingOutput": "多类型输出有效强化",
    "rolls.guaranteedCrit": "必暴输出有效强化",
    "rolls.hpGuaranteedCrit": "体力必暴有效强化",
    "rolls.speedCritHp": "高速暴击体力有效强化",
    "rolls.bomber": "炸弹有效强化", "rolls.control": "肉控有效强化",
    "rolls.tank": "坦度有效强化", "rolls.support": "辅助有效强化",
    "rolls.damageControl": "输出控制有效强化",
    "rolls.versatile": "泛用有效强化",
}


def _condition_label(field: str) -> str:
    if field in _FIELD_LABELS:
        return _FIELD_LABELS[field]
    for prefix, label in (
        ("buildStats.", "构筑属性"),
        ("substats.", "副属性"),
        ("baseSubstats.", "未磨炼副属性"),
        ("innate.", "先天属性"),
    ):
        if field.startswith(prefix):
            name = field.split(".", 1)[1]
            names = {
                "HP": "体力", "HP%": "体力%", "ATK": "攻击力", "ATK%": "攻击%",
                "DEF": "防御力", "DEF%": "防御%", "SPD": "速度", "CR": "暴击率",
                "CD": "暴击伤害", "RES": "抵抗", "ACC": "命中",
            }
            return f"{label}{names.get(name, name)}"
    return field


def _projected_substat_value(rune: Mapping[str, Any], target: str) -> tuple[float, tuple[int, ...], tuple[int, ...]]:
    """Return the best +12 base value and milestone roles for one substat."""
    stats: dict[str, tuple[float, bool]] = {}
    for raw in rune.get("substats", []) or []:
        parts = _substat_parts(raw)
        if parts is not None:
            name, value, gemmed = parts
            stats[name] = (value, gemmed)
    try:
        level = max(0, min(12, int(rune.get("level", 0) or 0)))
    except (TypeError, ValueError):
        level = 12
    quality = _canonical_quality(rune.get("quality"))
    initial_count = max(INITIAL_SUBSTATS_BY_QUALITY.get(str(quality), len(stats)), min(len(stats), 4))
    ranges = ANCIENT_SUBSTAT_ROLL_RANGES if rune.get("ancient") else SUBSTAT_ROLL_RANGES
    main = rune.get("main_stat") or {}
    innate = rune.get("innate") or {}
    excluded = set(stats)
    for source in (main, innate):
        if isinstance(source, Mapping):
            try:
                excluded.add(stat_name(source.get("type", source.get("stat"))))
            except (ValueError, TypeError, KeyError):
                pass
    try:
        forbidden = FORBIDDEN_SUBSTATS_BY_SLOT.get(int(rune.get("slot", 0) or 0), set())
    except (TypeError, ValueError):
        forbidden = set()
    roll_levels: list[int] = []
    reveal_levels: list[int] = []
    for milestone in (3, 6, 9, 12):
        if milestone <= level:
            continue
        must_reveal = len(stats) < 4 and milestone > initial_count * 3
        if must_reveal:
            reveal_levels.append(milestone)
            available = [
                name for name in ranges
                if name not in excluded and name not in forbidden
            ]
            if not available:
                continue
            revealed = target if target in available else available[0]
            stats[revealed] = (float(ranges[revealed][1]), False)
            excluded.add(revealed)
            continue
        if target in stats and not stats[target][1] and target in ranges:
            stats[target] = (stats[target][0] + float(ranges[target][1]), False)
            roll_levels.append(milestone)
    return stats.get(target, (0.0, False))[0], tuple(roll_levels), tuple(reveal_levels)


def _pretty_value(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value if value is not None else "-")
    return str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")


def _projected_condition_value(
    normalized: Mapping[str, Any], field: str, efficiency: Mapping[str, Any]
) -> tuple[Any, tuple[int, ...], tuple[int, ...]]:
    if field == "efficiency.current":
        return efficiency.get(
            "practicalMax",
            efficiency.get("max", efficiency.get("practicalCurrent", efficiency.get("current"))),
        ), (), ()
    if field == "efficiency.practicalCurrent":
        return efficiency.get("practicalMax", efficiency.get("practicalCurrent")), (), ()
    for prefix in ("substats.", "baseSubstats.", "buildStats."):
        if not field.startswith(prefix):
            continue
        try:
            target = stat_name(field.split(".", 1)[1])
        except (ValueError, TypeError, KeyError):
            return _lookup(normalized, field, efficiency), (), ()
        projected_base, rolls, reveals = _projected_substat_value(normalized, target)
        base_now = _lookup(normalized, f"baseSubstats.{target}", efficiency)
        if prefix == "baseSubstats.":
            return projected_base, rolls, reveals
        grind = max(float(_lookup(normalized, f"substats.{target}", efficiency) or 0) - float(base_now or 0), 0)
        projected_substat = projected_base + grind
        if prefix == "substats.":
            return projected_substat, rolls, reveals
        return max(float(_lookup(normalized, field, efficiency) or 0), projected_substat), rolls, reveals
    return _lookup(normalized, field, efficiency), (), ()


def _display_condition_gap(
    normalized: Mapping[str, Any], condition: Mapping[str, Any], efficiency: Mapping[str, Any]
) -> str:
    field = str(condition.get("field", ""))
    actual = _lookup(normalized, field, efficiency)
    expected = condition.get("value")
    label = _condition_label(field)
    op = condition.get("op", "eq")
    if op in {"gte", "gt"}:
        projected, roll_levels, reveal_levels = _projected_condition_value(normalized, field, efficiency)
        try:
            improved = float(projected) > float(actual)
        except (TypeError, ValueError):
            improved = False
        if improved:
            notes = []
            if roll_levels:
                notes.append("/".join(f"+{level}" for level in roll_levels) + " 可强化该属性")
            if reveal_levels:
                notes.append("/".join(f"+{level}" for level in reveal_levels) + " 需新增副属性")
            note = f"（{'；'.join(notes)}）" if notes else ""
            try:
                reachable = float(projected) >= float(expected)
            except (TypeError, ValueError):
                reachable = False
            relation = "可达到" if reachable else "<"
            if relation == "可达到":
                return (
                    f"{label}当前 {_pretty_value(actual)}，+12 最佳 {_pretty_value(projected)}，"
                    f"可达到 {expected}{note}"
                )
            return (
                f"{label}当前 {_pretty_value(actual)}，+12 最佳 {_pretty_value(projected)} < {expected}{note}"
            )
        return f"{label} {_pretty_value(actual)} < {expected}"
    if op in {"lte", "lt"}:
        return f"{label} {_pretty_value(actual)} > {expected}"
    if op == "in":
        return f"{label} {actual if actual is not None else '-'} 不在 {expected}"
    if op == "not_in":
        return f"{label} {actual if actual is not None else '-'} 位于排除项 {expected}"
    return f"{label} 当前 {actual if actual is not None else '-'}，要求 {expected}"


def _numeric_gap(
    normalized: Mapping[str, Any], condition: Mapping[str, Any], efficiency: Mapping[str, Any]
) -> float:
    try:
        actual = float(_lookup(normalized, str(condition.get("field", "")), efficiency))
        expected = float(condition.get("value"))
        return abs(expected - actual) / max(abs(expected), 1.0)
    except (TypeError, ValueError):
        return 1.0


def explain_unmatched_keep_rules(
    rune: Mapping[str, Any], config: Mapping[str, Any], *, limit: int = 2
) -> list[str]:
    """Describe the closest applicable keep rules for a fallback sale."""
    normalized = _normalize_rune(rune)
    try:
        efficiency = normalized.get("efficiency") or calculate_efficiency(normalized)
    except (ValueError, TypeError, KeyError):
        return []
    immutable = {"stars", "quality", "set", "slot", "ancient", "main_stat.type"}
    candidates: list[tuple[tuple[int, float], str]] = []
    for group in _iter_rule_groups(config):
        if not group.get("enabled", True) or str(group.get("action", "keep")).lower() != "keep":
            continue
        requirements = [c for c in group.get("requirements", []) or [] if isinstance(c, Mapping)]
        # Do not call a different set/slot/main-stat rule a near miss; those
        # properties cannot be fixed by strengthening this rune.
        if any(
            str(condition.get("field", "")) in immutable
            and not _condition_is_structurally_unavailable(normalized, condition)
            and not _condition_matches(normalized, condition, efficiency)
            for condition in requirements
        ):
            continue
        failed = [
            condition for condition in requirements
            if not _condition_is_structurally_unavailable(normalized, condition)
            and not _condition_matches(normalized, condition, efficiency)
        ]
        conditions = [
            condition for condition in _group_conditions(group)
            if not _condition_is_structurally_unavailable(normalized, condition)
        ]
        matched = [condition for condition in conditions if _condition_matches(normalized, condition, efficiency)]
        unmatched = [condition for condition in conditions if condition not in matched]
        match = group.get("match", "all")
        if match == "all":
            failed.extend(unmatched)
        else:
            required = 1 if match == "any" else min(int(group.get("min_match", 1)), len(conditions))
            needed = max(0, required - len(matched))
            unmatched.sort(key=lambda c: _numeric_gap(normalized, c, efficiency))
            failed.extend(unmatched[:needed])
        if not failed:
            continue
        failed.sort(key=lambda c: _numeric_gap(normalized, c, efficiency))
        details = "、".join(
            _display_condition_gap(normalized, condition, efficiency)
            for condition in failed[:3]
        )
        score = (len(failed), sum(_numeric_gap(normalized, c, efficiency) for c in failed))
        candidates.append((score, f"{group.get('name', group.get('id', '未命名规则'))}：{details}"))
    candidates.sort(key=lambda item: item[0])
    return [text for _, text in candidates[:max(0, limit)]]


def evaluate_rune(rune: Mapping[str, Any], config: Mapping[str, Any]) -> Decision:
    """Evaluate one rune.  Selling is never allowed for uncertain/protected data."""

    normalized = _normalize_rune(rune)
    if not normalized.get("recognized", False):
        return Decision("KEEP_UNCERTAIN", "识别不完整，按保护策略保留", uncertain=True)
    if normalized.get("equipped") or normalized.get("locked"):
        return Decision("KEEP", "符文已装备或已锁定")
    if normalized.get("_force_sell_rule") == "sell-attack-slot6-speed-deviated":
        return Decision(
            "SELL",
            "命中规则：攻击套装六号命中/抵抗主属性强化偏离速度",
            "sell-attack-slot6-speed-deviated",
        )
    try:
        efficiency = normalized.get("efficiency") or calculate_efficiency(normalized)
    except (ValueError, TypeError, KeyError) as exc:
        return Decision("KEEP_UNCERTAIN", f"效率字段无法计算: {exc}", uncertain=True)

    for rule in _iter_rule_groups(config):
        if not rule.get("enabled", True):
            continue
        if _group_matches(normalized, rule, efficiency):
            action = rule.get("action", "keep").upper()
            if action == "SELL":
                return Decision("SELL", f"命中规则：{rule.get('name', rule.get('id', '未命名'))}", rule.get("id"))
            return Decision("KEEP", f"命中规则：{rule.get('name', rule.get('id', '未命名'))}", rule.get("id"))
    fallback = str(config.get("fallback", "keep")).upper()
    if fallback == "SELL":
        return Decision("SELL", "未命中规则，执行 fallback=SELL")
    return Decision("KEEP", "未命中规则，执行 fallback=KEEP")
