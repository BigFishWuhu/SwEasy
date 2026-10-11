import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

import agent.rune.scanner as rune_scanner
import agent.web_server as web_server
import agent.web_action as web_action
from agent.rune.efficiency import calculate_efficiency
from agent.rune.json_import import load_account_runes, normalize_account_rune
from agent.rune.rules import (
    SINGLE_EXTREME_REPLACEMENT_IDS,
    default_config,
    evaluate_rune,
    explain_unmatched_keep_rules,
    merge_default_groups,
    validate_config,
)
from agent.rune.config_store import load_account as load_sqlite_account, load_config as load_sqlite_config, save_account as save_sqlite_account, save_config as save_sqlite_config
from agent.rune.scanner import _box_xy, _is_sell_confirmation_text, _rune_fingerprint, _sell_rune, _texts, _upgrade_once, parse_detail
from agent.rune.upgrade import _potential_stats, analyze_upgrade, attack_slot6_speed_deviated, next_upgrade_level, should_upgrade
from agent.web_launcher import _listener_pids
from agent.web_server import evaluate_account


class RuneEfficiencyTests(unittest.TestCase):
    def test_practical_efficiency_halves_flat_substats_but_keeps_swex_values(self):
        result = calculate_efficiency({
            "stars": 6,
            "level": 12,
            "main_stat": {"type": "SPD", "value": 31},
            "substats": [
                {"type": "DEF", "value": 38},
                {"type": "ATK", "value": 44},
                {"type": "HP%", "value": 11},
                {"type": "RES", "value": 7},
            ],
        })

        self.assertEqual(result["current"], 81.07)
        self.assertEqual(result["maxLegendGrind"], 111.43)
        self.assertEqual(result["practicalCurrent"], 66.43)
        self.assertEqual(result["practicalMaxLegendGrind"], 86.07)

    def test_six_star_speed_rune_matches_swex_shape(self):
        result = calculate_efficiency(
            {
                "stars": 6,
                "main_stat": {"type": "SPD", "value": 42},
                "substats": [
                    {"type": "SPD", "value": 20},
                    {"type": "CR", "value": 15},
                ],
                "level": 12,
            }
        )
        self.assertEqual(result["algorithm"], "swex-rune-card-1.2.1")
        self.assertAlmostEqual(result["current"], 77.38)
        self.assertAlmostEqual(result["max"], 77.38)
        self.assertAlmostEqual(result["maxHeroGrind"], 82.14)
        self.assertAlmostEqual(result["maxLegendGrind"], 83.33)
        self.assertAlmostEqual(result["maxWithGrind"], 83.33)

    def test_ancient_and_innate_are_supported(self):
        result = calculate_efficiency(
            {
                "ancient": True,
                "stars": 6,
                "main_stat": {"type": "ATK%", "value": 63},
                "innate": {"type": "SPD", "value": 6},
                "substats": [{"type": "CR", "value": 20, "grind": 4}],
                "level": 9,
            }
        )
        self.assertGreater(result["maxWithGrind"], result["current"])

    def test_ancient_rune_matches_swex_rune_card_values(self):
        rune = {
            "rune_id": 49662955715,
            "slot_no": 3,
            "rank": 14,
            "class": 16,
            "set_id": 15,
            "upgrade_curr": 9,
            "pri_eff": [5, 94],
            "prefix_eff": [0, 0],
            "sec_eff": [[2, 15, 0, 0], [8, 12, 0, 0], [11, 15, 0, 0]],
            "extra": 14,
        }
        result = calculate_efficiency(rune)
        self.assertEqual(result["current"], 76.79)
        self.assertEqual(result["max"], 83.93)
        self.assertEqual(result["maxHeroGrind"], 94.94)
        self.assertEqual(result["maxLegendGrind"], 98.81)
        self.assertEqual(result["maxWithGrind"], 98.81)


class WebLauncherTests(unittest.TestCase):
    def test_listener_parser_only_returns_exact_listening_port(self):
        output = """
          TCP    127.0.0.1:8765    0.0.0.0:0       LISTENING       38108
          TCP    127.0.0.1:8765    127.0.0.1:51000 TIME_WAIT       0
          TCP    127.0.0.1:8766    0.0.0.0:0       LISTENING       40000
          TCP    [::1]:8765        [::]:0          LISTENING       38108
        """
        self.assertEqual(_listener_pids(8765, output), {38108})

    def test_maa_web_task_uses_visible_agent_action(self):
        root = Path(__file__).resolve().parent.parent
        pipeline = json.loads((root / "assets/resource/pipeline/rune_inventory.json").read_text(encoding="utf-8"))
        task = pipeline["SWRuneOpenWeb"]
        self.assertEqual(task["action"], "Custom")
        self.assertEqual(task["custom_action"], "start_rune_web")
        self.assertEqual(task["custom_action_param"], {"port": 8765, "restart": True})
        self.assertIn("实际访问地址已打印到任务日志", task["focus"]["Node.Action.Succeeded"])

    def test_agent_web_action_relays_actual_address(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout="[web] 符文筛选网页已启动\n[web] 访问地址：http://127.0.0.1:8766/\n",
            stderr="",
        )
        with (
            patch.object(web_action.subprocess, "run", return_value=completed) as run,
            patch("builtins.print") as output,
        ):
            self.assertTrue(web_action.run_web_launcher(port=8765, restart=True))

        command = run.call_args.args[0]
        self.assertEqual(command[0], web_action.sys.executable)
        self.assertIn(str(web_action.LAUNCHER), command)
        self.assertIn("--restart", command)
        messages = "\n".join(str(item.args[0]) for item in output.call_args_list)
        self.assertIn("访问地址：http://127.0.0.1:8766/", messages)

    def test_agent_web_action_rejects_silent_command_success(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(web_action.subprocess, "run", return_value=completed),
            patch("builtins.print") as output,
        ):
            self.assertFalse(web_action.run_web_launcher())

        messages = "\n".join(str(item.args[0]) for item in output.call_args_list)
        self.assertIn("服务未返回经过健康检查的访问地址", messages)

    def test_web_startup_prints_the_access_address(self):
        server = Mock(server_address=("127.0.0.1", 9876))
        with (
            patch.object(web_server, "ThreadingHTTPServer", return_value=server),
            patch.object(web_server, "_publish_runtime"),
            patch.object(web_server.threading, "Thread"),
            patch("builtins.print") as output,
        ):
            self.assertIs(web_server.serve(9876, open_browser=False), server)

        messages = "\n".join(str(item.args[0]) for item in output.call_args_list)
        self.assertIn("访问地址：http://127.0.0.1:9876/", messages)
        self.assertIn("当前端口：9876", messages)


