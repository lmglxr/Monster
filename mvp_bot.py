from __future__ import annotations

import argparse
from collections import Counter
import ctypes
import json
import logging
import math
import os
import re
import sys
import time
import warnings
import winsound
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import ImageGrab
import win32api
import win32con
import win32event
import win32gui
import win32process
import winerror


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
ASSET_DIR = APP_DIR / "assets"
RUNTIME_LOG = APP_DIR / "runtime.log"
DEBUG_DIR = APP_DIR / "debug"

VK = {
    "A": 0x41,
    "SPACE": win32con.VK_SPACE,
    "ESC": win32con.VK_ESCAPE,
    "F2": win32con.VK_F2,
    "F8": win32con.VK_F8,
    "F12": win32con.VK_F12,
}

# 游戏用分母表示疲劳所在的正负区间：xxx/1000 为正，xxx/-1000 为负。
# 数字左侧的文字残影偶尔会被 EasyOCR 误认成负号，因此忽略分子符号，
# 只根据分母前是否存在负号决定最终结果的正负。
FATIGUE_RE = re.compile(r"(?<!\d)-?(\d{1,4})\s*/\s*(-?)\s*1000\b")
COMBAT_RE = re.compile(
    r"场景:(\d+)\s+当前场景:(\d+)\s+目标:(\d+).*?HP:(\d+).*?生命:(\w+)"
)
SCENE_RE = re.compile(r"当前场景:(\d+)")


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    raw_log_value = str(cfg.get("log_path", "auto")).strip()
    raw_log_path = Path(raw_log_value)
    if raw_log_value.lower() != "auto" and not raw_log_path.is_absolute():
        cfg["log_path"] = str((APP_DIR / raw_log_path).resolve())
    return cfg


def save_config(cfg: dict) -> None:
    saved = dict(cfg)
    if str(cfg.get("log_path", "auto")).lower() != "auto":
        try:
            saved["log_path"] = os.path.relpath(cfg["log_path"], APP_DIR).replace("\\", "/")
        except ValueError:
            pass
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False, indent=2)
        f.write("\n")


def pressed(vk: int) -> bool:
    return bool(win32api.GetAsyncKeyState(vk) & 0x8000)


def wait_key_edge(vk: int, stop_vk: int = VK["F12"]) -> bool:
    was_down = pressed(vk)
    while True:
        if pressed(stop_vk):
            return False
        down = pressed(vk)
        if down and not was_down:
            while pressed(vk):
                time.sleep(0.03)
            return True
        was_down = down
        time.sleep(0.03)


@dataclass
class GameWindow:
    title: str
    hwnd: int = 0
    reconnect_attempts: int = 1
    reconnect_interval_seconds: float = 2.0
    capture_retries: int = 5
    capture_retry_seconds: float = 0.5
    reconnect_epoch: int = 0

    def locate(self, attempts: Optional[int] = None) -> None:
        attempts = max(1, int(attempts or self.reconnect_attempts))
        previous_hwnd = self.hwnd
        previous_was_valid = bool(previous_hwnd and win32gui.IsWindow(previous_hwnd))
        candidate_count = 0

        for attempt in range(1, attempts + 1):
            candidates: list[int] = []

            def callback(hwnd: int, _: object) -> None:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                if win32gui.GetWindowText(hwnd).strip().lower() == self.title.lower():
                    candidates.append(hwnd)

            win32gui.EnumWindows(callback, None)
            candidate_count = len(candidates)
            if candidate_count == 1:
                self.hwnd = candidates[0]
                if previous_hwnd and (
                    not previous_was_valid or previous_hwnd != self.hwnd
                ):
                    self.reconnect_epoch += 1
                    logging.warning(
                        "已重新连接游戏窗口（第 %s/%s 次查找）。", attempt, attempts
                    )
                return
            if attempt < attempts:
                logging.warning(
                    "暂时无法唯一定位游戏窗口（找到 %s 个），%.1f 秒后重试 %s/%s。",
                    candidate_count,
                    self.reconnect_interval_seconds,
                    attempt + 1,
                    attempts,
                )
                time.sleep(self.reconnect_interval_seconds)
        raise RuntimeError(
            f"连续 {attempts} 次查找后，仍无法唯一定位标题为 {self.title!r} 的窗口；当前找到 {candidate_count} 个。"
        )

    def ensure(self) -> None:
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            self.locate()

    def client_size(self) -> tuple[int, int]:
        self.ensure()
        left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
        return right - left, bottom - top

    def client_origin(self) -> tuple[int, int]:
        self.ensure()
        return win32gui.ClientToScreen(self.hwnd, (0, 0))

    def is_foreground(self) -> bool:
        self.ensure()
        return win32gui.GetForegroundWindow() == self.hwnd

    def executable_path(self) -> Path:
        """Return the executable behind the game window without assuming Steam's path."""
        self.ensure()
        _, process_id = win32process.GetWindowThreadProcessId(self.hwnd)
        process = win32api.OpenProcess(0x1000, False, process_id)
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_ulong(len(buffer))
            query = ctypes.windll.kernel32.QueryFullProcessImageNameW
            query.argtypes = (
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_ulong),
            )
            query.restype = ctypes.c_int
            if not query(
                ctypes.c_void_p(int(process)), 0, buffer, ctypes.byref(size)
            ):
                raise ctypes.WinError()
            return Path(buffer.value).resolve()
        finally:
            win32api.CloseHandle(process)

    def require_foreground(self) -> None:
        if not self.is_foreground():
            raise RuntimeError("游戏不在前台，已暂停发送输入。切回游戏后按 F8 继续。")

    def capture(self) -> np.ndarray:
        last_error: Optional[Exception] = None
        retries = max(1, int(self.capture_retries))
        for attempt in range(1, retries + 1):
            try:
                self.ensure()
                x, y = self.client_origin()
                w, h = self.client_size()
                if w <= 0 or h <= 0:
                    raise RuntimeError(f"游戏客户区尺寸无效：{w}x{h}")
                image = ImageGrab.grab(
                    bbox=(x, y, x + w, y + h), all_screens=True
                )
                pixels = np.asarray(image)
                if pixels.size == 0:
                    raise RuntimeError("游戏窗口截图为空")
                return cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
            except (OSError, ValueError, RuntimeError, cv2.error) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                logging.warning(
                    "游戏截图失败：%s；%.1f 秒后重新获取窗口并重试 %s/%s。",
                    exc,
                    self.capture_retry_seconds,
                    attempt + 1,
                    retries,
                )
                # 保留旧句柄供 locate 判断这是一次重连，并强制重新枚举窗口。
                time.sleep(self.capture_retry_seconds)
                reconnect_epoch = self.reconnect_epoch
                self.locate()
                if self.reconnect_epoch == reconnect_epoch:
                    self.reconnect_epoch += 1
        raise RuntimeError(f"连续 {retries} 次游戏截图失败：{last_error}")

    def normalized_to_client(self, point: list[float]) -> tuple[int, int]:
        w, h = self.client_size()
        return int(round(point[0] * w)), int(round(point[1] * h))

    def client_to_normalized(self, point: tuple[int, int]) -> list[float]:
        w, h = self.client_size()
        return [round(point[0] / w, 6), round(point[1] / h, 6)]

    def move_client(self, x: int, y: int, live: bool = True) -> None:
        if not live:
            logging.info("DRY move (%s, %s)", x, y)
            return
        self.require_foreground()
        ox, oy = self.client_origin()
        win32api.SetCursorPos((ox + x, oy + y))

    def click_client(self, x: int, y: int, live: bool = True) -> None:
        if not live:
            logging.info("DRY click (%s, %s)", x, y)
            return
        self.move_client(x, y, live=True)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def click_normalized(self, point: list[float], live: bool = True) -> None:
        self.click_client(*self.normalized_to_client(point), live=live)

    def tap_key(self, name: str, hold_seconds: float = 0.05, live: bool = True) -> None:
        vk = VK[name.upper()]
        if not live:
            logging.info("DRY key %s hold %.2fs", name, hold_seconds)
            return
        self.require_foreground()
        win32api.keybd_event(vk, 0, 0, 0)
        time.sleep(hold_seconds)
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)

    def release_keys(self, names: tuple[str, ...], live: bool = True) -> None:
        if not live:
            logging.info("DRY release keys %s", ", ".join(names))
            return
        # 释放按键不能因窗口暂时消失而阻塞 F12 停止流程；句柄无效或不在
        # 前台时直接跳过，避免把全局 KEYUP 发送给其他程序。
        if (
            not self.hwnd
            or not win32gui.IsWindow(self.hwnd)
            or win32gui.GetForegroundWindow() != self.hwnd
        ):
            return
        for name in names:
            win32api.keybd_event(VK[name.upper()], 0, win32con.KEYEVENTF_KEYUP, 0)


