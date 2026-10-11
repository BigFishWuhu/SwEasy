"""Conservative rune-upgrade reachability checks for staged filtering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .efficiency import calculate_efficiency, normalize_stat_type, stat_name
from .rules import (
    ANCIENT_SUBSTAT_ROLL_RANGES,
    ATTACK_SETS,
    ATTACK_SLOT6_SPEED_TARGET,
    FORBIDDEN_SUBSTATS_BY_SLOT,
    evaluate_rune,
)

MAX_UPGRADE_LEVEL = 12
# The UI upgrades +0/+3 runes directly to +6. Reachability still models every
# +3 proc internally so skipping the +3 UI stop never loses a possible roll.
UPGRADE_STEPS = (6, 9, 12)
ROLL_MILESTONES = (3, 6, 9, 12)

# Per-proc upper bounds for six-star substats. Reachability deliberately uses
# upper bounds: a rune is sold only when no legal sequence can satisfy a keep rule.
MAX_ROLL = {
    "HP": 375, "HP%": 8, "ATK": 20, "ATK%": 8, "DEF": 20, "DEF%": 8,
    "SPD": 6, "CR": 6, "CD": 7, "RES": 8, "ACC": 8,
}
INITIAL_SUBSTATS_BY_QUALITY = {
    "Common": 0,
    "Magic": 1,
    "Rare": 2,
    "Hero": 3,
    "Legend": 4,
    "普通": 0,
    "魔法": 1,
    "稀有": 2,
    "英雄": 3,
    "传说": 4,
}


@dataclass(frozen=True)
class KeepPath:
    rule_id: str | None
    rule_name: str
    # (milestone level, stat name, whether this milestone revealed the stat)
    steps: tuple[tuple[int, str, bool], ...] = ()


@dataclass(frozen=True)
class UpgradePlan:
    should_upgrade: bool
    paths: tuple[KeepPath, ...] = ()
    explored_states: int = 0
    remaining_milestones: tuple[int, ...] = ()
    reason: str = ""


def next_upgrade_level(level: int) -> int | None:
    """Return the next UI target, starting at +6 and stopping at +12."""
    return next((step for step in UPGRADE_STEPS if level < step), None)


def _next_roll_level(level: int) -> int | None:
    """Return the next substat proc milestone used by reachability search."""
    return next((step for step in ROLL_MILESTONES if level < step), None)


def _stat_key(value: Any) -> str | None:
    try:
        return stat_name(value)
    except (ValueError, TypeError):
        return None


def _stats(rune: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in rune.get("substats", []) or []:
        try:
            if isinstance(item, Mapping):
                name = _stat_key(item.get("type", item.get("stat")))
                value = float(item.get("value", 0)) + float(item.get("grind", item.get("enchanted", 0)) or 0)
            else:
                name = _stat_key(item[0])
                value = float(item[1]) + (float(item[3]) if len(item) > 3 else 0)
            if name:
                result[name] = value
        except (TypeError, ValueError, IndexError):
            continue
    return result


def _is_attack_slot6_acc_res(rune: Mapping[str, Any]) -> bool:
    try:
        main = rune.get("main_stat") or {}
        return (
            str(rune.get("set")) in ATTACK_SETS
            and int(rune.get("slot", 0) or 0) == 6
            and _stat_key(main.get("type", main.get("stat"))) in {"ACC", "RES"}
        )
    except (TypeError, ValueError, AttributeError):
        return False


def _base_speed(rune: Mapping[str, Any]) -> tuple[float, bool]:
    base, _grinds, gemmed = _base_stats_and_grinds(rune)
    return float(base.get("SPD", 0)), "SPD" in gemmed


def attack_slot6_speed_deviated(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Whether an actual staged upgrade spent any crossed proc away from speed."""
    if not _is_attack_slot6_acc_res(before):
        return False
    before_speed, gemmed = _base_speed(before)
    after_speed, _ = _base_speed(after)
    try:
        before_level = max(0, min(12, int(before.get("level", 0) or 0)))
        after_level = max(0, min(12, int(after.get("level", 0) or 0)))
    except (TypeError, ValueError):
        return True
    crossed = sum(before_level < milestone <= after_level for milestone in ROLL_MILESTONES)
    if gemmed or crossed <= 0:
        return True
    minimum_roll = float(ANCIENT_SUBSTAT_ROLL_RANGES["SPD"][0] if before.get("ancient") else 4)
    return after_speed - before_speed < crossed * minimum_roll


