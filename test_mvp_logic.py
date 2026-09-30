from __future__ import annotations

import unittest
from pathlib import Path

from mono_runtime_probe import MonoRuntimeProbe
from mvp_bot import Bot


class _WindowStub:
    def __init__(self) -> None:
        self.keys: list[tuple[str, float, bool]] = []

    def tap_key(self, key: str, hold_seconds: float, live: bool) -> None:
        self.keys.append((key, hold_seconds, live))


class _ProbeStub:
    def __init__(self, value: int, age: float) -> None:
        self.value = value
        self.age = age

    def snapshot(self) -> dict:
        return {
            "available": True,
            "latest_fatigue": self.value,
            "fatigue_age_seconds": self.age,
        }


class BotLogicTests(unittest.TestCase):
    @staticmethod
    def bare_bot() -> Bot:
        bot = Bot.__new__(Bot)
        bot.stopped = False
        bot.running = True
        bot.live = False
        bot.phase = "idle"
        bot.revive_epoch = 0
        bot.last_fatigue = None
        return bot

    def test_runtime_fatigue_rejects_stale_value(self) -> None:
        bot = self.bare_bot()
        bot.cfg = {"fatigue": {"runtime_stale_fallback_seconds": 180.0}}
        bot.runtime_probe = _ProbeStub(123, 5.0)
        self.assertEqual(bot.runtime_fatigue(), 123)

        bot.runtime_probe = _ProbeStub(123, 181.0)
        self.assertIsNone(bot.runtime_fatigue())

    def test_world_navigation_mounts_once(self) -> None:
        bot = self.bare_bot()
        bot.cfg = {
            "travel_mount": {
                "enabled": True,
                "key": "F",
                "after_path_click_seconds": 0.4,
                "hold_seconds": 0.08,
                "settle_seconds": 0.8,
            }
        }
        bot.window = _WindowStub()
        waits: list[float] = []
        bot.wait = lambda seconds: waits.append(seconds) or True

        bot.wait_navigation_with_mount(10.0, "普通地图寻路")

        self.assertEqual(bot.window.keys, [("F", 0.08, False)])
        self.assertAlmostEqual(sum(waits), 10.0)

    def test_dungeon_boss_navigation_never_mounts(self) -> None:
        bot = self.bare_bot()
        bot.cfg = {"timing": {"boss_auto_path_seconds": 12.0}}
        events: list[tuple[str, object]] = []
        bot.set_phase = lambda name, combat=False: events.append(("phase", name))
        bot.open_map = lambda: events.append(("map", "M"))
        bot.click = lambda name: events.append(("click", name))
        bot.wait = lambda seconds: events.append(("wait", seconds)) or True
        bot.wait_navigation_with_mount = lambda *_: self.fail(
            "副本内前往 BOSS 不得调用骑乘寻路"
        )

        bot.travel_to_boss()

        self.assertIn(("map", "M"), events)
        self.assertIn(("click", "dungeon_boss_marker"), events)
        self.assertIn(("wait", 12.0), events)

    def test_target_fatigue_returns_to_farm_flow_immediately(self) -> None:
        bot = self.bare_bot()
        bot.cfg = {
            "boss_loop": {"minimum_runs": 10},
            "fatigue": {"boss_target": 800},
        }
        events: list[tuple[str, object]] = []
        bot.travel_to_first_entrance = lambda: events.append(("travel", "entrance"))
        bot.enter_dungeon = lambda: events.append(("enter", "dungeon"))
        bot.travel_to_boss = lambda: events.append(("travel", "boss"))
        bot.switch_weapon = lambda mode: events.append(("weapon", mode)) or True
        bot.fight_boss = lambda: events.append(("fight", "boss")) or True
        bot.pickup_and_exit = lambda: events.append(("exit", "dungeon"))
        bot.runtime_fatigue = lambda: 801
        bot.sell_cycle_medicine = lambda: events.append(("sell", "medicine")) or True
        bot.travel_to_farm_spot = lambda: events.append(("travel", "farm"))

        bot.boss_recovery_loop()

        self.assertEqual(
            events[-3:],
            [("weapon", "farm"), ("sell", "medicine"), ("travel", "farm")],
        )
        self.assertEqual(events.count(("fight", "boss")), 1)

    def test_exit_uses_loading_screen_confirmation_before_scene_id(self) -> None:
        bot = self.bare_bot()
        bot._dungeon_exit_started = False
        bot.cfg = {
            "timing": {
                "exit_click_attempts": 3,
                "exit_confirm_settle_seconds": 1.2,
                "exit_load_seconds": 3.0,
                "scene_transition_timeout_seconds": 8.0,
            }
        }
        bot.runtime_probe = None
        bot.log_tail = type("LogTailStub", (), {"scene_revision": 0})()
        events: list[tuple[str, object]] = []
        bot.set_phase = lambda name, combat=False: events.append(("phase", name))
        bot.click = lambda name: events.append(("click", name))
        bot.wait = lambda seconds: events.append(("wait", seconds)) or True
        bot.wait_for_exit_confirm_dialog = lambda timeout: True
        bot.wait_for_exit_loading_start = lambda timeout: True
        bot.wait_for_exit_loading_end = lambda timeout: True
        bot.wait_for_scene = lambda *_args, **_kwargs: self.fail(
            "已确认加载画面时不得等待失效的场景 ID"
        )

        bot.exit_dungeon()

        self.assertEqual(
            [event for event in events if event[0] == "click"],
            [("click", "exit_dungeon_button"), ("click", "confirm_exit_button")],
        )
        self.assertTrue(bot._dungeon_exit_started)

    def test_exit_does_not_click_confirm_without_visible_dialog(self) -> None:
        bot = self.bare_bot()
        bot._dungeon_exit_started = False
        bot.cfg = {
            "timing": {
                "exit_click_attempts": 1,
                "exit_confirm_timeout_seconds": 5.0,
                "exit_load_seconds": 3.0,
                "scene_transition_timeout_seconds": 8.0,
            }
        }
        bot.runtime_probe = None
        bot.log_tail = type("LogTailStub", (), {"scene_revision": 0})()
        clicks: list[str] = []
        bot.set_phase = lambda *_args, **_kwargs: None
        bot.click = lambda name: clicks.append(name)
        bot.wait_for_exit_confirm_dialog = lambda timeout: False

        with self.assertRaisesRegex(RuntimeError, "未收到普通场景确认"):
            bot.exit_dungeon()

        self.assertEqual(clicks, ["exit_dungeon_button"])

    def test_exit_confirm_uses_orange_button_fallback(self) -> None:
        bot = self.bare_bot()
        bot.cfg = {"timing": {}}
        bot.exit_confirm_dialog_visible = lambda: (False, 0.42)
        bot.exit_confirm_button_fallback_visible = lambda: (True, 0.31)
        bot.wait = lambda seconds: self.fail(
            "确认键色彩兜底命中后不应继续等待"
        )

        self.assertTrue(bot.wait_for_exit_confirm_dialog(5.0))

    def test_exit_retries_confirm_when_dialog_remains_after_no_scene(self) -> None:
        bot = self.bare_bot()
        bot._dungeon_exit_started = False
        bot.cfg = {
            "timing": {
                "exit_click_attempts": 1,
                "exit_confirm_timeout_seconds": 5.0,
                "exit_load_seconds": 3.0,
                "scene_transition_timeout_seconds": 8.0,
            }
        }
        bot.runtime_probe = None
        bot.log_tail = type("LogTailStub", (), {"scene_revision": 0})()
        clicks: list[str] = []
        scene_results = iter((False, True))
        bot.set_phase = lambda *_args, **_kwargs: None
        bot.click = lambda name: clicks.append(name)
        bot.wait = lambda seconds: True
        bot.wait_for_exit_confirm_dialog = lambda timeout: True
        bot.wait_for_exit_loading_start = lambda timeout: False
        bot.wait_for_scene = lambda *_args, **_kwargs: next(scene_results)

        bot.exit_dungeon()

        self.assertEqual(
            clicks,
            [
                "exit_dungeon_button",
                "confirm_exit_button",
                "confirm_exit_button",
            ],
        )
        self.assertTrue(bot._dungeon_exit_started)

    def test_boss_loot_uses_repeated_short_pickup_presses(self) -> None:
        bot = self.bare_bot()
        bot.cfg = {
            "combat": {
                "pickup_key": "SPACE",
                "combat_key_hold_seconds": 0.05,
                "boss_loot_pickup_presses": 3,
                "boss_loot_pickup_interval_seconds": 0.5,
            }
        }
        bot.window = _WindowStub()
        waits: list[float] = []
        bot.set_phase = lambda *_args, **_kwargs: None
        bot.wait = lambda seconds: waits.append(seconds) or True
        exits: list[str] = []
        bot.exit_dungeon = lambda: exits.append("exit")

        bot.pickup_and_exit()

        self.assertEqual(
            bot.window.keys,
            [
                ("SPACE", 0.05, False),
                ("SPACE", 0.05, False),
                ("SPACE", 0.05, False),
            ],
        )
        self.assertEqual(waits, [0.5, 0.5])
        self.assertEqual(exits, ["exit"])