class LogTail:
    def __init__(self, path: str):
        self.path = Path(path)
        self.position = 0
        self.latest_scene: Optional[int] = None
        self.scene_revision = 0
        self.boss_seen = False
        self.boss_dead = False

    def start_at_end(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"找不到游戏日志：{self.path}")
        self.position = self.path.stat().st_size

    def poll(self) -> list[str]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.position:
            self.position = 0
        if size == self.position:
            return []
        with self.path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(self.position)
            text = f.read()
            self.position = f.tell()
        lines = text.splitlines()
        for line in lines:
            scene_match = SCENE_RE.search(line)
            if scene_match:
                self.latest_scene = int(scene_match.group(1))
                self.scene_revision += 1
            match = COMBAT_RE.search(line)
            if not match:
                continue
            scene = int(match.group(2))
            target = int(match.group(3))
            hp = int(match.group(4))
            life = match.group(5)
            self.latest_scene = scene
            if target == 100043:
                self.boss_seen = True
                if hp == 0 or life == "LifeDead":
                    self.boss_dead = True
        return lines


def resolve_log_path(configured_path: str, window: GameWindow) -> Path:
    """Resolve Player.log, using the running game's directory for portable installs."""
    if str(configured_path).strip().lower() != "auto":
        return Path(configured_path).resolve()

    executable = window.executable_path()
    candidates = (
        executable.parent / "Logs" / "Player.log",
        executable.parent / "Player.log",
    )
    for candidate in candidates:
        if candidate.is_file():
            logging.info("已根据游戏进程自动找到日志：%s", candidate)
            return candidate
    checked = "、".join(str(path) for path in candidates)
    raise FileNotFoundError(
        f"已找到游戏 {executable}，但没有找到 Player.log（检查过：{checked}）。"
        "可在 config.json 的 log_path 中填写实际路径。"
    )