class RuneRulesTests(unittest.TestCase):
    def test_upgrade_milestones_are_staged(self):
        self.assertEqual(next_upgrade_level(0), 6)
        self.assertEqual(next_upgrade_level(3), 6)
        self.assertEqual(next_upgrade_level(6), 9)
        self.assertEqual(next_upgrade_level(9), 12)
        self.assertIsNone(next_upgrade_level(12))

    def test_plus_six_start_still_models_all_four_substat_rolls(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "four-roll-speed-path",
                "name": "四次满速度可达",
                "action": "keep",
                "match": "all",
                "rules": [{"field": "substats.SPD", "op": "gte", "value": 28}],
            }],
        }
        rune = {
            "stars": 6,
            "quality": "Legend",
            "set": "Swift",
            "level": 0,
            "main_stat": {"type": "ATK%", "value": 63},
            "substats": [{"type": "SPD", "value": 4}],
        }

        self.assertTrue(should_upgrade(rune, config))
        self.assertEqual(next_upgrade_level(rune["level"]), 6)

    def test_upgrade_is_only_allowed_when_a_keep_path_exists(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "speed-path",
                "name": "速度达标",
                "action": "keep",
                "match": "all",
                "rules": [{"field": "substats.SPD", "op": "gte", "value": 10}],
            }],
        }
        rune = {
            "stars": 6,
            "quality": "Legend",
            "set": "Swift",
            "level": 0,
            "main_stat": {"type": "ATK%", "value": 63},
            "substats": [{"type": "SPD", "value": 4}],
        }
        self.assertTrue(should_upgrade(rune, config))
        impossible_config = {
            **config,
            "groups": [{
                **config["groups"][0],
                "rules": [{"field": "substats.SPD", "op": "gte", "value": 100}],
            }],
        }
        self.assertFalse(should_upgrade(rune, impossible_config))

    def test_upgrade_analysis_reports_paths_and_exhausted_search(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "speed-path",
                "name": "速度达标",
                "action": "keep",
                "match": "all",
                "rules": [{"field": "substats.SPD", "op": "gte", "value": 10}],
            }],
        }
        rune = {
            "stars": 6,
            "quality": "Legend",
            "set": "Swift",
            "level": 0,
            "main_stat": {"type": "ATK%", "value": 63},
            "substats": [{"type": "SPD", "value": 4}],
        }

        reachable = analyze_upgrade(rune, config)
        self.assertTrue(reachable.should_upgrade)
        self.assertGreater(reachable.explored_states, 0)
        self.assertEqual(reachable.paths[0].rule_name, "速度达标")
        self.assertTrue(reachable.paths[0].steps)

        impossible = deepcopy(config)
        impossible["groups"][0]["rules"][0]["value"] = 100
        exhausted = analyze_upgrade(rune, impossible)
        self.assertFalse(exhausted.should_upgrade)
        self.assertGreater(exhausted.explored_states, 0)
        self.assertIn("所有合法分配", exhausted.reason)

    def test_fallback_sale_explains_the_closest_rule_gap(self):
        rune = {
            "stars": 6,
            "quality": "Hero",
            "level": 12,
            "set": "Fatal",
            "slot": 1,
            "main_stat": {"type": "ATK", "value": 118},
            "substats": [
                {"type": "CR", "value": 21},
                {"type": "CD", "value": 7},
                {"type": "ATK%", "value": 8},
                {"type": "RES", "value": 8},
            ],
            "efficiency": {"current": 82.14, "maxWithGrind": 91.07},
        }
        gaps = explain_unmatched_keep_rules(rune, default_config(), limit=3)

        self.assertTrue(any("猛攻攻击输出核心" in item for item in gaps))
        self.assertTrue(any("构筑属性攻击% 8" in item for item in gaps))

    def test_fallback_gap_explains_hero_reveal_and_only_one_remaining_roll(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "crit-damage-path",
                "name": "输出套装单条极品攻击/暴击/爆伤",
                "action": "keep",
                "match": "all",
                "rules": [{"field": "baseSubstats.CD", "op": "gte", "value": 23}],
            }],
        }
        rune = {
            "stars": 6,
            "quality": "Hero",
            "level": 6,
            "set": "Fatal",
            "slot": 5,
            "main_stat": {"type": "HP", "value": 1080},
            "innate": {"type": "DEF%", "value": 6},
            "substats": [
                {"type": "CD", "value": 11},
                {"type": "SPD", "value": 5},
                {"type": "CR", "value": 10},
            ],
        }

        gaps = explain_unmatched_keep_rules(rune, config, limit=1)

        self.assertEqual(len(gaps), 1)
        self.assertIn("未磨炼副属性暴击伤害当前 11，+12 最佳 18 < 23", gaps[0])
        self.assertIn("+9 可强化该属性；+12 需新增副属性", gaps[0])

    def test_fallback_gap_explains_two_remaining_legend_rolls(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "crit-damage-path",
                "name": "爆伤路径",
                "action": "keep",
                "match": "all",
                "rules": [{"field": "baseSubstats.CD", "op": "gte", "value": 26}],
            }],
        }
        rune = {
            "stars": 6,
            "quality": "Legend",
            "level": 6,
            "set": "Fatal",
            "slot": 5,
            "main_stat": {"type": "HP", "value": 1080},
            "substats": [
                {"type": "CD", "value": 11},
                {"type": "SPD", "value": 5},
                {"type": "CR", "value": 10},
                {"type": "ATK%", "value": 6},
            ],
        }

        gaps = explain_unmatched_keep_rules(rune, config, limit=1)

        self.assertIn("未磨炼副属性暴击伤害当前 11，+12 最佳 25 < 26", gaps[0])
        self.assertIn("+9/+12 可强化该属性", gaps[0])
        self.assertNotIn("需新增副属性", gaps[0])

    def test_hero_rune_plus_twelve_must_reveal_fourth_substat(self):
        specialist_ids = {
            "keep-single-speed-premium-set",
            "keep-single-speed-any-set",
            *SINGLE_EXTREME_REPLACEMENT_IDS,
        }
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in specialist_ids
            ],
        }

        def rune(level, crit):
            result = {
                "recognized": True,
                "stars": 6,
                "quality": "Hero",
                "set": "Swift",
                "slot": 1,
                "level": level,
                "main_stat": {"type": "ATK", "value": 22},
                "innate": {"type": "RES", "value": 5},
                "substats": [
                    {"type": "SPD", "value": 5},
                    {"type": "HP%", "value": 6},
                    {"type": "CR", "value": crit},
                ],
            }
            result["efficiency"] = calculate_efficiency(result)
            return result

        # At +0, three remaining rolls before the +12 reveal can still take
        # speed from 5 to 23, so trying the first +6 stage is justified.
        self.assertTrue(should_upgrade(rune(0, 4), config))
        # At +6 only the +9 proc can improve an existing stat. +12 must add a
        # fourth stat, so neither speed 5 nor crit rate 13 can reach a keeper.
        self.assertFalse(should_upgrade(rune(6, 13), config))
        self.assertFalse(should_upgrade(rune(9, 18), config))

    def test_unfinished_rune_can_reach_current_efficiency_rule_by_next_roll(self):
        rune = {
            "stars": 6,
            "quality": "Hero",
            "level": 9,
            "set": "Determination",
            "slot": 1,
            "main_stat": {"type": "ATK", "value": 94},
            "substats": [
                {"type": "RES", "value": 16},
                {"type": "ACC", "value": 16},
                {"type": "HP%", "value": 16},
                {"type": "ATK%", "value": 8},
            ],
        }
        config = default_config()

        self.assertEqual(evaluate_rune(rune, config).action, "SELL")
        self.assertTrue(should_upgrade(rune, config))

        failed_roll = {**rune, "level": 12}
        self.assertEqual(evaluate_rune(failed_roll, config).action, "SELL")
        successful_roll = deepcopy(failed_roll)
        successful_roll["substats"][2]["value"] = 24
        decision = evaluate_rune(successful_roll, config)
        self.assertEqual(decision.action, "KEEP")
        self.assertEqual(decision.rule_id, "keep-exceptional-efficiency")

    def test_rule_matched_keeper_continues_to_plus_twelve(self):
        config = {
            "version": 1,
            "fallback": "keep",
            "groups": [{
                "id": "keep-crit",
                "name": "暴击胚子",
                "action": "keep",
                "match": "all",
                "rules": [{"field": "substats.CR", "op": "gte", "value": 10}],
            }],
        }
        rune = {
            "stars": 6,
            "quality": "Legend",
            "set": "Fatal",
            "level": 6,
            "main_stat": {"type": "ATK", "value": 70},
            "substats": [{"type": "CR", "value": 15}],
        }
        fallback_keep = {**rune, "substats": [{"type": "CR", "value": 5}]}

        self.assertTrue(should_upgrade(rune, config))
        self.assertFalse(should_upgrade(fallback_keep, config))

    def test_flat_slot_four_and_six_only_keep_legend_speed_23(self):
        config = default_config()

        def rune(*, level, speed=None, quality="Legend", main="HP"):
            substats = [
                {"type": "CR", "value": 6},
                {"type": "CD", "value": 7},
                {"type": "RES", "value": 8},
            ]
            if speed is not None:
                substats.append({"type": "SPD", "value": speed})
            else:
                substats.append({"type": "ACC", "value": 8})
            return {
                "stars": 6,
                "quality": quality,
                "set": "Swift",
                "slot": 4,
                "level": level,
                "main_stat": {"type": main, "value": 160},
                "substats": substats,
                "efficiency": {"maxWithGrind": 120},
            }

        reached = rune(level=12, speed=25)
        promising = rune(level=0, speed=5)
        missed_roll = rune(level=3, speed=4)
        no_speed = rune(level=0)
        hero = rune(level=12, speed=25, quality="Hero")
        percent_main = rune(level=12, main="HP%")

        self.assertEqual(evaluate_rune(reached, config).rule_id, "keep-slot46-legend-speed-25")
        self.assertEqual(evaluate_rune(promising, config).rule_id, "sell-flat-slot46-main")
        self.assertTrue(should_upgrade(promising, config))
        self.assertFalse(should_upgrade(missed_roll, config))
        self.assertFalse(should_upgrade(no_speed, config))
        self.assertEqual(evaluate_rune(no_speed, config).action, "SELL")
        self.assertEqual(evaluate_rune(hero, config).action, "SELL")
        self.assertNotEqual(evaluate_rune(percent_main, config).rule_id, "sell-flat-slot46-main")

        merged = merge_default_groups({
            "version": 1,
            "fallback": "keep",
            "groups": [{
                "id": "generic-keep-first",
                "name": "旧配置中的通用保留规则",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [{"field": "stars", "op": "gte", "value": 6}],
            }],
        })
        self.assertEqual(
            evaluate_rune(no_speed, merged).rule_id,
            "sell-flat-slot46-main",
        )

    def test_slot_four_and_six_legend_speed_path_accepts_any_main_stat(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] == "keep-slot46-legend-speed-25"
            ],
        }

        def rune(speed, *, level=12, quality="Legend", main="CD"):
            return {
                "stars": 6,
                "quality": quality,
                "set": "Rage",
                "slot": 4,
                "level": level,
                "main_stat": {"type": main, "value": 80},
                "substats": [
                    {"type": "SPD", "value": speed},
                    {"type": "CR", "value": 6},
                    {"type": "ATK%", "value": 8},
                    {"type": "HP%", "value": 8},
                ],
            }

        self.assertEqual(evaluate_rune(rune(25), config).rule_id, "keep-slot46-legend-speed-25")
        self.assertTrue(should_upgrade(rune(7, level=0), config))
        self.assertFalse(should_upgrade(rune(16, level=9), config))
        self.assertEqual(evaluate_rune(rune(25, quality="Hero"), config).action, "SELL")

    def test_mismatched_set_main_stats_respect_set_specific_speed_exceptions(self):
        mismatch_ids = {
            "sell-fatal-mismatched-main-no-speed",
            "sell-swift-slot2-tank-main-no-speed",
            "sell-tank-set-attack-main-no-speed",
        }
        config = {
            "version": 1,
            "fallback": "keep",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in mismatch_ids
            ],
        }

        def rune(set_name, slot, main, *, speed=None):
            substats = [{"type": "CR", "value": 12}]
            if speed is not None:
                substats.append({"type": "SPD", "value": speed})
            return {
                "stars": 6,
                "quality": "Legend",
                "set": set_name,
                "slot": slot,
                "level": 12,
                "main_stat": {"type": main, "value": 63},
                "substats": substats,
                "efficiency": {"maxWithGrind": 120},
            }

        cases = [
            ("Fatal", 2, "HP%", "sell-fatal-mismatched-main-no-speed"),
            ("Fatal", 4, "DEF%", "sell-fatal-mismatched-main-no-speed"),
            ("Fatal", 6, "RES", "sell-fatal-mismatched-main-no-speed"),
            ("Swift", 2, "HP%", "sell-swift-slot2-tank-main-no-speed"),
            ("Swift", 2, "DEF%", "sell-swift-slot2-tank-main-no-speed"),
            ("Energy", 2, "ATK%", "sell-tank-set-attack-main-no-speed"),
            ("Guard", 4, "ATK%", "sell-tank-set-attack-main-no-speed"),
            ("Endure", 6, "ATK%", "sell-tank-set-attack-main-no-speed"),
        ]
        for set_name, slot, main, expected in cases:
            with self.subTest(set=set_name, slot=slot, main=main):
                self.assertEqual(evaluate_rune(rune(set_name, slot, main), config).rule_id, expected)

        safe_cases = [
            rune("Fatal", 2, "SPD"),
            rune("Fatal", 4, "CD"),
            rune("Fatal", 6, "ATK%"),
            rune("Swift", 2, "ATK%"),
            rune("Energy", 2, "HP%"),
            rune("Guard", 4, "DEF%"),
            rune("Endure", 6, "RES"),
            rune("Swift", 2, "HP%", speed=5),
            rune("Guard", 4, "ATK%", speed=5),
        ]
        for candidate in safe_cases:
            with self.subTest(set=candidate["set"], slot=candidate["slot"], main=candidate["main_stat"]["type"]):
                self.assertEqual(evaluate_rune(candidate, config).action, "KEEP")

        fatal_with_speed = rune("Fatal", 6, "HP%", speed=17)
        self.assertEqual(
            evaluate_rune(fatal_with_speed, config).rule_id,
            "sell-fatal-mismatched-main-no-speed",
        )

    def test_fatal_defense_main_is_sold_without_trying_another_upgrade(self):
        rune = {
            "stars": 6,
            "quality": "Hero",
            "set": "Fatal",
            "slot": 2,
            "level": 9,
            "main_stat": {"type": "DEF%", "value": 38},
            "substats": [
                {"type": "ATK%", "value": 10},
                {"type": "SPD", "value": 17},
                {"type": "ATK", "value": 19},
            ],
        }
        config = default_config()

        decision = evaluate_rune(rune, config)
        self.assertEqual(decision.action, "SELL")
        self.assertEqual(decision.rule_id, "sell-fatal-mismatched-main-no-speed")
        self.assertFalse(should_upgrade(rune, config))
        plan = analyze_upgrade(rune, config)
        self.assertEqual(plan.explored_states, 0)
        self.assertIn("直接出售", plan.reason)

    def test_legend_swift_violent_will_reappraisal_candidates_respect_main_stats(self):
        rule_ids = {
            "keep-legend-key-set-reappraisal-slot135",
            "keep-legend-key-set-reappraisal-slot2-speed",
            "keep-legend-key-set-reappraisal-slot46-percent",
        }
        config = {
            **default_config(),
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in rule_ids
            ],
        }

        def rune(set_name, slot, main, stat, *, innate=None, innate_value=None, quality="Legend"):
            return {
                "recognized": True,
                "stars": 6,
                "quality": quality,
                "set": set_name,
                "slot": slot,
                "level": 12,
                "main_stat": {"type": main, "value": 63},
                **({"innate": {"type": innate, "value": innate_value if innate_value is not None else 5}} if innate else {}),
                "substats": [
                    {"type": stat, "value": 4},
                    {"type": "HP%", "value": 5},
                    {"type": "DEF%", "value": 5},
                    {"type": "RES", "value": 4},
                ],
            }

        accepted = [
            rune("Swift", 1, "ATK", "RES", innate="CR", innate_value=6),
            rune("Violent", 3, "DEF", "RES", innate="CD", innate_value=7),
            rune("Will", 5, "HP", "RES", innate="ACC", innate_value=8),
            rune("Swift", 2, "SPD", "RES", innate="CR", innate_value=6),
            rune("Violent", 4, "CD", "RES", innate="CD", innate_value=7),
            rune("Will", 6, "HP%", "RES", innate="ACC", innate_value=8),
        ]
        for candidate in accepted:
            with self.subTest(set=candidate["set"], slot=candidate["slot"], stat=candidate["substats"][0]["type"]):
                decision = evaluate_rune(candidate, config)
                self.assertEqual(decision.action, "KEEP")
                self.assertIn(decision.rule_id, rule_ids)

        rejected = [
            # 2 号位只有速度主属性符合该洗炼保留规则。
            rune("Swift", 2, "ATK%", "ACC", innate="CR", innate_value=6),
            rune("Violent", 2, "HP%", "CR", innate="CD", innate_value=7),
            # 4/6 号位固定值主属性明确排除。
            rune("Will", 4, "ATK", "CD", innate="CR", innate_value=6),
            rune("Swift", 6, "HP", "ACC", innate="CD", innate_value=7),
            rune("Violent", 6, "DEF", "CR", innate="ACC", innate_value=8),
            # 套装、品质、副属性方向必须同时符合。
            rune("Rage", 1, "ATK", "ACC", innate="CR", innate_value=6),
            rune("Will", 1, "ATK", "CD", innate="CR", innate_value=6, quality="Hero"),
            rune("Will", 1, "ATK", "ACC"),
            rune("Will", 1, "ATK", "ACC", innate="SPD"),
            rune("Will", 1, "ATK", "ACC", innate="HP%"),
            rune("Will", 1, "ATK", "ACC", innate="CR", innate_value=5),
            rune("Will", 1, "ATK", "ACC", innate="CD", innate_value=6),
            rune("Will", 1, "ATK", "ACC", innate="ACC", innate_value=7),
        ]
        for candidate in rejected:
            with self.subTest(set=candidate["set"], slot=candidate["slot"], main=candidate["main_stat"]["type"]):
                self.assertEqual(evaluate_rune(candidate, config).action, "SELL")

    def test_saved_config_receives_legend_key_set_reappraisal_rules(self):
        merged = merge_default_groups({
            "version": 1,
            "fallback": "sell",
            "groups": [],
        })
        ids = {group["id"] for group in merged["groups"]}
        self.assertTrue({
            "keep-legend-key-set-reappraisal-slot135",
            "keep-legend-key-set-reappraisal-slot2-speed",
            "keep-legend-key-set-reappraisal-slot46-percent",
        }.issubset(ids))

    def test_saved_reappraisal_rules_migrate_to_desirable_innate_stats(self):
        merged = merge_default_groups({
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "keep-legend-key-set-reappraisal-slot135",
                "name": "旧洗炼规则",
                "enabled": False,
                "action": "keep",
                "match": "any",
                "requirements": [
                    {"field": "quality", "op": "eq", "value": "Legend"},
                ],
                "rules": [{
                    "field": "innate.type", "op": "in", "value": ["CR", "CD", "ACC"],
                }],
            }],
        })
        group = next(item for item in merged["groups"] if item["id"] == "keep-legend-key-set-reappraisal-slot135")
        self.assertFalse(group["enabled"])
        self.assertEqual(group["match"], "all")
        self.assertIn(
            {"field": "innate.maxed", "op": "eq", "value": True},
            group["rules"],
        )

    def test_reappraisal_candidates_require_ancient_innate_maximum(self):
        group_id = "keep-legend-key-set-reappraisal-slot135"
        config = {
            **default_config(),
            "fallback": "sell",
            "groups": [group for group in default_config()["groups"] if group["id"] == group_id],
        }
        base = {
            "recognized": True, "stars": 6, "ancient": True,
            "quality": "Legend", "set": "Swift", "slot": 3, "level": 15,
            "main_stat": {"type": "DEF", "value": 160},
            "substats": [{"type": "HP%", "value": 13}],
        }
        for stat, maximum in (("CR", 7), ("CD", 8), ("ACC", 10)):
            with self.subTest(stat=stat):
                below = {**base, "innate": {"type": stat, "value": maximum - 1}}
                maxed = {**base, "innate": {"type": stat, "value": maximum}}
                self.assertEqual(evaluate_rune(below, config).action, "SELL")
                self.assertEqual(evaluate_rune(maxed, config).rule_id, group_id)

    def test_slot4_crit_main_requires_coherent_output_rolls(self):
        group_id = "keep-slot4-crit-main"
        config = {
            **default_config(),
            "fallback": "sell",
            "groups": [group for group in default_config()["groups"] if group["id"] == group_id],
        }
        scattered = {
            "recognized": True, "stars": 6, "quality": "Hero",
            "set": "Swift", "slot": 4, "level": 12,
            "main_stat": {"type": "CD", "value": 59},
            "innate": {"type": "ATK%", "value": 7},
            "substats": [
                {"type": "SPD", "value": 16}, {"type": "ATK", "value": 25},
                {"type": "HP", "value": 301}, {"type": "HP%", "value": 6},
            ],
        }
        coherent = {
            **scattered,
            "substats": [
                {"type": "SPD", "value": 18}, {"type": "ATK%", "value": 18},
                {"type": "CR", "value": 12}, {"type": "HP", "value": 301},
            ],
        }
        self.assertEqual(evaluate_rune(scattered, config).action, "SELL")
        self.assertEqual(evaluate_rune(coherent, config).rule_id, group_id)

    def test_saved_slot4_crit_main_rule_migrates_to_coherent_output_check(self):
        merged = merge_default_groups({
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "keep-slot4-crit-main",
                "name": "旧四号位主属性保护",
                "enabled": False,
                "action": "keep",
                "match": "all",
                "requirements": [
                    {"field": "slot", "op": "eq", "value": 4},
                    {"field": "main_stat.type", "op": "in", "value": ["CR", "CD"]},
                ],
                "rules": [{"field": "efficiency.maxWithGrind", "op": "gte", "value": 90}],
            }],
        })
        group = next(item for item in merged["groups"] if item["id"] == "keep-slot4-crit-main")
        self.assertFalse(group["enabled"])
        self.assertEqual(group["match"], "at_least")
        self.assertEqual(group["min_match"], 2)
        self.assertIn(
            {"field": "rolls.scalingOutput", "op": "gte", "value": 3},
            group["requirements"],
        )

    def test_despair_concentrated_attack_rune_keeps_all_useful_upgrade_paths(self):
        rule_ids = {"keep-despair-damage-output", "keep-single-extreme-offense"}
        config = {
            **default_config(),
            "fallback": "sell",
            "groups": [group for group in default_config()["groups"] if group["id"] in rule_ids],
        }
        rune = {
            "recognized": True, "stars": 6, "quality": "Legend",
            "set": "Despair", "slot": 1, "level": 6,
            "main_stat": {"type": "ATK", "value": 70},
            "substats": [
                {"type": "SPD", "value": 5}, {"type": "ATK%", "value": 23},
                {"type": "CR", "value": 6}, {"type": "CD", "value": 5},
            ],
        }
        plan = analyze_upgrade(rune, config)
        self.assertEqual(evaluate_rune(rune, config).action, "SELL")
        self.assertTrue(plan.should_upgrade)
        self.assertIn("keep-despair-damage-output", {path.rule_id for path in plan.paths})

        speed_roll = {
            **rune, "level": 9,
            "substats": [{**item, "value": 11} if item["type"] == "SPD" else item for item in rune["substats"]],
        }
        all_attack = {
            **rune, "level": 12,
            "substats": [{**item, "value": 39} if item["type"] == "ATK%" else item for item in rune["substats"]],
        }
        self.assertEqual(evaluate_rune(speed_roll, config).rule_id, "keep-despair-damage-output")
        self.assertEqual(evaluate_rune(all_attack, config).rule_id, "keep-single-extreme-offense")

    def test_saved_extreme_offense_rule_adds_despair_set(self):
        merged = merge_default_groups({
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "keep-single-extreme-offense",
                "name": "旧单条极品输出",
                "enabled": False,
                "action": "keep",
                "match": "any",
                "requirements": [{"field": "set", "op": "in", "value": ["Fatal", "Rage"]}],
                "rules": [{"field": "baseSubstats.ATK%", "op": "gte", "value": 27}],
            }],
        })
        group = next(item for item in merged["groups"] if item["id"] == "keep-single-extreme-offense")
        set_rule = next(item for item in group["requirements"] if item["field"] == "set")
        self.assertFalse(group["enabled"])
        self.assertIn("Despair", set_rule["value"])
        self.assertIn("keep-despair-damage-output", {item["id"] for item in merged["groups"]})

    def test_default_config_is_valid(self):
        self.assertEqual(validate_config(default_config()), [])

    def test_rare_blue_rune_is_sold_immediately_without_upgrade_search(self):
        rune = {
            "recognized": True,
            "stars": 6,
            "quality": "Rare",
            "set": "Swift",
            "slot": 2,
            "level": 0,
            "main_stat": {"type": "SPD", "value": 7},
            "substats": [
                {"type": "SPD", "value": 6},
                {"type": "CR", "value": 6},
            ],
        }
        config = default_config()

        decision = evaluate_rune(rune, config)
        self.assertEqual(decision.action, "SELL")
        self.assertEqual(decision.rule_id, "sell-rare-quality-immediate")
        plan = analyze_upgrade(rune, config)
        self.assertFalse(plan.should_upgrade)
        self.assertEqual(plan.explored_states, 0)
        self.assertIn("立即出售", plan.reason)
        self.assertIn("不尝试强化", plan.reason)

        # Terminal blue quality must also override the special attack-slot-6
        # speed path instead of allowing this rune to enter enhancement.
        attack_slot_six = {
            **rune,
            "set": "Fatal",
            "slot": 6,
            "main_stat": {"type": "ACC", "value": 11},
        }
        attack_decision = evaluate_rune(attack_slot_six, config)
        self.assertEqual(attack_decision.rule_id, "sell-rare-quality-immediate")
        attack_plan = analyze_upgrade(attack_slot_six, config)
        self.assertFalse(attack_plan.should_upgrade)
        self.assertEqual(attack_plan.explored_states, 0)

    def test_rare_blue_immediate_sell_keeps_equipped_and_locked_protection(self):
        base = {
            "recognized": True,
            "stars": 6,
            "quality": "Rare",
            "set": "Violent",
            "slot": 1,
            "level": 12,
            "main_stat": {"type": "ATK", "value": 118},
            "substats": [{"type": "SPD", "value": 18}],
        }
        self.assertEqual(evaluate_rune({**base, "equipped": True}, default_config()).action, "KEEP")
        self.assertEqual(evaluate_rune({**base, "locked": True}, default_config()).action, "KEEP")

    def test_attack_set_slot_six_accuracy_resistance_only_allows_speed_path(self):
        def rune(set_name, main, speed=None, *, level=0, gemmed=False):
            substats = [
                {"type": "CR", "value": 6},
                {"type": "CD", "value": 7},
                {"type": "ATK%", "value": 8},
            ]
            if speed is not None:
                substats.append({"type": "SPD", "value": speed, "gemmed": gemmed})
            else:
                substats.append({"type": "HP%", "value": 8})
            return {
                "recognized": True,
                "stars": 6,
                "quality": "Legend",
                "set": set_name,
                "slot": 6,
                "level": level,
                "main_stat": {"type": main, "value": 64},
                "substats": substats,
            }

        config = default_config()
        for set_name in ("Fatal", "Rage", "Blade"):
            for main in ("ACC", "RES"):
                with self.subTest(set=set_name, main=main, case="no-speed"):
                    candidate = rune(set_name, main)
                    decision = evaluate_rune(candidate, config)
                    self.assertEqual(decision.rule_id, "sell-attack-slot6-acc-res-no-speed")
                    plan = analyze_upgrade(candidate, config)
                    self.assertFalse(plan.should_upgrade)
                    self.assertEqual(plan.explored_states, 0)
                    self.assertIn("立即出售", plan.reason)

                with self.subTest(set=set_name, main=main, case="speed-path"):
                    candidate = rune(set_name, main, 5)
                    decision = evaluate_rune(candidate, config)
                    self.assertEqual(decision.rule_id, "sell-attack-slot6-acc-res-speed-below-target")
                    plan = analyze_upgrade(candidate, config)
                    self.assertTrue(plan.should_upgrade)
                    self.assertTrue(plan.paths)
                    self.assertTrue(all(step[1] == "SPD" for step in plan.paths[0].steps))
                    self.assertIn("任一节点偏离速度立即出售", plan.reason)

                with self.subTest(set=set_name, main=main, case="target"):
                    decision = evaluate_rune(rune(set_name, main, 21, level=12), config)
                    self.assertEqual(decision.rule_id, "keep-attack-slot6-acc-res-speed-target")

        missed = rune("Fatal", "ACC", 20, level=12)
        self.assertEqual(
            evaluate_rune(missed, config).rule_id,
            "sell-attack-slot6-acc-res-speed-below-target",
        )
        gemmed = rune("Rage", "RES", 15, level=6, gemmed=True)
        self.assertFalse(analyze_upgrade(gemmed, config).should_upgrade)
        self.assertIn("宝石", analyze_upgrade(gemmed, config).reason)

    def test_attack_slot_six_speed_path_detects_any_actual_deviation(self):
        before = {
            "set": "Fatal", "slot": 6, "level": 0,
            "main_stat": {"type": "ACC", "value": 64},
            "substats": [{"type": "SPD", "value": 5}, {"type": "CR", "value": 6}],
        }
        both_speed = {
            **before,
            "level": 6,
            "substats": [{"type": "SPD", "value": 13}, {"type": "CR", "value": 6}],
        }
        one_missed = {
            **before,
            "level": 6,
            "substats": [{"type": "SPD", "value": 9}, {"type": "CR", "value": 12}],
        }
        self.assertFalse(attack_slot6_speed_deviated(before, both_speed))
        self.assertTrue(attack_slot6_speed_deviated(before, one_missed))

        forced = {**one_missed, "recognized": True, "stars": 6, "quality": "Legend"}
        forced["_force_sell_rule"] = "sell-attack-slot6-speed-deviated"
        decision = evaluate_rune(forced, default_config())
        self.assertEqual(decision.action, "SELL")
        self.assertEqual(decision.rule_id, "sell-attack-slot6-speed-deviated")
        self.assertFalse(analyze_upgrade(forced, default_config()).should_upgrade)

    def test_saved_config_receives_rare_immediate_sell_rule(self):
        merged = merge_default_groups({"version": 1, "fallback": "sell", "groups": []})
        group = next(item for item in merged["groups"] if item["id"] == "sell-rare-quality-immediate")
        self.assertEqual(group["action"], "sell")
        self.assertEqual(group["priority"], 200)
        self.assertTrue(group["terminal"])

    def test_slot_two_speed_generic_rule_uses_finished_practical_efficiency(self):
        config = {
            **default_config(),
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] == "keep-slot2-speed-main"
            ],
        }
        weak_flat_rune = {
            "recognized": True,
            "stars": 6,
            "quality": "Legend",
            "set": "Destroy",
            "slot": 2,
            "level": 12,
            "main_stat": {"type": "SPD", "value": 31},
            "substats": [
                {"type": "DEF", "value": 38},
                {"type": "ATK", "value": 44},
                {"type": "HP%", "value": 11},
                {"type": "RES", "value": 7},
            ],
        }
        useful_rune = deepcopy(weak_flat_rune)
        useful_rune["substats"] = [
            {"type": "HP%", "value": 16},
            {"type": "DEF%", "value": 16},
            {"type": "ACC", "value": 12},
            {"type": "RES", "value": 12},
        ]

        weak_decision = evaluate_rune(weak_flat_rune, config)
        self.assertEqual(weak_decision.action, "SELL")
        self.assertFalse(should_upgrade(weak_flat_rune, config))
        self.assertEqual(evaluate_rune(useful_rune, config).rule_id, "keep-slot2-speed-main")

    def test_all_legacy_efficiency_rule_fields_use_flat_half_weight(self):
        rune = {
            "recognized": True,
            "stars": 6,
            "quality": "Legend",
            "set": "Destroy",
            "slot": 2,
            "level": 12,
            "main_stat": {"type": "SPD", "value": 31},
            "substats": [
                {"type": "DEF", "value": 38},
                {"type": "ATK", "value": 44},
                {"type": "HP%", "value": 11},
                {"type": "RES", "value": 7},
            ],
        }
        efficiency = calculate_efficiency(rune)
        self.assertGreater(efficiency["current"], 80)
        self.assertGreater(efficiency["maxWithGrind"], 100)
        self.assertLess(efficiency["practicalCurrent"], 80)
        self.assertLess(efficiency["practicalMaxWithGrind"], 90)

        def config(field, value):
            return {
                "version": 1,
                "fallback": "sell",
                "groups": [{
                    "id": f"legacy-{field}",
                    "name": "旧效率字段",
                    "action": "keep",
                    "match": "all",
                    "rules": [{"field": field, "op": "gte", "value": value}],
                }],
            }

        # These would all match the raw SWEX values. They must fail because
        # rule evaluation globally resolves them to practical efficiency.
        self.assertEqual(evaluate_rune(rune, config("efficiency.current", 80)).action, "SELL")
        self.assertEqual(evaluate_rune(rune, config("efficiency.max", 80)).action, "SELL")
        self.assertEqual(evaluate_rune(rune, config("efficiency.maxHeroGrind", 90)).action, "SELL")
        self.assertEqual(evaluate_rune(rune, config("efficiency.maxWithGrind", 100)).action, "SELL")

    def test_saved_slot_two_speed_rule_migrates_from_grind_to_current_efficiency(self):
        merged = merge_default_groups({
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "keep-slot2-speed-main",
                "name": "旧二号位速度保护",
                "enabled": False,
                "action": "keep",
                "match": "all",
                "requirements": [
                    {"field": "slot", "op": "eq", "value": 2},
                    {"field": "main_stat.type", "op": "eq", "value": "SPD"},
                ],
                "rules": [
                    {"field": "efficiency.maxWithGrind", "op": "gte", "value": 90},
                ],
            }],
        })
        group = next(item for item in merged["groups"] if item["id"] == "keep-slot2-speed-main")
        self.assertFalse(group["enabled"])
        self.assertEqual(
            group["rules"],
            [{"field": "efficiency.practicalCurrent", "op": "gte", "value": 80}],
        )

    def test_build_stats_accept_main_stat_but_substat_rule_does_not(self):
        rune = {
            "stars": 6,
            "quality": "Legend",
            "slot": 6,
            "main_stat": {"type": "ATK%", "value": 11},
            "substats": [{"type": "SPD", "value": 10}],
        }
        build_config = {
            "version": 1, "fallback": "sell", "groups": [{
                "id": "build-attack", "name": "构筑攻击", "enabled": True,
                "action": "keep", "match": "all",
                "rules": [{"field": "buildStats.ATK%", "op": "gte", "value": 10}],
            }],
        }
        substat_config = deepcopy(build_config)
        substat_config["groups"][0]["rules"][0]["field"] = "substats.ATK%"
        self.assertEqual(evaluate_rune(rune, build_config).action, "KEEP")
        self.assertEqual(evaluate_rune(rune, substat_config).action, "SELL")

    def test_slot_restrictions_do_not_block_valid_build_rules(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in {"keep-fatal-attack-core", "keep-guard-defense-crit-damage"}
            ],
        }
        slot_three_fatal = {
            "stars": 6, "quality": "Legend", "set": "Fatal", "slot": 3,
            "main_stat": {"type": "DEF", "value": 160},
            "substats": [{"type": "CR", "value": 15}],
            "efficiency": {"maxWithGrind": 112},
        }
        slot_one_guard = {
            "stars": 6, "quality": "Legend", "set": "Guard", "slot": 1,
            "main_stat": {"type": "ATK", "value": 160},
            "substats": [{"type": "CR", "value": 10}, {"type": "CD", "value": 12}],
            "efficiency": {"maxWithGrind": 105},
        }
        self.assertEqual(evaluate_rune(slot_three_fatal, config).rule_id, "keep-fatal-attack-core")
        self.assertEqual(evaluate_rune(slot_one_guard, config).rule_id, "keep-guard-defense-crit-damage")

    def test_upgrade_candidates_respect_fixed_slot_substat_restrictions(self):
        slot_one = {"slot": 1, "main_stat": {"type": "ATK"}, "substats": []}
        slot_three = {"slot": 3, "main_stat": {"type": "DEF"}, "substats": []}
        self.assertTrue({"DEF", "DEF%"}.isdisjoint(_potential_stats(slot_one, {})))
        self.assertTrue({"ATK", "ATK%"}.isdisjoint(_potential_stats(slot_three, {})))

    def test_builtin_config_migration_preserves_thresholds_and_uses_build_stats(self):
        merged = merge_default_groups({
            "version": 1,
            "preset": "account-tuned-custom",
            "fallback": "sell",
            "groups": [{
                "id": "keep-fatal-attack-core",
                "name": "自定义猛攻",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "requirements": [{"field": "substats.ATK%", "op": "gte", "value": 17}],
                "rules": [{"field": "substats.CR", "op": "gte", "value": 13}],
            }],
        })
        group = next(item for item in merged["groups"] if item["id"] == "keep-fatal-attack-core")
        self.assertEqual(group["requirements"][0], {"field": "buildStats.ATK%", "op": "gte", "value": 17})
        self.assertEqual(group["rules"][0], {"field": "buildStats.CR", "op": "gte", "value": 13})
        self.assertIn(
            {"field": "rolls.attackOutput", "op": "gte", "value": 3},
            group["requirements"],
        )

    def test_general_high_efficiency_uses_finished_current_efficiency(self):
        config = default_config()
        group = next(
            item for item in config["groups"]
            if item["id"] == "keep-exceptional-efficiency"
        )
        self.assertEqual(group["name"], "任意套装高效率成品")
        self.assertIn(
            {"field": "level", "op": "gte", "value": 12},
            group["requirements"],
        )
        self.assertEqual(
            group["rules"],
            [{"field": "efficiency.current", "op": "gte", "value": 91}],
        )
        self.assertEqual(config["retention_policy"]["basis"], "level_gte_12")
        self.assertEqual(config["retention_policy"]["target_percent"], 60)

    def test_weak_finished_rune_is_not_saved_by_legend_grind_potential(self):
        rune = {
            "stars": 6,
            "quality": "Legend",
            "level": 15,
            "set": "Blade",
            "slot": 1,
            "main_stat": {"type": "ATK", "value": 160},
            "innate": {"type": "RES", "value": 5},
            "substats": [
                {"type": "ATK%", "value": 7},
                {"type": "SPD", "value": 15},
                {"type": "HP", "value": 320},
                {"type": "ACC", "value": 9},
            ],
        }

        efficiency = calculate_efficiency(rune)
        self.assertEqual(efficiency["current"], 78.42)
        self.assertEqual(efficiency["maxLegendGrind"], 103.77)
        decision = evaluate_rune(rune, default_config())
        self.assertEqual(decision.action, "SELL")
        self.assertIsNone(decision.rule_id)

    def test_forodo_v1_preset_migrates_all_builtin_thresholds_to_top_sixty(self):
        old = {
            "version": 1,
            "preset": "account-tuned-forodo-29649161-v1",
            "fallback": "sell",
            "groups": [{
                "id": "keep-exceptional-efficiency",
                "name": "旧高效率线",
                "enabled": False,
                "action": "keep",
                "match": "all",
                "requirements": [],
                "rules": [{"field": "efficiency.maxWithGrind", "op": "gte", "value": 115}],
            }, {
                "id": "my-custom-rule",
                "name": "用户规则",
                "enabled": True,
                "action": "keep",
                "match": "all",
                "rules": [{"field": "stars", "op": "gte", "value": 6}],
            }],
        }

        merged = merge_default_groups(old)
        efficiency = next(
            group for group in merged["groups"]
            if group["id"] == "keep-exceptional-efficiency"
        )

        self.assertEqual(merged["preset"], "account-tuned-forodo-29649161-v3-progressive-max90")
        self.assertEqual(
            efficiency["rules"],
            [{"field": "efficiency.current", "op": "gte", "value": 91}],
        )
        self.assertIn(
            {"field": "level", "op": "gte", "value": 12},
            efficiency["requirements"],
        )
        self.assertFalse(efficiency["enabled"])
        self.assertIn("my-custom-rule", {group["id"] for group in merged["groups"]})

    def test_progressive_preset_uses_max_with_grind_as_a_broad_floor(self):
        thresholds = [
            condition["value"]
            for group in default_config()["groups"]
            for section in ("requirements", "rules")
            for condition in group.get(section, [])
            if condition.get("field") == "efficiency.maxWithGrind"
            and condition.get("op") in {"gte", "gt"}
        ]
        self.assertTrue(thresholds)
        self.assertEqual(set(thresholds), {90})

    def test_current_top_sixty_preset_migrates_legacy_grind_only_rule(self):
        old = default_config()
        group = next(
            item for item in old["groups"]
            if item["id"] == "keep-exceptional-efficiency"
        )
        group["name"] = "任意套装高效率胚子"
        group["requirements"] = [
            condition for condition in group["requirements"]
            if condition.get("field") != "level"
        ]
        group["rules"] = [
            {"field": "efficiency.maxWithGrind", "op": "gte", "value": 101}
        ]
        group["enabled"] = False

        merged = merge_default_groups(old)
        migrated = next(
            item for item in merged["groups"]
            if item["id"] == "keep-exceptional-efficiency"
        )
        self.assertEqual(migrated["name"], "任意套装高效率成品")
        self.assertEqual(
            migrated["rules"],
            [{"field": "efficiency.current", "op": "gte", "value": 91}],
        )
        self.assertFalse(migrated["enabled"])

    def test_protected_runes_can_never_be_sold(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "rules": [],
        }
        rune = {
            "locked": True,
            "stars": 6,
            "main_stat": {"type": "SPD", "value": 42},
            "substats": [],
        }
        self.assertEqual(evaluate_rune(rune, config).action, "KEEP")

    def test_matching_sell_rule_returns_reason(self):
        config = {
            "version": 1,
            "fallback": "keep",
            "rules": [
                {
                    "id": "low-speed",
                    "name": "低速出售",
                    "action": "sell",
                    "match": "all",
                    "conditions": [{"field": "substats.SPD", "op": "lt", "value": 10}],
                }
            ],
        }
        rune = {
            "stars": 6,
            "main_stat": {"type": "SPD", "value": 42},
            "substats": [{"type": "SPD", "value": 5}],
        }
        decision = evaluate_rune(rune, config)
        self.assertEqual(decision.action, "SELL")
        self.assertIn("低速出售", decision.reason)

    def test_rule_group_combines_quality_set_and_threshold(self):
        config = {
            "version": 1,
            "fallback": "keep",
            "groups": [
                {
                    "id": "swift-hero",
                    "name": "迅速英雄符文",
                    "action": "sell",
                    "match": "all",
                    "rules": [
                        {"field": "quality", "op": "gte", "value": "Hero"},
                        {"field": "set", "op": "in", "value": ["Swift"]},
                        {"field": "substats.SPD", "op": "gte", "value": 10},
                    ],
                }
            ],
        }
        rune = {
            "stars": 6,
            "quality": "Legend",
            "set": "Swift",
            "main_stat": {"type": "SPD", "value": 42},
            "substats": [{"type": "SPD", "value": 12}],
        }
        decision = evaluate_rune(rune, config)
        self.assertEqual(decision.action, "SELL")

    def test_empty_group_rule_is_rejected(self):
        config = {"version": 1, "fallback": "keep", "groups": [{"id": "empty", "action": "sell", "rules": []}]}
        self.assertTrue(any("rules 不能为空" in error for error in validate_config(config)))

    def test_default_groups_are_merged_without_overwriting_custom_groups(self):
        config = {"version": 1, "fallback": "keep", "rules": [{"id": "custom", "name": "自定义", "action": "keep", "conditions": [{"field": "stars", "op": "gte", "value": 6}]}]}
        merged = merge_default_groups(config)
        self.assertEqual(merged["groups"][0]["id"], "custom")
        self.assertIn("keep-swift-violent-speed-accuracy", {group["id"] for group in merged["groups"]})

    def test_legacy_rage_fatal_group_is_replaced_by_separate_defaults(self):
        merged = merge_default_groups({
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "keep-rage-fatal-attack-crit",
                "name": "激怒/猛攻输出核心",
                "enabled": True,
                "action": "keep",
                "match": "any",
                "rules": [{"field": "substats.CR", "op": "gte", "value": 10}],
            }],
        })
        ids = {group["id"] for group in merged["groups"]}
        self.assertNotIn("keep-rage-fatal-attack-crit", ids)
        self.assertTrue({
            "keep-fatal-attack-core",
            "keep-fatal-attack-main-core",
            "keep-rage-crit-core",
            "keep-rage-crit-main-core",
        }.issubset(ids))

    def test_fatal_requires_attack_while_rage_accepts_multiple_scalers(self):
        split_ids = {
            "keep-fatal-attack-core",
            "keep-fatal-attack-main-core",
            "keep-rage-crit-core",
            "keep-rage-crit-main-core",
        }
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in split_ids
            ],
        }

        def rune(set_name, main, substats, slot=1):
            return {
                "recognized": True,
                "stars": 6,
                "quality": "Legend",
                "set": set_name,
                "slot": slot,
                "level": 15,
                "main_stat": {"type": main, "value": 63},
                "substats": [
                    {"type": stat, "value": value}
                    for stat, value in substats.items()
                ],
                "efficiency": {"maxWithGrind": 112},
            }

        fatal = rune("Fatal", "ATK", {"ATK%": 18, "CR": 15})
        fatal_without_attack = rune("Fatal", "ATK", {"HP%": 20, "CR": 15, "CD": 20})
        fatal_attack_main = rune("Fatal", "ATK%", {"CR": 15, "CD": 20}, slot=6)
        rage_hp = rune("Rage", "HP", {"HP%": 18, "CR": 15, "CD": 20}, slot=5)
        rage_def = rune("Rage", "DEF", {"DEF%": 18, "CR": 15, "CD": 20}, slot=3)
        rage_cd_main = rune("Rage", "CD", {"DEF%": 18, "CR": 15}, slot=4)

        self.assertEqual(evaluate_rune(fatal, config).rule_id, "keep-fatal-attack-core")
        self.assertEqual(evaluate_rune(fatal_without_attack, config).action, "SELL")
        self.assertEqual(evaluate_rune(fatal_attack_main, config).rule_id, "keep-fatal-attack-main-core")
        self.assertEqual(evaluate_rune(rage_hp, config).rule_id, "keep-rage-crit-core")
        self.assertEqual(evaluate_rune(rage_def, config).rule_id, "keep-rage-crit-core")
        self.assertEqual(evaluate_rune(rage_cd_main, config).rule_id, "keep-rage-crit-main-core")

    def test_output_rules_reject_tank_only_will_rune(self):
        output_ids = {
            "keep-blade-will-attack-crit",
            "keep-damage-hp-def",
            "keep-defense-scaler-crit-output",
        }
        config = {
            **default_config(),
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in output_ids
            ],
        }

        def rune(substats, *, slot=3, main="DEF"):
            return {
                "stars": 6,
                "quality": "Legend",
                "level": 12,
                "set": "Will",
                "slot": slot,
                "main_stat": {"type": main, "value": 118},
                "substats": [
                    {"type": stat, "value": value}
                    for stat, value in substats.items()
                ],
                "efficiency": {"maxWithGrind": 112.32},
            }

        tank_only = rune({"HP": 753, "CR": 6, "HP%": 16, "DEF%": 14})
        blade_will = rune({"ATK%": 12, "CR": 12}, slot=1, main="ATK")
        hp_scaler = rune({"HP%": 16, "CR": 12})
        defense_scaler = rune({"DEF%": 16, "CD": 15})
        slot_one_without_defense = rune({"CR": 12, "CD": 15}, slot=1, main="ATK")

        self.assertEqual(evaluate_rune(tank_only, config).action, "SELL")
        self.assertEqual(
            evaluate_rune(blade_will, config).rule_id,
            "keep-blade-will-attack-crit",
        )
        self.assertEqual(evaluate_rune(hp_scaler, config).rule_id, "keep-damage-hp-def")
        self.assertEqual(
            evaluate_rune(defense_scaler, config).rule_id,
            "keep-defense-scaler-crit-output",
        )
        self.assertNotEqual(
            evaluate_rune(slot_one_without_defense, {
                **config,
                "groups": [
                    group for group in config["groups"]
                    if group["id"] == "keep-defense-scaler-crit-output"
                ],
            }).rule_id,
            "keep-defense-scaler-crit-output",
        )

    def test_output_rules_measure_useful_rolls_not_attribute_count(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] == "keep-blade-will-attack-crit"
            ],
        }

        def rune(substats):
            return {
                "stars": 6,
                "quality": "Legend",
                "level": 12,
                "set": "Blade",
                "slot": 1,
                "main_stat": {"type": "ATK", "value": 118},
                "substats": substats,
                "efficiency": {"maxWithGrind": 114},
            }

        # RES 18 proves two RES rolls; ATK% 12 and SPD 12 prove one useful
        # roll each.  Two matching attribute kinds must not hide the 2/4 split.
        half_wasted = rune([
            {"type": "RES", "value": 18},
            {"type": "ATK%", "value": 12},
            {"type": "SPD", "value": 12},
            {"type": "HP%", "value": 8},
        ])
        # Only two useful attribute kinds are present, but all four completed
        # rolls are forced onto them, so concentration is good even though
        # each value alone only proves one roll (the other rolls may be low).
        concentrated_two_stats = rune([
            {"type": "ATK%", "value": 15},
            {"type": "SPD", "value": 12},
            {"type": "RES", "value": 4},
            {"type": "HP%", "value": 5},
        ])

        self.assertEqual(evaluate_rune(half_wasted, config).action, "SELL")
        self.assertEqual(
            evaluate_rune(concentrated_two_stats, config).rule_id,
            "keep-blade-will-attack-crit",
        )

    def test_grinds_and_gems_do_not_create_useful_enhancement_rolls(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] == "keep-blade-will-attack-crit"
            ],
        }
        base = {
            "stars": 6,
            "quality": "Legend",
            "level": 12,
            "set": "Will",
            "slot": 1,
            "main_stat": {"type": "ATK", "value": 160},
            "efficiency": {"maxWithGrind": 114},
        }
        grinded = {
            **base,
            "substats": [
                {"type": "ATK%", "value": 8, "grind": 10},
                {"type": "SPD", "value": 6, "grind": 5},
                {"type": "CR", "value": 6},
                {"type": "RES", "value": 32},
            ],
        }
        gemmed = {
            **base,
            "substats": [
                {"type": "ATK%", "value": 24, "gemmed": True},
                {"type": "SPD", "value": 12},
                {"type": "CR", "value": 10},
                {"type": "RES", "value": 8},
            ],
        }

        self.assertEqual(evaluate_rune(grinded, config).action, "SELL")
        self.assertEqual(evaluate_rune(gemmed, config).action, "SELL")

    def test_output_rule_migration_preserves_enabled_state(self):
        old = default_config()
        blade = next(
            group for group in old["groups"]
            if group["id"] == "keep-blade-will-attack-crit"
        )
        blade["rules"].extend([
            {"field": "buildStats.HP%", "op": "gte", "value": 9},
            {"field": "buildStats.DEF%", "op": "gte", "value": 9},
        ])
        hp_scaler = next(
            group for group in old["groups"]
            if group["id"] == "keep-damage-hp-def"
        )
        hp_scaler["enabled"] = False
        old["groups"] = [
            group for group in old["groups"]
            if group["id"] != "keep-defense-scaler-crit-output"
        ]

        merged = merge_default_groups(old)
        migrated_blade = next(
            group for group in merged["groups"]
            if group["id"] == "keep-blade-will-attack-crit"
        )
        migrated_hp = next(
            group for group in merged["groups"]
            if group["id"] == "keep-damage-hp-def"
        )
        migrated_def = next(
            group for group in merged["groups"]
            if group["id"] == "keep-defense-scaler-crit-output"
        )
        self.assertEqual(
            {condition["field"] for condition in migrated_blade["rules"]},
            {"buildStats.ATK%", "buildStats.CR", "buildStats.CD", "buildStats.SPD"},
        )
        self.assertIn(
            {"field": "rolls.attackOutput", "op": "gte", "value": 3},
            migrated_blade["requirements"],
        )
        self.assertFalse(migrated_hp["enabled"])
        self.assertFalse(migrated_def["enabled"])

    def test_pure_tank_rule_requires_res_speed_hp_and_def(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [group for group in default_config()["groups"] if group["id"] == "keep-tank-res-speed"],
        }
        rune = {
            "stars": 6,
            "quality": "Legend",
            "set": "Guard",
            "main_stat": {"type": "HP%", "value": 63},
            "substats": [
                {"type": "RES", "value": 20},
                {"type": "SPD", "value": 12},
                {"type": "HP%", "value": 15},
                {"type": "DEF%", "value": 12},
            ],
        }
        decision = evaluate_rune(rune, config)
        self.assertEqual(decision.rule_id, "keep-tank-res-speed")

    def test_special_rune_archetypes_do_not_require_crit_rate(self):
        special_ids = {
            "keep-wind-occult-damage-control",
            "keep-speed-attack-crit",
            "keep-speed-attack-cd",
            "keep-speed-crit-hp",
            "keep-bomber-speed-accuracy",
            "keep-hp-cd-guaranteed-crit",
            "keep-guaranteed-crit-damage",
            "keep-guaranteed-crit-speed-main",
            "keep-guaranteed-crit-cd-main",
            "keep-guaranteed-crit-attack-main",
        }
        config = {
            **default_config(),
            "fallback": "sell",
            "groups": [group for group in default_config()["groups"] if group["id"] in special_ids],
        }

        def rune(set_name, substats, main="ATK%"):
            return {
                "stars": 6,
                "quality": "Legend",
                "set": set_name,
                "main_stat": {"type": main, "value": 63},
                "substats": [{"type": key, "value": value} for key, value in substats.items()],
                "efficiency": {"maxWithGrind": 110},
            }

        wind_occult = evaluate_rune(rune("Despair", {"SPD": 18, "ATK%": 12, "ACC": 12}), config)
        speed_attack_crit = evaluate_rune(rune("Swift", {"SPD": 18, "ATK%": 15, "CR": 15, "HP%": 12}), config)
        speed_attack_cd = evaluate_rune(rune("Violent", {"SPD": 18, "ATK%": 15, "CD": 20, "HP%": 12}), config)
        speed_crit_hp = evaluate_rune(rune("Will", {"SPD": 18, "CR": 20, "DEF%": 12}, main="HP%"), config)
        bomber = evaluate_rune(rune("Focus", {"SPD": 12, "ACC": 18, "ATK%": 12}), config)
        hp_cd_guaranteed = evaluate_rune(rune("Will", {"HP%": 20, "CD": 20, "DEF%": 15}), config)
        guaranteed_crit = evaluate_rune(rune("Violent", {"CD": 20, "ATK%": 15, "SPD": 12}), config)

        self.assertEqual(wind_occult.rule_id, "keep-wind-occult-damage-control")
        self.assertEqual(speed_attack_crit.rule_id, "keep-speed-attack-crit")
        self.assertEqual(speed_attack_cd.rule_id, "keep-speed-attack-cd")
        self.assertEqual(speed_crit_hp.rule_id, "keep-speed-crit-hp")
        self.assertEqual(bomber.rule_id, "keep-bomber-speed-accuracy")
        self.assertEqual(hp_cd_guaranteed.rule_id, "keep-hp-cd-guaranteed-crit")
        self.assertEqual(guaranteed_crit.rule_id, "keep-guaranteed-crit-damage")
        self.assertEqual(
            evaluate_rune(rune("Swift", {"CD": 20, "ATK%": 15, "SPD": 12}), config).rule_id,
            "keep-guaranteed-crit-damage",
        )

    def test_sqlite_config_store_migrates_reads_and_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "rules.sqlite3"
            legacy_path = root / "legacy.json"
            default_path = root / "defaults.json"
            legacy = {
                "version": 1,
                "fallback": "sell",
                "groups": [{
                    "id": "legacy-rule",
                    "name": "迁移规则",
                    "enabled": True,
                    "action": "keep",
                    "match": "all",
                    "rules": [{"field": "stars", "op": "gte", "value": 6}],
                }],
            }
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
            default_path.write_text(json.dumps({"version": 1, "fallback": "keep", "groups": []}), encoding="utf-8")

            migrated = load_sqlite_config(db_path=db_path, legacy_path=legacy_path, default_path=default_path)
            self.assertEqual(migrated["fallback"], "sell")
            self.assertIn("legacy-rule", {group["id"] for group in migrated["groups"]})
            self.assertTrue(db_path.exists())

            updated = {**migrated, "fallback": "keep"}
            save_sqlite_config(updated, db_path=db_path, legacy_path=legacy_path)
            loaded = load_sqlite_config(db_path=db_path, legacy_path=legacy_path, default_path=default_path)
            self.assertEqual(loaded["fallback"], "keep")
            self.assertEqual(json.loads(legacy_path.read_text(encoding="utf-8"))["fallback"], "keep")

    def test_swift_and_violent_attack_core_supports_output_main_stats(self):
        rule_ids = {
            "keep-swift-violent-attack-core",
            "keep-swift-violent-attack-speed-main",
            "keep-swift-violent-attack-main",
        }
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in rule_ids
            ],
        }

        def rune(set_name, main_stat, substats, *, level=12):
            return {
                "stars": 6,
                "quality": "Legend",
                "set": set_name,
                "level": level,
                "main_stat": {"type": main_stat, "value": 63},
                "substats": [
                    {"type": stat, "value": value}
                    for stat, value in substats.items()
                ],
                "efficiency": {"maxWithGrind": 105},
            }

        standard = rune("Swift", "HP%", {"SPD": 15, "ATK%": 10, "CR": 10})
        crit_main = rune("Violent", "CD", {"SPD": 15, "ATK%": 10})
        speed_main = rune("Swift", "SPD", {"ATK%": 10, "CD": 12})
        attack_main = rune("Violent", "ATK%", {"SPD": 15, "CR": 10})
        wrong_set = rune("Will", "HP%", {"SPD": 15, "ATK%": 10, "CR": 10})
        no_damage = rune("Swift", "HP%", {"SPD": 15, "ATK%": 10})
        promising = rune(
            "Swift",
            "HP%",
            {"SPD": 6, "ATK%": 8, "CR": 6, "HP": 375},
            level=0,
        )
        missed_first_roll = {**promising, "level": 3}

        self.assertEqual(evaluate_rune(standard, config).rule_id, "keep-swift-violent-attack-core")
        self.assertEqual(evaluate_rune(crit_main, config).rule_id, "keep-swift-violent-attack-core")
        self.assertEqual(evaluate_rune(speed_main, config).rule_id, "keep-swift-violent-attack-speed-main")
        self.assertEqual(evaluate_rune(attack_main, config).rule_id, "keep-swift-violent-attack-main")
        self.assertEqual(evaluate_rune(wrong_set, config).action, "SELL")
        self.assertEqual(evaluate_rune(no_damage, config).action, "SELL")
        self.assertTrue(should_upgrade(promising, config))
        self.assertFalse(should_upgrade(missed_first_roll, config))

    def test_guaranteed_crit_rules_require_speed_crit_damage_and_attack(self):
        rule_ids = {
            "keep-guaranteed-crit-damage",
            "keep-guaranteed-crit-speed-main",
            "keep-guaranteed-crit-cd-main",
            "keep-guaranteed-crit-attack-main",
        }
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [group for group in default_config()["groups"] if group["id"] in rule_ids],
        }

        def rune(slot, main, substats, set_name="Swift"):
            return {
                "stars": 6,
                "quality": "Legend",
                "set": set_name,
                "slot": slot,
                "main_stat": {"type": main, "value": 63},
                "substats": [{"type": stat, "value": value} for stat, value in substats.items()],
                "efficiency": {"maxWithGrind": 110},
            }

        standard = rune(1, "ATK", {"SPD": 12, "CD": 20, "ATK%": 15})
        speed_main = rune(2, "SPD", {"CD": 20, "ATK%": 15})
        cd_main = rune(4, "CD", {"SPD": 12, "ATK%": 15})
        attack_main = rune(6, "ATK%", {"SPD": 12, "CD": 20})

        self.assertEqual(evaluate_rune(standard, config).rule_id, "keep-guaranteed-crit-damage")
        self.assertEqual(evaluate_rune(speed_main, config).rule_id, "keep-guaranteed-crit-speed-main")
        self.assertEqual(evaluate_rune(cd_main, config).rule_id, "keep-guaranteed-crit-cd-main")
        self.assertEqual(evaluate_rune(attack_main, config).rule_id, "keep-guaranteed-crit-attack-main")

        missing_speed = rune(1, "ATK", {"CD": 20, "ATK%": 15, "DEF%": 20})
        missing_cd = rune(1, "ATK", {"SPD": 12, "ATK%": 15, "HP%": 20})
        missing_attack = rune(1, "ATK", {"SPD": 12, "CD": 20, "DEF%": 20})
        crit_rate_only = rune(1, "ATK", {"SPD": 12, "CD": 20, "CR": 25})
        for candidate in (missing_speed, missing_cd, missing_attack, crit_rate_only):
            self.assertEqual(evaluate_rune(candidate, config).action, "SELL")

    def test_will_slot_two_raid_rules_keep_coherent_percent_main_builds(self):
        rule_ids = {
            "keep-will-slot2-raid-attack-main",
            "keep-will-slot2-raid-tank-main",
        }
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in rule_ids
            ],
        }

        def rune(main, substats, *, efficiency=97, set_name="Will"):
            return {
                "stars": 6,
                "quality": "Legend",
                "set": set_name,
                "slot": 2,
                "level": 12,
                "main_stat": {"type": main, "value": 63},
                "substats": [
                    {"type": stat, "value": value}
                    for stat, value in substats.items()
                ],
                "efficiency": {"maxWithGrind": efficiency},
            }

        # At least three of the completed enhancement rolls must be proven to
        # land on the rule's useful stats; a matching main stat alone is not
        # enough when half of the substat rolls went elsewhere.
        attack = rune("ATK%", {"CD": 18, "SPD": 9, "HP%": 8})
        tank_hp = rune("HP%", {"DEF%": 17, "RES": 9, "CR": 6})
        tank_def = rune("DEF%", {"HP%": 17, "SPD": 9, "CD": 7})
        scattered_attack = rune("ATK%", {"HP%": 15, "DEF%": 15, "ACC": 12})
        one_tank_stat = rune("HP%", {"RES": 20, "CR": 15, "CD": 20})
        flat_main = rune("ATK", {"CR": 15, "CD": 20, "SPD": 12})
        low_efficiency = rune("ATK%", {"CR": 12, "CD": 15}, efficiency=89.99)

        self.assertEqual(
            evaluate_rune(attack, config).rule_id,
            "keep-will-slot2-raid-attack-main",
        )
        self.assertEqual(
            evaluate_rune(tank_hp, config).rule_id,
            "keep-will-slot2-raid-tank-main",
        )
        self.assertEqual(
            evaluate_rune(tank_def, config).rule_id,
            "keep-will-slot2-raid-tank-main",
        )
        for candidate in (scattered_attack, one_tank_stat, flat_main, low_efficiency):
            self.assertEqual(evaluate_rune(candidate, config).action, "SELL")

    def test_fight_and_intangible_use_separate_build_rules(self):
        rule_ids = {
            "keep-fight-support-core",
            "keep-fight-damage-core",
            "keep-intangible-versatile-support",
        }
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in rule_ids
            ],
        }

        def rune(set_name, substats, efficiency=105, main="HP%"):
            return {
                "stars": 6,
                "quality": "Hero",
                "set": set_name,
                "main_stat": {"type": main, "value": 63},
                "substats": [
                    {"type": stat, "value": value}
                    for stat, value in substats.items()
                ],
                "efficiency": {"maxWithGrind": efficiency},
            }

        tank = evaluate_rune(
            rune("Fight", {"SPD": 10, "DEF%": 10, "RES": 10}),
            config,
        )
        damage = evaluate_rune(
            rune("Fight", {"ATK%": 10, "CR": 10, "CD": 12}, main="ATK%"),
            config,
        )
        intangible = evaluate_rune(rune("Intangible", {"CR": 10, "CD": 12}), config)
        support_pair = evaluate_rune(
            rune("Fight", {"SPD": 10, "DEF%": 10}, main="ATK"),
            config,
        )
        incoherent_fight = evaluate_rune(rune("Fight", {"SPD": 10}, main="ATK"), config)
        weak_efficiency = evaluate_rune(
            rune("Intangible", {"HP%": 10, "ACC": 10}, efficiency=89.99),
            config,
        )

        self.assertEqual(tank.rule_id, "keep-fight-support-core")
        self.assertEqual(damage.rule_id, "keep-fight-damage-core")
        self.assertEqual(intangible.rule_id, "keep-intangible-versatile-support")
        self.assertEqual(support_pair.rule_id, "keep-fight-support-core")
        self.assertEqual(incoherent_fight.action, "SELL")
        self.assertEqual(weak_efficiency.action, "SELL")

    def test_old_fight_intangible_rule_is_replaced_during_config_merge(self):
        old = {
            "version": 1,
            "fallback": "sell",
            "groups": [{
                "id": "keep-fight-intangible-versatile-support",
                "name": "斗志/无形百搭辅助",
                "enabled": True,
                "action": "keep",
                "match": "any",
                "rules": [{"field": "buildStats.SPD", "op": "gte", "value": 5}],
            }],
        }

        merged = merge_default_groups(old)
        ids = {group["id"] for group in merged["groups"]}

        self.assertNotIn("keep-fight-intangible-versatile-support", ids)
        self.assertTrue({
            "keep-fight-support-core",
            "keep-fight-damage-core",
            "keep-intangible-versatile-support",
        }.issubset(ids))

    def test_guard_and_energy_damage_rules_require_scaling_crit_and_crit_damage(self):
        rule_ids = {
            "keep-guard-defense-crit-damage",
            "keep-energy-hp-crit-damage",
        }
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in rule_ids
            ],
        }

        def rune(set_name, main_stat, substats, *, level=12):
            return {
                "stars": 6,
                "quality": "Legend",
                "set": set_name,
                "slot": 4,
                "level": level,
                "main_stat": {"type": main_stat, "value": 63},
                "substats": [
                    {"type": stat, "value": value}
                    for stat, value in substats.items()
                ],
                "efficiency": {"maxWithGrind": 105},
            }

        guard = rune("Guard", "CD", {"DEF%": 10, "CR": 10})
        energy = rune("Energy", "HP%", {"CR": 10, "CD": 12})
        wrong_scaling = rune("Guard", "ATK%", {"HP%": 10, "CR": 10, "CD": 12})
        promising = rune(
            "Guard",
            "ATK%",
            {"DEF%": 8, "CR": 6, "CD": 7, "SPD": 6},
            level=0,
        )
        missed_rolls = {**promising, "level": 9}

        self.assertEqual(evaluate_rune(guard, config).rule_id, "keep-guard-defense-crit-damage")
        self.assertEqual(evaluate_rune(energy, config).rule_id, "keep-energy-hp-crit-damage")
        self.assertEqual(evaluate_rune(wrong_scaling, config).action, "SELL")
        self.assertTrue(should_upgrade(promising, config))
        self.assertFalse(should_upgrade(missed_rolls, config))

    def test_single_extreme_substats_are_kept_without_efficiency_requirement(self):
        specialist_ids = {
            "keep-single-speed-premium-set",
            "keep-single-speed-any-set",
            *SINGLE_EXTREME_REPLACEMENT_IDS,
        }
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in specialist_ids
            ],
        }

        def rune(set_name, stat, value, *, grind=0):
            return {
                "stars": 6,
                "quality": "Hero",
                "set": set_name,
                "slot": 1,
                "level": 12,
                "main_stat": {"type": "ATK", "value": 160},
                "substats": [{"type": stat, "value": value, "grind": grind}],
                "efficiency": {"maxWithGrind": 50},
            }

        self.assertEqual(
            evaluate_rune(rune("Violent", "SPD", 21), config).rule_id,
            "keep-single-speed-premium-set",
        )
        self.assertEqual(evaluate_rune(rune("Fatal", "SPD", 21), config).action, "SELL")
        self.assertEqual(
            evaluate_rune(rune("Fatal", "SPD", 22), config).rule_id,
            "keep-single-speed-any-set",
        )

        compatible = {
            ("Fatal", "CR", 22): "keep-single-extreme-offense",
            ("Fatal", "CD", 23): "keep-single-extreme-offense",
            ("Fatal", "ATK%", 27): "keep-single-extreme-offense",
            ("Rage", "HP%", 27): "keep-single-extreme-scaling",
            ("Guard", "DEF%", 27): "keep-single-extreme-scaling",
            ("Energy", "RES", 27): "keep-single-extreme-resistance",
            ("Despair", "ACC", 27): "keep-single-extreme-accuracy",
        }
        for (set_name, stat, threshold), rule_id in compatible.items():
            with self.subTest(set=set_name, stat=stat):
                self.assertEqual(
                    evaluate_rune(rune(set_name, stat, threshold), config).rule_id,
                    rule_id,
                )
                self.assertEqual(
                    evaluate_rune(rune(set_name, stat, threshold - 1), config).action,
                    "SELL",
                )

        # A single tank/control roll does not fit Fatal's attack purpose. Such
        # a rune can still be kept by a complete bomber/output build rule.
        self.assertEqual(evaluate_rune(rune("Fatal", "RES", 27), config).action, "SELL")
        self.assertEqual(evaluate_rune(rune("Fatal", "ACC", 27), config).action, "SELL")
        self.assertEqual(evaluate_rune(rune("Fatal", "HP%", 27), config).action, "SELL")
        self.assertEqual(evaluate_rune(rune("Energy", "HP", 1000), config).action, "SELL")

    def test_fatal_attack_rune_is_not_kept_for_extreme_resistance_alone(self):
        rune = {
            "recognized": True,
            "stars": 6,
            "quality": "Hero",
            "set": "Fatal",
            "slot": 1,
            "level": 12,
            "main_stat": {"type": "ATK", "value": 118},
            "innate": None,
            "substats": [
                {"type": "RES", "value": 27},
                {"type": "CD", "value": 5},
                {"type": "SPD", "value": 5},
                {"type": "HP%", "value": 6},
            ],
        }
        rune["efficiency"] = calculate_efficiency(rune)

        decision = evaluate_rune(rune, default_config())
        self.assertEqual(decision.action, "SELL")
        self.assertIsNone(decision.rule_id)

    def test_legacy_universal_extreme_rule_is_replaced_and_keeps_enabled_state(self):
        old = {
            "version": 1,
            "preset": "account-tuned-forodo-29649161-v2-top60",
            "fallback": "sell",
            "groups": [{
                "id": "keep-single-extreme-substat",
                "name": "任意套装单条极品副属性",
                "enabled": False,
                "action": "keep",
                "match": "any",
                "rules": [{"field": "baseSubstats.RES", "op": "gte", "value": 27}],
            }],
        }

        merged = merge_default_groups(old)
        by_id = {group["id"]: group for group in merged["groups"]}
        self.assertNotIn("keep-single-extreme-substat", by_id)
        self.assertTrue(SINGLE_EXTREME_REPLACEMENT_IDS.issubset(by_id))
        self.assertTrue(all(not by_id[group_id]["enabled"] for group_id in SINGLE_EXTREME_REPLACEMENT_IDS))

    def test_key_sets_keep_ungrinded_speed_twenty_without_relaxing_off_sets(self):
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] == "keep-key-set-base-speed-20"
            ],
        }

        def rune(set_name, speed, *, grind=0, quality="Hero"):
            return {
                "stars": 6, "quality": quality, "set": set_name, "slot": 2,
                "main_stat": {"type": "HP%", "value": 63},
                "substats": [{"type": "SPD", "value": speed, "grind": grind}],
                "efficiency": {"maxWithGrind": 50},
            }

        self.assertEqual(evaluate_rune(rune("Swift", 20), config).rule_id, "keep-key-set-base-speed-20")
        self.assertEqual(evaluate_rune(rune("Violent", 20), config).rule_id, "keep-key-set-base-speed-20")
        self.assertEqual(evaluate_rune(rune("Will", 20), config).rule_id, "keep-key-set-base-speed-20")
        self.assertEqual(evaluate_rune(rune("Fatal", 20), config).action, "SELL")
        self.assertEqual(evaluate_rune(rune("Swift", 18, grind=6), config).action, "SELL")
        self.assertEqual(evaluate_rune(rune("Swift", 20, quality="Rare"), config).action, "SELL")

    def test_single_extreme_rules_use_ungrinded_values(self):
        specialist_ids = {
            "keep-single-speed-premium-set",
            "keep-single-speed-any-set",
            *SINGLE_EXTREME_REPLACEMENT_IDS,
        }
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in specialist_ids
            ],
        }
        grinded_speed = {
            "stars": 6,
            "quality": "Legend",
            "set": "Fatal",
            "slot": 1,
            "level": 12,
            "main_stat": {"type": "ATK", "value": 160},
            "substats": [{"type": "SPD", "value": 18, "grind": 5}],
            "efficiency": {"maxWithGrind": 50},
        }
        grinded_crit = {
            **grinded_speed,
            "substats": [{"type": "CR", "value": 17, "grind": 3}],
        }

        self.assertEqual(evaluate_rune(grinded_speed, config).action, "SELL")
        self.assertEqual(evaluate_rune(grinded_crit, config).action, "SELL")

    def test_single_speed_upgrade_path_uses_base_rolls(self):
        speed_ids = {"keep-single-speed-premium-set", "keep-single-speed-any-set"}
        config = {
            "version": 1,
            "fallback": "sell",
            "groups": [
                group for group in default_config()["groups"]
                if group["id"] in speed_ids
            ],
        }

        def rune(speed, *, grind=0):
            return {
                "stars": 6,
                "quality": "Legend",
                "set": "Fatal",
                "slot": 1,
                "level": 9,
                "main_stat": {"type": "ATK", "value": 160},
                "substats": [
                    {"type": "SPD", "value": speed, "grind": grind},
                    {"type": "CR", "value": 5},
                    {"type": "ATK%", "value": 5},
                    {"type": "HP%", "value": 5},
                ],
            }

        self.assertTrue(should_upgrade(rune(17), config))
        self.assertFalse(should_upgrade(rune(15), config))
        self.assertFalse(should_upgrade(rune(12, grind=5), config))

    def test_flat_slot_four_six_sell_rule_overrides_single_stat_specialists(self):
        rune = {
            "stars": 6,
            "quality": "Legend",
            "set": "Fatal",
            "slot": 4,
            "level": 12,
            "main_stat": {"type": "ATK", "value": 160},
            "substats": [{"type": "CR", "value": 25}],
            "efficiency": {"maxWithGrind": 120},
        }
        self.assertEqual(evaluate_rune(rune, default_config()).rule_id, "sell-flat-slot46-main")


