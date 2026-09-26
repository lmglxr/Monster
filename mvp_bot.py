from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from contextlib import contextmanager
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
from dataclasses import dataclass, field
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
import win32ui
import winerror


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
SMOKE_CONFIG_PATH = APP_DIR / "smoke_test_config.json"
INVENTORY_PROFILE_PATH = APP_DIR / "inventory_profile.json"
INVENTORY_PROFILE_EXAMPLE_PATH = APP_DIR / "inventory_profile.example.json"
ASSET_DIR = APP_DIR / "assets"
RUNTIME_LOG = APP_DIR / "runtime.log"
DEBUG_DIR = APP_DIR / "debug"
APP_VERSION = "0.0.2"

VK = {
    "A": 0x41,
    "B": 0x42,
    "F": 0x46,
    "Q": 0x51,
    "W": 0x57,
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


def load_inventory_profile() -> dict:
    """加载每位玩家单独校准的背包坐标；缺失时安全返回空配置。"""
    if not INVENTORY_PROFILE_PATH.exists():
        return {}
    try:
        with INVENTORY_PROFILE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("个人背包坐标文件读取失败，将禁用本轮背包自动化：%s", exc)
        return {}


def save_inventory_profile(profile: dict) -> None:
    with INVENTORY_PROFILE_PATH.open("w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
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
    input_mode: str = "foreground_only"
    focus_settle_seconds: float = 0.08
    focus_activation_attempts: int = 3
    focus_activation_retry_seconds: float = 0.08
    post_input_seconds: float = 0.12
    restore_previous_window: bool = True
    capture_mode: str = "screen"
    _input_session_depth: int = field(default=0, init=False, repr=False)
    _previous_foreground: int = field(default=0, init=False, repr=False)
    _previous_cursor: Optional[tuple[int, int]] = field(
        default=None, init=False, repr=False
    )
    _last_bot_cursor: Optional[tuple[int, int]] = field(
        default=None, init=False, repr=False
    )
    _focus_notice_logged: bool = field(default=False, init=False, repr=False)

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

    @property
    def supports_focus_pulse(self) -> bool:
        return self.input_mode == "focus_pulse"

    @staticmethod
    def _activate_window(hwnd: int) -> None:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        foreground = win32gui.GetForegroundWindow()
        current_thread = kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        attached = bool(
            foreground_thread
            and foreground_thread != current_thread
            and user32.AttachThreadInput(current_thread, foreground_thread, True)
        )
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)

    @contextmanager
    def input_session(self, live: bool = True):
        """Temporarily focus the game and restore the user's window/cursor."""
        if not live:
            yield
            return
        if self.input_mode == "foreground_only":
            self.require_foreground()
            yield
            return
        if self.input_mode != "focus_pulse":
            raise RuntimeError(f"不支持的 input_mode：{self.input_mode}")
        if self._input_session_depth:
            self._input_session_depth += 1
            try:
                yield
            finally:
                self._input_session_depth -= 1
            return

        self.ensure()
        self._input_session_depth = 1
        self._previous_foreground = win32gui.GetForegroundWindow()
        self._previous_cursor = win32api.GetCursorPos()
        self._last_bot_cursor = None
        activation_error: Optional[Exception] = None
        for attempt in range(1, max(1, self.focus_activation_attempts) + 1):
            if win32gui.GetForegroundWindow() == self.hwnd:
                break
            try:
                self._activate_window(self.hwnd)
            except Exception as exc:
                activation_error = exc
            time.sleep(max(0.0, self.focus_settle_seconds))
            if win32gui.GetForegroundWindow() == self.hwnd:
                break
            if attempt < max(1, self.focus_activation_attempts):
                time.sleep(max(0.0, self.focus_activation_retry_seconds))
        if win32gui.GetForegroundWindow() != self.hwnd:
            self._input_session_depth = 0
            detail = f"：{activation_error}" if activation_error else ""
            raise RuntimeError(
                f"连续 {max(1, self.focus_activation_attempts)} 次无法临时切换到游戏窗口，"
                f"已取消本次输入以避免误操作{detail}"
            )
        if not self._focus_notice_logged:
            logging.warning(
                "焦点脉冲模式已启用：发送输入时会短暂切到游戏，随后恢复原窗口和鼠标。"
            )
            self._focus_notice_logged = True
        try:
            yield
        finally:
            # 只有光标仍停在脚本最后设置的位置时才恢复；如果用户在脉冲期间
            # 主动移动了鼠标，保留用户的新位置，避免反向抢鼠标。
            if (
                self._previous_cursor is not None
                and self._last_bot_cursor is not None
                and win32api.GetCursorPos() == self._last_bot_cursor
            ):
                win32api.SetCursorPos(self._previous_cursor)
            # 用户若已主动切到第三个窗口，则不再强行恢复旧窗口。
            if (
                self.restore_previous_window
                and win32gui.GetForegroundWindow() == self.hwnd
                and self._previous_foreground != self.hwnd
                and win32gui.IsWindow(self._previous_foreground)
            ):
                self._activate_window(self._previous_foreground)
            self._input_session_depth = 0
            self._previous_foreground = 0
            self._previous_cursor = None
            self._last_bot_cursor = None

    def _capture_print_window(self) -> np.ndarray:
        self.ensure()
        if win32gui.IsIconic(self.hwnd):
            raise RuntimeError("游戏窗口已最小化；焦点脉冲模式允许遮挡，但不能最小化游戏。")
        width, height = self.client_size()
        if width <= 0 or height <= 0:
            raise RuntimeError(f"游戏客户区尺寸无效：{width}x{height}")
        window_dc = win32gui.GetWindowDC(self.hwnd)
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source_dc, width, height)
            memory_dc.SelectObject(bitmap)
            # PW_CLIENTONLY | PW_RENDERFULLCONTENT。该游戏实测可在被遮挡时返回
            # 完整客户区，而不是桌面上覆盖它的其他窗口。
            if not ctypes.windll.user32.PrintWindow(
                self.hwnd, memory_dc.GetSafeHdc(), 3
            ):
                raise RuntimeError("PrintWindow 未返回游戏画面")
            info = bitmap.GetInfo()
            frame = np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8).reshape(
                info["bmHeight"], info["bmWidth"], 4
            )[:, :, :3].copy()
            if frame.size == 0 or float(frame.std()) < 1.0:
                raise RuntimeError("PrintWindow 返回空白画面")
            return frame
        finally:
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, window_dc)
            if bitmap.GetHandle():
                win32gui.DeleteObject(bitmap.GetHandle())

    def capture(self) -> np.ndarray:
        last_error: Optional[Exception] = None
        retries = max(1, int(self.capture_retries))
        for attempt in range(1, retries + 1):
            try:
                self.ensure()
                if self.capture_mode == "print_window":
                    return self._capture_print_window()
                if self.capture_mode != "screen":
                    raise RuntimeError(f"不支持的 capture_mode：{self.capture_mode}")
                x, y = self.client_origin()
                w, h = self.client_size()
                if w <= 0 or h <= 0:
                    raise RuntimeError(f"游戏客户区尺寸无效：{w}x{h}")
                image = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
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
        with self.input_session(live=True):
            ox, oy = self.client_origin()
            target = (ox + x, oy + y)
            win32api.SetCursorPos(target)
            self._last_bot_cursor = target

    def click_client(self, x: int, y: int, live: bool = True) -> None:
        if not live:
            logging.info("DRY click (%s, %s)", x, y)
            return
        with self.input_session(live=True):
            self.move_client(x, y, live=True)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            if self.supports_focus_pulse:
                time.sleep(max(0.0, self.post_input_seconds))

    def click_normalized(self, point: list[float], live: bool = True) -> None:
        self.click_client(*self.normalized_to_client(point), live=live)

    def double_click_normalized(
        self,
        point: list[float],
        interval_seconds: float = 0.12,
        live: bool = True,
    ) -> None:
        x, y = self.normalized_to_client(point)
        if not live:
            logging.info("DRY double click (%s, %s)", x, y)
            return
        with self.input_session(live=True):
            self.move_client(x, y, live=True)
            for click_index in range(2):
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                time.sleep(0.05)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                if click_index == 0:
                    time.sleep(max(0.05, interval_seconds))
            if self.supports_focus_pulse:
                time.sleep(max(0.0, self.post_input_seconds))

    def drag_normalized(
        self,
        source: list[float],
        target: list[float],
        hold_seconds: float = 0.35,
        live: bool = True,
    ) -> None:
        sx, sy = self.normalized_to_client(source)
        tx, ty = self.normalized_to_client(target)
        if not live:
            logging.info("DRY drag (%s, %s) -> (%s, %s)", sx, sy, tx, ty)
            return
        with self.input_session(live=True):
            self.move_client(sx, sy, live=True)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.12)
            steps = max(4, int(max(0.2, hold_seconds) / 0.04))
            ox, oy = self.client_origin()
            for step in range(1, steps + 1):
                ratio = step / steps
                point = (
                    ox + int(round(sx + (tx - sx) * ratio)),
                    oy + int(round(sy + (ty - sy) * ratio)),
                )
                win32api.SetCursorPos(point)
                self._last_bot_cursor = point
                time.sleep(max(0.01, hold_seconds / steps))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            if self.supports_focus_pulse:
                time.sleep(max(0.0, self.post_input_seconds))

    def tap_key(self, name: str, hold_seconds: float = 0.05, live: bool = True) -> None:
        vk = VK[name.upper()]
        if not live:
            logging.info("DRY key %s hold %.2fs", name, hold_seconds)
            return
        with self.input_session(live=True):
            win32api.keybd_event(vk, 0, 0, 0)
            time.sleep(hold_seconds)
            win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
            if self.supports_focus_pulse:
                time.sleep(max(0.0, self.post_input_seconds))

    def release_keys(self, names: tuple[str, ...], live: bool = True) -> None:
        if not live:
            logging.info("DRY release keys %s", ", ".join(names))
            return
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            return
        if self.input_mode == "foreground_only" and not self.is_foreground():
            return
        with self.input_session(live=True):
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
        window_cfg = cfg.get("window_control", {})
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
            input_mode=str(window_cfg.get("input_mode", "foreground_only")),
            focus_settle_seconds=float(
                window_cfg.get("focus_settle_seconds", 0.08)
            ),
            focus_activation_attempts=max(
                1, int(window_cfg.get("focus_activation_attempts", 3))
            ),
            focus_activation_retry_seconds=float(
                window_cfg.get("focus_activation_retry_seconds", 0.08)
            ),
            post_input_seconds=float(window_cfg.get("post_input_seconds", 0.12)),
            restore_previous_window=bool(
                window_cfg.get("restore_previous_window", True)
            ),
            capture_mode=str(window_cfg.get("capture_mode", "screen")),
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
        self.inventory_template = self._load_gray_template("inventory_panel_title.png")
        self.shop_template = self._load_gray_template("general_store_title.png")
        self.sell_quantity_template = self._load_gray_template("sell_quantity_title.png")
        self.medicine_item_template = self._load_gray_template("medicine_item_icon.png")
        self.weapon_variant_templates = {
            "a": self._load_gray_template("weapon_variant_a_icon.png"),
            "b": self._load_gray_template("weapon_variant_b_icon.png"),
        }
        self.inventory_profile = load_inventory_profile()
        self.running = False
        self.stopped = False
        self.last_fatigue: Optional[int] = None
        self.phase = "idle"
        self.revive_epoch = 0
        self._next_revive_probe = 0.0
        self._f8_down = False
        self._f12_down = False
        self._window_reconnect_epoch = self.window.reconnect_epoch
        self._boss_weapon_active = False
        self._inventory_missing_warned = False
        self.combat_input_counts: Counter[str] = Counter()

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

    def tap_combat_key(self, name: str, hold_seconds: float = 0.05) -> bool:
        if self.phase not in ("normal_combat", "boss_combat"):
            raise RuntimeError(f"输入隔离阻止了在 {self.phase} 阶段发送战斗按键 {name}。")
        combat = self.cfg.get("combat", {})
        attempts = max(1, int(combat.get("input_retry_attempts", 3)))
        retry_seconds = max(0.05, float(combat.get("input_retry_seconds", 0.15)))
        for attempt in range(1, attempts + 1):
            try:
                self.window.ensure()
                self._confirm_reconnected_window()
                self.window.tap_key(name, hold_seconds=hold_seconds, live=self.live)
                self.combat_input_counts[name.upper()] += 1
                return True
            except RuntimeError as exc:
                if attempt >= attempts:
                    logging.warning(
                        "战斗按键 %s 连续 %s 次发送失败，本次跳过并在下一周期继续：%s",
                        name,
                        attempts,
                        exc,
                    )
                    return False
                logging.warning(
                    "战斗按键 %s 第 %s/%s 次发送失败，%.2f 秒后重试：%s",
                    name,
                    attempt,
                    attempts,
                    retry_seconds,
                    exc,
                )
                time.sleep(retry_seconds)
        return False

    def new_combat_schedule(self, boss: bool = False) -> dict[str, float]:
        """为 A、Space、Q、W 建立彼此独立的下一次执行时间。"""
        combat = self.cfg["combat"]
        now = time.monotonic()
        return {
            "attack": now,
            "pickup": now + float(combat.get("pickup_initial_delay_seconds", 0.2)),
            "skill_q": now + float(combat.get("skill_q_initial_delay_seconds", 1.0)),
            "skill_w": now + float(combat.get("skill_w_initial_delay_seconds", 2.0)),
            "boss": 1.0 if boss else 0.0,
        }

    def run_due_combat_inputs(self, schedule: dict[str, float]) -> None:
        """执行当前到期的战斗输入；任一技能冷却都不会阻塞其他按键。"""
        combat = self.cfg["combat"]
        boss = bool(schedule.get("boss", 0.0))
        now = time.monotonic()
        actions = [
            (
                "attack",
                True,
                str(combat.get("attack_key", "A")),
                float(combat.get("attack_interval_seconds", 0.45)),
            ),
            (
                "skill_q",
                bool(combat.get("skill_q_enabled", True)),
                str(combat.get("skill_q_key", "Q")),
                float(combat.get("skill_q_interval_seconds", 6.0)),
            ),
            (
                "skill_w",
                bool(combat.get("skill_w_enabled", True)),
                str(combat.get("skill_w_key", "W")),
                float(combat.get("skill_w_interval_seconds", 12.0)),
            ),
            (
                "pickup",
                True,
                str(combat.get("pickup_key", "SPACE")),
                float(
                    combat.get(
                        "boss_pickup_interval_seconds" if boss else "pickup_interval_seconds",
                        combat.get("pickup_interval_seconds", 0.8),
                    )
                ),
            ),
        ]
        for action_name, enabled, key, interval in actions:
            if not enabled or now < schedule[action_name]:
                continue
            sent = self.tap_combat_key(
                key,
                hold_seconds=float(combat.get("combat_key_hold_seconds", 0.05)),
            )
            if sent and action_name.startswith("skill_"):
                logging.debug("战斗技能 %s 已发送，下次间隔 %.2f 秒。", key, interval)
            if sent:
                schedule[action_name] = time.monotonic() + max(0.05, interval)
            else:
                schedule[action_name] = time.monotonic() + max(
                    0.05, float(combat.get("input_retry_seconds", 0.15))
                )

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
                if (
                    not self.window.supports_focus_pulse
                    and not self.window.is_foreground()
                ):
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
                # 前台专用模式沿用悬停检测；焦点脉冲模式在按钮真正可用后
                # 才短暂切前台点击，避免死亡等待期间每 0.2 秒抢焦点。
                if not self.window.supports_focus_pulse:
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
                if (
                    self.window.supports_focus_pulse
                    or self.window.is_foreground()
                ):
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

    def ui_template_visible(
        self, template: Optional[np.ndarray], threshold: float
    ) -> tuple[bool, float]:
        if template is None:
            return False, 0.0
        _, score = self.template_match(self.capture_frame(), template)
        return score >= threshold, score

    def find_inventory_icon(
        self, templates: dict[str, Optional[np.ndarray]]
    ) -> tuple[Optional[str], Optional[list[float]], float]:
        """在个人校准的背包网格内寻找图标，允许售卖后的物品自动重排。"""
        top_left = self.inventory_profile.get("inventory_grid_top_left")
        bottom_right = self.inventory_profile.get("inventory_grid_bottom_right")
        if not top_left or not bottom_right:
            return None, None, 0.0
        frame = self.capture_frame()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape
        x1, y1 = int(top_left[0] * width), int(top_left[1] * height)
        x2, y2 = int(bottom_right[0] * width), int(bottom_right[1] * height)
        x1, x2 = sorted((max(0, x1), min(width, x2)))
        y1, y2 = sorted((max(0, y1), min(height, y2)))
        crop = gray[y1:y2, x1:x2]
        reference_width = float(self.cfg.get("reference_client_size", [1280, 720])[0])
        scale = width / reference_width
        best_name: Optional[str] = None
        best_point: Optional[list[float]] = None
        best_score = -1.0
        for name, template in templates.items():
            if template is None:
                continue
            resized = cv2.resize(
                template,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
            )
            if crop.shape[0] < resized.shape[0] or crop.shape[1] < resized.shape[1]:
                continue
            _, score, _, location = cv2.minMaxLoc(
                cv2.matchTemplate(crop, resized, cv2.TM_CCOEFF_NORMED)
            )
            if score > best_score:
                center_x = x1 + location[0] + resized.shape[1] // 2
                center_y = y1 + location[1] + resized.shape[0] // 2
                best_name = name
                best_point = [center_x / width, center_y / height]
                best_score = float(score)
        return best_name, best_point, best_score

    def find_sell_confirm_button(self) -> tuple[Optional[list[float]], float]:
        """在售卖数量弹窗中按橙色按钮形状定位“确认”，不依赖弹窗固定坐标。"""
        action_cfg = self.cfg.get("inventory_automation", {})
        frame = self.capture_frame()
        height, width = frame.shape[:2]
        region = action_cfg.get("quantity_confirm_search_region", [0.30, 0.50, 0.48, 0.72])
        x1, y1 = int(region[0] * width), int(region[1] * height)
        x2, y2 = int(region[2] * width), int(region[3] * height)
        crop = frame[y1:y2, x1:x2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        low = np.array(action_cfg.get("confirm_orange_hsv_low", [5, 100, 100]), dtype=np.uint8)
        high = np.array(action_cfg.get("confirm_orange_hsv_high", [30, 255, 255]), dtype=np.uint8)
        mask = cv2.inRange(hsv, low, high)
        count, _, stats, centroids = cv2.connectedComponentsWithStats(mask)
        best_point: Optional[list[float]] = None
        best_score = 0.0
        for index in range(1, count):
            left, top, box_width, box_height, area = stats[index]
            width_ratio = box_width / width
            height_ratio = box_height / height
            if not (0.06 <= width_ratio <= 0.12 and 0.035 <= height_ratio <= 0.08):
                continue
            fill_ratio = area / max(1, box_width * box_height)
            if fill_ratio < 0.45:
                continue
            center_x = x1 + float(centroids[index][0])
            center_y = y1 + float(centroids[index][1])
            score = fill_ratio * min(1.0, area / 2500.0)
            if score > best_score:
                best_score = score
                best_point = [center_x / width, center_y / height]
        return best_point, best_score

    def inventory_actions_available(self, required: tuple[str, ...]) -> bool:
        action_cfg = self.cfg.get("inventory_automation", {})
        if not bool(action_cfg.get("enabled", False)):
            return False
        missing = [name for name in required if not self.inventory_profile.get(name)]
        if missing:
            if not self._inventory_missing_warned:
                logging.warning(
                    "背包自动化尚未校准（缺少 %s），本次将安全跳过；请运行“校准背包装备.bat”。",
                    ", ".join(missing),
                )
                self._inventory_missing_warned = True
            return False
        return True

    def close_optional_panels(self) -> None:
        """按需用 ESC 收起数量框、商店和背包，不在 HUD 上多按 ESC。"""
        action_cfg = self.cfg.get("inventory_automation", {})
        threshold = float(action_cfg.get("panel_match_threshold", 0.85))
        templates = (
            self.sell_quantity_template,
            self.shop_template,
            self.inventory_template,
        )
        for _ in range(max(1, int(action_cfg.get("close_escape_presses", 3)))):
            if not any(
                self.ui_template_visible(template, threshold)[0]
                for template in templates
                if template is not None
            ):
                return
            self.window.tap_key("ESC", live=self.live)
            time.sleep(max(0.05, float(action_cfg.get("panel_step_seconds", 0.6))))

    def open_inventory_checked(self) -> bool:
        action_cfg = self.cfg.get("inventory_automation", {})
        threshold = float(action_cfg.get("panel_match_threshold", 0.72))
        visible, _ = self.ui_template_visible(self.inventory_template, threshold)
        if visible:
            return True
        attempts = max(1, int(action_cfg.get("inventory_open_attempts", 2)))
        score = 0.0
        for attempt in range(1, attempts + 1):
            self.window.tap_key(
                str(action_cfg.get("open_key", "B")),
                hold_seconds=float(action_cfg.get("open_key_hold_seconds", 0.12)),
                live=self.live,
            )
            self.wait(float(action_cfg.get("inventory_open_seconds", 1.2)))
            visible, score = self.ui_template_visible(self.inventory_template, threshold)
            if visible:
                return True
            if attempt < attempts:
                self.wait(float(action_cfg.get("inventory_open_retry_seconds", 0.5)))
                # 再截一次，避免背包只是显示较慢而第二次 B 反而将其关闭。
                visible, score = self.ui_template_visible(
                    self.inventory_template, threshold
                )
                if visible:
                    return True
        logging.warning(
            "连续 %s 次按 B 后仍未确认背包已打开（匹配分数 %.3f），跳过本次背包操作。",
            attempts,
            score,
        )
        return False

    def switch_weapon(self, target: str) -> bool:
        """同一格双击可在两把主武器间切换；target 只用于维护流程状态。"""
        if target not in ("boss", "farm"):
            raise ValueError(f"未知武器目标：{target}")
        if (target == "boss") == self._boss_weapon_active:
            return True
        grid_keys = ("inventory_grid_top_left", "inventory_grid_bottom_right")
        if not self.inventory_actions_available(grid_keys):
            return False
        action_cfg = self.cfg.get("inventory_automation", {})
        try:
            self.set_phase("inventory_weapon_switch")
            if not self.open_inventory_checked():
                return False
            before_name, weapon_point, before_score = self.find_inventory_icon(
                self.weapon_variant_templates
            )
            weapon_threshold = float(action_cfg.get("weapon_match_threshold", 0.78))
            if weapon_point is None or before_score < weapon_threshold:
                logging.warning(
                    "背包网格内未可靠识别换武器图标（匹配分数 %.3f），保留当前武器。",
                    before_score,
                )
                return False
            logging.info(
                "识别到换武器图标 %s（%.3f），双击切换为%s武器。",
                before_name,
                before_score,
                "BOSS" if target == "boss" else "刷怪",
            )
            self.window.double_click_normalized(
                weapon_point,
                interval_seconds=float(action_cfg.get("double_click_interval_seconds", 0.12)),
                live=self.live,
            )
            self.wait(float(action_cfg.get("post_weapon_switch_seconds", 0.8)))
            after_name, _, after_score = self.find_inventory_icon(
                self.weapon_variant_templates
            )
            if (
                after_name is None
                or after_score < weapon_threshold
                or after_name == before_name
            ):
                logging.warning(
                    "双击后未确认武器图标已改变（切换前 %s，切换后 %s/%.3f），不更新内部装备状态。",
                    before_name,
                    after_name,
                    after_score,
                )
                return False
            self._boss_weapon_active = target == "boss"
            return True
        except Exception as exc:
            logging.warning("切换%s武器失败，将保留当前武器继续流程：%s", target, exc)
            return False
        finally:
            try:
                self.close_optional_panels()
            except Exception as exc:
                logging.warning("切换武器后的面板收尾失败；主流程仍将继续：%s", exc)

    def sell_cycle_medicine(self) -> bool:
        """每个完整疲劳循环结束后出售一次第一格药品；识别失败则安全跳过。"""
        required = (
            "inventory_grid_top_left",
            "inventory_grid_bottom_right",
            "shop_button",
            "sell_slot",
            "sell_all_button",
        )
        if not self.inventory_actions_available(required):
            return False
        action_cfg = self.cfg.get("inventory_automation", {})
        threshold = float(action_cfg.get("panel_match_threshold", 0.72))
        try:
            self.set_phase("inventory_cycle_sale")
            if not self.open_inventory_checked():
                return False
            logging.info("完整疲劳循环结束：打开杂货铺，准备出售药品。")
            self.window.click_normalized(self.inventory_profile["shop_button"], live=self.live)
            self.wait(float(action_cfg.get("shop_open_seconds", 1.0)))
            shop_visible, shop_score = self.ui_template_visible(self.shop_template, threshold)
            if not shop_visible:
                logging.warning("未确认杂货铺已打开（匹配分数 %.3f），取消本次售卖。", shop_score)
                return False

            _, medicine_point, medicine_score = self.find_inventory_icon(
                {"medicine": self.medicine_item_template}
            )
            medicine_threshold = float(action_cfg.get("medicine_match_threshold", 0.76))
            if medicine_point is None or medicine_score < medicine_threshold:
                logging.warning(
                    "背包网格内未确认目标药品（匹配分数 %.3f）；为避免卖错物品，本轮不拖拽。",
                    medicine_score,
                )
                return False

            self.window.drag_normalized(
                medicine_point,
                self.inventory_profile["sell_slot"],
                hold_seconds=float(action_cfg.get("drag_seconds", 0.45)),
                live=self.live,
            )
            self.wait(float(action_cfg.get("quantity_dialog_seconds", 0.7)))
            dialog_visible, dialog_score = self.ui_template_visible(
                self.sell_quantity_template, threshold
            )
            if not dialog_visible:
                logging.warning(
                    "拖拽后未出现售卖数量框（匹配分数 %.3f）；可能药品格为空，取消本次售卖。",
                    dialog_score,
                )
                return False
            # 数量框默认选中整组最大数量。弹窗在不同实机上的纵向位置
            # 有偏差，因此识别橙色按钮并在点击后确认弹窗真的消失。
            confirm_attempts = max(
                1, int(action_cfg.get("quantity_confirm_attempts", 3))
            )
            confirmed = False
            for confirm_attempt in range(1, confirm_attempts + 1):
                confirm_point, confirm_score = self.find_sell_confirm_button()
                if confirm_point is None:
                    logging.warning(
                        "第 %s/%s 次未定位到橙色售卖确认按钮（形状分数 %.3f）。",
                        confirm_attempt,
                        confirm_attempts,
                        confirm_score,
                    )
                    break
                logging.info(
                    "点击售卖数量确认按钮：位置 %s，形状分数 %.3f。",
                    [round(value, 4) for value in confirm_point],
                    confirm_score,
                )
                self.window.click_normalized(confirm_point, live=self.live)
                self.wait(
                    float(action_cfg.get("after_quantity_confirm_seconds", 0.7))
                )
                still_visible, _ = self.ui_template_visible(
                    self.sell_quantity_template, threshold
                )
                if not still_visible:
                    confirmed = True
                    break
                logging.warning("点击后售卖数量弹窗仍存在，将重新定位并重试。")
            if not confirmed:
                logging.warning("未能确认售卖数量弹窗已经关闭，本轮不会点击‘全部出售’。")
                return False

            self.window.click_normalized(
                self.inventory_profile["sell_all_button"], live=self.live
            )
            self.wait(float(action_cfg.get("after_sale_seconds", 1.0)))
            _, remaining_point, remaining_score = self.find_inventory_icon(
                {"medicine": self.medicine_item_template}
            )
            if remaining_point is not None and remaining_score >= medicine_threshold:
                logging.warning(
                    "点击‘全部出售’后背包中仍识别到目标药品（%.3f），不记录为售卖成功。",
                    remaining_score,
                )
                return False
            logging.info("药品已从背包网格消失，确认售卖完成。")
            return True
        except Exception as exc:
            logging.warning("药品售卖失败；不中断挂机，将在下一完整循环再次尝试：%s", exc)
            return False
        finally:
            try:
                self.close_optional_panels()
            except Exception as exc:
                logging.warning("售药后的面板收尾失败；主流程仍将继续：%s", exc)

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

            # 疲劳提示依赖真实鼠标。焦点脉冲模式将整次“移出、移回、
            # OCR”放在同一个短会话中，识别完成后一次性恢复用户窗口。
            with self.window.input_session(live=self.live):
                self.window.move_client(*refresh, live=self.live)
                if not self.wait(
                    float(fatigue_cfg.get("refresh_leave_seconds", 0.25))
                ):
                    return None
                if self.revive_epoch != revive_epoch:
                    return self.read_fatigue(
                        attempts=attempts, refresh_rounds=refresh_rounds
                    )
                self.window.move_client(*hover, live=self.live)
                if not self.wait(
                    float(fatigue_cfg.get("hover_settle_seconds", 0.8))
                ):
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
        schedule = self.new_combat_schedule(boss=False)
        next_fatigue = 0.0
        while not self.stopped:
            self.check_control_keys()
            self.log_tail.poll()
            if not self.running:
                time.sleep(0.05)
                continue
            if (
                not self.window.supports_focus_pulse
                and not self.window.is_foreground()
            ):
                logging.warning("游戏失去前台，挂机暂停；切回游戏后继续。")
                while not self.window.is_foreground() and not self.stopped:
                    self.check_control_keys()
                    time.sleep(0.2)
                continue
            if self.check_and_handle_revive():
                schedule = self.new_combat_schedule(boss=False)
                continue
            now = time.monotonic()
            self.run_due_combat_inputs(schedule)
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
        self.mount_for_dungeon()

    def mount_for_dungeon(self) -> None:
        """进入副本后骑乘宠物；此动作只依赖快捷键，不依赖任何屏幕坐标。"""
        mount_cfg = self.cfg.get("dungeon_mount", {})
        if not bool(mount_cfg.get("enabled", True)) or self.stopped:
            return
        key = str(mount_cfg.get("key", "F")).upper()
        if key not in VK:
            raise RuntimeError(f"骑乘快捷键 {key!r} 不受支持，请检查 dungeon_mount.key。")
        logging.info("状态：进入副本后按 %s 骑乘宠物。", key)
        self.set_phase("dungeon_mounting")
        self.window.tap_key(
            key,
            hold_seconds=max(0.02, float(mount_cfg.get("hold_seconds", 0.08))),
            live=self.live,
        )
        self.wait(max(0.0, float(mount_cfg.get("settle_seconds", 0.8))))

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
        combat_cfg = self.cfg["combat"]
        fatigue_fallback_enabled = bool(
            combat_cfg.get("boss_fatigue_fallback_enabled", True)
        )
        fatigue_baseline = self.last_fatigue
        if fatigue_fallback_enabled and fatigue_baseline is None:
            logging.info("尚无战前疲劳基准，先读取一次用于 BOSS 死亡兜底判断。")
            fatigue_baseline = self.read_fatigue(attempts=3, refresh_rounds=1)
        fatigue_probe_delay = max(
            1.0,
            float(combat_cfg.get("boss_fatigue_probe_initial_delay_seconds", 12.0)),
        )
        fatigue_probe_interval = max(
            1.0,
            float(combat_cfg.get("boss_fatigue_probe_interval_seconds", 8.0)),
        )
        fatigue_gain_threshold = max(
            1,
            int(combat_cfg.get("boss_fatigue_gain_threshold", 40)),
        )
        next_fatigue_probe = time.monotonic() + fatigue_probe_delay
        deadline = time.monotonic() + self.cfg["timing"]["boss_timeout_seconds"]
        revive_epoch = self.revive_epoch
        schedule = self.new_combat_schedule(boss=True)
        while time.monotonic() < deadline and not self.stopped:
            if self.check_and_handle_revive():
                deadline = time.monotonic() + self.cfg["timing"]["boss_timeout_seconds"]
                revive_epoch = self.revive_epoch
                schedule = self.new_combat_schedule(boss=True)
                continue
            self.run_due_combat_inputs(schedule)
            self.log_tail.poll()
            if self.log_tail.boss_seen:
                logging.info("日志已识别 BOSS 实体 %s。", self.cfg["combat"]["boss_entity_id"])
            if self.log_tail.boss_dead:
                logging.info("日志确认 BOSS 已死亡。")
                return True
            now = time.monotonic()
            if (
                fatigue_fallback_enabled
                and fatigue_baseline is not None
                and now >= next_fatigue_probe
            ):
                # 某些游戏运行状态不再向 Player.log 输出战斗诊断。BOSS 击杀
                # 仍会立即恢复约 100 点疲劳，因此用战前值作独立兜底。第一次
                # 达到阈值后立刻再读一次，避免一次 OCR 误读导致提前退出。
                fatigue = self.read_fatigue(attempts=3, refresh_rounds=1)
                if (
                    fatigue is not None
                    and fatigue - fatigue_baseline >= fatigue_gain_threshold
                ):
                    confirmed_fatigue = self.read_fatigue(
                        attempts=2, refresh_rounds=1
                    )
                    if (
                        confirmed_fatigue is not None
                        and confirmed_fatigue - fatigue_baseline
                        >= fatigue_gain_threshold
                    ):
                        logging.info(
                            "疲劳从 %s 恢复到 %s，连续两次确认增量达到 %s；"
                            "判定 BOSS 已死亡。",
                            fatigue_baseline,
                            confirmed_fatigue,
                            fatigue_gain_threshold,
                        )
                        return True
                    logging.warning(
                        "BOSS 疲劳增量第一次达到阈值，但复核值为 %s；"
                        "继续战斗并等待下一轮确认。",
                        confirmed_fatigue,
                    )
                # OCR 会占用数秒，重新建立调度，避免恢复战斗时一次性补发
                # 已经过期的 A、Q、W、Space。
                schedule = self.new_combat_schedule(boss=True)
                next_fatigue_probe = time.monotonic() + fatigue_probe_interval
            self.wait(0.03)
            if self.revive_epoch != revive_epoch:
                deadline = time.monotonic() + self.cfg["timing"]["boss_timeout_seconds"]
                revive_epoch = self.revive_epoch
                next_fatigue_probe = time.monotonic() + fatigue_probe_delay
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
            self.switch_weapon("boss")
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
                # 先切回刷怪武器，再出售第一格药品。售出物品后背包会自动
                # 紧凑排列，因此必须保持这个顺序，避免武器格提前发生位移。
                self.switch_weapon("farm")
                self.sell_cycle_medicine()
                return
            logging.info("疲劳仍为 %s，准备再次进入副本。", fatigue)

    def dungeon_only_loop(self) -> None:
        completed_runs = 0
        logging.info("状态：只刷副本；不会读取疲劳，也不会返回普通挂机。")
        while not self.stopped:
            self.travel_to_first_entrance()
            self.enter_dungeon()
            self.travel_to_boss()
            self.switch_weapon("boss")
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
        logging.info(
            "已连接游戏窗口，客户区 %sx%s，live=%s，input_mode=%s，capture_mode=%s",
            w,
            h,
            self.live,
            self.window.input_mode,
            self.window.capture_mode,
        )
        if self.window.supports_focus_pulse:
            print("准备完成。可停留在其他窗口，按 F8 开始/暂停，按 F12 立即停止。")
            print("输入时会短暂切到游戏并自动恢复；连续战斗期间可能影响正在进行的打字。")
        else:
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

INVENTORY_CALIBRATION_STEPS = [
    (
        "inventory_grid_top_left",
        "按 B 打开背包，把鼠标移到第一排第一列物品格的左上角",
    ),
    (
        "inventory_grid_bottom_right",
        "保持背包打开，把鼠标移到当前可见背包网格最后一格的右下角（覆盖整个物品区域）",
    ),
    (
        "shop_button",
        "保持背包打开，把鼠标移到画面顶部的‘杂货铺’按钮中心",
    ),
    (
        "sell_slot",
        "手动打开杂货铺，把鼠标移到左侧窗口底部回收栏的第一个空方框中心",
    ),
    (
        "sell_all_button",
        "把鼠标移到回收栏右侧的‘全部出售’按钮中心",
    ),
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


def calibrate_inventory(cfg: dict) -> None:
    """只记录个人背包/商店坐标，不修改主流程地图坐标。"""
    window = GameWindow(cfg["window_title"])
    window.locate()
    w, h = window.client_size()
    if INVENTORY_PROFILE_PATH.exists():
        profile = load_inventory_profile()
    else:
        with INVENTORY_PROFILE_EXAMPLE_PATH.open("r", encoding="utf-8") as f:
            profile = json.load(f)
    print(f"找到游戏客户区：{w}x{h}")
    print("本流程只记录坐标，不会点击、拖拽或出售任何物品。")
    print("每一步按说明手动准备界面，把鼠标放到目标中心后按 F2；按 F12 取消。\n")
    for name, instruction in INVENTORY_CALIBRATION_STEPS:
        print(f"[{name}] {instruction}，然后按 F2。")
        winsound.Beep(750, 120)
        if not wait_key_edge(VK["F2"]):
            print("已取消背包装备校准；此前记录尚未写入文件。")
            return
        sx, sy = win32api.GetCursorPos()
        cx, cy = win32gui.ScreenToClient(window.hwnd, (sx, sy))
        if not (0 <= cx < w and 0 <= cy < h):
            raise RuntimeError(f"记录点 ({cx}, {cy}) 不在游戏客户区内，请重新运行校准。")
        profile[name] = window.client_to_normalized((cx, cy))
        print(f"  已记录客户区坐标 ({cx}, {cy})，归一化 {profile[name]}\n")
    save_inventory_profile(profile)
    winsound.Beep(1000, 250)
    print(f"背包装备校准完成，个人坐标已保存到 {INVENTORY_PROFILE_PATH}")


def ocr_test(cfg: dict) -> None:
    recovery_cfg = cfg.get("recovery", {})
    window_cfg = cfg.get("window_control", {})
    window = GameWindow(
        cfg["window_title"],
        reconnect_attempts=max(
            1, int(recovery_cfg.get("window_reconnect_attempts", 5))
        ),
        reconnect_interval_seconds=max(
            0.1, float(recovery_cfg.get("window_reconnect_interval_seconds", 2.0))
        ),
        capture_retries=max(1, int(recovery_cfg.get("capture_retries", 5))),
        capture_retry_seconds=max(
            0.1, float(recovery_cfg.get("capture_retry_seconds", 0.5))
        ),
        input_mode=str(window_cfg.get("input_mode", "foreground_only")),
        focus_settle_seconds=max(
            0.0, float(window_cfg.get("focus_settle_seconds", 0.08))
        ),
        focus_activation_attempts=max(
            1, int(window_cfg.get("focus_activation_attempts", 3))
        ),
        focus_activation_retry_seconds=max(
            0.01, float(window_cfg.get("focus_activation_retry_seconds", 0.08))
        ),
        post_input_seconds=max(
            0.0, float(window_cfg.get("post_input_seconds", 0.12))
        ),
        restore_previous_window=bool(
            window_cfg.get("restore_previous_window", True)
        ),
        capture_mode=str(window_cfg.get("capture_mode", "screen")),
    )
    window.locate()
    reader = FatigueOCR(cfg.get("debug"))
    fatigue_cfg = cfg["fatigue"]
    refresh = window.normalized_to_client(
        fatigue_cfg.get("refresh_point", [0.5, 0.5])
    )
    hover = window.normalized_to_client(fatigue_cfg["hover_point"])
    print("程序将按正式流程把鼠标移出再移回疲劳条，并读取最新提示框。")
    time.sleep(2)
    with window.input_session(live=True):
        window.move_client(*refresh, live=True)
        time.sleep(float(fatigue_cfg.get("refresh_leave_seconds", 0.25)))
        window.move_client(*hover, live=True)
        time.sleep(float(fatigue_cfg.get("hover_settle_seconds", 0.8)))
        value, texts = reader.read(
            window.capture(),
            fatigue_cfg["ocr_region"],
            fatigue_cfg.get("value_region"),
        )
    print(f"识别结果：{value}/1000；原始OCR：{texts}")


def focus_pulse_test(cfg: dict) -> None:
    """Safe smoke test for background capture, focus restoration, map click and OCR."""
    recovery_cfg = cfg.get("recovery", {})
    window_cfg = cfg.get("window_control", {})
    window = GameWindow(
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
        input_mode="focus_pulse",
        capture_mode="print_window",
        focus_settle_seconds=float(window_cfg.get("focus_settle_seconds", 0.08)),
        post_input_seconds=float(window_cfg.get("post_input_seconds", 0.12)),
        restore_previous_window=True,
    )
    window.locate()
    if win32gui.IsIconic(window.hwnd):
        raise RuntimeError("请先还原游戏窗口；可以遮挡，但不能最小化。")

    previous_window = win32gui.GetForegroundWindow()
    previous_cursor = win32api.GetCursorPos()
    template = cv2.imread(str(ASSET_DIR / "map_open_indicator.png"), cv2.IMREAD_GRAYSCALE)
    if template is None:
        raise RuntimeError("缺少地图检测模板 assets/map_open_indicator.png")

    def map_score() -> float:
        frame = window.capture()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        reference_width = float(cfg.get("reference_client_size", [1280, 720])[0])
        scale = gray.shape[1] / reference_width
        resized = cv2.resize(
            template,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
        )
        return float(
            cv2.minMaxLoc(
                cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
            )[1]
        )

    threshold = float(cfg["map_detection"]["open_match_threshold"])
    if map_score() >= threshold:
        window.tap_key("ESC", live=True)
        time.sleep(0.6)
    closed_score = map_score()
    if closed_score >= threshold:
        raise RuntimeError("测试前无法关闭地图，请回到普通 HUD 后重试。")

    started = time.perf_counter()
    window.click_normalized(cfg["coordinates"]["map_button"], live=True)
    pulse_ms = (time.perf_counter() - started) * 1000
    time.sleep(0.7)
    opened_score = map_score()
    if opened_score < threshold:
        raise RuntimeError(
            f"焦点脉冲点击未能打开地图（匹配分数 {opened_score:.3f}）。"
        )
    window.tap_key("ESC", live=True)
    time.sleep(0.6)
    final_score = map_score()
    if final_score >= threshold:
        raise RuntimeError("地图已打开，但焦点脉冲 ESC 未能将其关闭。")

    reader = FatigueOCR(cfg.get("debug"))
    fatigue_cfg = cfg["fatigue"]
    with window.input_session(live=True):
        window.move_client(
            *window.normalized_to_client(
                fatigue_cfg.get("refresh_point", [0.5, 0.5])
            ),
            live=True,
        )
        time.sleep(float(fatigue_cfg.get("refresh_leave_seconds", 0.25)))
        window.move_client(
            *window.normalized_to_client(fatigue_cfg["hover_point"]), live=True
        )
        time.sleep(float(fatigue_cfg.get("hover_settle_seconds", 0.8)))
        fatigue, texts = reader.read(
            window.capture(),
            fatigue_cfg["ocr_region"],
            fatigue_cfg.get("value_region"),
        )

    focus_restored = win32gui.GetForegroundWindow() == previous_window
    cursor_restored = win32api.GetCursorPos() == previous_cursor
    print(
        f"地图测试：关闭 {closed_score:.3f} -> 打开 {opened_score:.3f} -> "
        f"关闭 {final_score:.3f}；点击脉冲 {pulse_ms:.0f} ms"
    )
    print(f"疲劳 OCR：{fatigue}；原始结果：{texts}")
    print(
        f"窗口恢复：{focus_restored}；鼠标回到测试前位置：{cursor_restored}"
        "（测试期间若主动移动鼠标，程序会保留用户的新位置）"
    )
    if fatigue is None or not focus_restored:
        raise RuntimeError("焦点脉冲测试未完全通过，请查看以上结果。")
    print("焦点脉冲冒烟测试通过。")


def inventory_detection_test(cfg: dict) -> None:
    """只验证背包/商店和图标定位，不换装、不拖拽、不出售。"""
    bot = Bot(cfg, live=True, mode="full")
    bot.running = True
    required = (
        "inventory_grid_top_left",
        "inventory_grid_bottom_right",
        "shop_button",
    )
    missing = [name for name in required if not bot.inventory_profile.get(name)]
    if missing:
        raise RuntimeError(
            "背包检测测试前请先运行“校准背包装备.bat”；缺少："
            + ", ".join(missing)
        )
    action_cfg = cfg.get("inventory_automation", {})
    panel_threshold = float(action_cfg.get("panel_match_threshold", 0.85))
    weapon_threshold = float(action_cfg.get("weapon_match_threshold", 0.78))
    medicine_threshold = float(action_cfg.get("medicine_match_threshold", 0.76))
    try:
        if not bot.open_inventory_checked():
            raise RuntimeError("未能打开并确认背包界面。")
        weapon_name, weapon_point, weapon_score = bot.find_inventory_icon(
            bot.weapon_variant_templates
        )
        medicine_name, medicine_point, medicine_score = bot.find_inventory_icon(
            {"medicine": bot.medicine_item_template}
        )
        print(
            f"武器识别：{weapon_name}，分数 {weapon_score:.3f}，位置 {weapon_point}"
        )
        if weapon_point is None or weapon_score < weapon_threshold:
            raise RuntimeError("没有可靠识别到换武器图标。")
        if medicine_point is None or medicine_score < medicine_threshold:
            print(f"药品识别：当前背包中未找到目标药品（分数 {medicine_score:.3f}），这是允许的。")
        else:
            print(
                f"药品识别：{medicine_name}，分数 {medicine_score:.3f}，位置 {medicine_point}"
            )

        bot.window.click_normalized(bot.inventory_profile["shop_button"], live=True)
        bot.wait(float(action_cfg.get("shop_open_seconds", 1.0)))
        shop_visible, shop_score = bot.ui_template_visible(
            bot.shop_template, panel_threshold
        )
        print(f"杂货铺识别：{shop_visible}，分数 {shop_score:.3f}")
        if not shop_visible:
            raise RuntimeError("未能打开并确认杂货铺界面。")
        print("背包识别测试通过；没有换武器、拖拽物品或执行出售。")
    finally:
        bot.close_optional_panels()


def merge_config_overrides(target: dict, overrides: dict) -> None:
    """递归合并测试参数；以下划线开头的说明字段仅供人阅读。"""
    for key, value in overrides.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_config_overrides(target[key], value)
        else:
            target[key] = deepcopy(value)


def full_chain_smoke_test(cfg: dict, cycles: int = 2) -> None:
    """从独立 JSON 载入快速阈值并真实跑完整闭环；不覆盖正式配置。"""
    if not SMOKE_CONFIG_PATH.exists():
        raise FileNotFoundError(f"找不到冒烟测试参数：{SMOKE_CONFIG_PATH}")
    with SMOKE_CONFIG_PATH.open("r", encoding="utf-8") as f:
        smoke_cfg = json.load(f)

    smoke_meta = smoke_cfg.get("smoke_test", {})
    minimum_cycles = max(2, int(smoke_meta.get("minimum_cycles", 2)))
    cycles = max(minimum_cycles, int(cycles))
    test_cfg = deepcopy(cfg)
    merge_config_overrides(
        test_cfg,
        {key: value for key, value in smoke_cfg.items() if key != "smoke_test"},
    )

    print(
        f"快速完整链路测试：{cycles} 轮；每轮只要求 1 次副本，"
        f"Q/W 测试间隔为 {test_cfg['combat']['skill_q_interval_seconds']}/"
        f"{test_cfg['combat']['skill_w_interval_seconds']} 秒。"
    )
    print(
        f"测试参数来自 {SMOKE_CONFIG_PATH.name}，仅存在于本次进程；"
        "config.json 保持正式参数不变。"
    )
    bot = Bot(test_cfg, live=True, mode="full")
    bot.running = True
    try:
        for cycle_index in range(1, cycles + 1):
            logging.info("========== 完整链路冒烟测试第 %s/%s 轮 =========", cycle_index, cycles)
            bot.normal_farm_until_low()
            if bot.stopped:
                raise RuntimeError(f"第 {cycle_index} 轮普通挂机阶段被停止。")
            bot.boss_recovery_loop()
            if bot.stopped:
                raise RuntimeError(f"第 {cycle_index} 轮 BOSS 恢复阶段被停止。")
            logging.info("完整链路冒烟测试第 %s/%s 轮完成。", cycle_index, cycles)
    finally:
        bot.window.release_keys(("A", "SPACE", "Q", "W"), live=True)

    expected_keys = {
        str(test_cfg["combat"]["attack_key"]).upper(),
        str(test_cfg["combat"]["pickup_key"]).upper(),
        str(test_cfg["combat"]["skill_q_key"]).upper(),
        str(test_cfg["combat"]["skill_w_key"]).upper(),
    }
    missing_keys = sorted(
        key for key in expected_keys if bot.combat_input_counts.get(key, 0) <= 0
    )
    print(f"两轮战斗按键计数：{dict(bot.combat_input_counts)}")
    if missing_keys:
        raise RuntimeError("完整链路虽然结束，但这些战斗按键没有被覆盖：" + ", ".join(missing_keys))
    print(f"完整链路 {cycles} 轮冒烟测试通过。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Curious Beast 外部挂机 MVP")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    parser.add_argument("--calibrate", action="store_true", help="运行一次性坐标校准")
    parser.add_argument("--calibrate-map", action="store_true", help="只重新校准右上角小地图")
    parser.add_argument(
        "--calibrate-inventory",
        action="store_true",
        help="单独校准换武器、药品拖拽和杂货铺坐标",
    )
    parser.add_argument("--ocr-test", action="store_true", help="只测试疲劳 OCR")
    parser.add_argument(
        "--focus-test", action="store_true", help="测试被遮挡截图、焦点脉冲和 OCR"
    )
    parser.add_argument(
        "--inventory-test",
        action="store_true",
        help="无损测试背包、杂货铺、武器和药品图标识别",
    )
    parser.add_argument(
        "--full-smoke-test",
        action="store_true",
        help="用临时快速阈值真实执行至少两轮完整疲劳闭环",
    )
    parser.add_argument(
        "--smoke-cycles",
        type=int,
        default=2,
        help="完整闭环冒烟测试轮数，最小为 2",
    )
    parser.add_argument(
        "--config-overrides",
        help="额外配置覆盖文件；相对路径以程序目录为基准，例如 laptop_config.json",
    )
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
    if args.config_overrides:
        override_path = Path(args.config_overrides).expanduser()
        if not override_path.is_absolute():
            override_path = APP_DIR / override_path
        override_path = override_path.resolve()
        if not override_path.exists():
            raise FileNotFoundError(f"找不到额外配置文件：{override_path}")
        with override_path.open("r", encoding="utf-8") as f:
            overrides = json.load(f)
        merge_config_overrides(cfg, overrides)
        logging.info("已加载额外配置：%s", override_path)
    if args.calibrate:
        calibrate(cfg)
        return 0
    if args.calibrate_map:
        calibrate_map_button(cfg)
        return 0
    if args.calibrate_inventory:
        calibrate_inventory(cfg)
        return 0
    if args.ocr_test:
        ocr_test(cfg)
        return 0
    if args.focus_test:
        focus_pulse_test(cfg)
        return 0
    if args.inventory_test:
        inventory_detection_test(cfg)
        return 0
    if args.full_smoke_test:
        if not args.live:
            raise RuntimeError("完整链路冒烟测试会真实控制游戏，必须同时传入 --live。")
        full_chain_smoke_test(cfg, cycles=args.smoke_cycles)
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
