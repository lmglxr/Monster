from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional


class MonoRuntimeProbe:
    """Optional read-only Frida bridge for the game's embedded Mono runtime."""

    def __init__(
        self,
        process_id: int,
        boss_monster_id: int,
        script_path: Path,
        boss_monster_ids: Optional[Iterable[int]] = None,
    ):
        self.process_id = int(process_id)
        self.boss_monster_id = int(boss_monster_id)
        configured_ids = boss_monster_ids or (self.boss_monster_id,)
        self.boss_monster_ids = tuple(
            dict.fromkeys(int(monster_id) for monster_id in configured_ids)
        )
        if not self.boss_monster_ids:
            self.boss_monster_ids = (self.boss_monster_id,)
        self.script_path = script_path
        self.available = False
        self.boss_seen = False
        self.boss_dead = False
        self.death_source: Optional[str] = None
        self.latest_hp: Optional[int] = None
        self.latest_max_hp: Optional[int] = None
        self.latest_monster_id: Optional[int] = None
        self.latest_fatigue: Optional[int] = None
        self.latest_scene_id: Optional[int] = None
        self.latest_scene_uid: Optional[int] = None
        self.latest_scene_source: Optional[str] = None
        self.scene_revision = 0
        self.fatigue_revision = 0
        self.dungeon_exit_revision = 0
        self.dungeon_enter_revision = 0
        self.latest_dungeon_exit_ret: Optional[int] = None
        self.latest_dungeon_enter_ret: Optional[int] = None
        self._ready_at: Optional[float] = None
        self._fatigue_updated_at: Optional[float] = None
        self._last_logged_fatigue: Optional[int] = None
        self._last_logged_boss_hp: Optional[tuple[int, int]] = None
        self._last_boss_hp_log_at = 0.0
        self._monster_death_log_state: dict[int, tuple[float, int]] = {}
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self._session: Any = None
        self._script: Any = None
        self._stopping = False

    def start(self) -> bool:
        try:
            import frida

            source = self.script_path.read_text(encoding="utf-8").replace(
                "__BOSS_MONSTER_IDS__", json.dumps(list(self.boss_monster_ids))
            )
            self._session = frida.attach(self.process_id)
            self._session.on("detached", self._on_detached)
            self._script = self._session.create_script(source)
            self._script.on("message", self._on_message)
            self._script.load()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logging.warning("Mono 运行时只读探针启动失败，将使用日志/OCR兜底：%s", exc)
            self.stop()
            return False

    def _on_detached(self, reason: Any, crash: Any = None) -> None:
        detail = str(reason or "unknown")
        if crash:
            detail = f"{detail}; {crash}"
        with self._lock:
            self.available = False
            stopping = self._stopping
            if not stopping:
                self.last_error = f"Mono session detached: {detail}"
        if not stopping:
            logging.warning(
                "Mono 运行时探针已断开，后续允许日志/OCR降级：%s",
                detail,
            )

    def _on_message(self, message: dict, data: Any) -> None:
        if message.get("type") == "error":
            description = str(message.get("description", message))
            with self._lock:
                self.available = False
                self.last_error = description
            logging.warning("Mono 运行时探针脚本异常：%s", description)
            return
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        with self._lock:
            if kind == "probe_ready":
                self.available = True
                self._ready_at = time.monotonic()
                logging.info(
                    "Mono 运行时只读探针已连接：%s 个事件钩子，"
                    "追踪 BOSS MonsterId=%s。",
                    payload.get("hooks"),
                    payload.get("boss_monster_ids", self.boss_monster_ids),
                )
            elif kind == "boss_seen":
                first_seen = not self.boss_seen
                self.boss_seen = True
                if payload.get("monster_id") is not None:
                    self.latest_monster_id = int(payload["monster_id"])
                if payload.get("hp") is not None:
                    self.latest_hp = int(payload["hp"])
                if first_seen:
                    logging.info(
                        "Mono 运行时已识别本轮 BOSS：MonsterId=%s（%s）。",
                        self.latest_monster_id,
                        payload.get("source", "runtime"),
                    )
            elif kind == "boss_dead":
                was_dead = self.boss_dead
                self.boss_seen = True
                self.boss_dead = True
                self.death_source = str(payload.get("source", "Mono runtime"))
                if payload.get("monster_id") is not None:
                    self.latest_monster_id = int(payload["monster_id"])
                if payload.get("hp") is not None:
                    self.latest_hp = int(payload["hp"])
                if not was_dead:
                    logging.info("Mono 运行时收到 BOSS 死亡事件：%s", self.death_source)
            elif kind == "boss_hp":
                if payload.get("monster_id") is not None:
                    self.latest_monster_id = int(payload["monster_id"])
                if payload.get("hp") is not None:
                    self.latest_hp = int(payload["hp"])
                if payload.get("max_hp") is not None:
                    self.latest_max_hp = int(payload["max_hp"])
                hp_pair = (self.latest_hp, self.latest_max_hp)
                now = time.monotonic()
                if (
                    hp_pair != self._last_logged_boss_hp
                    or now - self._last_boss_hp_log_at >= 5.0
                ):
                    logging.info(
                        "Mono Boss HP 更新：MonsterId=%s，当前=%s，最大=%s（%s）。",
                        self.latest_monster_id,
                        self.latest_hp,
                        self.latest_max_hp,
                        payload.get("source", "runtime"),
                    )
                    self._last_logged_boss_hp = hp_pair
                    self._last_boss_hp_log_at = now
            elif kind == "probe_diagnostic":
                if "monster_hp_field_found" in payload:
                    logging.info(
                        "Mono HP 入口诊断：字段 m_HP=%s，字段 m_MaxHP=%s，"
                        "GetCurHp=%s，GetMaxHp=%s。",
                        payload.get("monster_hp_field_found"),
                        payload.get("monster_max_hp_field_found"),
                        payload.get("get_cur_hp_found"),
                        payload.get("get_max_hp_found"),
                    )
                    candidates = payload.get("monster_hp_candidates") or []
                    logging.info("Mono Monster HP 候选入口：%s。", ", ".join(candidates) or "未找到")
                elif payload.get("scene_hook"):
                    logging.info("Mono 场景入口已挂钩：%s。", payload["scene_hook"])
                elif payload.get("scene_info_hook"):
                    logging.info(
                        "Mono 嵌套场景入口已挂钩：%s。",
                        payload["scene_info_hook"],
                    )
                elif payload.get("dungeon_response_hook"):
                    logging.info(
                        "Mono 副本响应入口已挂钩：%s。",
                        payload["dungeon_response_hook"],
                    )
                elif payload.get("dungeon_response_hook_error"):
                    logging.warning(
                        "Mono 副本响应入口不可用，将仅依赖真实场景确认：%s。",
                        payload["dungeon_response_hook_error"],
                    )
                else:
                    logging.warning("Mono HP 方法诊断：%s", payload)
            elif kind == "fatigue":
                value = int(payload["value"])
                if value != self.latest_fatigue:
                    self.latest_fatigue = value
                    self.fatigue_revision += 1
                    self._fatigue_updated_at = time.monotonic()
                    # 每次同步都保留在内存中，但不再把每一点变化都
                    # 打到常规日志。首值和大幅变化才记 INFO，其余只记 DEBUG。
                    should_log = (
                        self._last_logged_fatigue is None
                        or abs(value - self._last_logged_fatigue) >= 50
                    )
                    log = logging.info if should_log else logging.debug
                    log(
                        "Mono 运行时疲劳更新：%s/1000（%s）。",
                        value,
                        payload.get("source", "runtime"),
                    )
                    if should_log:
                        self._last_logged_fatigue = value
            elif kind == "scene_update":
                scene_id = payload.get("scene_id")
                if scene_id is not None:
                    scene_id = int(scene_id)
                    if scene_id <= 0:
                        logging.debug(
                            "忽略无效场景更新 SceneTid=%s（%s）。",
                            scene_id,
                            payload.get("source", "runtime"),
                        )
                        return
                    self.latest_scene_id = scene_id
                    scene_uid = payload.get("scene_uid")
                    self.latest_scene_uid = (
                        None if scene_uid is None else int(scene_uid)
                    )
                    self.latest_scene_source = str(
                        payload.get("source", "runtime")
                    )
                    self.scene_revision += 1
                    logging.info(
                        "Mono 运行时场景更新：SceneTid=%s，SceneUid=%s（%s）。",
                        self.latest_scene_id,
                        self.latest_scene_uid,
                        self.latest_scene_source,
                    )
            elif kind == "dungeon_exit_response":
                self.latest_dungeon_exit_ret = int(payload["ret"])
                self.dungeon_exit_revision += 1
                logging.info(
                    "Mono 已收到副本退出响应：Ret=%s（第 %s 次）。",
                    self.latest_dungeon_exit_ret,
                    self.dungeon_exit_revision,
                )
            elif kind == "dungeon_enter_response":
                self.latest_dungeon_enter_ret = int(payload["ret"])
                self.dungeon_enter_revision += 1
                logging.info(
                    "Mono 已收到副本进入响应：Ret=%s（第 %s 次）。",
                    self.latest_dungeon_enter_ret,
                    self.dungeon_enter_revision,
                )
            elif kind == "monster_dead_observed":
                monster_id = int(payload.get("monster_id", 0))
                now = time.monotonic()
                last_at, count = self._monster_death_log_state.get(
                    monster_id, (0.0, 0)
                )
                count += 1
                # 小怪死亡只保留首条和每 30 秒一次的累计摘要，所有模式共用。
                if now - last_at >= 30.0:
                    logging.info(
                        "Mono 运行时小怪死亡汇总：MonsterId=%s，最近累计 %s 次。",
                        monster_id,
                        count,
                    )
                    self._monster_death_log_state[monster_id] = (now, 0)
                else:
                    self._monster_death_log_state[monster_id] = (last_at, count)
            elif kind in {"probe_error", "probe_warning"}:
                self.last_error = str(payload.get("error", payload))
                logging.warning("Mono 运行时探针：%s", self.last_error)

    def reset_boss_state(self) -> None:
        with self._lock:
            self.boss_seen = False
            self.boss_dead = False
            self.death_source = None
            self.latest_monster_id = None
            self.latest_hp = None
            self.latest_max_hp = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            return {
                "available": self.available,
                "boss_seen": self.boss_seen,
                "boss_dead": self.boss_dead,
                "death_source": self.death_source,
                "latest_monster_id": self.latest_monster_id,
                "latest_hp": self.latest_hp,
                "latest_max_hp": self.latest_max_hp,
                "latest_fatigue": self.latest_fatigue,
                "latest_scene_id": self.latest_scene_id,
                "latest_scene_uid": self.latest_scene_uid,
                "latest_scene_source": self.latest_scene_source,
                "scene_revision": self.scene_revision,
                "fatigue_revision": self.fatigue_revision,
                "dungeon_exit_revision": self.dungeon_exit_revision,
                "dungeon_enter_revision": self.dungeon_enter_revision,
                "latest_dungeon_exit_ret": self.latest_dungeon_exit_ret,
                "latest_dungeon_enter_ret": self.latest_dungeon_enter_ret,
                "ready_age_seconds": (
                    None if self._ready_at is None else now - self._ready_at
                ),
                "fatigue_age_seconds": (
                    None
                    if self._fatigue_updated_at is None
                    else now - self._fatigue_updated_at
                ),
                "last_error": self.last_error,
            }

    def stop(self) -> None:
        script, session = self._script, self._session
        self._script = None
        self._session = None
        with self._lock:
            self._stopping = True
            self.available = False
        if session is None:
            return

        # Frida 在目标线程正经过 Interceptor 时同步 unload/detach 偶尔会
        # 等待很久。放到守护线程并限制等待时间，主脚本退出绝不能被探针拖住。
        def detach() -> None:
            try:
                if script is not None:
                    script.unload()
                session.detach()
            except Exception:
                pass

        cleanup = threading.Thread(target=detach, daemon=True)
        cleanup.start()
        cleanup.join(timeout=2.0)
        if cleanup.is_alive():
            logging.warning("Mono 运行时探针正在后台释放，不阻塞主脚本退出。")
