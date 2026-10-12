"""Maa custom action for scanning the in-game rune management grid.

The scanner deliberately uses OCR for the detail popup instead of trying to
infer values from the small grid icons.  A failed parse closes the popup and
continues without adding the rune to the sell queue.
"""

from __future__ import annotations

import json
import random
import re
import traceback
from pathlib import Path
from typing import Any, Iterable

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .efficiency import calculate_efficiency
from .rules import evaluate_rune, explain_unmatched_keep_rules, merge_default_groups
from .config_store import DB_PATH, DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH, load_config as load_stored_config
from .upgrade import analyze_upgrade, attack_slot6_speed_deviated, next_upgrade_level

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = DB_PATH

GRID_COLUMNS = (613, 697, 783, 867, 954, 1040, 1125, 1211)
# Only the first three rows are fully inside the 1280x720 interactive viewport.
# The next row is visible at the bottom but clipped; clicks around y=678 are
# ignored even though its icons are rendered. Scroll it into a full row first.
GRID_ROWS = (423, 508, 593)
DETAIL_CLOSE = (1248, 32)
SELL_BUTTON = (1190, 313)
UPGRADE_TARGETS = {6: (394, 432), 9: (475, 432), 12: (556, 432)}

SET_NAMES = {
    "祝福": "Energy", "守护": "Guard", "迅速": "Swift", "刀刃": "Blade",
    "激怒": "Rage", "集中": "Focus", "忍耐": "Endure", "猛攻": "Fatal",
    "绝望": "Despair", "吸血": "Vampire", "暴走": "Violent", "应报": "Nemesis",
    "意志": "Will", "保护": "Shield", "反击": "Revenge", "覆灭": "Destroy",
    "斗志": "Fight", "决心": "Determination", "发扬": "Enhance", "命中": "Accuracy",
    "韧性": "Tolerance", "封印": "Seal", "无形": "Intangible",
}
STAT_NAMES = {
    "体力": "HP", "攻击力": "ATK", "防御力": "DEF", "攻击速度": "SPD",
    "暴击率": "CR", "暴击伤害": "CD", "效果抵抗": "RES", "效果命中": "ACC",
}
QUALITY_NAMES = {"普通": "Common", "魔法": "Magic", "稀有": "Rare", "英雄": "Hero", "传说": "Legend"}
SET_LABELS = {value: key for key, value in SET_NAMES.items()}
QUALITY_LABELS = {value: key for key, value in QUALITY_NAMES.items()}
STAT_LABELS = {
    "HP": "体力", "HP%": "体力", "ATK": "攻击力", "ATK%": "攻击力",
    "DEF": "防御力", "DEF%": "防御力", "SPD": "攻击速度", "CR": "暴击率",
    "CD": "暴击伤害", "RES": "效果抵抗", "ACC": "效果命中",
}
PERCENT_STATS = {"HP%", "ATK%", "DEF%", "CR", "CD", "RES", "ACC"}


class _InteractionStopped(RuntimeError):
    """Raised when Maa accepts a nested task but executes no UI node."""


def _task_action_completed(task: Any, expected_node: str) -> bool:
    """Require proof that a nested pipeline action actually ran.

    After Maa receives ``MaaTaskerPostStop``, ``Context.run_task`` can return a
    task marked as succeeded with an empty node list. Checking status alone
    therefore lets the scanner continue with clicks and swipes that never
    reached the emulator.
    """
    if task is None or not getattr(getattr(task, "status", None), "succeeded", False):
        return False
    try:
        nodes = task.nodes
    except (AttributeError, RuntimeError):
        return False
    return any(
        getattr(node, "name", None) == expected_node
        and bool(getattr(node, "completed", False))
        and getattr(node, "action", None) is not None
        and bool(getattr(node.action, "success", False))
        for node in nodes
    )


def _read_config() -> dict[str, Any]:
    return load_stored_config(
        db_path=DB_PATH,
        legacy_path=LEGACY_CONFIG_PATH,
        default_path=DEFAULT_CONFIG_PATH,
    )


def _box_xy(value: Any) -> tuple[int, int]:
    """Read an OCR box from either Maa's list payload or a Rect object."""
    box = getattr(value, "box", value)
    if hasattr(box, "x") and hasattr(box, "y"):
        return int(box.x), int(box.y)
    if isinstance(box, (list, tuple)) and len(box) >= 2:
        return int(box[0]), int(box[1])
    return 0, 0


