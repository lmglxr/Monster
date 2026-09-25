from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
import winsound
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import ImageGrab
import win32api
import win32con
import win32gui


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
ASSET_DIR = APP_DIR / "assets"
RUNTIME_LOG = APP_DIR / "runtime.log"
DEBUG_DIR = APP_DIR / "debug"

VK = {
    "A": 0x41,
    "SPACE": win32con.VK_SPACE,
    "F2": win32con.VK_F2,
    "F8": win32con.VK_F8,
    "F12": win32con.VK_F12,
}

FATIGUE_RE = re.compile(r"(-?\d{1,4})\s*/\s*1000")
COMBAT_RE = re.compile(
    r"场景:(\d+)\s+当前场景:(\d+)\s+目标:(\d+).*?HP:(\d+).*?生命:(\w+)"
)


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    raw_log_path = Path(cfg["log_path"])
    if not raw_log_path.is_absolute():
        cfg["log_path"] = str((APP_DIR / raw_log_path).resolve())
    return cfg


def save_config(cfg: dict) -> None:
    saved = dict(cfg)
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

    def locate(self) -> None:
        candidates: list[int] = []

        def callback(hwnd: int, _: object) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetWindowText(hwnd).strip().lower() == self.title.lower():
                candidates.append(hwnd)

        win32gui.EnumWindows(callback, None)
        if len(candidates) != 1:
            raise RuntimeError(
                f"需要且只能找到一个标题为 {self.title!r} 的窗口，当前找到 {len(candidates)} 个。"
            )
        self.hwnd = candidates[0]

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

    def require_foreground(self) -> None:
        if not self.is_foreground():
            raise RuntimeError("游戏不在前台，已暂停发送输入。切回游戏后按 F8 继续。")

    def capture(self) -> np.ndarray:
        self.ensure()
        x, y = self.client_origin()
        w, h = self.client_size()
        image = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)

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


class LogTail:
    def __init__(self, path: str):
        self.path = Path(path)
        self.position = 0
        self.latest_scene: Optional[int] = None
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


