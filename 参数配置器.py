from __future__ import annotations

import copy
import json
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
PYTHON_RUNNER = APP_DIR / "_运行Python.bat"
MVP_SCRIPT = APP_DIR / "mvp_bot.py"
SMOKE_RUNNER = APP_DIR / "完整链路冒烟测试.bat"


FIELDS = [
    ("基础设置", [
        ("游戏窗口标题", ("window_title",), "text", None),
    ]),
    ("疲劳与循环", [
        ("普通挂机低疲劳阈值", ("fatigue", "low_threshold"), "int", "低于或等于此值时进入副本"),
        ("Boss 恢复目标值", ("fatigue", "boss_target"), "int", "达到或超过此值时返回普通地图"),
        ("普通挂机检查间隔（秒）", ("fatigue", "check_interval_seconds"), "float", None),
        ("OCR 结果等待上限（秒）", ("fatigue", "result_timeout_seconds"), "float", "仅在进出副本等非战斗关口等待后台 OCR"),
    ]),
    ("战斗参数", [
        ("基础攻击间隔（秒）", ("combat", "attack_interval_seconds"), "float", None),
        ("拾取间隔（秒）", ("combat", "pickup_interval_seconds"), "float", None),
        ("启用 Q 技能", ("combat", "skill_q_enabled"), "bool", None),
        ("Q 技能间隔（秒）", ("combat", "skill_q_interval_seconds"), "float", None),
        ("启用 W 技能", ("combat", "skill_w_enabled"), "bool", None),
        ("W 技能间隔（秒）", ("combat", "skill_w_interval_seconds"), "float", None),
        ("Boss 疲劳增量阈值", ("combat", "boss_fatigue_gain_threshold"), "int", "达到该增量后作为死亡候选"),
        ("Boss 连续确认次数", ("combat", "boss_fatigue_confirm_samples"), "int", "连续 OCR 结果均满足阈值才判定死亡"),
    ]),
    ("背包与武器", [
        ("启用背包自动化", ("inventory_automation", "enabled"), "bool", None),
        ("武器识别阈值", ("inventory_automation", "weapon_match_threshold"), "float", "越高越严格，默认 0.78"),
        ("药品识别阈值", ("inventory_automation", "medicine_match_threshold"), "float", "越高越严格，默认 0.76"),
        ("切换武器后等待（秒）", ("inventory_automation", "post_weapon_switch_seconds"), "float", None),
    ]),
    ("窗口与截图", [
        ("输入模式", ("window_control", "input_mode"), "choice", ["focus_pulse", "foreground_only"]),
        ("截图模式", ("window_control", "capture_mode"), "choice", ["print_window", "screen"]),
    ]),
]


def get_value(data: dict, path: tuple[str, ...]):
    current = data
    for key in path:
        current = current[key]
    return current


def set_value(data: dict, path: tuple[str, ...], value) -> None:
    current = data
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value


