import unittest
from unittest.mock import Mock, patch

from agent.awakening_battle import AwakeningBattle, BattleProgress, awakening_percent, end_reason


class BattlePolicyTests(unittest.TestCase):
    def test_experience_uses_percentage_not_monster_level(self):
        self.assertIsNone(awakening_percent("20 最大等级"))
        self.assertEqual(awakening_percent("+1760觉醒经验值 98.5%"), 98.5)
        self.assertEqual(awakening_percent("100％"), 100)
        self.assertIsNone(awakening_percent("1000%"))
        self.assertIsNone(awakening_percent("50% 100%"))

    def test_fourth_failure_stops_across_batches_without_double_counting(self):
        progress = BattleProgress()
        for batch in range(4):
            progress.batch = batch
            progress.record(1, "defeat")
            progress.record(1, "defeat")
            self.assertEqual(progress.failures, batch + 1)
            self.assertEqual(progress.stop_reason is not None, batch == 3)

    def test_conflicting_result_stops(self):
        progress = BattleProgress()
        progress.record(1, "victory", 89.7)
        with self.assertRaises(ValueError):
            progress.record(1, "defeat")

    def test_end_reason_priority(self):
        self.assertEqual(end_reason("2次觉醒魔灵已满级，连续战斗结束。", 7), "experience_full")
        self.assertEqual(end_reason("二觉经验已满，连续战斗结束", 30), "experience_full")
        self.assertEqual(end_reason("战斗失败，连续战斗结束", 30), "defeat")
        self.assertEqual(end_reason("能量不足，连续战斗结束", 30), "energy_empty")
        self.assertEqual(end_reason("手动终止连续战斗", 30), "cancelled")
        self.assertEqual(end_reason("连续战斗次数已完成", 30), "batch_complete")
        self.assertEqual(end_reason("完成连续战斗30次。 A", 30), "batch_complete")
        self.assertEqual(end_reason("完成连续战斗30次。 A", 12), "unknown")
        self.assertEqual(end_reason("完成连续战斗30次。 A", None), "unknown")
        self.assertEqual(end_reason("完成连续战斗30次。二觉经验已满", 30), "experience_full")
        self.assertEqual(end_reason("连续战斗结束", 12), "unknown")
        self.assertEqual(end_reason("网络异常", 30), "unknown")


class BattleFlowTests(unittest.TestCase):
    def make_runner(self, states):
        runner = AwakeningBattle()
        iterator = iter(states)
        current = {}

        def frame(context):
            current.clear()
            current.update(next(iterator))
            return current.copy()

        runner._hit = Mock(side_effect=lambda context, name, frame: name in frame.get("hits", []))
        runner._text = Mock(side_effect=lambda context, name, frame: frame.get(name, ""))
        runner._number = Mock(side_effect=lambda context, frame: frame.get("number"))
        runner._click_match = Mock()
        runner._click = Mock()
        runner._wait = Mock(return_value=True)
        runner._pause = Mock()
        return runner, patch("agent.awakening_battle.SelectAwakeningEntry._frame", side_effect=frame)

    def test_full_result_never_clicks_again(self):
        runner, frames = self.make_runner([{
            "hits": ["SWAwakeningResult"], "number": 7,
            "SWAwakeningEndReason": "2次觉醒魔灵已满级，连续战斗结束。",
        }])
        with frames:
            self.assertTrue(runner._run(Mock()))
        runner._click_match.assert_not_called()

    def test_three_failure_retries_then_stops_on_fourth(self):
        state = {"hits": ["SWAwakeningResult"], "number": 1,
                 "SWAwakeningEndReason": "战斗失败，连续战斗结束"}
        runner, frames = self.make_runner([state] * 4)
        with frames:
            self.assertFalse(runner._run(Mock()))
        self.assertEqual(runner._click_match.call_count, 3)

    def test_unchecked_thirty_must_be_confirmed_before_start(self):
        team = {"hits": ["SWSelectionTeamReady", "SWAwakeningThirtyLabel"]}
        runner, frames = self.make_runner([team])
        runner._wait.return_value = False
        with frames, self.assertRaisesRegex(RuntimeError, "已勾选"):
            runner._run(Mock())
        runner._click.assert_called_once()
        runner._click_match.assert_not_called()

    def test_checked_thirty_starts_then_stops_at_full(self):
        team = {"hits": ["SWSelectionTeamReady", "SWAwakeningThirtyLabel", "SWAwakeningThirtyChecked"]}
        full = {"hits": ["SWAwakeningResult"], "number": 7,
                "SWAwakeningEndReason": "2次觉醒魔灵已满级，连续战斗结束。"}
        runner, frames = self.make_runner([team, team, full])
        with frames:
            self.assertTrue(runner._run(Mock()))
        runner._click.assert_not_called()
        self.assertEqual(runner._click_match.call_args.args[1], "SWAwakeningStart")

    def test_normal_batch_continues_only_with_known_incomplete_experience(self):
        states = [{"hits": ["SWAwakeningRunning"], "number": number,
                   "SWAwakeningRoundResult": "胜利", "SWAwakeningExperience": "50.5%"}
                  for number in range(1, 31)]
        states += [{"hits": ["SWAwakeningResult"], "number": 30,
                    "SWAwakeningEndReason": "完成连续战斗30次。 A"},
                   {"hits": ["SWAwakeningResult"], "number": 5,
                    "SWAwakeningEndReason": "2次觉醒魔灵已满级，连续战斗结束。"}]
        runner, frames = self.make_runner(states)
        with frames:
            self.assertTrue(runner._run(Mock()))
        runner._click_match.assert_called_once()
        self.assertEqual(runner._click_match.call_args.args[1], "SWAwakeningAgain")

    def test_unknown_reason_never_retries(self):
        runner, frames = self.make_runner([{"hits": ["SWAwakeningResult"], "number": 30,
                                           "SWAwakeningEndReason": "网络异常"}])
        with frames:
            self.assertFalse(runner._run(Mock()))
        runner._click_match.assert_not_called()

    def test_missed_round_result_stops_without_retry(self):
        runner, frames = self.make_runner([{"hits": ["SWAwakeningRunning"], "number": 1},
                                           {"hits": ["SWAwakeningRunning"], "number": 2}])
        with frames, self.assertRaisesRegex(RuntimeError, "上一场结果未识别"):
            runner._run(Mock())
        runner._click_match.assert_not_called()


if __name__ == "__main__":
    unittest.main()