def _base_stats_and_grinds(
    rune: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, float], set[str]]:
    base: dict[str, float] = {}
    grinds: dict[str, float] = {}
    gemmed: set[str] = set()
    for item in rune.get("substats", []) or []:
        try:
            if isinstance(item, Mapping):
                name = _stat_key(item.get("type", item.get("stat")))
                value = float(item.get("value", 0))
                grind = float(item.get("grind", item.get("enchanted", 0)) or 0)
                is_gemmed = bool(item.get("gemmed", item.get("swapped", False)))
            else:
                name = _stat_key(item[0])
                value = float(item[1])
                grind = float(item[3]) if len(item) > 3 else 0
                is_gemmed = bool(item[2]) if len(item) > 2 else False
            if name:
                base[name] = value
                grinds[name] = grind
                if is_gemmed:
                    gemmed.add(name)
        except (TypeError, ValueError, IndexError):
            continue
    return base, grinds, gemmed


def _max_roll(rune: Mapping[str, Any], name: str) -> float:
    if rune.get("ancient") and name in ANCIENT_SUBSTAT_ROLL_RANGES:
        return float(ANCIENT_SUBSTAT_ROLL_RANGES[name][1])
    return float(MAX_ROLL[name])


def _potential_stats(rune: Mapping[str, Any], stats: Mapping[str, float]) -> tuple[str, ...]:
    main = rune.get("main_stat", {})
    main_name = _stat_key(main.get("type", main.get("stat"))) if isinstance(main, Mapping) else None
    innate = rune.get("innate", {})
    innate_name = _stat_key(innate.get("type", innate.get("stat"))) if isinstance(innate, Mapping) else None
    excluded = set(stats) | {name for name in (main_name, innate_name) if name}
    try:
        excluded |= FORBIDDEN_SUBSTATS_BY_SLOT.get(int(rune.get("slot", 0) or 0), set())
    except (TypeError, ValueError):
        pass
    return tuple(name for name in MAX_ROLL if name not in excluded)


def _state_key(level: int, stats: Mapping[str, float]) -> tuple[int, tuple[tuple[str, float], ...]]:
    return level, tuple(sorted((name, round(value, 2)) for name, value in stats.items()))


def _initial_substat_count(rune: Mapping[str, Any], stats: Mapping[str, float]) -> int:
    """Return the rune's natural substat count before milestone upgrades."""
    quality_count = INITIAL_SUBSTATS_BY_QUALITY.get(str(rune.get("quality", "")))
    observed_count = min(len(stats), 4)
    if quality_count is None:
        return observed_count
    # OCR/fixtures can expose more stats than the quality normally starts
    # with. Never pretend an already visible stat still needs revealing.
    return max(quality_count, observed_count)


def _rule_name(config: Mapping[str, Any], rule_id: str | None) -> str:
    for group in config.get("groups", []) or []:
        if isinstance(group, Mapping) and group.get("id") == rule_id:
            return str(group.get("name", rule_id or "保留规则"))
    return str(rule_id or "保护策略")


def _is_terminal_sell_rule(config: Mapping[str, Any], rule_id: str | None) -> bool:
    if not rule_id:
        return False
    return any(
        isinstance(group, Mapping)
        and group.get("id") == rule_id
        and str(group.get("action", "keep")).lower() == "sell"
        and bool(group.get("terminal", False))
        for group in config.get("groups", []) or []
    )