class FatigueOCR:
    def __init__(self) -> None:
        import easyocr

        logging.info("正在加载 EasyOCR 数字识别模型……")
        self.reader = easyocr.Reader(["en"], gpu=False, verbose=False)

    def read(self, frame: np.ndarray, region: list[float]) -> tuple[Optional[int], list[str]]:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = region
        crop = frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
        if crop.size == 0:
            return None, []
        crop = cv2.resize(crop, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
        texts = self.reader.readtext(
            crop,
            detail=0,
            paragraph=False,
            allowlist="0123456789/-:",
        )
        joined = " ".join(texts)
        match = FATIGUE_RE.search(joined)
        if match:
            return int(match.group(1)), texts
        DEBUG_DIR.mkdir(exist_ok=True)
        cv2.imwrite(str(DEBUG_DIR / f"ocr_failed_{int(time.time())}.png"), crop)
        return None, texts


class Bot:
    def __init__(self, cfg: dict, live: bool):
        self.cfg = cfg
        self.live = live
        self.window = GameWindow(cfg["window_title"])
        self.window.locate()
        self.log_tail = LogTail(cfg["log_path"])
        self.log_tail.start_at_end()
        self.ocr = FatigueOCR()
        self.portal_template = self._load_gray_template("portal.png")
        self.panel_template = self._load_gray_template("entry_panel_title.png")
        self.running = False
        self.stopped = False
        self.last_fatigue: Optional[int] = None
        self._f8_down = False
        self._f12_down = False

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
            logging.warning("收到 F12，立即停止。")
            winsound.Beep(500, 300)
        self._f12_down = f12_down

        f8_down = pressed(VK["F8"])
        if f8_down and not self._f8_down and not self.stopped:
            self.running = not self.running
            logging.info("%s", "开始" if self.running else "暂停")
            winsound.Beep(900 if self.running else 600, 180)
        self._f8_down = f8_down

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
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return True

    def click(self, name: str) -> None:
        point = self.cfg["coordinates"].get(name)
        if point is None:
            raise RuntimeError(f"坐标 {name} 尚未校准，请先运行 --calibrate。")
        logging.info("点击 %s -> %s", name, point)
        self.window.click_normalized(point, live=self.live)

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
        _, score = self.template_match(self.window.capture(), self.panel_template)
        logging.debug("副本面板匹配分数 %.3f", score)
        return score >= self.cfg["entrance"]["panel_match_threshold"]

    def find_portal(self) -> tuple[int, int]:
        deadline = time.monotonic() + 25.0
        best_score = 0.0
        best_center: Optional[tuple[int, int]] = None
        while time.monotonic() < deadline and not self.stopped:
            frame = self.window.capture()
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

    def read_fatigue(self, attempts: int = 3) -> Optional[int]:
        hover = self.window.normalized_to_client(self.cfg["fatigue"]["hover_point"])
        self.window.move_client(*hover, live=self.live)
        self.wait(0.8)
        values: list[int] = []
        for _ in range(attempts):
            frame = self.window.capture()
            value, texts = self.ocr.read(frame, self.cfg["fatigue"]["ocr_region"])
            logging.info("疲劳 OCR: value=%s raw=%s", value, texts)
            if value is not None and -1000 <= value <= 1000:
                values.append(value)
            self.wait(0.25)
        if not values:
            return None
        values.sort()
        value = values[len(values) // 2]
        self.last_fatigue = value
        logging.info("确认当前疲劳值：%s/1000", value)
        return value

    def normal_farm_until_low(self) -> None:
        logging.info("状态：普通区域挂机。")
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
            now = time.monotonic()
            if now >= next_attack:
                self.window.tap_key(combat["attack_key"], live=self.live)
                next_attack = now + combat["attack_interval_seconds"]
            if now >= next_pickup:
                self.window.tap_key(combat["pickup_key"], live=self.live)
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
        self.click("map_button")
        self.wait(self.cfg["timing"]["map_open_seconds"])
        self.click("normal_portal_marker")
        self.wait(self.cfg["timing"]["normal_auto_path_seconds"])

    def enter_dungeon(self) -> None:
        logging.info("状态：触发入口并进入冰牙海湾。")
        self.trigger_entrance_panel()
        self.click("enter_dungeon_button")
        self.wait(self.cfg["timing"]["dungeon_load_seconds"])

    def travel_to_boss(self) -> None:
        logging.info("状态：副本内地图寻路到 BOSS。")
        self.click("map_button")
        self.wait(self.cfg["timing"]["map_open_seconds"])
        self.click("dungeon_boss_marker")
        self.wait(self.cfg["timing"]["boss_auto_path_seconds"])

    def fight_boss(self) -> None:
        logging.info("状态：寻找并攻击 BOSS。")
        self.log_tail.boss_seen = False
        self.log_tail.boss_dead = False
        deadline = time.monotonic() + self.cfg["timing"]["boss_timeout_seconds"]
        while time.monotonic() < deadline and not self.stopped:
            self.window.tap_key(
                self.cfg["combat"]["attack_key"],
                live=self.live,
            )
            lines = self.log_tail.poll()
            if self.log_tail.boss_seen:
                logging.info("日志已识别 BOSS 实体 %s。", self.cfg["combat"]["boss_entity_id"])
            if self.log_tail.boss_dead:
                logging.info("日志确认 BOSS 已死亡。")
                return
            if any("角色死亡" in line or "玩家死亡" in line for line in lines):
                raise RuntimeError("日志检测到角色死亡，MVP 暂停。")
            self.wait(self.cfg["combat"]["attack_interval_seconds"])
        raise RuntimeError("BOSS 战超时，未从日志检测到 BOSS 死亡。")

    def pickup_and_exit(self) -> None:
        logging.info("状态：拾取 BOSS 掉落。")
        self.window.tap_key(
            self.cfg["combat"]["pickup_key"],
            hold_seconds=self.cfg["combat"]["boss_pickup_hold_seconds"],
            live=self.live,
        )
        self.wait(1.5)
        logging.info("状态：退出副本。")
        self.click("exit_dungeon_button")
        self.wait(0.8)
        self.click("confirm_exit_button")
        self.wait(self.cfg["timing"]["exit_load_seconds"])

    def boss_recovery_loop(self) -> None:
        first = True
        while not self.stopped:
            if first:
                self.travel_to_first_entrance()
                first = False
            self.enter_dungeon()
            self.travel_to_boss()
            self.fight_boss()
            self.pickup_and_exit()
            fatigue = self.read_fatigue()
            if fatigue is None:
                raise RuntimeError("退出副本后无法识别疲劳值，已安全暂停。")
            if fatigue >= self.cfg["fatigue"]["boss_target"]:
                logging.info("疲劳已恢复到 %s，结束 BOSS 循环。", fatigue)
                return
            logging.info("疲劳仍为 %s，准备再次进入副本。", fatigue)

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
        print("当前模式：" + ("实时输入" if self.live else "演练（不发送输入）"))
        while not self.stopped:
            self.check_control_keys()
            if not self.running:
                time.sleep(0.05)
                continue
            try:
                self.normal_farm_until_low()
                if not self.stopped:
                    self.boss_recovery_loop()
            except RuntimeError as exc:
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


def ocr_test(cfg: dict) -> None:
    window = GameWindow(cfg["window_title"])
    window.locate()
    reader = FatigueOCR()
    hover = window.normalized_to_client(cfg["fatigue"]["hover_point"])
    print("请保持游戏在前台。程序将把鼠标移到疲劳条并读取提示框。")
    time.sleep(2)
    window.move_client(*hover, live=True)
    time.sleep(0.8)
    value, texts = reader.read(window.capture(), cfg["fatigue"]["ocr_region"])
    print(f"识别结果：{value}/1000；原始OCR：{texts}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Curious Beast 外部挂机 MVP")
    parser.add_argument("--calibrate", action="store_true", help="运行一次性坐标校准")
    parser.add_argument("--ocr-test", action="store_true", help="只测试疲劳 OCR")
    parser.add_argument("--live", action="store_true", help="允许真实发送输入；否则为演练模式")
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
    if args.ocr_test:
        ocr_test(cfg)
        return 0
    bot = Bot(cfg, live=args.live)
    bot.run()
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
