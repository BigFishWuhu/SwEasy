"""Run second-awakening repeat battles with explicit stop conditions."""

import re
import random
import time
from dataclasses import dataclass, field

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from .selection import SelectAwakeningEntry, _completed
except ImportError:
    from selection import SelectAwakeningEntry, _completed


def awakening_percent(text: str) -> float | None:
    values = re.findall(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*[%％]", text)
    if len(values) != 1:
        return None
    value = float(values[0])
    return value if 0 <= value <= 100 else None


@dataclass
class BattleProgress:
    failures: int = 0
    batch: int = 0
    results: dict = field(default_factory=dict)
    experience: float | None = None

    def record(self, number: int, result: str, experience: float | None = None):
        if not 1 <= number <= 30 or result not in {"victory", "defeat"}:
            raise ValueError("无法确认战斗编号或结果")
        key = (self.batch, number)
        previous = self.results.get(key)
        if previous and previous != result:
            raise ValueError("同一场战斗识别到矛盾结果")
        if not previous:
            self.results[key] = result
            self.failures += int(result == "defeat")
        if experience is not None:
            self.experience = experience

    @property
    def stop_reason(self):
        if self.failures > 3:
            return "战斗失败次数超过3次"
        if self.experience == 100:
            return "二觉经验已满"
        return None


def end_reason(text: str, number: int | None) -> str:
    text = re.sub(r"\s+", "", text)
    if re.search(r"(?:(?:2次|二次)觉醒|二觉).*(?:已满级|已满|达到最大)", text):
        return "experience_full"
    if re.search(r"(?:能量|体力).*(?:不足|不够|耗尽)", text):
        return "energy_empty"
    if re.search(r"战斗失败|挑战失败|战败", text):
        return "defeat"
    if re.search(r"取消|手动|中止|终止", text):
        return "cancelled"
    if number == 30 and re.search(r"完成连续战斗30次|(?:连续战斗|战斗次数).*(?:结束|完成)|已完成", text):
        return "batch_complete"
    return "unknown"


@AgentServer.custom_action("awakening_battle")
class AwakeningBattle(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            return self._run(context)
        except Exception as exc:
            print(f"[awakening] 停止：{exc}", flush=True)
            return False

    def _run(self, context):
        progress = BattleProgress()
        deadline = time.monotonic() + 3600
        pending_start = False
        last_number = 0
        while time.monotonic() < deadline:
            frame = SelectAwakeningEntry._frame(context)
            if frame is None:
                return False
            if self._hit(context, "SWAwakeningResult", frame):
                number = self._number(context, frame)
                reason_text = self._text(context, "SWAwakeningEndReason", frame)
                reason = end_reason(reason_text, number)
                print(f"[awakening] 连战结束：{reason_text}；判定={reason}；累计失败={progress.failures}", flush=True)
                if reason == "experience_full" or progress.experience == 100:
                    print("[awakening] 二觉经验已满，不再执行", flush=True)
                    return True
                if reason == "defeat" and number is not None:
                    progress.record(number, "defeat")
                if reason == "batch_complete" and number is not None:
                    progress.record(number, "victory")
                if progress.failures > 3:
                    print("[awakening] 战斗失败次数超过3次，停止脚本", flush=True)
                    return False
                if reason not in {"batch_complete", "defeat"}:
                    return False
                if reason == "batch_complete" and (progress.experience is None or not 0 <= progress.experience < 100):
                    raise RuntimeError("未确认二觉经验未满，停止续战")
                if number is None or any((progress.batch, index) not in progress.results for index in range(1, number + 1)):
                    raise RuntimeError("战斗记录存在遗漏，不能安全累计失败次数")
                self._click_match(context, "SWAwakeningAgain", frame)
                if not self._wait(context, "SWSelectionTeamReady", 15):
                    raise RuntimeError("再来一次后未进入队伍页")
                progress.batch += 1
                pending_start = False
                last_number = 0
                deadline = time.monotonic() + 3600
                continue
            if self._hit(context, "SWSelectionTeamReady", frame):
                if pending_start:
                    raise RuntimeError("启动后返回队伍页，停止重复点击")
                if not self._hit(context, "SWAwakeningThirtyLabel", frame):
                    raise RuntimeError("未识别到30次连续战斗选项")
                if not self._hit(context, "SWAwakeningThirtyChecked", frame):
                    self._click(context, [1013, 455])
                    if not self._wait(context, "SWAwakeningThirtyChecked", 5):
                        raise RuntimeError("未能确认30次连续战斗已勾选")
                frame = SelectAwakeningEntry._frame(context)
                if frame is None or not all(self._hit(context, name, frame) for name in (
                    "SWSelectionTeamReady", "SWAwakeningThirtyChecked", "SWAwakeningThirtyLabel",
                )):
                    raise RuntimeError("启动前30次连续战斗状态校验失败")
                self._click_match(context, "SWAwakeningStart", frame)
                pending_start = True
                if not self._wait(context, "SWAwakeningRunning", 20):
                    raise RuntimeError("未确认战斗启动，可能有弹窗或能量不足")
                continue
            if self._hit(context, "SWAwakeningRunning", frame):
                number = self._number(context, frame)
                if number is None:
                    raise RuntimeError("无法识别30次连战编号")
                if number < last_number or number > last_number + 1:
                    raise RuntimeError("连战编号不连续，停止以避免遗漏失败次数")
                if number > last_number and last_number and (progress.batch, last_number) not in progress.results:
                    raise RuntimeError("上一场结果未识别，停止以避免遗漏失败次数")
                last_number = number
                result = self._text(context, "SWAwakeningRoundResult", frame)
                if "胜利" in result or "失败" in result:
                    experience = awakening_percent(self._text(context, "SWAwakeningExperience", frame))
                    progress.record(number, "victory" if "胜利" in result else "defeat", experience)
                    if progress.stop_reason:
                        print(f"[awakening] {progress.stop_reason}；累计失败={progress.failures}", flush=True)
                        if progress.failures > 3:
                            self._click_match(context, "SWAwakeningRunning", frame)
                        if not self._wait(context, "SWAwakeningResult", 20):
                            raise RuntimeError("请求停止后未确认连战结束，停止操作")
                        return progress.failures <= 3
                self._pause(context)
                continue
            raise RuntimeError("未知画面或弹窗，停止操作")
        raise RuntimeError("等待单批战斗超过60分钟，停止脚本")

    @staticmethod
    def _text(context, name, frame):
        detail = context.run_recognition(name, frame)
        if detail is None:
            return ""
        return " ".join(str(item.text) for item in detail.all_results if getattr(item, "text", None))

    @staticmethod
    def _hit(context, name, frame):
        detail = context.run_recognition(name, frame)
        return bool(detail and detail.hit)

    def _number(self, context, frame):
        text = self._text(context, "SWAwakeningCount", frame)
        match = re.search(r"(?<!\d)([1-9]|[12]\d|30)\s*/\s*30", text)
        return int(match[1]) if match else None

    @staticmethod
    def _pause(context):
        task = context.run_task("SWAwakeningPoll", {
            "SWAwakeningPoll": {"post_delay": random.randint(450, 650)},
        })
        if not _completed(task, "SWAwakeningPoll"):
            raise RuntimeError("任务已被停止")

    def _wait(self, context, name, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            frame = SelectAwakeningEntry._frame(context)
            if frame is None:
                return False
            if self._hit(context, name, frame):
                return True
            self._pause(context)
        return False

    @staticmethod
    def _click(context, point):
        task = context.run_task("SWAwakeningClick", {
            "SWAwakeningClick": {
                "target": [point[0] + random.randint(-3, 3), point[1] + random.randint(-3, 3)],
                "post_delay": random.randint(700, 950),
            },
        })
        if not _completed(task, "SWAwakeningClick"):
            raise RuntimeError("点击未实际执行")

    def _click_match(self, context, name, frame):
        detail = context.run_recognition(name, frame)
        if not detail or not detail.hit or detail.box is None:
            raise RuntimeError(f"未识别到按钮：{name}")
        box = detail.box
        self._click(context, [box.x + box.w // 2, box.y + box.h // 2])