def _search_keep_paths(
    rune: Mapping[str, Any], config: Mapping[str, Any], *, limit: int = 3
) -> tuple[tuple[KeepPath, ...], int]:
    """Return a few optimistic keep paths and the number of searched states.

    Existing stats may receive their maximum plausible proc. If a rune has
    fewer than four secondary stats, a future proc may add a new stat instead.
    Treating unknown roll details optimistically prevents false-positive sales.
    """
    try:
        level = int(rune.get("level", 0) or 0)
    except (TypeError, ValueError):
        return (), 0
    if level >= MAX_UPGRADE_LEVEL:
        decision = evaluate_rune(rune, config)
        if decision.action in {"KEEP", "KEEP_UNCERTAIN"}:
            return (KeepPath(decision.rule_id, _rule_name(config, decision.rule_id)),), 1
        return (), 1

    initial, grinds, gemmed = _base_stats_and_grinds(rune)
    initial_substat_count = _initial_substat_count(rune, initial)
    pending = [(level, initial, ())]
    seen: set[tuple[int, tuple[tuple[str, float], ...]]] = set()
    paths: list[KeepPath] = []
    path_keys: set[tuple[str | None, tuple[tuple[int, str, bool], ...]]] = set()
    explored = 0
    while pending:
        current_level, current_stats, steps = pending.pop()
        key = _state_key(current_level, current_stats)
        if key in seen:
            continue
        seen.add(key)
        explored += 1

        candidate = {**rune, "level": current_level, "substats": [
                {
                    "type": name,
                    "value": value,
                    "grind": grinds.get(name, 0),
                    **({"gemmed": True} if name in gemmed else {}),
                }
                for name, value in current_stats.items()
            ]}
        # The source rune carries efficiency for its current real stats. It
        # must not leak into simulated branches after a hypothetical roll.
        candidate["efficiency"] = calculate_efficiency(candidate)
        decision = evaluate_rune(candidate, config)
        if decision.action in {"KEEP", "KEEP_UNCERTAIN"}:
            path_key = (decision.rule_id, steps)
            if path_key not in path_keys:
                path_keys.add(path_key)
                paths.append(KeepPath(
                    decision.rule_id,
                    _rule_name(config, decision.rule_id),
                    steps,
                ))
            if len(paths) >= max(1, limit):
                break
            continue

        next_level = _next_roll_level(current_level)
        if next_level is None:
            continue
        outcomes: list[tuple[dict[str, float], tuple[tuple[int, str, bool], ...]]] = []
        must_reveal_substat = (
            len(current_stats) < 4
            and next_level > initial_substat_count * 3
        )
        if must_reveal_substat:
            for name in _potential_stats(rune, current_stats):
                updated = dict(current_stats)
                updated[name] = _max_roll(rune, name)
                outcomes.append((updated, (*steps, (next_level, name, True))))
        else:
            for name in current_stats:
                if name in gemmed:
                    continue
                updated = dict(current_stats)
                updated[name] += _max_roll(rune, name)
                outcomes.append((updated, (*steps, (next_level, name, False))))
        pending.extend((next_level, outcome, outcome_steps) for outcome, outcome_steps in outcomes)
    return tuple(paths), explored