def _texts(detail: Any) -> list[str]:
    results = [
        result for result in (detail.all_results if detail else [])
        if getattr(result, "text", None)
    ]
    # OCR results are confidence-ranked. Rune stats must instead be parsed in
    # visual order because the first stat in the popup is the main stat.
    results.sort(key=lambda result: (_box_xy(result)[1], _box_xy(result)[0]))
    values = [str(result.text).strip() for result in results]
    if not values and detail and detail.best_result:
        text = getattr(detail.best_result, "text", None)
        if text:
            values.append(str(text).strip())
    return values


def _live_recognition(context: Context, name: str) -> Any:
    """Recognize one fresh frame without failing the parent custom action.

    ``Context.run_task`` treats an OCR miss as a failed nested task. Maa then
    cancels later context calls in the same custom action, which is unsuitable
    for optional UI states such as the legend-sale warning. A DirectHit refresh
    checks that the task is still active; an explicit screencap obtains a fresh
    frame because DirectHit does not refresh the cache. ``run_recognition`` reports misses
    through ``detail.hit`` without changing the parent task state.
    """
    refresh = context.run_task("SWRuneRefreshScreen")
    if not _task_action_completed(refresh, "SWRuneRefreshScreen"):
        raise _InteractionStopped("Maa 已停止任务，屏幕刷新未实际执行")
    try:
        screenshot = context.tasker.controller.post_screencap().wait()
    except RuntimeError as exc:
        raise _InteractionStopped("Maa 截图异常，停止识别以避免使用旧画面") from exc
    if not screenshot.succeeded:
        raise _InteractionStopped("Maa 截图失败，停止识别以避免使用旧画面")
    image = screenshot.get()
    if image is None:
        raise _InteractionStopped("Maa 截图为空，停止识别以避免使用旧画面")
    return context.run_recognition(name, image)


def _ocr_live(context: Context, name: str) -> list[str]:
    detail = _live_recognition(context, name)
    if not detail or not detail.hit:
        return []
    return _texts(detail)


def _inventory_unobstructed(context: Context) -> bool:
    """Recognize the usable inventory even when a chat toast hides its title.

    The sort marker alone remains visible behind the rune detail panel, so it
    is not sufficient for a safe click.  The main-stat filter is covered by
    detail/upgrade/modal layers and therefore forms a safe pair with the grid.
    """
    title = bool(_ocr_live(context, "SWRuneInventoryReady"))
    controls = bool(_ocr_live(context, "SWRuneInventoryControlsReady"))
    grid = bool(_ocr_live(context, "SWRuneInventoryGridReady"))
    return (title or controls) and grid


def _joined(lines: Iterable[str]) -> str:
    return " ".join(line for line in lines if line)


def _parse_number(text: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group(0)) if match else None


def _parse_stat(line: str) -> tuple[str, float, float] | None:
    for label, stat in STAT_NAMES.items():
        if label not in line:
            continue
        suffix = line.split(label, 1)[1]
        values = [float(value) for value in re.findall(r"[-+]?\d+(?:\.\d+)?", suffix.replace(",", ""))]
        if values:
            if stat in {"HP", "ATK", "DEF"} and "%" in suffix:
                stat += "%"
            # A refined substat is rendered as e.g. ``攻击力+13%+10%``.
            # Keep the rolled value and grind separate because efficiency and
            # high-roll rules deliberately treat them differently.
            return stat, values[0], values[1] if len(values) > 1 else 0.0
    return None


