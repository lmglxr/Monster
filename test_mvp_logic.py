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


if __name__ == "__main__":
    unittest.main()