class RuneJsonImportTests(unittest.TestCase):
    def test_swex_arrays_are_normalized_with_correct_set_and_quality(self):
        rune = normalize_account_rune(
            {
                "rune_id": 1,
                "class": 16,
                "rank": 15,
                "set_id": 13,
                "slot_no": 4,
                "upgrade_curr": 12,
                "pri_eff": [4, 58],
                "prefix_eff": [8, 6],
                "sec_eff": [[8, 15, 0, 5], [9, 12, 1, 0]],
                "occupied_id": 0,
            }
        )
        self.assertEqual(rune["set"], "Violent")
        self.assertEqual(rune["quality"], "Legend")
        self.assertTrue(rune["ancient"])
        self.assertEqual(rune["substats"][0]["grind"], 5)
        self.assertTrue(rune["substats"][1]["gemmed"])

    def test_account_export_contains_equipped_and_inventory_runes(self):
        account = self.synthetic_account()
        with tempfile.TemporaryDirectory() as directory:
            export_path = Path(directory) / "account.json"
            export_path.write_text(json.dumps(account), encoding="utf-8")
            runes = load_account_runes(export_path)
        self.assertEqual(len(runes), 2)
        self.assertTrue(any(rune["equipped"] for rune in runes))
        self.assertTrue(any(rune["set"] == "Violent" for rune in runes))

    @staticmethod
    def synthetic_account():
        rune = {
            "rune_id": 1, "class": 6, "rank": 5, "set_id": 13,
            "slot_no": 2, "upgrade_curr": 12, "pri_eff": [8, 39],
            "sec_eff": [[2, 15, 0, 0], [9, 6, 0, 0]], "occupied_id": 0,
        }
        equipped = {**rune, "rune_id": 2, "occupied_id": 1}
        return {"runes": [rune], "unit_list": [{"unit_id": 1, "runes": [equipped]}]}

    def test_account_preview_includes_equipped_in_keep_sell_prediction(self):
        account = self.synthetic_account()
        result = evaluate_account(account, {"version": 1, "fallback": "sell", "groups": []})
        self.assertGreater(result["summary"]["equipped"], 0)
        self.assertEqual(result["summary"]["total"], 2)
        equipped = [rune for rune in result["runes"] if rune["equipped"]]
        self.assertTrue(equipped)
        self.assertTrue(all(rune["action"] == "SELL" for rune in equipped))
        self.assertEqual(result["summary"]["equipped_sell"], len(equipped))
        self.assertTrue(any(rune["substats"] for rune in result["runes"]))

    def test_account_preview_ui_has_chinese_filters_and_efficiency_sorting(self):
        source = Path("web/app.js").read_text(encoding="utf-8")
        page = Path("web/index.html").read_text(encoding="utf-8")
        for label in ("装备状态", "符文类型", "位置", "主属性", "传说磨炼：高到低", "英雄磨炼：高到低"):
            self.assertIn(label, source)
        for label in ("副属性速度阶梯", "只计算副属性原始速度", "其他套装（散件候选）"):
            self.assertIn(label, source)
        self.assertIn("分析包含仓库与已装备符文", page)
        self.assertNotIn("官方评分", source + page)

    def test_account_json_is_persisted_and_can_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rules.sqlite3"
            first = {"wizard_info": {"wizard_id": 1}, "runes": [{"rune_id": 10}]}
            second = {"wizard_info": {"wizard_id": 1}, "runes": [{"rune_id": 20}]}

            saved = save_sqlite_account(first, "first.json", db_path=db_path)
            self.assertEqual(saved["filename"], "first.json")
            self.assertEqual(load_sqlite_account(db_path=db_path)["account"], first)

            save_sqlite_account(second, "folder/updated.json", db_path=db_path)
            loaded = load_sqlite_account(db_path=db_path)
            self.assertEqual(loaded["filename"], "updated.json")
            self.assertEqual(loaded["account"], second)

    def test_account_preview_backend_returns_normalized_substats(self):
        account = {
            "runes": [{
                "rune_id": 1, "class": 6, "rank": 5, "set_id": 8,
                "slot_no": 1, "upgrade_curr": 15, "pri_eff": [3, 160],
                "prefix_eff": [0, 0], "sec_eff": [[4, 12, 0, 7], [8, 6, 1, 3]],
                "occupied_id": 0,
            }],
        }
        result = evaluate_account(account, {"version": 1, "fallback": "keep", "groups": []})
        self.assertEqual(
            result["runes"][0]["substats"],
            [
                {"type": 4, "value": 12, "grind": 7},
                {"type": 8, "value": 6, "grind": 3, "gemmed": True},
            ],
        )

    def test_speed_tiers_use_ungrinded_substat_speed_and_group_sets(self):
        def rune(rune_id, set_id, speed, grind=0):
            return {
                "rune_id": rune_id, "class": 6, "rank": 5, "set_id": set_id,
                "slot_no": 1, "upgrade_curr": 15, "pri_eff": [3, 160],
                "prefix_eff": [0, 0], "sec_eff": [[8, speed, 0, grind]],
                "occupied_id": 0,
            }

        account = {"runes": [
            rune(1, 3, 31),       # Swift: 30+
            rune(2, 13, 25, 5),   # Violent: displayed 30, base speed is 25
            rune(3, 8, 20),       # Fatal: other/off-set bucket
            rune(4, 15, 19, 6),   # Will: displayed 25, below base 20
        ]}
        result = evaluate_account(account, {"version": 1, "fallback": "keep", "groups": []})
        speed = result["speed_tiers"]
        self.assertEqual(speed["all"]["tiers"], {"30": 1, "25": 2, "20": 3})
        self.assertEqual(speed["key_total"]["tiers"], {"30": 1, "25": 2, "20": 2})
        self.assertEqual(speed["off_set"]["tiers"], {"30": 0, "25": 0, "20": 1})
        violent = next(item for item in speed["by_set"] if item["set"] == "Violent")
        self.assertEqual(violent["tiers"], {"30": 0, "25": 1, "20": 1})