def parse_detail(
    title_lines: list[str],
    body_lines: list[str],
    *,
    main_line: str | None = None,
    innate_line: str | None = None,
    gemmed_line: str | None = None,
) -> dict[str, Any] | None:
    title = _joined(title_lines)
    body = _joined(body_lines)
    if not title or not body:
        return None
    level_match = re.search(r"\+(\d+)", title)
    slot_match = re.search(r"[（(]\s*([1-6])\s*[）)]", title)
    set_name = next((value for key, value in SET_NAMES.items() if key in title), None)
    quality = next((value for key, value in QUALITY_NAMES.items() if key in body), None)
    if not (slot_match and set_name and quality):
        return None
    slot = int(slot_match.group(1))
    lines = [line.strip() for line in body_lines if line.strip() and "件套" not in line]
    parsed = [(index, line, _parse_stat(line)) for index, line in enumerate(lines)]
    parsed = [(index, line, stat) for index, line, stat in parsed if stat]
    if not parsed:
        return None

    main_index = next((index for index, line, _stat in parsed if main_line and line == main_line), None)
    if main_index is None:
        # Odd slots have fixed main stats. This also repairs confidence-ordered
        # OCR where the visual main-stat line is returned after the substats.
        fixed_main = {1: "ATK", 3: "DEF", 5: "HP"}.get(slot)
        if fixed_main:
            main_index = next((index for index, _line, stat in parsed if stat[0] == fixed_main), None)
    if main_index is None:
        main_index = parsed[0][0]
    main = next(stat for index, _line, stat in parsed if index == main_index)

    remaining = [(index, line, stat) for index, line, stat in parsed if index != main_index]
    innate_index = next((index for index, line, _stat in remaining if innate_line and line == innate_line), None)
    # A rune has at most four secondary stats. A fifth non-main stat is the
    # innate property, which appears before the secondary-stat block.
    if innate_index is None and len(remaining) > 4:
        innate_index = remaining[0][0]
    innate = next((stat for index, _line, stat in remaining if index == innate_index), None)
    substats = []
    for index, line, (stat, value, grind) in remaining:
        if index == innate_index:
            continue
        parsed_stat: dict[str, Any] = {"type": stat, "value": value}
        if grind:
            parsed_stat["grind"] = grind
        if gemmed_line and line == gemmed_line:
            parsed_stat["gemmed"] = True
        substats.append(parsed_stat)
    return {
        "recognized": True,
        # A lock marker is occasionally rendered in the popup title.  The
        # management-page pre-filter hides equipped runes; either signal keeps
        # the rule engine's hard protection active.
        "locked": any("锁" in line for line in (*title_lines, *body_lines)),
        "equipped": False,
        "stars": 6,
        "quality": quality,
        "set": set_name,
        "slot": slot,
        # The game omits "+0" from an unupgraded rune title.
        "level": int(level_match.group(1)) if level_match else 0,
        "main_stat": {"type": main[0], "value": main[1]},
        "innate": {"type": innate[0], "value": innate[1]} if innate else None,
        "substats": substats,
        "source_title": title,
        "source_body": lines,
    }


def _click(context: Context, point: tuple[int, int], wait_ms: int = 350) -> None:
    """Click through Maa's task context with human-scale jitter and pacing."""
    x = max(2, min(1277, point[0] + random.randint(-5, 5)))
    y = max(2, min(717, point[1] + random.randint(-4, 4)))
    base_delay = max(180, wait_ms)
    delay = random.randint(max(180, int(base_delay * 0.85)), int(base_delay * 1.25))
    task = context.run_task("SWRuneHumanClick", pipeline_override={
        "SWRuneHumanClick": {"target": [x, y], "post_delay": delay},
    })
    if not _task_action_completed(task, "SWRuneHumanClick"):
        raise _InteractionStopped(
            f"Maa 已停止任务，点击 ({point[0]},{point[1]}) 未实际执行"
        )


def _is_sell_confirmation_text(value: Any) -> bool:
    """Accept affirmative labels only; a negative button must never sell."""
    return re.sub(r"\s+", "", str(value or "")) in {"是", "确定", "确认", "出售"}


def _recognized_affirmative_button(context: Context, recognition: str) -> Any:
    """Return an OCR-confirmed affirmative button, or ``None``."""
    detail = _live_recognition(context, recognition)
    if not detail or not detail.hit or not detail.best_result or not detail.box:
        return None
    text = getattr(detail.best_result, "text", "")
    if not _is_sell_confirmation_text(text):
        return None
    return detail