class MonoRuntimeProbeMessageTests(unittest.TestCase):
    @staticmethod
    def bare_probe() -> MonoRuntimeProbe:
        return MonoRuntimeProbe(
            process_id=0,
            boss_monster_id=10005,
            script_path=Path("unused.js"),
        )

    def test_scene_update_records_nested_scene_identity(self) -> None:
        probe = self.bare_probe()
        probe._on_message(
            {
                "payload": {
                    "type": "scene_update",
                    "scene_id": 1002,
                    "scene_uid": 987654,
                    "source": "Protoc.SceneChangeRsp.set_ResSceneInfo",
                }
            },
            None,
        )

        snapshot = probe.snapshot()
        self.assertEqual(snapshot["latest_scene_id"], 1002)
        self.assertEqual(snapshot["latest_scene_uid"], 987654)
        self.assertEqual(
            snapshot["latest_scene_source"],
            "Protoc.SceneChangeRsp.set_ResSceneInfo",
        )
        self.assertEqual(snapshot["scene_revision"], 1)

    def test_dungeon_exit_response_is_exposed_in_snapshot(self) -> None:
        probe = self.bare_probe()
        probe._on_message(
            {"payload": {"type": "dungeon_exit_response", "ret": 0}},
            None,
        )

        snapshot = probe.snapshot()
        self.assertEqual(snapshot["latest_dungeon_exit_ret"], 0)
        self.assertEqual(snapshot["dungeon_exit_revision"], 1)

    def test_invalid_scene_zero_does_not_create_transition(self) -> None:
        probe = self.bare_probe()
        probe._on_message(
            {
                "payload": {
                    "type": "scene_update",
                    "scene_id": 0,
                    "source": "Protoc.SceneChangeRsp.set_ResSceneInfo",
                }
            },
            None,
        )

        snapshot = probe.snapshot()
        self.assertIsNone(snapshot["latest_scene_id"])
        self.assertEqual(snapshot["scene_revision"], 0)

    def test_dark_boss_hp_event_retains_monster_id(self) -> None:
        probe = MonoRuntimeProbe(
            process_id=0,
            boss_monster_id=10005,
            boss_monster_ids=(10005, 10009),
            script_path=Path("unused.js"),
        )
        probe._on_message(
            {
                "payload": {
                    "type": "boss_hp",
                    "monster_id": 10009,
                    "hp": 26000,
                    "max_hp": 26000,
                    "source": "Monster.OnShow.fields",
                }
            },
            None,
        )

        snapshot = probe.snapshot()
        self.assertEqual(probe.boss_monster_ids, (10005, 10009))
        self.assertEqual(snapshot["latest_monster_id"], 10009)
        self.assertEqual(snapshot["latest_max_hp"], 26000)


if __name__ == "__main__":
    unittest.main()
