"""Select a second-awakening branch in the dimension dungeon list."""

import json
import random

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


def _completed(task, name):
    return bool(task and task.status.succeeded and any(
        node.name == name and node.completed and node.action and node.action.success
        for node in task.nodes
    ))


@AgentServer.custom_action("select_awakening_entry")
class SelectAwakeningEntry(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = json.loads(argv.custom_action_param)
            region = params["region"]
            family = params["family"]
            overrides = {
                "SWAwakeningRegionTitle": {"expected": region},
                "SWAwakeningFamily": {"expected": family + "$"},
            }
            context.override_pipeline(overrides)
            for direction in (None, "down", "up", "up", "down", "down"):
                frame = self._frame(context)
                if frame is None or not context.run_recognition("SWAwakeningRegionTitle", frame).hit:
                    return False
                if direction:
                    begin_y, end_y = (595, 280) if direction == "down" else (280, 595)
                    task = context.run_task("SWAwakeningSwipe", {
                        "SWAwakeningSwipe": {
                            "begin": [350 + random.randint(-4, 4), begin_y + random.randint(-4, 4)],
                            "end": [350 + random.randint(-4, 4), end_y + random.randint(-4, 4)],
                            "duration": random.randint(650, 850),
                            "post_delay": random.randint(450, 650),
                        },
                    })
                    if not _completed(task, "SWAwakeningSwipe"):
                        return False
                    frame = self._frame(context)
                    if frame is None or not context.run_recognition("SWAwakeningRegionTitle", frame).hit:
                        return False
                match = context.run_recognition("SWAwakeningFamily", frame)
                if not match.hit:
                    continue
                box = match.box
                if box is None or box.y < 210 or box.y + box.h > 665:
                    continue
                target = [
                    box.x + box.w // 2 + random.randint(-min(4, box.w // 4), min(4, box.w // 4)),
                    box.y + box.h // 2 + random.randint(-min(3, box.h // 4), min(3, box.h // 4)),
                ]
                task = context.run_task("SWAwakeningClick", {
                    "SWAwakeningClick": {"target": target, "post_delay": random.randint(650, 850)},
                })
                if not _completed(task, "SWAwakeningClick"):
                    return False
                frame = self._frame(context)
                if frame is None or not context.run_recognition("SWAwakeningRegionTitle", frame).hit:
                    return False
                selected = context.run_recognition("SWAwakeningSelected", frame, {
                    "SWAwakeningSelected": {"roi": [483, max(200, box.y - 45), 34, box.h + 50]},
                })
                if selected.hit and context.run_recognition("SWAwakeningFamily", frame).hit:
                    print(f"[selection] 已选择二次觉醒：{region} / {family}", flush=True)
                    return True
                return False
            print(f"[selection] 未找到二次觉醒入口：{region} / {family}", flush=True)
            return False
        except Exception as exc:
            print(f"[selection] 二次觉醒入口选择失败：{exc}", flush=True)
            return False

    @staticmethod
    def _frame(context):
        if not _completed(context.run_task("SWAwakeningRefresh"), "SWAwakeningRefresh"):
            return None
        screenshot = context.tasker.controller.post_screencap().wait()
        return screenshot.get() if screenshot.succeeded else None
