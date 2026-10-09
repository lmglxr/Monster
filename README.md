# Curious Beast 外部挂机 MVP

当前版本：`v0.0.3`（纯视觉版）

> 联网游戏可能禁止自动化工具。使用前请确认游戏规则，并自行承担账号风险。本工具不修改游戏文件、不注入游戏进程、不附加或读取游戏内存，也不读取游戏日志。

## v0.0.3 的识别方式

所有状态均来自游戏画面：

- **疲劳值**：鼠标悬停右下角疲劳提示后截取画面，用 EasyOCR 识别红色 `xxx/±1000` 数字；OpenCV 负责截图、红字增强和模板匹配，但它本身不是通用文字识别引擎，因此不能单独替代 OCR。
- **BOSS 死亡**：战前建立一次疲劳基准。战斗中持续攻击；后台 OCR 连续识别到疲劳恢复增量达到阈值后，才判定 BOSS 已死亡。
- **副本退出**：通过“场景切换中”加载画面的出现与消失确认，不使用场景 ID 或游戏日志。
- **暗黑鲨鱼筛选**：通过 `assets/dark_shark_boss.png` 外观模板确认，不读取 Boss HP。
- **复活、地图、背包、商店和入口**：全部使用 OpenCV 模板/颜色判断及已校准坐标。

## OCR 不打断战斗

EasyOCR/torch 的模型加载和推理运行在一个后台线程中。主循环只在采样时短暂移动鼠标刷新疲劳提示并截取一帧；**OCR 推理期间 A、Q、W、Space 的战斗调度继续执行**，不会因为识别计算而停住。为避免旧截图堆积，同一时间只允许一个 OCR 任务。

第一次运行时 EasyOCR 需要下载英文模型；模型加载也在后台线程中完成。首次建立疲劳基准前可能比后续采样慢一些，这是正常现象。

## 快速开始

1. 启动游戏并进入角色画面，保持窗口化运行。游戏可以被遮挡，但不能最小化、锁屏或休眠。
2. 双击 `启动挂机MVP.bat`。首次会创建 `.venv` 并安装 OpenCV、EasyOCR、torch 等依赖。
3. 首次使用请按需要运行：
   - `校准坐标.bat`
   - `测试疲劳识别.bat`
   - `测试焦点脉冲.bat`
4. 按 `F8` 开始/暂停；按 `F12` 立即停止。

低帧率笔记本使用 `启动挂机MVP-笔记本.bat`。它会加载 `laptop_config.json` 覆盖较慢的等待时间。

## 模式

- `启动挂机MVP.bat`：完整闭环。普通挂机 → 疲劳低于阈值 → 刷 BOSS 恢复疲劳 → 疲劳达到目标 → 返回刷怪点。
- `只刷副本.bat`：只刷暗黑鲨鱼。用外观模板过滤目标，不读取疲劳，也不会返回普通挂机。
- `测试暗黑鲨鱼视觉路线.bat`：只验证暗黑鲨鱼外观和路线，在 Boss 点保存截图，不进行战斗。

旧的 `测试暗黑鲨鱼HP路线.bat` 保留为兼容入口，实际会转到视觉路线测试。

## 关键参数

在 `config.json` 中调整：

- `fatigue.low_threshold`：普通挂机切入 BOSS 恢复的疲劳阈值。
- `fatigue.boss_target`：恢复完成后返回普通挂机的疲劳阈值。
- `fatigue.check_interval_seconds`：普通挂机的疲劳采样间隔。
- `fatigue.result_timeout_seconds`：进出副本等非战斗关口等待后台 OCR 返回的最长时间。
- `combat.boss_fatigue_gain_threshold`：BOSS 死亡候选所需的疲劳恢复增量。
- `combat.boss_fatigue_confirm_samples`：需要连续满足增量阈值的 OCR 次数；默认 `2`，用于降低误判。
- `boss_loop.minimum_runs`：每次进入 BOSS 恢复阶段至少确认击杀多少次。正式默认 `10`；前十次退出后跳过疲劳 OCR，第十次以后才复核是否达到 `boss_target`。
- `dark_boss.match_threshold`、`dark_boss.required_hits`：暗黑鲨鱼外观模板的判定严格度。

如果 UI 缩放或分辨率改变，重点重新校准 `fatigue.hover_point`、`fatigue.ocr_region` 和 `fatigue.value_region`。OCR 失败截图会保存为 `debug/ocr_failed_*.png`，数量和保存频率由 `debug` 段控制。

## 诊断与安全停止

- `环境检测.py --download-model`：检查依赖并预下载 EasyOCR 模型。
- `完整链路冒烟测试.bat`：以临时快速阈值执行至少两轮完整流程。
- `runtime.log`：脚本自身运行日志；不读取游戏日志。
- 窗口/截图恢复、复活失败、退出加载页未确认等情况会安全暂停或重试，避免盲目点击。

## 状态流程

```text
普通区域：A 攻击 + Space 拾取
  ↓ 定时后台 OCR 读取疲劳
疲劳 <= low_threshold
  ↓
地图寻路 → 入口模板识别 → 进入副本 → 副本地图寻路至 BOSS
  ↓
战前 OCR 建立疲劳基准
  ↓
A / Q / W / Space 持续战斗，同时后台 OCR 定期读取疲劳
  ↓
连续 N 次：当前疲劳 - 战前疲劳 >= gain_threshold
  ↓
判定 BOSS 死亡 → 拾取 → 视觉确认退出加载页
  ↓
OCR 读取当前疲劳
  ├─ < boss_target：再次进入副本
  └─ >= boss_target：切回刷怪武器 → 售药 → 返回刷怪点
```