def analyze_upgrade(rune: Mapping[str, Any], config: Mapping[str, Any], *, limit: int = 3) -> UpgradePlan:
    """Explain whether upgrading is justified and show optimistic keep paths."""
    try:
        level = int(rune.get("level", 0) or 0)
    except (TypeError, ValueError):
        return UpgradePlan(False, reason="强化等级无法解析")
    remaining = tuple(step for step in ROLL_MILESTONES if level < step)
    if level >= MAX_UPGRADE_LEVEL:
        return UpgradePlan(False, remaining_milestones=remaining, reason="已达到 +12 决策等级")
    decision = evaluate_rune(rune, config)
    if decision.uncertain:
        return UpgradePlan(False, remaining_milestones=remaining, reason="识别或计算结果不确定")
    if decision.rule_id == "sell-attack-slot6-speed-deviated":
        return UpgradePlan(
            False,
            explored_states=0,
            remaining_milestones=remaining,
            reason="实际强化未全部落在速度，路径已偏离，立即出售",
        )
    if _is_terminal_sell_rule(config, decision.rule_id):
        return UpgradePlan(
            False,
            explored_states=0,
            remaining_milestones=remaining,
            reason=f"命中立即出售规则：{_rule_name(config, decision.rule_id)}，不尝试强化",
        )
    if _is_attack_slot6_acc_res(rune):
        speed, gemmed = _base_speed(rune)
        if speed <= 0:
            return UpgradePlan(
                False,
                explored_states=0,
                remaining_milestones=remaining,
                reason="攻击套装六号命中/抵抗主属性没有速度，立即出售",
            )
        if gemmed:
            return UpgradePlan(
                False,
                explored_states=0,
                remaining_milestones=remaining,
                reason="速度由宝石转换，后续无法强化速度，立即出售",
            )
        if level >= MAX_UPGRADE_LEVEL:
            return UpgradePlan(
                False,
                explored_states=0,
                remaining_milestones=remaining,
                reason=(
                    f"速度 {speed:g} 已达标"
                    if speed >= ATTACK_SLOT6_SPEED_TARGET
                    else f"已到 +12，速度 {speed:g} < {ATTACK_SLOT6_SPEED_TARGET}，立即出售"
                ),
            )
        maximum = speed + sum(_max_roll(rune, "SPD") for _ in remaining)
        if maximum < ATTACK_SLOT6_SPEED_TARGET:
            return UpgradePlan(
                False,
                explored_states=1,
                remaining_milestones=remaining,
                reason=f"剩余节点全部强化速度也只能达到 {maximum:g} < {ATTACK_SLOT6_SPEED_TARGET}",
            )
        steps = tuple((milestone, "SPD", False) for milestone in remaining)
        return UpgradePlan(
            True,
            (KeepPath(
                "keep-attack-slot6-acc-res-speed-target",
                "攻击套装六号命中/抵抗主属性速度21+",
                steps,
            ),),
            explored_states=1,
            remaining_milestones=remaining,
            reason=f"仅允许速度强化路径；任一节点偏离速度立即出售（目标 {ATTACK_SLOT6_SPEED_TARGET}）",
        )
    if decision.action == "KEEP":
        if decision.rule_id is None:
            return UpgradePlan(False, remaining_milestones=remaining, reason="保护策略保留，不自动强化")
        path = KeepPath(decision.rule_id, _rule_name(config, decision.rule_id))
        return UpgradePlan(
            True,
            (path,),
            explored_states=1,
            remaining_milestones=remaining,
            reason="当前已命中保留规则，继续分阶段强化并复核",
        )
    if decision.rule_id == "sell-fatal-mismatched-main-no-speed":
        return UpgradePlan(
            False,
            remaining_milestones=remaining,
            reason="猛攻主属性与输出用途冲突，直接出售，不尝试强化",
        )
    if decision.rule_id == "sell-flat-slot46-main" and _stats(rune).get("SPD", 0) <= 0:
        return UpgradePlan(False, remaining_milestones=remaining, reason="固定主属性且没有速度，规则要求直接出售")
    paths, explored = _search_keep_paths(rune, config, limit=limit)
    return UpgradePlan(
        bool(paths),
        paths,
        explored_states=explored,
        remaining_milestones=remaining,
        reason=(
            "存在达到保留规则的最佳强化路径"
            if paths
            else "剩余强化的所有合法分配均无法达到保留规则"
        ),
    )


def has_keep_path_to_12(rune: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    """Whether some possible +3 proc sequence can match a keep rule by +12."""
    return bool(_search_keep_paths(rune, config, limit=1)[0])


def should_upgrade(rune: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    """Upgrade a rule-matched keeper or a sell candidate with a keep path.

    Protected and fallback-kept runes have no rule id and are deliberately not
    upgraded. A rune that matches a keep rule before +12 still needs its
    remaining rolls revealed so it can be reassessed at each milestone.
    """
    return analyze_upgrade(rune, config, limit=1).should_upgrade