class ConfigEditor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Curious Beast 参数配置器 v0.0.3")
        self.geometry("720x760")
        self.minsize(650, 620)
        self.config_data: dict = {}
        self.variables: dict[tuple[str, ...], tuple[str, tk.Variable]] = {}
        self.status = tk.StringVar(value="正在读取 config.json……")
        self._build_ui()
        self.load_config()

    def _build_ui(self) -> None:
        header = ttk.Frame(self, padding=(16, 14, 16, 8))
        header.pack(fill="x")
        ttk.Label(header, text="Curious Beast 参数配置", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            header,
            text="修改后点击“保存参数”。保存后再启动测试或挂机，运行中修改不会自动生效。",
            foreground="#555555",
        ).pack(anchor="w", pady=(5, 0))

        outer = ttk.Frame(self, padding=(12, 0, 12, 0))
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.form = ttk.Frame(canvas)
        self.form.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.form, anchor="nw", width=670)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        row = 0
        for section, fields in FIELDS:
            ttk.Separator(self.form).grid(row=row, column=0, columnspan=3, sticky="ew", pady=(12, 4))
            row += 1
            ttk.Label(self.form, text=section, font=("Microsoft YaHei UI", 11, "bold")).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(0, 5)
            )
            row += 1
            for label, path, kind, extra in fields:
                ttk.Label(self.form, text=label).grid(row=row, column=0, sticky="w", padx=(8, 12), pady=4)
                if kind == "bool":
                    variable = tk.BooleanVar()
                    widget = ttk.Checkbutton(self.form, variable=variable)
                elif kind == "choice":
                    variable = tk.StringVar()
                    widget = ttk.Combobox(self.form, textvariable=variable, values=extra, state="readonly", width=22)
                else:
                    variable = tk.StringVar()
                    widget = ttk.Entry(self.form, textvariable=variable, width=25)
                widget.grid(row=row, column=1, sticky="w", pady=4)
                if extra and kind not in {"choice", "bool"}:
                    ttk.Label(self.form, text=extra, foreground="#777777").grid(row=row, column=2, sticky="w", padx=8)
                self.variables[path] = (kind, variable)
                row += 1
        self.form.columnconfigure(2, weight=1)

        footer = ttk.Frame(self, padding=(16, 8, 16, 12))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status, foreground="#2b6f44").pack(anchor="w", pady=(0, 7))
        buttons = ttk.Frame(footer)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="保存参数", command=self.save_config).pack(side="left")
        ttk.Button(buttons, text="重新读取", command=self.load_config).pack(side="left", padx=6)
        ttk.Button(buttons, text="启动完整冒烟测试", command=self.launch_smoke).pack(side="right")
        ttk.Button(buttons, text="启动挂机 MVP", command=self.launch_mvp).pack(side="right", padx=6)

    def load_config(self) -> None:
        try:
            self.config_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for path, (kind, variable) in self.variables.items():
                value = get_value(self.config_data, path)
                variable.set(value if kind != "text" else str(value))
            self.status.set("已读取当前参数。")
        except Exception as exc:
            self.status.set("读取失败")
            messagebox.showerror("读取配置失败", str(exc))

    def collect_config(self) -> dict:
        updated = copy.deepcopy(self.config_data)
        for path, (kind, variable) in self.variables.items():
            raw = variable.get()
            if kind == "int":
                value = int(raw)
            elif kind == "float":
                value = float(raw)
            elif kind == "bool":
                value = bool(raw)
            else:
                value = raw
            set_value(updated, path, value)
        return updated

    def save_config(self, quiet: bool = False) -> bool:
        try:
            updated = self.collect_config()
            CONFIG_PATH.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.config_data = updated
            self.status.set("参数已保存。")
            if not quiet:
                messagebox.showinfo("保存成功", "参数已保存，下一次启动时生效。")
            return True
        except (ValueError, KeyError) as exc:
            messagebox.showerror("参数格式错误", f"请检查数字输入：\n{exc}")
            return False
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return False

    def launch(self, args: list[str], label: str) -> None:
        if not self.save_config(quiet=True):
            return
        try:
            subprocess.Popen(
                [str(PYTHON_RUNNER), str(MVP_SCRIPT), *args],
                cwd=APP_DIR,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self.status.set(f"已启动{label}。请切回游戏后按 F8 开始，F12 停止。")
        except OSError as exc:
            messagebox.showerror("启动失败", str(exc))

    def launch_batch(self, batch_path: Path, label: str) -> None:
        if not self.save_config(quiet=True):
            return
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", str(batch_path)],
                cwd=APP_DIR,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self.status.set(f"已启动{label}，正在先进行环境预检。")
        except OSError as exc:
            messagebox.showerror("启动失败", str(exc))

    def launch_smoke(self) -> None:
        self.launch_batch(SMOKE_RUNNER, "完整链路冒烟测试")

    def launch_mvp(self) -> None:
        self.launch(["--live"], "挂机 MVP")


if __name__ == "__main__":
    app = ConfigEditor()
    app.mainloop()
