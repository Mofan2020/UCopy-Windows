# UCopy 开发笔记

## v1.4.1 — perf: 摄像头/麦克风检测稳定性强化

### 改动概述

1. **DLL 模块级缓存**（`device_indicator.py`）
   - 新增 `_win32_dll(name)` / `_WIN32_DLLS = {}`
   - `_load_setupapi` / `_load_winmm` 改走缓存，避免 1Hz 轮询下每秒重复 LoadLibrary
   - 解决潜在的反病毒软件告警（重复加载敏感 DLL 可能被误报）

2. **智能降频**（`IndicatorMonitor`）
   - 新参数：`active_interval`（默认 1.0s）/ `idle_interval`（默认 5.0s）/ `enable_idle_slowdown`（默认 True）
   - 规则：连续 2 次空闲采样后切到 `idle_interval`；状态变化立刻回到 `active_interval`
   - **首次变化延迟仍是 1s 以内**（不是 5s）
   - 未知状态 `(None, None)` 期间绝不进入降频（探测异常需尽快恢复）
   - 旧参数 `interval=` 仍可用，等同于 `active_interval`

3. **持续未知告警**（`IndicatorMonitor`）
   - 新参数：`unknown_streak_threshold`（默认 3）/ `on_unknown_streak`（可选回调）
   - 触发：连续 N 次 `(None, None)` 后调 `on_unknown_streak(N)` 一次（防 spam）
   - 复位：得到真实状态时调 `on_unknown_streak(0)` 通知上层

4. **ucopy.py 接线**
   - `DEFAULT_CONFIG` 新增三个键：`indicator_slowdown` / `indicator_active_interval` / `indicator_idle_interval`
   - `get_config()` 做类型归一化（旧配置可能把 bool/数值写成字符串）
   - `start_indicator()` 读 cfg 注入 IndicatorMonitor + on_unknown_streak 回调
   - 新方法 `UCopyApp._on_indicator_unknown_streak(n)`：n=0 跳过；n≥1 写 logger.warning

5. **README** 小圆点颜色含义段补两行说明（降频 + 持续未知告警行为）

### 测试覆盖

- `python3 device_indicator.py --selftest` → **48/48 项断言通过**
  - 原 22 项（状态机 / 颜色映射 / 线程生命周期 / 非 Windows 兼容）
  - 新增 26 项：DLL 缓存（5）/ 旧 interval 向后兼容（3）/ 智能降频（7）/ 持续未知告警（5）/ 未知与降频隔离（2）/ Event.wait monkey-patch（4）
- `python3 .selftest.py` → **全部通过**
  - 第 10 节「摄像头/麦克风指示灯接线」新增 14 项：节能降频（7）/ UCopyApp 配置接线（4）/ 未知 streak 日志格式（3）
  - 原有所有断言（51+ 项）继续通过，未回退任何行为

### 关键设计取舍

| 取舍 | 决定 | 理由 |
|---|---|---|
| 首次空闲采样是否计入"连续空闲" | 否 | 初始 `_state = STATE_IDLE`，首次空闲采样不等于"持续空闲"，不应立刻切降频 |
| 未知状态是否降频 | 否 | 探测异常时需尽快恢复检测，把间隔拉长会让故障窗口更久 |
| 未知告警 spam 控制 | 模块内只触发一次 / 上层每次记录 | 模块防 spam 防 logger 刷屏；上层每次记录便于按时间线追踪多次故障 |
| `interval` 旧参数处理 | 等同于 `active_interval` | 旧调用方（`UCopyApp.start_indicator` 之前传 `interval=1.0`）零改动也能用新逻辑 |
| 是否改 settings UI 加降频开关 | 否 | 配置文件手改即可，UI 改动成本远大于收益；README 写明键名 |

### 兼容性

- `IndicatorMonitor(interval=...)` 旧 API 完全兼容
- `~/.ucopy_settings.json` 缺三个新键时 `get_config()` 自动回填默认值
- 历史 UCopy.exe 数据文件（设备列表 / 密码密保）零迁移

### 未在本版做的事

- **虚拟显示器 / 扬声器输出 / 屏幕录制 / 地理位置** —— Windows 无对应 API（已在开发对话中确认），强行做会违反「宁可漏检不误亮灯」的保守策略
- **列出占用进程名** —— 需要 QueryDeviceRelations + 进程枚举，越界且不同驱动拿不到
- **Windows 系统托盘通知** —— 引入 `pywinrt` 等依赖会破「无新增第三方依赖」原则

### 相关 commit

- `f342059` perf: 摄像头/麦克风检测 DLL 缓存 + 空闲降频 + 持续未知告警
- 基础指示灯功能来自 `38aa144` feat: 摄像头/麦克风占用指示灯（绿/橙/紫），覆盖虚拟设备