def _click_affirmative_button(context: Context, recognition: str) -> bool:
    """Click one specific confirmation dialog's recognized affirmative button."""
    detail = _recognized_affirmative_button(context, recognition)
    if detail is None:
        return False
    box = detail.box
    _click(context, (box.x + box.w // 2, box.y + box.h // 2), 600)
    return True


def _sell_rune(context: Context) -> bool:
    """Sell the open rune through validated ordinary and legend dialogs.

    The legend warning is optional and appears after the ordinary confirmation.
    Every input is followed by recognition of the resulting state.  Unknown
    states fail closed so the scanner cannot click through a modal dialog.
    """
    if not _ocr_live(context, "SWRuneDetailTitle"):
        print("[rune] sale stopped: rune detail is not open", flush=True)
        return False

    _click(context, SELL_BUTTON, 350)
    if not _click_affirmative_button(context, "SWRuneSellConfirmButton"):
        print("[rune] sale stopped: ordinary confirmation was not recognized", flush=True)
        return False

    # The first sale of a protected legend rune can open a second warning.
    # Its affirmative button is lower than the ordinary dialog's button and
    # must never be interpreted as a confirmation for another rune.
    legend_warning = bool(_ocr_live(context, "SWRuneLegendSellWarning"))
    legend_button = _recognized_affirmative_button(context, "SWRuneLegendSellConfirmButton")
    if legend_warning or legend_button is not None:
        if legend_button is None:
            print("[rune] sale stopped: legend warning button was not recognized", flush=True)
            return False
        if not _click_affirmative_button(context, "SWRuneLegendSellConfirmButton"):
            print("[rune] sale stopped: legend warning confirmation was not recognized", flush=True)
            return False

    # Verify that no confirmation layer remains. These are pure recognition
    # probes, so a legitimate miss no longer cancels the parent custom action.
    if _recognized_affirmative_button(context, "SWRuneSellConfirmButton") is not None:
        print("[rune] sale stopped: ordinary confirmation remained open", flush=True)
        return False
    if _ocr_live(context, "SWRuneLegendSellWarning"):
        print("[rune] sale stopped: legend warning remained open", flush=True)
        return False
    if _recognized_affirmative_button(context, "SWRuneLegendSellConfirmButton") is not None:
        print("[rune] sale stopped: legend warning button remained open", flush=True)
        return False

    inventory_ready = _inventory_unobstructed(context)
    if not inventory_ready:
        # The game sometimes leaves the sold rune's detail panel open. Closing
        # it is permitted only after all dialog checks above are clear.
        if not _ocr_live(context, "SWRuneDetailTitle"):
            print("[rune] sale stopped: post-sale screen is unknown", flush=True)
            return False
        _click(context, DETAIL_CLOSE, 250)
        inventory_ready = _inventory_unobstructed(context)

    # The inventory title remains visible behind some dialogs, while parts of
    # the grid remain visible behind the rune detail. Requiring both markers
    # proves that neither layer is intercepting input.
    if not inventory_ready:
        print("[rune] sale stopped: unobstructed inventory controls/grid were not recognized", flush=True)
        return False
    return True


def _click_ocr_button(context: Context, recognition: str, words: tuple[str, ...]) -> bool:
    """Click only a recognized button containing one of the expected labels."""
    detail = _live_recognition(context, recognition)
    if not detail or not detail.hit or not detail.best_result or not detail.box:
        return False
    text = str(getattr(detail.best_result, "text", ""))
    if not any(word in text for word in words):
        return False
    box = detail.box
    _click(context, (box.x + box.w // 2, box.y + box.h // 2), 500)
    return True


def _read_detail(context: Context) -> dict[str, Any] | None:
    title_detail = _live_recognition(context, "SWRuneDetailTitle")
    body_detail = _live_recognition(context, "SWRuneDetailBody")
    if not title_detail or not title_detail.hit or not body_detail or not body_detail.hit:
        return None
    title_lines = _texts(title_detail)
    body_lines = _texts(body_detail)
    body_results = [
        result for result in body_detail.all_results
        if getattr(result, "text", None) and getattr(result, "box", None)
    ]
    main_result = next((
        result for result in body_results
        if 55 <= _box_xy(result)[1] < 105
        and _box_xy(result)[0] >= 840
        and _parse_stat(str(result.text))
    ), None)
    innate_result = next((
        result for result in body_results
        if 100 <= _box_xy(result)[1] < 150
        and _box_xy(result)[0] >= 840
        and _parse_stat(str(result.text))
    ), None)
    gemmed_detail = _live_recognition(context, "SWRuneGemmedMarker")
    gemmed_line = None
    if gemmed_detail and gemmed_detail.hit and gemmed_detail.box:
        marker_box = getattr(gemmed_detail.box, "box", gemmed_detail.box)
        marker_y = (
            float(marker_box.y) + float(marker_box.h) / 2
            if hasattr(marker_box, "y") and hasattr(marker_box, "h")
            else float(marker_box[1]) + float(marker_box[3]) / 2
        )
        candidates = []
        for result in body_results:
            x, y = _box_xy(result)
            box = getattr(result, "box", None)
            height = float(box.h) if hasattr(box, "h") else float(box[3]) if isinstance(box, (list, tuple)) and len(box) >= 4 else 0
            text = str(result.text).strip()
            if 145 <= y < 290 and x >= 740 and "件套" not in text and _parse_stat(text):
                candidates.append((abs((y + height / 2) - marker_y), text))
        if candidates:
            distance, candidate = min(candidates)
            if distance <= 18:
                gemmed_line = candidate
    return parse_detail(
        title_lines,
        body_lines,
        main_line=str(main_result.text).strip() if main_result else None,
        innate_line=str(innate_result.text).strip() if innate_result else None,
        gemmed_line=gemmed_line,
    )


def _display_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")


def _display_stat(stat: Any) -> str:
    if not isinstance(stat, dict):
        return "-"
    stat_type = str(stat.get("type", ""))
    suffix = "%" if stat_type in PERCENT_STATS else ""
    text = f"{STAT_LABELS.get(stat_type, stat_type or '-')}+{_display_number(stat.get('value'))}{suffix}"
    grind = stat.get("grind", stat.get("enchanted", 0)) or 0
    if grind:
        text += f"+{_display_number(grind)}{suffix}"
    if stat.get("gemmed", stat.get("swapped", False)):
        text += "（宝石）"
    return text


def _rune_fingerprint(rune: dict[str, Any]) -> str:
    """Stable OCR fingerprint used only to suppress overlapping page rows."""
    payload = {
        "set": rune.get("set"),
        "slot": rune.get("slot"),
        "quality": rune.get("quality"),
        "level": rune.get("level"),
        "main_stat": rune.get("main_stat"),
        "innate": rune.get("innate"),
        "substats": rune.get("substats", []),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _result_label(next_action: str) -> str:
    if next_action == "KEEP":
        return "保留"
    if next_action == "KEEP_UNCERTAIN":
        return "保留（识别或计算不确定）"
    if next_action == "SELL":
        return "出售"
    if next_action == "WOULD_SELL":
        return "预览：出售"
    if next_action.startswith("WOULD_UPGRADE_TO_"):
        return "预览：强化至 " + next_action.removeprefix("WOULD_UPGRADE_TO_")
    if next_action.startswith("UPGRADE_TO_"):
        return "强化至 " + next_action.removeprefix("UPGRADE_TO_")
    return next_action


def _log_rune(rune: dict[str, Any], decision: Any, next_action: str) -> None:
    efficiency = rune.get("efficiency") or {}
    set_name = SET_LABELS.get(rune.get("set"), rune.get("set", "未知套装"))
    quality = QUALITY_LABELS.get(rune.get("quality"), rune.get("quality", "未知品质"))
    substats = "、".join(_display_stat(stat) for stat in rune.get("substats", [])) or "无"
    innate = _display_stat(rune.get("innate")) if rune.get("innate") else "无"
    if decision is None:
        rule = "无（计算异常）"
    else:
        rule = str(decision.reason).removeprefix("命中规则：")
    rune_text = (
        f"{quality} +{rune.get('level', 0)} {set_name}符文({rune.get('slot', '-')})，"
        f"主属性：{_display_stat(rune.get('main_stat'))}，先天属性：{innate}，副属性：{substats}"
    )
    efficiency_text = (
        f"当前已知 {_display_number(efficiency.get('current'))} / "
        f"Max {_display_number(efficiency.get('max'))} / "
        f"英雄磨炼 {_display_number(efficiency.get('maxHeroGrind'))} / "
        f"传说磨炼 {_display_number(efficiency.get('maxLegendGrind', efficiency.get('maxWithGrind')))} / "
        f"实用当前 {_display_number(efficiency.get('practicalCurrent'))} / "
        f"实用传说磨炼 {_display_number(efficiency.get('practicalMaxLegendGrind', efficiency.get('practicalMaxWithGrind')))}"
    )
    print(
        f"[rune] 符文：{rune_text} | 效率：{efficiency_text} | "
        f"命中规则：{rule} | 结果：{_result_label(next_action)}",
        flush=True,
    )


def _log_upgrade_plan(plan: Any) -> None:
    milestones = "、".join(f"+{level}" for level in plan.remaining_milestones) or "无"
    paths = []
    for path in plan.paths[:3]:
        if path.steps:
            steps = " → ".join(
                f"+{level}{'新增' if revealed else '强化'}{STAT_LABELS.get(stat, stat)}"
                for level, stat, revealed in path.steps
            )
            paths.append(f"{path.rule_name}（{steps}）")
        else:
            paths.append(f"{path.rule_name}（当前已命中，强化后复核）")
    path_text = "；".join(paths) or "无"
    print(
        f"[rune] 强化分析：剩余节点 {milestones} | 搜索状态 {plan.explored_states} | "
        f"可能路径（按单次最高值）：{path_text} | 结论：{plan.reason}",
        flush=True,
    )


def _log_sell_reason(rune: dict[str, Any], decision: Any, plan: Any, config: dict[str, Any]) -> None:
    if decision.rule_id:
        print(f"[rune] 出售原因：{decision.reason}", flush=True)
        return
    milestones = "、".join(f"+{level}" for level in plan.remaining_milestones) or "无"
    searched = f"，已搜索 {plan.explored_states} 种合法状态" if plan.explored_states else ""
    near = explain_unmatched_keep_rules(rune, config, limit=3)
    near_text = "；".join(near) if near else "没有适用于当前套装/位置的接近规则"
    print(
        f"[rune] 出售原因：{decision.reason}；{plan.reason}；剩余节点 {milestones}{searched}；"
        f"最接近规则：{near_text}",
        flush=True,
    )


def _scroll_next_page(context: Context) -> bool:
    # Rows are about 85 px apart. Move exactly three rows so the clipped fourth
    # row becomes the next page's first fully interactive row.
    begin = [900 + random.randint(-12, 12), 660 + random.randint(-8, 8)]
    end = [900 + random.randint(-12, 12), 405 + random.randint(-8, 8)]
    duration = random.randint(550, 780)
    post_delay = random.randint(600, 900)
    print(
        f"[rune] scroll start begin={tuple(begin)} end={tuple(end)} duration={duration}ms",
        flush=True,
    )
    task = context.run_task("SWRuneScrollNextPage", pipeline_override={
        "SWRuneScrollNextPage": {
            "begin": begin,
            "end": end,
            "duration": duration,
            "post_delay": post_delay,
        },
    })
    return _task_action_completed(task, "SWRuneScrollNextPage")


def _upgrade_once(context: Context, rune: dict[str, Any], grid_point: tuple[int, int]) -> dict[str, Any] | None:
    """Perform one confirmed upgrade and reparse the detail popup."""
    def fail(reason: str) -> None:
        print(f"[rune] upgrade stopped: {reason}", flush=True)

    old_level = int(rune.get("level", 0))
    target_level = next_upgrade_level(old_level)
    if target_level is None:
        fail("no next upgrade level")
        return None
    if not _click_ocr_button(context, "SWRuneUpgradeButton", ("强化", "升级")):
        fail("upgrade button not recognized")
        return None
    if not _ocr_live(context, "SWRuneUpgradePage"):
        fail("upgrade page not recognized after opening")
        return None
    _click(context, UPGRADE_TARGETS[target_level], 250)
    if not _click_ocr_button(context, "SWRuneUpgradeExecuteButton", ("强化",)):
        fail(f"execute button not recognized after selecting +{target_level}")
        return None
    if not context.wait_freezes(time=1200, box=(180, 50, 930, 620)):
        fail("upgrade animation did not settle")
        return None

    # The completed-upgrade screen has a destructive "出售" action on the
    # left and a confirmation action on the right. Recognize the page first,
    # then restrict confirmation OCR to the right button so the two actions
    # can never be confused.
    if not _ocr_live(context, "SWRuneUpgradeResultPage"):
        fail("upgrade result page not recognized")
        return None
    if not _click_ocr_button(context, "SWRuneUpgradeResultConfirm", ("确定", "确认", "完成")):
        fail("upgrade result confirmation not recognized")
        return None

    # Depending on the game version/state, closing the regular upgrade page
    # either returns directly to the upgraded rune detail or to the inventory
    # grid. Accept both explicit states; never click from an unknown state.
    if not _click_ocr_button(context, "SWRuneUpgradeCloseButton", ("关闭",)):
        fail("upgrade page close button not recognized")
        return None

    if not _ocr_live(context, "SWRuneDetailTitle"):
        if not _inventory_unobstructed(context):
            fail("neither upgraded rune detail nor unobstructed inventory was recognized after closing")
            return None
        _click(context, grid_point, 400)
    upgraded = _read_detail(context)
    if upgraded is None:
        fail("upgraded rune detail could not be parsed")
        return None
    if int(upgraded.get("level", 0)) < target_level:
        fail(f"parsed level +{upgraded.get('level', 0)} is below target +{target_level}")
        return None
    if upgraded.get("set") != rune.get("set") or upgraded.get("slot") != rune.get("slot"):
        fail("parsed rune set or slot changed after upgrade")
        return None
    return upgraded


def _close_detail_if_open(context: Context) -> None:
    if _ocr_live(context, "SWRuneDetailTitle"):
        _click(context, DETAIL_CLOSE, 100)


@AgentServer.custom_action("scan_rune_inventory")
class RuneInventoryScanner(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        print("[rune] scan action started (fresh screenshot recognition)", flush=True)
        try:
            return self._run(context, argv)
        except _InteractionStopped as exc:
            print(f"[rune] scan stopped: {exc}", flush=True)
            return False
        except Exception as exc:
            # ctypes discards exceptions escaping callbacks and can make Maa
            # report an incomplete custom action as successful.
            print(f"[rune] fatal {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            return False

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = json.loads(argv.custom_action_param or "{}") or {}
        except json.JSONDecodeError:
            params = {}
        dry_run = bool(params.get("dry_run", False))
        config = _read_config()
        allow_upgrade = bool(params.get("upgrade", not dry_run))
        stats = {"scanned": 0, "duplicates": 0, "kept": 0, "sold": 0, "would_sell": 0, "upgraded": 0, "would_upgrade": 0, "uncertain": 0, "skipped": 0}
        seen_runes: set[str] = set()
        max_pages = int(params.get("max_pages", 100))
        consecutive_empty_pages = 0

        for page in range(max_pages):
            inventory_ready = _inventory_unobstructed(context)
            if page == 0 and not inventory_ready and _ocr_live(context, "SWRuneDetailTitle"):
                _click(context, DETAIL_CLOSE, 250)
                inventory_ready = _inventory_unobstructed(context)
            if not inventory_ready:
                print("[rune] inventory page not recognized; stopping", flush=True)
                return False
            print(f"[rune] scanning page {page + 1}", flush=True)
            page_new_count = 0
            repeated_page = False
            repeated_fingerprints = 0
            page_ended_on_empty = False

            for y in GRID_ROWS:
                column_index = 0
                while column_index < len(GRID_COLUMNS):
                    x = GRID_COLUMNS[column_index]
                    # Advance by default so all existing early ``continue``
                    # paths retain their original next-cell behavior. A
                    # successful sale rewinds this cursor below because the
                    # game shifts the next rune into the just-emptied cell.
                    column_index += 1
                    rune = None
                    inventory_visible = False
                    for attempt in range(2):
                        _click(context, (x, y), 400 if attempt else 300)
                        rune = _read_detail(context)
                        if rune is not None:
                            break
                        # Retry only when no popup is open. An unparseable popup
                        # is unsafe and must still stop the task immediately.
                        inventory_visible = _inventory_unobstructed(context)
                        if not inventory_visible:
                            break
                        if attempt == 0:
                            print(f"[rune] cell did not open; retrying ({x},{y})", flush=True)
                    if rune is None:
                        # The inventory is densely packed. An unobstructed
                        # empty cell therefore marks the end of this visible
                        # viewport; do not keep clicking later coordinates.
                        # Scroll the next three rows into place immediately.
                        if inventory_visible:
                            page_ended_on_empty = True
                            print(
                                f"[rune] empty cell reached page={page + 1} cell=({x},{y}); "
                                "current viewport ended, switching page",
                                flush=True,
                            )
                            break
                        stats["uncertain"] += 1
                        print("[rune] detail opened but could not be parsed; stopping", flush=True)
                        print(json.dumps(stats, ensure_ascii=False), flush=True)
                        return False
                    fingerprint = _rune_fingerprint(rune)
                    if fingerprint in seen_runes:
                        stats["duplicates"] += 1
                        _click(context, DETAIL_CLOSE, 100)
                        if page > 0 and page_new_count == 0:
                            repeated_fingerprints += 1
                            if repeated_fingerprints >= 2:
                                repeated_page = True
                                break
                        continue
                    repeated_fingerprints = 0
                    seen_runes.add(fingerprint)
                    page_new_count += 1
                    stats["scanned"] += 1
                    try:
                        rune["efficiency"] = calculate_efficiency(rune)
                        decision = evaluate_rune(rune, config)
                    except (ValueError, TypeError, KeyError) as exc:
                        print(f"[rune] uncertain: {exc}", flush=True)
                        decision = None
                    if decision is None or decision.uncertain:
                        _log_rune(rune, decision, "KEEP_UNCERTAIN")
                        stats["uncertain"] += 1
                        _click(context, DETAIL_CLOSE, 100)
                        continue
                    plan = analyze_upgrade(rune, config)
                    upgrade_reachable = plan.should_upgrade
                    if upgrade_reachable:
                        target = next_upgrade_level(int(rune.get("level", 0)))
                        next_action = f"{'WOULD_UPGRADE' if not allow_upgrade else 'UPGRADE'}_TO_+{target}"
                    elif decision.action == "SELL":
                        next_action = "WOULD_SELL" if dry_run else "SELL"
                    else:
                        next_action = "KEEP"
                    _log_rune(rune, decision, next_action)
                    if upgrade_reachable:
                        _log_upgrade_plan(plan)
                    elif decision.action == "SELL":
                        _log_sell_reason(rune, decision, plan, config)
                    deferred_upgrade = False
                    while int(rune.get("level", 0)) < 12 and plan.should_upgrade:
                        target = next_upgrade_level(int(rune.get("level", 0)))
                        if not allow_upgrade:
                            stats["would_upgrade"] += 1
                            deferred_upgrade = True
                            break
                        print(f"[rune] upgrade start page={page + 1} cell=({x},{y}) target=+{target}", flush=True)
                        upgraded = _upgrade_once(context, rune, (x, y))
                        if upgraded is None:
                            print(json.dumps(stats, ensure_ascii=False), flush=True)
                            return False
                        previous_rune = rune
                        rune = upgraded
                        if attack_slot6_speed_deviated(previous_rune, rune):
                            rune["_force_sell_rule"] = "sell-attack-slot6-speed-deviated"
                            print(
                                "[rune] 强化路径偏离：攻击套装六号命中/抵抗主属性的本阶段强化未全部落在速度，立即出售",
                                flush=True,
                            )
                        stats["upgraded"] += 1
                        try:
                            rune["efficiency"] = calculate_efficiency(rune)
                            decision = evaluate_rune(rune, config)
                        except (ValueError, TypeError, KeyError) as exc:
                            print(f"[rune] uncertain after upgrade: {exc}", flush=True)
                            decision = None
                            break
                        plan = analyze_upgrade(rune, config)
                        can_continue = plan.should_upgrade
                        if can_continue:
                            upcoming = next_upgrade_level(int(rune.get("level", 0)))
                            next_action = f"UPGRADE_TO_+{upcoming}"
                        elif decision.action == "SELL":
                            next_action = "SELL"
                        else:
                            next_action = "KEEP"
                        _log_rune(rune, decision, next_action)
                        if can_continue:
                            _log_upgrade_plan(plan)
                        elif decision.action == "SELL":
                            _log_sell_reason(rune, decision, plan, config)
                    if deferred_upgrade:
                        stats["kept"] += 1
                        _click(context, DETAIL_CLOSE, 100)
                        continue
                    if decision is None or decision.uncertain:
                        stats["uncertain"] += 1
                        _click(context, DETAIL_CLOSE, 100)
                        continue
                    if decision.action == "SELL" and not dry_run:
                        print(f"[rune] sell requested page={page + 1} cell=({x},{y}) rule={decision.rule_id or 'fallback'} reason={decision.reason}", flush=True)
                        if _sell_rune(context):
                            stats["sold"] += 1
                            print(f"[rune] sold page={page + 1} cell=({x},{y})", flush=True)
                            column_index -= 1
                        else:
                            print("[rune] sale state validation failed; stopping", flush=True)
                            print(json.dumps(stats, ensure_ascii=False), flush=True)
                            return False
                    else:
                        if decision.action == "SELL":
                            stats["would_sell"] += 1
                        else:
                            stats["kept"] += 1
                        _click(context, DETAIL_CLOSE, 100)
                if repeated_page or page_ended_on_empty:
                    break
            if repeated_page:
                print("[rune] viewport unchanged after scroll; stopping", flush=True)
                break
            if page_ended_on_empty and page_new_count == 0:
                consecutive_empty_pages += 1
            else:
                consecutive_empty_pages = 0
            if consecutive_empty_pages >= 2:
                print("[rune] two consecutive pages produced no new rune; inventory ended", flush=True)
                break
            if page_new_count == 0 and not page_ended_on_empty:
                print("[rune] page contains only previously scanned runes; stopping", flush=True)
                break
            if page + 1 >= max_pages:
                break
            print(f"[rune] scrolling after page {page + 1}", flush=True)
            if not _scroll_next_page(context):
                print(f"[rune] scroll failed after page {page + 1}; stopping", flush=True)
                print(json.dumps(stats, ensure_ascii=False), flush=True)
                return False
            print(f"[rune] scroll complete after page {page + 1}", flush=True)

        print("[rune] scan complete " + json.dumps(stats, ensure_ascii=False), flush=True)
        return True