class FatigueOCR:
    def __init__(self, debug_cfg: Optional[dict] = None) -> None:
        import easyocr

        logging.info("正在加载 EasyOCR 数字识别模型……")
        warnings.filterwarnings(
            "ignore",
            message=r"'pin_memory' argument is set as true but no accelerator is found.*",
            category=UserWarning,
        )
        self.reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        self.debug_cfg = debug_cfg or {}
        self.last_debug_save = 0.0

    def _save_failure(self, crop: np.ndarray) -> None:
        now = time.time()
        min_interval = float(self.debug_cfg.get("ocr_failure_min_interval_seconds", 60))
        if now - self.last_debug_save < min_interval:
            return
        self.last_debug_save = now
        DEBUG_DIR.mkdir(exist_ok=True)
        cv2.imwrite(str(DEBUG_DIR / f"ocr_failed_{int(now)}.png"), crop)
        limit = max(1, int(self.debug_cfg.get("max_ocr_failure_images", 20)))
        images = sorted(
            DEBUG_DIR.glob("ocr_failed_*.png"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in images[limit:]:
            try:
                stale.unlink()
            except OSError:
                logging.warning("无法删除旧调试截图：%s", stale)

    @staticmethod
    def _crop(frame: np.ndarray, region: list[float]) -> np.ndarray:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = region
        return frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]

    @staticmethod
    def _extract_value(texts: list[str]) -> Optional[int]:
        joined = " ".join(texts)
        match = FATIGUE_RE.search(joined)
        if not match:
            return None
        magnitude = int(match.group(1))
        value = -magnitude if match.group(2) == "-" else magnitude
        return value if -1000 <= value <= 1000 else None

    def _recognize(self, image: np.ndarray, scale: float = 2.5) -> list[str]:
        enlarged = cv2.resize(
            image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
        )
        return self.reader.readtext(
            enlarged,
            detail=0,
            paragraph=False,
            allowlist="0123456789/-:",
        )

    def read(
        self,
        frame: np.ndarray,
        region: list[float],
        value_region: Optional[list[float]] = None,
    ) -> tuple[Optional[int], list[str]]:
        crop = self._crop(frame, region)
        if crop.size == 0:
            return None, []

        # 先只识别提示框第一行。疲劳数值为红色，使用 R-max(G,B) 可以消除
        # 灰色说明文字和冰面背景；这对 989/1000、805/1000 一类红字明显更稳。
        if value_region is None:
            value_region = [0.895, 0.785, 0.998, 0.835]
        value_crop = self._crop(frame, value_region)
        diagnostic_texts: list[str] = []
        candidates: list[int] = []
        if value_crop.size:
            red = value_crop[:, :, 2].astype(np.float32)
            green_blue = np.maximum(value_crop[:, :, 1], value_crop[:, :, 0]).astype(
                np.float32
            )
            red_difference = cv2.normalize(
                red - green_blue, None, 0, 255, cv2.NORM_MINMAX
            ).astype(np.uint8)
            red_texts = self._recognize(red_difference, scale=4.0)
            diagnostic_texts.extend(f"red:{text}" for text in red_texts)
            value = self._extract_value(red_texts)
            if value is not None:
                candidates.append(value)

            focused_texts = self._recognize(value_crop, scale=4.0)
            diagnostic_texts.extend(f"focus:{text}" for text in focused_texts)
            value = self._extract_value(focused_texts)
            if value is not None:
                candidates.append(value)

        # 保留原来的整块提示框识别作为兼容兜底。
        texts = self._recognize(crop)
        diagnostic_texts.extend(texts)
        value = self._extract_value(texts)
        if value is not None:
            candidates.append(value)
        if candidates:
            # 红色增强偶尔会漏掉很细的负号；原图/整框两路一致时由多数票纠正。
            counts = Counter(candidates)
            selected = max(
                enumerate(candidates), key=lambda item: (counts[item[1]], -item[0])
            )[1]
            return selected, diagnostic_texts
        self._save_failure(
            cv2.resize(crop, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
        )
        return None, diagnostic_texts


class Bot:
    def __init__(self, cfg: dict, live: bool, mode: str):
        self.cfg = cfg
        self.live = live
        self.mode = mode
        recovery_cfg = cfg.get("recovery", {})
        self.window = GameWindow(
            cfg["window_title"],
            reconnect_attempts=max(
                1, int(recovery_cfg.get("window_reconnect_attempts", 5))
            ),
            reconnect_interval_seconds=float(
                recovery_cfg.get("window_reconnect_interval_seconds", 2.0)
            ),
            capture_retries=max(1, int(recovery_cfg.get("capture_retries", 5))),
            capture_retry_seconds=float(
                recovery_cfg.get("capture_retry_seconds", 0.5)
            ),
        )
        self.window.locate()
        resolved_log_path = resolve_log_path(cfg.get("log_path", "auto"), self.window)
        self.log_tail = LogTail(str(resolved_log_path))
        self.log_tail.start_at_end()
        # OCR 模型占用明显高于其余模块；延迟到完整闭环真正读取疲劳时再加载。
        self.ocr: Optional[FatigueOCR] = None
        self.portal_template = self._load_gray_template("portal.png")
        self.panel_template = self._load_gray_template("entry_panel_title.png")
        self.map_template = self._load_gray_template("map_open_indicator.png")
        self.revive_template = self._load_gray_template("revive_panel_title.png")
        self.running = False
        self.stopped = False
        self.last_fatigue: Optional[int] = None
        self.phase = "idle"
        self.revive_epoch = 0
        self._next_revive_probe = 0.0
        self._f8_down = False
        self._f12_down = False
        self._window_reconnect_epoch = self.window.reconnect_epoch

    @staticmethod
    def _load_gray_template(name: str) -> Optional[np.ndarray]:
        path = ASSET_DIR / name
        if not path.exists():
            logging.warning("缺少模板：%s", path)
            return None
        return cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)

    def check_control_keys(self) -> None:
        f12_down = pressed(VK["F12"])
        if f12_down and not self._f12_down:
            self.stopped = True
            self.running = False
            self.window.release_keys(("A", "SPACE"), live=self.live)
            logging.warning("收到 F12，立即停止。")
            winsound.Beep(500, 300)
        self._f12_down = f12_down

        f8_down = pressed(VK["F8"])
        if f8_down and not self._f8_down and not self.stopped:
            self.running = not self.running
            if not self.running:
                self.window.release_keys(("A", "SPACE"), live=self.live)
            logging.info("%s", "开始" if self.running else "暂停")
            winsound.Beep(900 if self.running else 600, 180)
        self._f8_down = f8_down

    def _confirm_reconnected_window(self) -> None:
        if self.window.reconnect_epoch == self._window_reconnect_epoch:
            return
        self._window_reconnect_epoch = self.window.reconnect_epoch
        self.window.release_keys(("A", "SPACE"), live=self.live)
        recovery_cfg = self.cfg.get("recovery", {})
        confirm_seconds = max(
            0.0, float(recovery_cfg.get("scene_confirm_seconds", 3.0))
        )
        initial_revision = self.log_tail.scene_revision
        deadline = time.monotonic() + confirm_seconds
        while time.monotonic() < deadline and not self.stopped:
            self.log_tail.poll()
            if self.log_tail.scene_revision > initial_revision:
                break
            self.check_control_keys()
            time.sleep(0.2)

        scene = self.log_tail.latest_scene
        normal_scene = int(self.cfg.get("scenes", {}).get("normal_scene_id", 1002))
        if scene is None:
            logging.warning(
                "游戏窗口已恢复，但日志暂时没有场景记录；保持阶段 %s 并在后续流程中继续验证。",
                self.phase,
            )
            return
        scene_kind = "普通场景" if scene == normal_scene else "副本场景"
        logging.warning(
            "游戏窗口已恢复，日志确认最近场景为 %s（ID %s），当前阶段 %s。",
            scene_kind,
            scene,
            self.phase,
        )

    def capture_frame(self) -> np.ndarray:
        frame = self.window.capture()
        self._confirm_reconnected_window()
        return frame

    def wait(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_control_keys()
            self.log_tail.poll()
            if self.stopped:
                return False
            if not self.running:
                pause_started = time.monotonic()
                while not self.running and not self.stopped:
                    self.check_control_keys()
                    time.sleep(0.05)
                deadline += time.monotonic() - pause_started
                continue
            revive_started = time.monotonic()
            if self.check_and_handle_revive():
                # 死亡倒计时不应吃掉寻路、加载或战斗的原有等待时间。
                deadline += time.monotonic() - revive_started
                continue
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return True

    def click(self, name: str) -> None:
        point = self.cfg["coordinates"].get(name)
        if point is None:
            raise RuntimeError(f"坐标 {name} 尚未校准，请先运行 --calibrate。")
        self.window.ensure()
        self._confirm_reconnected_window()
        logging.info("点击 %s -> %s", name, point)
        self.window.click_normalized(point, live=self.live)

    def set_phase(self, name: str, combat: bool = False) -> None:
        if self.phase == name:
            return
        logging.info("阶段切换：%s -> %s", self.phase, name)
        self.phase = name
        if not combat:
            self.window.release_keys(("A", "SPACE"), live=self.live)

    def tap_combat_key(self, name: str, hold_seconds: float = 0.05) -> None:
        if self.phase not in ("normal_combat", "boss_combat"):
            raise RuntimeError(f"输入隔离阻止了在 {self.phase} 阶段发送战斗按键 {name}。")
        self.window.ensure()
        self._confirm_reconnected_window()
        self.window.tap_key(name, hold_seconds=hold_seconds, live=self.live)

    def revive_panel_visible(
        self, frame: Optional[np.ndarray] = None
    ) -> tuple[bool, float]:
        if self.revive_template is None:
            return False, 0.0
        if frame is None:
            frame = self.capture_frame()
        _, score = self.template_match(frame, self.revive_template)
        threshold = float(self.cfg.get("revive", {}).get("panel_match_threshold", 0.78))
        return score >= threshold, score

    def revive_ready(self, frame: np.ndarray) -> tuple[bool, float]:
        """用按钮的橙色占比判断“原地复活”是否已解除 15 秒灰置。"""
        revive_cfg = self.cfg.get("revive", {})
        region = revive_cfg.get(
            "ready_button_region", [0.396, 0.551, 0.479, 0.588]
        )
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = region
        crop = frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
        if crop.size == 0:
            return False, 0.0
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        orange = (
            (hsv[:, :, 0] >= 5)
            & (hsv[:, :, 0] <= 35)
            & (hsv[:, :, 1] >= 80)
            & (hsv[:, :, 2] >= 80)
        )
        ratio = float(np.mean(orange))
        threshold = float(revive_cfg.get("ready_orange_ratio", 0.45))
        return ratio >= threshold, ratio

    def check_and_handle_revive(self, force: bool = False) -> bool:
        """发现死亡面板后冻结输入，等待原地复活可用并点击。"""
        if self.revive_template is None or not self.live:
            return False
        revive_cfg = self.cfg.get("revive", {})
        now = time.monotonic()
        if not force and now < self._next_revive_probe:
            return False
        self._next_revive_probe = now + float(
            revive_cfg.get("check_interval_seconds", 0.5)
        )
        frame = self.capture_frame()
        visible, score = self.revive_panel_visible(frame)
        if not visible:
            return False

        previous_phase = self.phase
        self.set_phase("reviving")
        logging.warning("检测到死亡复活面板（匹配分数 %.3f），已冻结所有操作。", score)
        point = revive_cfg.get("revive_button_point", [0.4375, 0.5833])
        ready_region = revive_cfg.get(
            "ready_button_region", [0.396, 0.551, 0.479, 0.588]
        )
        relocated_point = [
            (ready_region[0] + ready_region[2]) / 2,
            (ready_region[1] + ready_region[3]) / 2,
        ]
        wait_rounds = max(1, int(revive_cfg.get("wait_rounds", 2)))
        max_clicks = max(1, int(revive_cfg.get("max_click_attempts", 3)))
        max_wait_seconds = float(revive_cfg.get("max_wait_seconds", 45.0))

        for wait_round in range(1, wait_rounds + 1):
            deadline = time.monotonic() + max_wait_seconds
            last_status_log = 0.0
            clicks = 0
            foreground_warned = False
            if wait_round > 1:
                point = relocated_point
                logging.warning(
                    "第一轮复活等待未成功，已根据按钮检测区域重新定位，开始第 %s/%s 轮。",
                    wait_round,
                    wait_rounds,
                )

            while time.monotonic() < deadline and not self.stopped:
                self.check_control_keys()
                if not self.running:
                    pause_started = time.monotonic()
                    while not self.running and not self.stopped:
                        self.check_control_keys()
                        time.sleep(0.05)
                    deadline += time.monotonic() - pause_started
                    continue
                if not self.window.is_foreground():
                    if not foreground_warned:
                        logging.warning("死亡等待中游戏失去前台；切回游戏后才会点击原地复活。")
                        foreground_warned = True
                    foreground_lost_at = time.monotonic()
                    while not self.window.is_foreground() and not self.stopped:
                        self.check_control_keys()
                        time.sleep(0.2)
                    deadline += time.monotonic() - foreground_lost_at
                    continue
                foreground_warned = False
                # 鼠标保持在按钮上：按钮可用时会稳定显示为橙色。
                self.window.move_client(
                    *self.window.normalized_to_client(point), live=self.live
                )
                frame = self.capture_frame()
                visible, panel_score = self.revive_panel_visible(frame)
                if not visible:
                    logging.info("复活面板已消失，确认人物已恢复操作。")
                    self.revive_epoch += 1
                    self.set_phase(
                        previous_phase,
                        combat=previous_phase in ("normal_combat", "boss_combat"),
                    )
                    time.sleep(float(revive_cfg.get("post_revive_seconds", 1.0)))
                    return True
                ready, orange_ratio = self.revive_ready(frame)
                if ready and clicks < max_clicks:
                    clicks += 1
                    logging.info(
                        "原地复活按钮已可用（橙色占比 %.3f），点击第 %s/%s 次。",
                        orange_ratio,
                        clicks,
                        max_clicks,
                    )
                    self.window.click_normalized(point, live=self.live)
                    time.sleep(0.8)
                    continue
                if time.monotonic() - last_status_log >= 3.0:
                    logging.info(
                        "等待原地复活解除倒计时：面板 %.3f，按钮橙色占比 %.3f。",
                        panel_score,
                        orange_ratio,
                    )
                    last_status_log = time.monotonic()
                time.sleep(0.2)

            if self.stopped:
                return True
            if wait_round < wait_rounds:
                self.window.release_keys(("A", "SPACE"), live=self.live)
                retry_point = revive_cfg.get("retry_move_point", [0.5, 0.5])
                if self.window.is_foreground():
                    self.window.move_client(
                        *self.window.normalized_to_client(retry_point), live=self.live
                    )
                time.sleep(float(revive_cfg.get("retry_settle_seconds", 1.0)))
                retry_frame = self.capture_frame()
                still_visible, _ = self.revive_panel_visible(retry_frame)
                if not still_visible:
                    logging.info("重新截图后复活面板已消失，确认人物已恢复操作。")
                    self.revive_epoch += 1
                    self.set_phase(
                        previous_phase,
                        combat=previous_phase in ("normal_combat", "boss_combat"),
                    )
                    time.sleep(float(revive_cfg.get("post_revive_seconds", 1.0)))
                    return True
                continue

        raise RuntimeError(
            f"检测到死亡，但连续 {wait_rounds} 轮等待和重新定位后仍无法原地复活；已安全暂停。"
        )

    def template_match(
        self, frame: np.ndarray, template: Optional[np.ndarray]
    ) -> tuple[Optional[tuple[int, int]], float]:
        if template is None:
            return None, 0.0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        scale = frame.shape[1] / self.cfg["reference_client_size"][0]
        resized = cv2.resize(
            template,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
        )
        if resized.shape[0] >= gray.shape[0] or resized.shape[1] >= gray.shape[1]:
            return None, 0.0
        result = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(result)
        center = (loc[0] + resized.shape[1] // 2, loc[1] + resized.shape[0] // 2)
        return center, float(score)

    def panel_visible(self) -> bool:
        _, score = self.template_match(self.capture_frame(), self.panel_template)
        logging.debug("副本面板匹配分数 %.3f", score)
        return score >= self.cfg["entrance"]["panel_match_threshold"]

    def map_visible(self) -> bool:
        _, score = self.template_match(self.capture_frame(), self.map_template)
        logging.info("地图打开状态匹配分数 %.3f", score)
        return score >= self.cfg["map_detection"]["open_match_threshold"]

    def open_map(self) -> None:
        if self.map_visible():
            logging.info("地图已经打开，无需重复点击小地图。")
            return
        map_cfg = self.cfg["map_detection"]
        legacy_retries = max(1, int(map_cfg.get("open_retries", 3)))
        recovery_rounds = max(
            0, int(map_cfg.get("recovery_rounds", legacy_retries - 1))
        )
        total_attempts = 1 + recovery_rounds
        settle_seconds = max(
            0.0, float(map_cfg.get("recovery_settle_seconds", 0.8))
        )
        for attempt in range(1, total_attempts + 1):
            if attempt > 1:
                logging.warning(
                    "地图未能打开，开始恢复轮次 %s/%s：释放按键并尝试关闭遮挡面板。",
                    attempt - 1,
                    recovery_rounds,
                )
                self.window.release_keys(("A", "SPACE"), live=self.live)
                if not self.wait(settle_seconds):
                    return
                self.window.ensure()
                self._confirm_reconnected_window()
                self.window.tap_key("ESC", live=self.live)
                if not self.wait(settle_seconds):
                    return
                if self.map_visible():
                    logging.info("关闭遮挡面板后检测到地图已经打开。")
                    return
            logging.info(
                "点击右上角小地图并验证地图界面，第 %s/%s 次。",
                attempt,
                total_attempts,
            )
            self.click("map_button")
            if not self.wait(self.cfg["timing"]["map_open_seconds"]):
                return
            if self.map_visible():
                logging.info("已确认地图界面打开。")
                return
        raise RuntimeError(
            f"初次尝试及 {recovery_rounds} 轮恢复后仍未检测到地图界面；"
            "请运行“重新校准小地图.bat”。"
        )

    def find_portal(self) -> tuple[int, int]:
        deadline = time.monotonic() + 25.0
        best_score = 0.0
        best_center: Optional[tuple[int, int]] = None
        while time.monotonic() < deadline and not self.stopped:
            frame = self.capture_frame()
            center, score = self.template_match(frame, self.portal_template)
            if score > best_score:
                best_score, best_center = score, center
            if center and score >= self.cfg["entrance"]["portal_match_threshold"]:
                logging.info("识别到入口圆圈：%s，分数 %.3f", center, score)
                return center
            self.wait(0.5)
        raise RuntimeError(f"未识别到副本入口圆圈，最佳匹配分数 {best_score:.3f}，位置 {best_center}")

    def trigger_entrance_panel(self) -> None:
        if self.panel_visible():
            return
        cx, cy = self.find_portal()
        base_w = self.cfg["reference_client_size"][0]
        width, _ = self.window.client_size()
        radius = self.cfg["entrance"]["circle_radius_pixels_at_1280x720"] * width / base_w
        points = self.cfg["entrance"]["circle_points"]
        rounds = self.cfg["entrance"]["max_circle_rounds"]
        for round_index in range(rounds):
            logging.info("入口圆形移动，第 %s/%s 圈", round_index + 1, rounds)
            for index in range(points):
                angle = (2 * math.pi * index / points) + round_index * 0.25
                x = int(cx + math.cos(angle) * radius)
                y = int(cy + math.sin(angle) * radius)
                self.window.click_client(x, y, live=self.live)
                if not self.wait(self.cfg["timing"]["circle_click_interval_seconds"]):
                    return
                if self.panel_visible():
                    logging.info("已触发副本面板。")
                    return
        raise RuntimeError("绕入口移动后仍未检测到副本面板。")

    def read_fatigue(
        self, attempts: int = 3, refresh_rounds: int = 1
    ) -> Optional[int]:
        if self.ocr is None:
            self.ocr = FatigueOCR(self.cfg.get("debug"))
        fatigue_cfg = self.cfg["fatigue"]
        refresh_rounds = max(1, int(refresh_rounds))
        for refresh_round in range(1, refresh_rounds + 1):
            hover = self.window.normalized_to_client(fatigue_cfg["hover_point"])
            refresh = self.window.normalized_to_client(
                fatigue_cfg.get("refresh_point", [0.5, 0.5])
            )
            self._confirm_reconnected_window()
            revive_epoch = self.revive_epoch

            # 每一轮都先移出再移回，强制游戏丢弃旧提示框并生成最新数值。
            self.window.move_client(*refresh, live=self.live)
            if not self.wait(float(fatigue_cfg.get("refresh_leave_seconds", 0.25))):
                return None
            if self.revive_epoch != revive_epoch:
                return self.read_fatigue(
                    attempts=attempts, refresh_rounds=refresh_rounds
                )
            self.window.move_client(*hover, live=self.live)
            if not self.wait(float(fatigue_cfg.get("hover_settle_seconds", 0.8))):
                return None
            if self.revive_epoch != revive_epoch:
                return self.read_fatigue(
                    attempts=attempts, refresh_rounds=refresh_rounds
                )

            values: list[int] = []
            for _ in range(attempts):
                frame = self.capture_frame()
                value, texts = self.ocr.read(
                    frame,
                    fatigue_cfg["ocr_region"],
                    fatigue_cfg.get("value_region"),
                )
                logging.info("疲劳 OCR: value=%s raw=%s", value, texts)
                if value is not None and -1000 <= value <= 1000:
                    values.append(value)
                if not self.wait(0.25):
                    return None
            if values:
                values.sort()
                value = values[len(values) // 2]
                self.last_fatigue = value
                logging.info("确认当前疲劳值：%s/1000", value)
                return value
            if refresh_round < refresh_rounds:
                logging.warning(
                    "疲劳 OCR 第 %s/%s 轮未识别成功，将重新移出并移回状态栏。",
                    refresh_round,
                    refresh_rounds,
                )
                if not self.wait(
                    float(fatigue_cfg.get("ocr_retry_interval_seconds", 0.5))
                ):
                    return None
        logging.warning("疲劳 OCR 连续 %s 轮均未识别成功。", refresh_rounds)
        return None

    def normal_farm_until_low(self) -> None:
        logging.info("状态：普通区域挂机。")
        self.set_phase("normal_combat", combat=True)
        combat = self.cfg["combat"]
        next_attack = 0.0
        next_pickup = 0.0
        next_fatigue = 0.0
        while not self.stopped:
            self.check_control_keys()
            self.log_tail.poll()
            if not self.running:
                time.sleep(0.05)
                continue
            if not self.window.is_foreground():
                logging.warning("游戏失去前台，挂机暂停；切回游戏后继续。")
                while not self.window.is_foreground() and not self.stopped:
                    self.check_control_keys()
                    time.sleep(0.2)
                continue
            if self.check_and_handle_revive():
                next_attack = time.monotonic()
                next_pickup = time.monotonic()
                continue
            now = time.monotonic()
            if now >= next_attack:
                self.tap_combat_key(combat["attack_key"])
                next_attack = now + combat["attack_interval_seconds"]
            if now >= next_pickup:
                self.tap_combat_key(combat["pickup_key"])
                next_pickup = now + combat["pickup_interval_seconds"]
            if now >= next_fatigue:
                fatigue = self.read_fatigue()
                next_fatigue = now + self.cfg["fatigue"]["check_interval_seconds"]
                if fatigue is not None and fatigue <= self.cfg["fatigue"]["low_threshold"]:
                    logging.warning("疲劳 %s 低于阈值，转入 BOSS 循环。", fatigue)
                    return
            time.sleep(0.03)

    def travel_to_first_entrance(self) -> None:
        logging.info("状态：通过普通地图寻路到副本入口。")
        self.set_phase("normal_navigation")
        while not self.stopped:
            revive_epoch = self.revive_epoch
            self.wait(self.cfg["timing"].get("combat_input_quiet_seconds", 1.0))
            self.open_map()
            self.click("normal_portal_marker")
            self.wait(self.cfg["timing"]["normal_auto_path_seconds"])
            if self.revive_epoch == revive_epoch:
                return
            logging.info("寻路期间发生过复活，重新打开地图并下发入口寻路。")

    def enter_dungeon(self) -> None:
        logging.info("状态：触发入口并进入冰牙海湾。")
        self.set_phase("entering_dungeon")
        self.trigger_entrance_panel()
        self.click("enter_dungeon_button")
        self.wait(self.cfg["timing"]["dungeon_load_seconds"])

    def travel_to_boss(self) -> None:
        logging.info("状态：副本内地图寻路到 BOSS。")
        self.set_phase("dungeon_navigation")
        while not self.stopped:
            revive_epoch = self.revive_epoch
            self.open_map()
            self.click("dungeon_boss_marker")
            self.wait(self.cfg["timing"]["boss_auto_path_seconds"])
            if self.revive_epoch == revive_epoch:
                return
            logging.info("副本寻路期间发生过复活，重新下发 BOSS 寻路。")

    def fight_boss(self) -> bool:
        logging.info("状态：寻找并攻击 BOSS。")
        self.set_phase("boss_combat", combat=True)
        self.log_tail.boss_seen = False
        self.log_tail.boss_dead = False
        deadline = time.monotonic() + self.cfg["timing"]["boss_timeout_seconds"]
        revive_epoch = self.revive_epoch
        while time.monotonic() < deadline and not self.stopped:
            if self.check_and_handle_revive():
                deadline = time.monotonic() + self.cfg["timing"]["boss_timeout_seconds"]
                revive_epoch = self.revive_epoch
                continue
            self.tap_combat_key(self.cfg["combat"]["attack_key"])
            self.log_tail.poll()
            if self.log_tail.boss_seen:
                logging.info("日志已识别 BOSS 实体 %s。", self.cfg["combat"]["boss_entity_id"])
            if self.log_tail.boss_dead:
                logging.info("日志确认 BOSS 已死亡。")
                return True
            self.wait(self.cfg["combat"]["attack_interval_seconds"])
            if self.revive_epoch != revive_epoch:
                deadline = time.monotonic() + self.cfg["timing"]["boss_timeout_seconds"]
                revive_epoch = self.revive_epoch
        if self.stopped:
            return False
        logging.warning(
            "BOSS 战超时，未从日志检测到 BOSS 死亡；将退出本次副本并继续恢复流程。"
        )
        return False

    def pickup_and_exit(self) -> None:
        logging.info("状态：拾取 BOSS 掉落。")
        self.set_phase("boss_loot")
        self.window.tap_key(
            self.cfg["combat"]["pickup_key"],
            hold_seconds=self.cfg["combat"]["boss_pickup_hold_seconds"],
            live=self.live,
        )
        self.wait(1.5)
        self.exit_dungeon()

    def exit_dungeon(self) -> None:
        logging.info("状态：退出副本。")
        self.set_phase("exiting_dungeon")
        self.click("exit_dungeon_button")
        self.wait(0.8)
        self.click("confirm_exit_button")
        self.wait(self.cfg["timing"]["exit_load_seconds"])

    def boss_recovery_loop(self) -> None:
        completed_runs = 0
        minimum_runs = max(1, int(self.cfg.get("boss_loop", {}).get("minimum_runs", 8)))
        while not self.stopped:
            # 每次退出都会回到普通地图，因此每一轮都重新用地图寻路到入口。
            self.travel_to_first_entrance()
            self.enter_dungeon()
            self.travel_to_boss()
            boss_defeated = self.fight_boss()
            if self.stopped:
                return
            if boss_defeated:
                self.pickup_and_exit()
                completed_runs += 1
                logging.info(
                    "本轮疲劳恢复已完成 %s 次副本（至少 %s 次）。",
                    completed_runs,
                    minimum_runs,
                )
                if completed_runs < minimum_runs:
                    continue
            else:
                # 超时可能是未击杀，也可能只是日志漏报。无论哪种情况都先安全
                # 退出副本，再以实际疲劳值决定返回挂机还是继续下一轮。
                self.exit_dungeon()
                if self.stopped:
                    return
                logging.info("BOSS 战超时后已退出副本，立即重新检查疲劳值。")
            extra_ocr_rounds = max(
                0,
                int(
                    self.cfg["fatigue"].get(
                        "post_dungeon_extra_ocr_rounds", 2
                    )
                ),
            )
            fatigue = self.read_fatigue(refresh_rounds=1 + extra_ocr_rounds)
            if fatigue is None:
                logging.warning(
                    "退出副本后追加 OCR 仍未识别疲劳值；不中断挂机，继续下一次副本。"
                )
                continue
            if fatigue >= self.cfg["fatigue"]["boss_target"]:
                logging.info("疲劳已恢复到 %s，结束 BOSS 循环。", fatigue)
                return
            logging.info("疲劳仍为 %s，准备再次进入副本。", fatigue)

    def dungeon_only_loop(self) -> None:
        completed_runs = 0
        logging.info("状态：只刷副本；不会读取疲劳，也不会返回普通挂机。")
        while not self.stopped:
            self.travel_to_first_entrance()
            self.enter_dungeon()
            self.travel_to_boss()
            boss_defeated = self.fight_boss()
            if self.stopped:
                return
            if boss_defeated:
                self.pickup_and_exit()
                completed_runs += 1
                logging.info("只刷副本模式已完成 %s 次。", completed_runs)
            else:
                self.exit_dungeon()
                logging.warning("只刷副本模式本次 BOSS 超时，已退出并准备下一轮。")

    def run(self) -> None:
        required = [
            "map_button",
            "normal_portal_marker",
            "enter_dungeon_button",
            "dungeon_boss_marker",
            "exit_dungeon_button",
            "confirm_exit_button",
        ]
        missing = [name for name in required if self.cfg["coordinates"].get(name) is None]
        if missing:
            raise RuntimeError(f"尚未校准这些坐标：{', '.join(missing)}")
        w, h = self.window.client_size()
        logging.info("已连接游戏窗口，客户区 %sx%s，live=%s", w, h, self.live)
        print("准备完成。请切到游戏窗口，按 F8 开始/暂停，按 F12 立即停止。")
        mode_text = "完整闭环" if self.mode == "full" else "只刷副本"
        print("当前模式：" + mode_text + " / " + ("实时输入" if self.live else "演练（不发送输入）"))
        while not self.stopped:
            self.check_control_keys()
            if not self.running:
                time.sleep(0.05)
                continue
            try:
                if self.mode == "dungeon":
                    self.dungeon_only_loop()
                else:
                    self.normal_farm_until_low()
                    if not self.stopped:
                        self.boss_recovery_loop()
            except RuntimeError as exc:
                self.window.release_keys(("A", "SPACE"), live=self.live)
                logging.exception("自动流程暂停：%s", exc)
                print(f"\n自动流程暂停：{exc}")
                self.running = False
                winsound.Beep(450, 500)


CALIBRATION_STEPS = [
    ("map_button", "普通HUD：把鼠标移到用于打开大地图的位置"),
    ("normal_portal_marker", "打开普通区域大地图：把鼠标移到冰牙海湾入口的地图点位"),
    ("enter_dungeon_button", "触发副本面板：把鼠标移到“进入副本”按钮中心"),
    ("dungeon_boss_marker", "进入副本并打开大地图：把鼠标移到最远端BOSS点位"),
    ("exit_dungeon_button", "副本HUD：把鼠标移到右上角“退出副本”按钮中心"),
    ("confirm_exit_button", "打开退出确认框：把鼠标移到红色“确认”按钮中心"),
]


def calibrate(cfg: dict) -> None:
    window = GameWindow(cfg["window_title"])
    window.locate()
    w, h = window.client_size()
    print(f"找到游戏客户区：{w}x{h}")
    print("每一步请在游戏中准备好相应界面，把鼠标放到目标中心，然后按 F2 记录。")
    print("按 F12 可随时取消；校准过程本身不会点击鼠标。\n")
    for name, instruction in CALIBRATION_STEPS:
        print(f"[{name}] {instruction}，然后按 F2。")
        winsound.Beep(750, 120)
        if not wait_key_edge(VK["F2"]):
            print("已取消校准。")
            return
        sx, sy = win32api.GetCursorPos()
        cx, cy = win32gui.ScreenToClient(window.hwnd, (sx, sy))
        if not (0 <= cx < w and 0 <= cy < h):
            raise RuntimeError(f"记录点 ({cx}, {cy}) 不在游戏客户区内，请重新运行校准。")
        normalized = window.client_to_normalized((cx, cy))
        cfg["coordinates"][name] = normalized
        print(f"  已记录客户区坐标 ({cx}, {cy})，归一化 {normalized}\n")
    print("[fatigue_hover] 返回普通HUD，把鼠标移到右下角疲劳条上能弹出说明的位置，然后按 F2。")
    winsound.Beep(750, 120)
    if not wait_key_edge(VK["F2"]):
        print("已取消校准。")
        return
    sx, sy = win32api.GetCursorPos()
    cx, cy = win32gui.ScreenToClient(window.hwnd, (sx, sy))
    if not (0 <= cx < w and 0 <= cy < h):
        raise RuntimeError(f"疲劳条记录点 ({cx}, {cy}) 不在游戏客户区内。")
    cfg["fatigue"]["hover_point"] = window.client_to_normalized((cx, cy))
    print(f"  已记录疲劳条位置 ({cx}, {cy})。\n")
    save_config(cfg)
    winsound.Beep(1000, 250)
    print(f"校准完成，已保存到 {CONFIG_PATH}")


def calibrate_map_button(cfg: dict) -> None:
    window = GameWindow(cfg["window_title"])
    window.locate()
    w, h = window.client_size()
    print(f"找到游戏客户区：{w}x{h}")
    print("请先关闭地图界面，回到正常 HUD。")
    print("把鼠标放到右上角圆形小地图的中心；这里是用于展开地图的入口，不是地图窗口内的坐标。")
    print("按 F2 记录，按 F12 取消。校准过程不会自动点击。")
    winsound.Beep(750, 120)
    if not wait_key_edge(VK["F2"]):
        print("已取消小地图校准。")
        return
    sx, sy = win32api.GetCursorPos()
    cx, cy = win32gui.ScreenToClient(window.hwnd, (sx, sy))
    if not (0 <= cx < w and 0 <= cy < h):
        raise RuntimeError(f"记录点 ({cx}, {cy}) 不在游戏客户区内。")
    cfg["coordinates"]["map_button"] = window.client_to_normalized((cx, cy))
    save_config(cfg)
    winsound.Beep(1000, 250)
    print(f"小地图位置已记录：客户区 ({cx}, {cy})，配置 {cfg['coordinates']['map_button']}")


def ocr_test(cfg: dict) -> None:
    window = GameWindow(cfg["window_title"])
    window.locate()
    reader = FatigueOCR(cfg.get("debug"))
    hover = window.normalized_to_client(cfg["fatigue"]["hover_point"])
    print("请保持游戏在前台。程序将把鼠标移到疲劳条并读取提示框。")
    time.sleep(2)
    window.move_client(*hover, live=True)
    time.sleep(0.8)
    value, texts = reader.read(
        window.capture(),
        cfg["fatigue"]["ocr_region"],
        cfg["fatigue"].get("value_region"),
    )
    print(f"识别结果：{value}/1000；原始OCR：{texts}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Curious Beast 外部挂机 MVP")
    parser.add_argument("--calibrate", action="store_true", help="运行一次性坐标校准")
    parser.add_argument("--calibrate-map", action="store_true", help="只重新校准右上角小地图")
    parser.add_argument("--ocr-test", action="store_true", help="只测试疲劳 OCR")
    parser.add_argument("--live", action="store_true", help="允许真实发送输入；否则为演练模式")
    parser.add_argument(
        "--mode",
        choices=("full", "dungeon"),
        help="full=普通挂机与疲劳恢复闭环；dungeon=只刷副本",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(RUNTIME_LOG, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    cfg = load_config()
    if args.calibrate:
        calibrate(cfg)
        return 0
    if args.calibrate_map:
        calibrate_map_button(cfg)
        return 0
    if args.ocr_test:
        ocr_test(cfg)
        return 0
    instance_mutex = win32event.CreateMutex(None, False, "Local\\CuriousBeastAutomationMVP")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        raise RuntimeError("已有一个挂机脚本正在运行，请先按 F12 关闭旧实例。")
    mode = args.mode
    if mode is None:
        minimum_runs = max(
            1, int(cfg.get("boss_loop", {}).get("minimum_runs", 8))
        )
        print("请选择运行模式：")
        print(
            f"  1. 完整闭环（普通挂机 -> 疲劳低于阈值 -> "
            f"至少 {minimum_runs} 次副本 -> 返回挂机）"
        )
        print("  2. 只刷副本（持续重复进入、击杀、拾取、退出）")
        choice = input("输入 1 或 2，直接回车默认 1：").strip()
        mode = "dungeon" if choice == "2" else "full"
    bot = Bot(cfg, live=args.live, mode=mode)
    bot.run()
    win32api.CloseHandle(instance_mutex)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已停止。")
        raise SystemExit(130)
    except Exception as exc:
        logging.exception("程序退出：%s", exc)
        print(f"程序退出：{exc}")
        raise SystemExit(1)
