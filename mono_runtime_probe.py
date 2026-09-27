from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional


class MonoRuntimeProbe:
    """Optional read-only Frida bridge for the game's embedded Mono runtime."""

    def __init__(self, process_id: int, boss_monster_id: int, script_path: Path):
        self.process_id = int(process_id)
        self.boss_monster_id = int(boss_monster_id)
        self.script_path = script_path
        self.available = False
        self.boss_seen = False
        self.boss_dead = False
        self.death_source: Optional[str] = None
        self.latest_hp: Optional[int] = None
        self.latest_fatigue: Optional[int] = None
        self.fatigue_revision = 0
        self._ready_at: Optional[float] = None
        self._fatigue_updated_at: Optional[float] = None
        self._last_logged_fatigue: Optional[int] = None
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self._session: Any = None
        self._script: Any = None
        self._stopping = False

    def start(self) -> bool:
        try:
            import frida

            source = self.script_path.read_text(encoding="utf-8").replace(
                "__BOSS_MONSTER_ID__", json.dumps(self.boss_monster_id)
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
                    "Mono 运行时只读探针已连接：%s 个事件钩子，BOSS MonsterId=%s。",
                    payload.get("hooks"),
                    payload.get("boss_monster_id"),
                )
            elif kind == "boss_seen":
                first_seen = not self.boss_seen
                self.boss_seen = True
                if payload.get("hp") is not None:
                    self.latest_hp = int(payload["hp"])
                if first_seen:
                    logging.info(
                        "Mono 运行时已识别本轮 BOSS：MonsterId=%s（%s）。",
                        payload.get("monster_id", self.boss_monster_id),
                        payload.get("source", "runtime"),
                    )
            elif kind == "boss_dead":
                self.boss_seen = True
                self.boss_dead = True
                self.death_source = str(payload.get("source", "Mono runtime"))
                if payload.get("hp") is not None:
                    self.latest_hp = int(payload["hp"])
                logging.info("Mono 运行时收到 BOSS 死亡事件：%s", self.death_source)
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
            elif kind == "monster_dead_observed":
                monster_id = int(payload.get("monster_id", 0))
                log = logging.debug if monster_id in {10008, 10010, 10011} else logging.info
                log("Mono 运行时观察到怪物死亡：MonsterId=%s。", monster_id)
            elif kind in {"probe_error", "probe_warning"}:
                self.last_error = str(payload.get("error", payload))
                logging.warning("Mono 运行时探针：%s", self.last_error)

    def reset_boss_state(self) -> None:
        with self._lock:
            self.boss_seen = False
            self.boss_dead = False
            self.death_source = None
            self.latest_hp = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            return {
                "available": self.available,
                "boss_seen": self.boss_seen,
                "boss_dead": self.boss_dead,
                "death_source": self.death_source,
                "latest_hp": self.latest_hp,
                "latest_fatigue": self.latest_fatigue,
                "fatigue_revision": self.fatigue_revision,
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