class RuneScannerTests(unittest.TestCase):
    def test_grid_and_scroll_advance_exactly_three_full_rows(self):
        pipeline = json.loads(
            Path("assets/resource/pipeline/rune_inventory.json").read_text(encoding="utf-8")
        )

        self.assertEqual(rune_scanner.GRID_ROWS, (423, 508, 593))
        self.assertEqual(pipeline["SWRuneScrollNextPage"]["begin"], [900, 660])
        self.assertEqual(pipeline["SWRuneScrollNextPage"]["end"], [900, 405])
        self.assertEqual(
            pipeline["SWRuneScrollNextPage"]["begin"][1]
            - pipeline["SWRuneScrollNextPage"]["end"][1],
            3 * 85,
        )

    def test_runtime_scroll_keeps_three_row_distance_with_humanized_timing(self):
        completed = SimpleNamespace(
            status=SimpleNamespace(succeeded=True),
            nodes=[SimpleNamespace(
                name="SWRuneScrollNextPage",
                completed=True,
                action=SimpleNamespace(success=True),
            )],
        )
        context = SimpleNamespace(run_task=Mock(return_value=completed))

        with patch.object(
            rune_scanner.random,
            "randint",
            side_effect=lambda lower, upper: (lower + upper) // 2,
        ):
            self.assertTrue(rune_scanner._scroll_next_page(context))

        override = context.run_task.call_args.kwargs["pipeline_override"]
        scroll = override["SWRuneScrollNextPage"]
        self.assertEqual(scroll["begin"], [900, 660])
        self.assertEqual(scroll["end"], [900, 405])
        self.assertEqual(scroll["begin"][1] - scroll["end"][1], 3 * 85)
        self.assertGreaterEqual(scroll["duration"], 550)
        self.assertGreaterEqual(scroll["post_delay"], 600)

    def test_scroll_rejects_succeeded_task_when_maa_executed_no_node(self):
        stopped = SimpleNamespace(
            status=SimpleNamespace(succeeded=True),
            nodes=[],
        )
        context = SimpleNamespace(run_task=Mock(return_value=stopped))

        self.assertFalse(rune_scanner._scroll_next_page(context))

    def test_click_rejects_succeeded_task_when_maa_executed_no_node(self):
        stopped = SimpleNamespace(
            status=SimpleNamespace(succeeded=True),
            nodes=[],
        )
        context = SimpleNamespace(run_task=Mock(return_value=stopped))

        with self.assertRaisesRegex(rune_scanner._InteractionStopped, "点击.*未实际执行"):
            rune_scanner._click(context, (613, 423))

    def test_gemmed_marker_template_is_scoped_to_substat_rows(self):
        pipeline = json.loads(
            Path("assets/resource/pipeline/rune_inventory.json").read_text(encoding="utf-8")
        )
        marker = pipeline["SWRuneGemmedMarker"]
        self.assertEqual(marker["recognition"], "TemplateMatch")
        self.assertTrue(Path("assets/resource/image") .joinpath(marker["template"]).exists())
        x, y, width, height = marker["roi"]
        self.assertLessEqual(x, 913)
        self.assertLessEqual(y, 160)
        self.assertGreaterEqual(x + width, 935)
        self.assertGreaterEqual(y + height, 272)

    def test_inventory_controls_marker_covers_the_unobstructed_filter(self):
        pipeline = json.loads(
            Path("assets/resource/pipeline/rune_inventory.json").read_text(encoding="utf-8")
        )
        marker = pipeline["SWRuneInventoryControlsReady"]
        self.assertEqual(marker["expected"], "主属性")
        x, y, width, height = marker["roi"]
        self.assertLessEqual(x, 807)
        self.assertLessEqual(y, 69)
        self.assertGreaterEqual(x + width, 900)
        self.assertGreaterEqual(y + height, 105)

    def test_inventory_remains_ready_when_chat_hides_title(self):
        def recognize(_context, name):
            expected = {
                "SWRuneInventoryReady": [],
                "SWRuneInventoryControlsReady": ["主属性"],
                "SWRuneInventoryGridReady": ["分数顺序"],
            }
            return expected.get(name, [])

        with patch.object(rune_scanner, "_ocr_live", side_effect=recognize):
            self.assertTrue(rune_scanner._inventory_unobstructed(object()))

    def test_inventory_marker_rejects_grid_visible_behind_detail(self):
        def recognize(_context, name):
            return ["分数顺序"] if name == "SWRuneInventoryGridReady" else []

        with patch.object(rune_scanner, "_ocr_live", side_effect=recognize):
            self.assertFalse(rune_scanner._inventory_unobstructed(object()))

    def test_upgrade_button_roi_covers_the_detail_action(self):
        pipeline = json.loads(
            Path("assets/resource/pipeline/rune_inventory.json").read_text(encoding="utf-8")
        )
        x, y, width, height = pipeline["SWRuneUpgradeButton"]["roi"]
        # Real OCR boxes place the action at (1151, 223, 80, 47). Keep the
        # whole label inside the ROI while excluding the Sell button below it.
        self.assertLessEqual(x, 1151)
        self.assertLessEqual(y, 223)
        self.assertGreaterEqual(x + width, 1231)
        self.assertGreaterEqual(y + height, 270)
        self.assertLessEqual(y + height, 285)

    def test_sell_confirmation_accepts_only_affirmative_button_text(self):
        for text in ("是", "确定", "确认", "出售", " 确 定 "):
            self.assertTrue(_is_sell_confirmation_text(text))
        for text in ("否", "不是", "取消", "是否出售", ""):
            self.assertFalse(_is_sell_confirmation_text(text))

    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_click_affirmative_button", return_value=True)
    def test_sell_handles_optional_legend_warning_as_a_separate_state(
        self,
        click_affirmative,
        click,
    ):
        warning_calls = 0

        def recognize(_context, name):
            nonlocal warning_calls
            if name == "SWRuneDetailTitle":
                return ["+15 猛攻符文(1)"]
            if name == "SWRuneLegendSellWarning":
                warning_calls += 1
                return ["今日不再提示"] if warning_calls == 1 else []
            if name == "SWRuneInventoryReady":
                return ["符文管理"]
            if name == "SWRuneInventoryGridReady":
                return ["分数顺序"]
            return []

        legend_button_calls = 0

        def recognize_button(_context, name):
            nonlocal legend_button_calls
            if name != "SWRuneLegendSellConfirmButton":
                return None
            legend_button_calls += 1
            return object() if legend_button_calls == 1 else None

        with (
            patch.object(rune_scanner, "_ocr_live", side_effect=recognize),
            patch.object(
                rune_scanner,
                "_recognized_affirmative_button",
                side_effect=recognize_button,
            ) as recognized_affirmative,
        ):
            self.assertTrue(_sell_rune(object()))

        self.assertEqual(
            click_affirmative.call_args_list,
            [
                call(ANY, "SWRuneSellConfirmButton"),
                call(ANY, "SWRuneLegendSellConfirmButton"),
            ],
        )
        self.assertEqual(click.call_args_list[0].args[1], rune_scanner.SELL_BUTTON)
        self.assertEqual(
            [item.args[1] for item in recognized_affirmative.call_args_list],
            [
                "SWRuneLegendSellConfirmButton",
                "SWRuneSellConfirmButton",
                "SWRuneLegendSellConfirmButton",
            ],
        )

    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_click_affirmative_button", return_value=True)
    def test_sell_stops_when_legend_warning_button_is_unknown(self, click_affirmative, click):
        def recognize(_context, name):
            if name == "SWRuneDetailTitle":
                return ["+15 猛攻符文(1)"]
            if name == "SWRuneLegendSellWarning":
                return ["完成的符文"]
            return []

        with (
            patch.object(rune_scanner, "_ocr_live", side_effect=recognize),
            patch.object(rune_scanner, "_recognized_affirmative_button", return_value=None),
        ):
            self.assertFalse(_sell_rune(object()))

        self.assertEqual(click_affirmative.call_count, 1)
        self.assertEqual(click.call_count, 1)

    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_recognized_affirmative_button", return_value=None)
    @patch.object(rune_scanner, "_click_affirmative_button", return_value=True)
    def test_sell_without_legend_warning_requires_unobstructed_inventory(
        self,
        click_affirmative,
        _recognized_affirmative,
        click,
    ):
        def recognize(_context, name):
            expected = {
                "SWRuneDetailTitle": ["+15 猛攻符文(1)"],
                "SWRuneInventoryReady": ["符文管理"],
                "SWRuneInventoryGridReady": ["分数顺序"],
            }
            return expected.get(name, [])

        with patch.object(rune_scanner, "_ocr_live", side_effect=recognize):
            self.assertTrue(_sell_rune(object()))

        click_affirmative.assert_called_once_with(ANY, "SWRuneSellConfirmButton")
        self.assertEqual(click.call_count, 1)

    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_click_affirmative_button", return_value=True)
    def test_sell_closes_legend_prompt_when_only_its_button_is_recognized(
        self,
        click_affirmative,
        click,
    ):
        legend_button_calls = 0

        def recognize_button(_context, name):
            nonlocal legend_button_calls
            if name != "SWRuneLegendSellConfirmButton":
                return None
            legend_button_calls += 1
            return object() if legend_button_calls == 1 else None

        def recognize(_context, name):
            expected = {
                "SWRuneDetailTitle": ["+15 猛攻符文(1)"],
                "SWRuneInventoryReady": ["符文管理"],
                "SWRuneInventoryGridReady": ["分数顺序"],
            }
            return expected.get(name, [])

        with (
            patch.object(rune_scanner, "_ocr_live", side_effect=recognize),
            patch.object(
                rune_scanner,
                "_recognized_affirmative_button",
                side_effect=recognize_button,
            ),
        ):
            self.assertTrue(_sell_rune(object()))

        self.assertEqual(
            [item.args[1] for item in click_affirmative.call_args_list],
            ["SWRuneSellConfirmButton", "SWRuneLegendSellConfirmButton"],
        )
        self.assertEqual(click.call_count, 1)

    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_recognized_affirmative_button", return_value=None)
    @patch.object(rune_scanner, "_click_affirmative_button", return_value=True)
    def test_sell_closes_leftover_detail_only_after_dialogs_are_clear(
        self,
        _click_affirmative,
        _recognized_affirmative,
        click,
    ):
        inventory_title_calls = 0

        def recognize(_context, name):
            nonlocal inventory_title_calls
            if name == "SWRuneDetailTitle":
                return ["+15 猛攻符文(1)"]
            if name == "SWRuneInventoryReady":
                inventory_title_calls += 1
                return [] if inventory_title_calls == 1 else ["符文管理"]
            if name == "SWRuneInventoryGridReady":
                return ["分数顺序"]
            return []

        with patch.object(rune_scanner, "_ocr_live", side_effect=recognize):
            self.assertTrue(_sell_rune(object()))

        self.assertEqual(
            [item.args[1] for item in click.call_args_list],
            [rune_scanner.SELL_BUTTON, rune_scanner.DETAIL_CLOSE],
        )

    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_click_affirmative_button", return_value=False)
    def test_sell_stops_when_ordinary_confirmation_is_unknown(self, click_affirmative, click):
        with patch.object(rune_scanner, "_ocr_live", return_value=["符文"]):
            self.assertFalse(_sell_rune(object()))

        click_affirmative.assert_called_once_with(
            ANY,
            "SWRuneSellConfirmButton",
        )
        self.assertEqual(click.call_count, 1)
        self.assertEqual(click.call_args.args[1], rune_scanner.SELL_BUTTON)

    def test_live_recognition_miss_does_not_fail_a_nested_task(self):
        miss = SimpleNamespace(hit=False)
        refresh = SimpleNamespace(
            status=SimpleNamespace(succeeded=True),
            nodes=[SimpleNamespace(
                name="SWRuneRefreshScreen",
                completed=True,
                action=SimpleNamespace(success=True),
            )],
        )
        controller = SimpleNamespace(cached_image=object())
        context = SimpleNamespace(
            run_task=Mock(return_value=refresh),
            run_recognition=Mock(return_value=miss),
            tasker=SimpleNamespace(controller=controller),
        )

        self.assertIs(rune_scanner._live_recognition(context, "OptionalState"), miss)
        context.run_task.assert_called_once_with("SWRuneRefreshScreen")
        context.run_recognition.assert_called_once_with("OptionalState", controller.cached_image)

    def test_live_recognition_stops_when_refresh_has_no_executed_node(self):
        stopped = SimpleNamespace(
            status=SimpleNamespace(succeeded=True),
            nodes=[],
        )
        context = SimpleNamespace(run_task=Mock(return_value=stopped))

        with self.assertRaisesRegex(rune_scanner._InteractionStopped, "未实际执行"):
            rune_scanner._live_recognition(context, "OptionalState")

    @patch.object(rune_scanner, "_read_detail", return_value=None)
    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_read_config", return_value={"version": 1, "fallback": "keep"})
    def test_scanner_starts_without_reading_a_stale_controller_image(
        self,
        _read_config,
        click,
        _read_detail,
    ):
        def recognize(_context, name):
            expected = {
                "SWRuneInventoryReady": ["符文管理"],
                "SWRuneInventoryGridReady": ["分数顺序"],
            }
            return expected.get(name, [])

        argv = SimpleNamespace(custom_action_param='{"max_pages": 1}')
        with (
            patch.object(rune_scanner, "_ocr_live", side_effect=recognize),
            patch.object(rune_scanner, "GRID_COLUMNS", (613, 697)),
            patch.object(rune_scanner, "GRID_ROWS", (423, 508)),
        ):
            self.assertTrue(rune_scanner.RuneInventoryScanner()._run(object(), argv))

        # An unobstructed empty cell is retried once, then ends this viewport;
        # later coordinates must not be clicked speculatively.
        self.assertEqual(click.call_count, 2)
        self.assertEqual(_read_detail.call_count, 2)

    def test_scanner_empty_cell_ends_viewport_and_scrolls_before_later_cells(self):
        first = {
            "set": "Fatal", "slot": 1, "quality": "Legend", "level": 15,
            "main_stat": {"type": "ATK", "value": 160},
            "substats": [{"type": "CR", "value": 20}],
        }
        later = {
            "set": "Will", "slot": 2, "quality": "Legend", "level": 15,
            "main_stat": {"type": "SPD", "value": 42},
            "substats": [{"type": "HP%", "value": 20}],
        }
        keep = SimpleNamespace(
            action="KEEP", reason="keep", rule_id="keep", uncertain=False
        )

        with (
            patch.object(rune_scanner, "GRID_COLUMNS", (613, 697)),
            patch.object(rune_scanner, "GRID_ROWS", (423,)),
            patch.object(rune_scanner, "_read_config", return_value={"version": 1}),
            patch.object(rune_scanner, "_inventory_unobstructed", return_value=True),
            patch.object(
                rune_scanner,
                "_read_detail",
                side_effect=[first, None, None, later, None, None],
            ),
            patch.object(rune_scanner, "calculate_efficiency", return_value={}),
            patch.object(rune_scanner, "evaluate_rune", return_value=keep) as evaluate,
            patch.object(rune_scanner, "_scroll_next_page", return_value=True) as scroll,
            patch.object(rune_scanner, "_click"),
            patch.object(rune_scanner, "_log_rune"),
        ):
            argv = SimpleNamespace(custom_action_param='{"max_pages": 2}')
            self.assertTrue(rune_scanner.RuneInventoryScanner()._run(object(), argv))

        self.assertEqual(evaluate.call_count, 2)
        scroll.assert_called_once()

    def test_scanner_stops_after_two_consecutive_empty_viewports(self):
        with (
            patch.object(rune_scanner, "GRID_COLUMNS", (613,)),
            patch.object(rune_scanner, "GRID_ROWS", (423,)),
            patch.object(rune_scanner, "_read_config", return_value={"version": 1}),
            patch.object(rune_scanner, "_inventory_unobstructed", return_value=True),
            patch.object(rune_scanner, "_read_detail", return_value=None) as read_detail,
            patch.object(rune_scanner, "_scroll_next_page", return_value=True) as scroll,
            patch.object(rune_scanner, "_click"),
        ):
            argv = SimpleNamespace(custom_action_param='{"max_pages": 5}')
            self.assertTrue(rune_scanner.RuneInventoryScanner()._run(object(), argv))

        self.assertEqual(read_detail.call_count, 4)
        scroll.assert_called_once()

    def test_scanner_stops_early_when_scroll_returns_same_viewport(self):
        runes = [
            {
                "set": set_name,
                "slot": slot,
                "quality": "Legend",
                "level": 15,
                "main_stat": {"type": "ATK", "value": 160},
                "substats": [{"type": "CR", "value": crit}],
            }
            for set_name, slot, crit in (
                ("Fatal", 1, 20),
                ("Will", 2, 18),
                ("Swift", 3, 16),
            )
        ]
        keep = SimpleNamespace(
            action="KEEP", reason="keep", rule_id="keep", uncertain=False
        )

        with (
            patch.object(rune_scanner, "GRID_COLUMNS", (613, 697, 783)),
            patch.object(rune_scanner, "GRID_ROWS", (423,)),
            patch.object(rune_scanner, "_read_config", return_value={"version": 1}),
            patch.object(rune_scanner, "_inventory_unobstructed", return_value=True),
            patch.object(
                rune_scanner,
                "_read_detail",
                side_effect=[*runes, runes[0], runes[1]],
            ) as read_detail,
            patch.object(rune_scanner, "calculate_efficiency", return_value={}),
            patch.object(rune_scanner, "evaluate_rune", return_value=keep) as evaluate,
            patch.object(rune_scanner, "_scroll_next_page", return_value=True) as scroll,
            patch.object(rune_scanner, "_click"),
            patch.object(rune_scanner, "_log_rune"),
        ):
            argv = SimpleNamespace(custom_action_param='{"max_pages": 5}')
            self.assertTrue(rune_scanner.RuneInventoryScanner()._run(object(), argv))

        self.assertEqual(read_detail.call_count, 5)
        self.assertEqual(evaluate.call_count, 3)
        scroll.assert_called_once()

    def test_scanner_rechecks_same_cell_after_sale_shifts_next_rune_left(self):
        sold_rune = {
            "set": "Fatal",
            "slot": 1,
            "quality": "Legend",
            "level": 15,
            "main_stat": {"type": "ATK", "value": 160},
            "substats": [{"type": "CR", "value": 22}],
        }
        shifted_rune = {
            "set": "Swift",
            "slot": 2,
            "quality": "Legend",
            "level": 15,
            "main_stat": {"type": "SPD", "value": 42},
            "substats": [{"type": "HP%", "value": 20}],
        }
        sell = SimpleNamespace(
            action="SELL", reason="fallback", rule_id=None, uncertain=False
        )
        keep = SimpleNamespace(
            action="KEEP", reason="keep", rule_id="keep", uncertain=False
        )
        clicks = []

        with (
            patch.object(rune_scanner, "GRID_COLUMNS", (613,)),
            patch.object(rune_scanner, "GRID_ROWS", (423,)),
            patch.object(rune_scanner, "_read_config", return_value={"version": 1}),
            patch.object(rune_scanner, "_inventory_unobstructed", return_value=True),
            patch.object(
                rune_scanner,
                "_read_detail",
                side_effect=[sold_rune, shifted_rune, None, None],
            ),
            patch.object(rune_scanner, "calculate_efficiency", return_value={}),
            patch.object(rune_scanner, "evaluate_rune", side_effect=[sell, keep]),
            patch.object(rune_scanner, "_sell_rune", return_value=True) as sell_rune,
            patch.object(
                rune_scanner, "_click", side_effect=lambda _context, point, _delay: clicks.append(point)
            ),
            patch.object(rune_scanner, "_log_rune"),
        ):
            argv = SimpleNamespace(custom_action_param='{"max_pages": 1}')
            self.assertTrue(rune_scanner.RuneInventoryScanner()._run(object(), argv))

        sell_rune.assert_called_once()
        grid_clicks = [point for point in clicks if point in {
            (x, y) for y in rune_scanner.GRID_ROWS for x in rune_scanner.GRID_COLUMNS
        }]
        self.assertEqual(grid_clicks[:2], [(613, 423), (613, 423)])

    @patch.object(rune_scanner, "_read_detail")
    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_click_ocr_button", return_value=True)
    def test_upgrade_uses_detail_opened_automatically_after_closing(
        self,
        click_ocr_button,
        click,
        read_detail,
    ):
        rune = {"set": "Fatal", "slot": 1, "level": 6}
        upgraded = {"set": "Fatal", "slot": 1, "level": 9}
        read_detail.return_value = upgraded
        context = SimpleNamespace(wait_freezes=lambda **_kwargs: True)

        def recognize(_context, name):
            expected = {
                "SWRuneUpgradePage": ["符文强化"],
                "SWRuneUpgradeResultPage": ["出售"],
                "SWRuneDetailTitle": ["+9 激怒符文(1)"],
            }
            return expected.get(name, [])

        with patch.object(rune_scanner, "_ocr_live", side_effect=recognize):
            self.assertEqual(_upgrade_once(context, rune, (613, 423)), upgraded)

        self.assertEqual(
            [item.args[1] for item in click_ocr_button.call_args_list],
            [
                "SWRuneUpgradeButton",
                "SWRuneUpgradeExecuteButton",
                "SWRuneUpgradeResultConfirm",
                "SWRuneUpgradeCloseButton",
            ],
        )
        self.assertEqual(
            [item.args[1] for item in click.call_args_list],
            [rune_scanner.UPGRADE_TARGETS[9]],
        )

    @patch.object(rune_scanner, "_read_detail")
    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_click_ocr_button", return_value=True)
    def test_upgrade_from_zero_selects_plus_six(
        self,
        _click_ocr_button,
        click,
        read_detail,
    ):
        rune = {"set": "Fatal", "slot": 1, "level": 0}
        upgraded = {"set": "Fatal", "slot": 1, "level": 6}
        read_detail.return_value = upgraded
        context = SimpleNamespace(wait_freezes=lambda **_kwargs: True)

        def recognize(_context, name):
            expected = {
                "SWRuneUpgradePage": ["符文强化"],
                "SWRuneUpgradeResultPage": ["出售"],
                "SWRuneDetailTitle": ["+6 猛攻符文(1)"],
            }
            return expected.get(name, [])

        with patch.object(rune_scanner, "_ocr_live", side_effect=recognize):
            self.assertEqual(_upgrade_once(context, rune, (613, 423)), upgraded)

        self.assertNotIn(3, rune_scanner.UPGRADE_TARGETS)
        self.assertEqual(
            [item.args[1] for item in click.call_args_list],
            [rune_scanner.UPGRADE_TARGETS[6]],
        )

    @patch.object(rune_scanner, "_read_detail")
    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_click_ocr_button", return_value=True)
    def test_upgrade_reopens_cell_when_close_returns_to_inventory(
        self,
        _click_ocr_button,
        click,
        read_detail,
    ):
        rune = {"set": "Fatal", "slot": 1, "level": 6}
        upgraded = {"set": "Fatal", "slot": 1, "level": 9}
        read_detail.return_value = upgraded
        context = SimpleNamespace(wait_freezes=lambda **_kwargs: True)

        def recognize(_context, name):
            expected = {
                "SWRuneUpgradePage": ["符文强化"],
                "SWRuneUpgradeResultPage": ["出售"],
                "SWRuneInventoryControlsReady": ["主属性"],
                "SWRuneInventoryGridReady": ["分数顺序"],
            }
            return expected.get(name, [])

        with patch.object(rune_scanner, "_ocr_live", side_effect=recognize):
            self.assertEqual(_upgrade_once(context, rune, (613, 423)), upgraded)

        self.assertEqual(
            [item.args[1] for item in click.call_args_list],
            [rune_scanner.UPGRADE_TARGETS[9], (613, 423)],
        )

    @patch.object(rune_scanner, "_read_detail")
    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_click_ocr_button", return_value=True)
    def test_upgrade_stops_without_click_when_post_close_state_is_unknown(
        self,
        _click_ocr_button,
        click,
        read_detail,
    ):
        context = SimpleNamespace(wait_freezes=lambda **_kwargs: True)

        def recognize(_context, name):
            expected = {
                "SWRuneUpgradePage": ["符文强化"],
                "SWRuneUpgradeResultPage": ["出售"],
            }
            return expected.get(name, [])

        with patch.object(rune_scanner, "_ocr_live", side_effect=recognize):
            self.assertIsNone(
                _upgrade_once(context, {"set": "Fatal", "slot": 1, "level": 6}, (613, 423))
            )

        self.assertEqual(
            [item.args[1] for item in click.call_args_list],
            [rune_scanner.UPGRADE_TARGETS[9]],
        )
        read_detail.assert_not_called()

    @patch.object(rune_scanner, "_click")
    @patch.object(rune_scanner, "_click_ocr_button", return_value=True)
    def test_upgrade_stops_if_result_page_is_not_recognized(self, click_ocr_button, click):
        context = SimpleNamespace(wait_freezes=lambda **_kwargs: True)

        def recognize(_context, name):
            return ["符文强化"] if name == "SWRuneUpgradePage" else []

        with patch.object(rune_scanner, "_ocr_live", side_effect=recognize):
            self.assertIsNone(
                _upgrade_once(context, {"set": "Fatal", "slot": 1, "level": 6}, (613, 423))
            )

        self.assertEqual(click_ocr_button.call_count, 2)
        self.assertEqual(click.call_count, 1)

    def test_ocr_box_coordinates_support_raw_lists_and_rect_objects(self):
        self.assertEqual(_box_xy([840, 72, 100, 25]), (840, 72))
        self.assertEqual(_box_xy(SimpleNamespace(x=841, y=73, w=100, h=25)), (841, 73))
        self.assertEqual(
            _box_xy(SimpleNamespace(box=[842, 74, 100, 25])),
            (842, 74),
        )

    def test_slot_one_main_and_innate_are_recovered_from_unordered_ocr(self):
        rune = parse_detail(
            ["+6 猛攻符文(1)"],
            [
                "效果抵抗 +7%", "暴击伤害 +5%", "暴击率 +15%",
                "效果命中 +8%", "攻击力 +6%", "攻击力 +70", "传说",
            ],
        )

        self.assertEqual(rune["main_stat"], {"type": "ATK", "value": 70.0})
        self.assertEqual(rune["innate"], {"type": "RES", "value": 7.0})
        self.assertEqual(len(rune["substats"]), 4)

    def test_refined_and_gemmed_substats_are_parsed_separately(self):
        rune = parse_detail(
            ["+15 猛攻符文(1)"],
            [
                "攻击力+160", "传说", "暴击伤害+4%", "攻击力+12%+7%",
                "暴击率+21%", "攻击速度+6+3", "4件套：攻击力+35%",
            ],
            main_line="攻击力+160",
            gemmed_line="攻击速度+6+3",
        )

        attack = next(stat for stat in rune["substats"] if stat["type"] == "ATK%")
        speed = next(stat for stat in rune["substats"] if stat["type"] == "SPD")
        self.assertEqual(attack, {"type": "ATK%", "value": 12.0, "grind": 7.0})
        self.assertEqual(speed, {"type": "SPD", "value": 6.0, "grind": 3.0, "gemmed": True})

    def test_live_detail_matches_gem_marker_to_its_substat_row(self):
        def result(text, x, y, width=150, height=27):
            return SimpleNamespace(text=text, box=[x, y, width, height])

        title = SimpleNamespace(
            hit=True,
            all_results=[result("+15 猛攻符文(1)", 893, 15, 220, 42)],
            best_result=None,
        )
        body = SimpleNamespace(
            hit=True,
            all_results=[
                result("传说", 1164, 67),
                result("攻击力+160", 858, 70, 162, 34),
                result("暴击伤害+4%", 749, 160),
                result("攻击力+12%+7%", 749, 188),
                result("暴击率+21%", 749, 216),
                result("攻击速度+6+3", 749, 244, 165, 28),
                result("4件套：攻击力+35%", 749, 306),
            ],
            best_result=None,
        )
        marker = SimpleNamespace(
            hit=True,
            box=SimpleNamespace(x=913, y=248, w=22, h=24),
        )

        with patch.object(
            rune_scanner,
            "_live_recognition",
            side_effect=lambda _context, name: {
                "SWRuneDetailTitle": title,
                "SWRuneDetailBody": body,
                "SWRuneGemmedMarker": marker,
            }[name],
        ):
            rune = rune_scanner._read_detail(object())

        speed = next(stat for stat in rune["substats"] if stat["type"] == "SPD")
        self.assertEqual(speed["grind"], 3.0)
        self.assertTrue(speed["gemmed"])

    def test_rune_fingerprint_suppresses_only_equivalent_runes(self):
        rune = {
            "set": "Swift", "slot": 2, "quality": "Legend", "level": 15,
            "main_stat": {"type": "SPD", "value": 42},
            "substats": [{"type": "HP%", "value": 20}, {"type": "RES", "value": 12}],
        }
        equivalent = {
            **rune,
            "source_title": "+15 迅速符文(2)",
            "efficiency": {"current": 90},
        }
        changed = {
            **rune,
            "substats": [{"type": "HP%", "value": 21}, {"type": "RES", "value": 12}],
        }

        self.assertEqual(_rune_fingerprint(rune), _rune_fingerprint(equivalent))
        self.assertNotEqual(_rune_fingerprint(rune), _rune_fingerprint(changed))

    def test_ocr_results_are_sorted_by_visual_position(self):
        def result(text, x, y):
            # Maa 5.13 currently leaves nested OCR boxes as raw JSON lists.
            return SimpleNamespace(text=text, box=[x, y, 100, 25])

        detail = SimpleNamespace(
            all_results=[
                result("暴击率 +15%", 760, 140),
                result("攻击力 +160", 760, 70),
                result("体力 +222", 760, 105),
            ],
            best_result=None,
        )

        self.assertEqual(_texts(detail), ["攻击力 +160", "体力 +222", "暴击率 +15%"])

    def test_unupgraded_detail_parses_percent_stats_and_ignores_set_bonus(self):
        rune = parse_detail(
            ["精巧的祝福符文 (1)"],
            [
                "攻击力 +22", "传说", "效果命中 +6%", "效果抵抗 +7%",
                "体力 +7%", "攻击速度 +4", "暴击率 +4%", "2件套：体力 +15%",
            ],
        )
        self.assertIsNotNone(rune)
        self.assertEqual(rune["level"], 0)
        self.assertEqual(rune["main_stat"], {"type": "ATK", "value": 22.0})
        self.assertIn({"type": "HP%", "value": 7.0}, rune["substats"])
        self.assertNotIn({"type": "HP%", "value": 15.0}, rune["substats"])


if __name__ == "__main__":
    unittest.main()
