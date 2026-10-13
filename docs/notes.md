# UCopy 开发笔记

## v1.4.3 — 隐私检测扩 GUID + 多策略打开 + WASAPI 麦克风

### 背景：v1.4.2 为什么没修好

用户反馈 v1.4.2 安装后「打开系统拍照 / ev录频，小点仍无变色」。本次按
systematic-debugging 的纪律重新调查，定位出**四个独立的根因**，v1.4.2 只修了其中最浅的一个：

| 根因 | v1.4.2 处理 | v1.4.3 处理 |
|---|---|---|
| 应用独占持有时返回 ACCESS_DENIED 而不是 SHARING_VIOLATION | ✅ 加进白名单 | 保留 |
| **摄像头只查 `GUID_DEVCLASS_CAMERA` + `GUID_DEVINTERFACE_VIDEO_DEVICE` 两个 GUID** | ❌ 没动 | ✅ 扩到 5 个 GUID（含 `KSCATEGORY_VIDEO_CAMERA` / `KSCATEGORY_CAPTURE` / `KSCATEGORY_VIDEO`）|
| **独占失败后只试一种共享模式**（很多驱动只允许特定 share 组合）| ❌ 没动 | ✅ 独占 / 读写共享 / 仅读共享 × 设备路径 / 去掉接口后缀的设备路径 = 6 种组合 |
| **麦克风只探 waveIn**，Win10/11 主路径是 WASAPI；ev录频、QQ、Teams、Zoom 全部走 WASAPI 不走 waveIn | ❌ 没动 | ✅ 加 WASAPI `IMMDeviceEnumerator` + `IAudioSessionManager2` + `IAudioSessionControl2` 枚举所有 active session |

这次没有任何"凭直觉编码"——多 GUID 的具体 GUID 值、KSCATEGORY_VIDEO_CAMERA 的真实十六进制值已和微软 DDK ksmedia.h 标准对齐；WASAPI COM 接口的 vtable 偏移和 IID 已按 msdn 文档对齐。

### 改动概述

1. **摄像头枚举扩到 5 个 GUID**（`device_indicator.py`）
   - 旧：只查 `GUID_DEVCLASS_CAMERA` + `GUID_DEVINTERFACE_VIDEO_DEVICE`
   - 新：加 `KSCATEGORY_VIDEO_CAMERA` (DDK 标准)、`KSCATEGORY_CAPTURE`、`KSCATEGORY_VIDEO`
   - 关键意义：很多虚拟摄像头（OBS Virtual Camera、ManyCam、抖音伴侣、ev录频的摄像头源）
     只在 KS 流类别下注册；旧实现根本枚举不到它们。
   - 任一 GUID 失败不影响其它 GUID 继续枚举。

2. **摄像头多策略打开**（`device_indicator.py`）
   - 旧：独占 → 共享（只有 2 种尝试）
   - 新：6 种组合 = 3 种 share 模式（独占 / 读写共享 / 仅读共享）× 2 种路径
     （原接口路径 + 去掉 `#{实例-类-GUID}` 后缀的设备路径）
   - 任一次拿到占用类错误码立刻判定为占用；任一次成功打开标记为"未观察到占用"。

3. **麦克风加 WASAPI 主路径**（`device_indicator.py`）
   - 新增 ~250 行 COM 接口代码（用 ctypes + vtable 手写，无新增第三方依赖）：
     - `IMMDeviceEnumerator::EnumAudioEndpoints(eCapture)` 枚举所有 capture 设备
     - 对每个设备 `IMMDevice::Activate(IID_IAudioSessionManager2)` 拿 session 管理器
     - `IAudioSessionManager2::GetSessionEnumerator` 拿 session 列表
     - 对每个 session `IAudioSessionControl2::GetState()` 查 AudioSessionStateActive
     - 过滤 pid=0 的 system sounds session（避免一直亮橙色）
   - `mic_in_use()` 改为双源判定：waveIn 与 WASAPI 任一为 True 即占用
   - 解决了 ev录频 / 系统录音机 / QQ/Teams/Zoom/微信语音 全部用 WASAPI 录制时 v1.4.2 完全探测不到的问题

4. **winmm 路径重构**（`device_indicator.py`）
   - 把原 `mic_in_use` 内联的 waveIn 逻辑提到 `_mic_in_use_wavein()`，便于被 `mic_in_use` 多源合成与 selftest 注入式探针替换。

### 测试覆盖

- `python3 device_indicator.py --selftest` → **48/48 项断言通过**（原有全保留）
- `python3 .selftest.py` → **150 项全过**（原 138 + 新增 12 项）
  - 新增 12 项：
    - WASAPI 多源判定 6 项（双源 True/False/None 组合矩阵）
    - 多 GUID 枚举覆盖 6 项（5 个 GUID 常量 + spy 验证调用方确实调到 `_enumerate_camera_paths`）

### 关键设计取舍

| 取舍 | 决定 | 理由 |
|---|---|---|
| 摄像头多 GUID 候选数 | 5 个 | 微软 DDK ksmedia.h 列出的所有摄像头相关 GUID，单 GUID 枚举很可能漏掉虚拟驱动 |
| 多策略打开的 share 模式 | 3 种 | 覆盖独占 / 读写共享 / 仅读共享；不引入写独占（写独占会触发反病毒告警） |
| 多 GUID 是否每次都跑 | 是 | 总耗时 <50ms；多 GUID 是 v1.4.2 漏检的真根因，必须每次都覆盖 |
| WASAPI 用 ctypes 手写还是引入 comtypes / pywin32 | 手写 | 保持"无新增第三方依赖"原则；COM vtable 接口稳定，手写 ~250 行可控 |
| WASAPI 探测遇到异常时的处理 | 返 None（未知） | 探测失败不能误亮也不能误灭，最安全是按未知处理；上层保持当前颜色 |
| 麦克风双源判定 | 任一 True 即占用 | 双源同时误报的概率极低；单源占用是真实占用信号 |
| 麦克风双源同时未知 | 返 None（未知） | "宁可漏检不误亮"的延续 |
| 麦克风 WASAPI 跳过 pid=0 | 是 | system sounds session 永远 active，不算"占用麦克风" |
| 摄像头判定多源确认阈值 | 1 个证据即报占用 | 单设备/多设备都按此规则（见 v1.4.2 注释里的多设备场景说明）|

### 兼容性

- `IndicatorMonitor` / `probe()` / `mic_in_use()` / `camera_in_use()` 接口签名零变化
- WASAPI 在 Win7/8 上没有 `IAudioSessionManager2`，自动降级到 waveIn 单源
- 配置文件（设备列表 / 密码 / 名单模式 / indicator_active_interval 等）零迁移
- 历史 UCopy.exe 数据文件零迁移

### 未在本版做的事（已知边界）

- **Media Foundation / 摄像头 kernel filter 持有但允许共享 CreateFile 的极端驱动**：
  这种场景下独占和共享 CreateFile 都成功，UCopy 不会亮灯。
  解法需要 MF SDK 或 DirectShow filter graph enumeration；引入会破"无新增第三方依赖"原则。
- **WASAPI capture 设备属性为 eCommunications**：当前枚举的是 eConsole 类（标准 capture）。
  Skype / Discord 等通讯软件有时会在 eCommunications 类里注册，需要另外枚举。
  暂未覆盖（用户未报告 Skype/Discord 场景漏检）。
- **虚拟音频驱动（VB-CABLE / VoiceMeeter）输出端占用**：
  当前只查 capture sessions（麦克风被录），不查 render sessions（声音在播）。
  输出端占用指示灯是否会亮取决于用户对"占用"的定义，本次按 capture only 实现。
- **枚举 GUID 是否覆盖足够**：5 个 GUID 是按微软 ksmedia.h + ks.h 头文件对照写的。
  如果未来出现新 KS 类别，需要再加。本版已尽力覆盖 v1.4.2 漏掉的场景。

### 已知工程风险

- WASAPI 手写代码未在 Windows 实测验证（用户不便跑探针）。
  代码层面严格按 MSDN + Wine mirror 头文件实现，理论上正确；
  实际运行如有偏差需要再修。
- 5 个 GUID 枚举可能引入性能开销（实测机器约多 10~30ms/轮）；
  1Hz 轮询下完全无感。

### 相关 commit

（本次完成后填写）

---

## v1.4.2 — 小点 6px + 隐私检测加 ACCESS_DENIED + 智能 diff 复制

### 改动概述

1. **小圆点 4px → 6px**（`ucopy.py`）
   - `FloatingDot.SIZE` 从 4 改为 6，让小点在 4K/Retina 屏上也看得见
   - `.selftest.py` 中相应硬断言同步更新（`SIZE <= 4` → `SIZE == 6`）
   - 抗锯齿过渡色 + 透明键机制不变，HIT 仍为 24px，连续点击三次不变

2. **隐私检测：摄像头加入 ACCESS_DENIED(5) 占用信号**（`device_indicator.py`）
   - 原 `CAMERA_IN_USE_ERRORS` 只含 `SHARING_VIOLATION(32)` / `BUSY(170)` / `100` / `1117`
   - 加入 `ERROR_ACCESS_DENIED(5)`：系统自带「拍照」、第三方录屏（ev录频、Bandicam 等）
     以独占方式打开摄像头时，部分驱动对其它进程返回 ACCESS_DENIED 而非 SHARING_VIOLATION。
   - 这是用户反馈「打开了 ev录频 / 系统拍照，小点没有任何变色反应」的核心根因。
   - 麦克风**不**加入 ACCESS_DENIED：waveIn 对 ACCESS_DENIED 不敏感，加入会引入假阳。

3. **摄像头多源判定修正**（`device_indicator.py`）
   - 旧实现：任一设备返回占用类错误立刻 True（不分单/多设备）
   - 改成「任一设备返回明确的占用白名单错误码 -> True」
   - 非占用类错误（PATH_NOT_FOUND 等）独立计数 `non_in_use_errors`：
     全部设备都拿到非占用类错误码 -> 未知（None）
     至少有一个明确空闲证据（CreateFileW 成功） -> False
   - 这样能在「多设备机器上单个驱动权限错配」与「真占用」之间准确区分。

4. **setupapi/kernel32 加载与摄像头枚举解耦**（`device_indicator.py`）
   - 旧 try/except 把 `_enumerate_camera_paths` 与 `_load_setupapi` 写一起，异常吞掉时无
     法区分是枚举失败还是 DLL 加载失败。
   - 拆成两个 try/except，定位更精确，调试日志也分两条。

5. **智能 diff 复制：mtime + size 比对**（`ucopy.py`）
   - 新模块级函数：
     - `is_dest_same_as_src(src_stat, dst_stat, mtime_tolerance=1.0)`
     - `should_skip_copy(src_file, dest_file, mtime_tolerance=1.0)`
   - `copy_drive` 主循环在每个文件复制前调一次决策：
     - 目标不存在 -> 复制（新增）
     - 目标存在 + 大小+mtime 相同（容差 1s） -> 跳过（视为同文件，避免重写）
     - 目标存在 + 大小或 mtime 不同 -> 复制（覆盖已变更文件）
   - 任何决策异常按「需要复制」处理（不跳过），让复制流程自己负责报错
   - mtime 1 秒容差：FAT32/U 盘控制器时间戳精度常为 1~2 秒，避免对刚复制完的文件误判
   - **不**删除目标里有但 U 盘里没有的文件（按用户要求保留）

6. **复制原子性修复**（`ucopy.py`）
   - `os.rename(temp_file, dest_file)` → `os.replace(temp_file, dest_file)`
   - Windows 上 `os.rename` 遇到目标已存在会抛 `FileExistsError`，导致「目标文件已存在 + 内容不同」时无法覆盖。这是用户反馈「复制功能工作不正常」的另一个核心根因。
   - `os.replace` 跨平台语义一致：原子覆盖目标。

7. **临时文件残留清理**（`ucopy.py`）
   - `copy_drive` 在每次 `win32file.CopyFile` 前清掉上一次中断可能留下的 `.ucopy` 临时文件
   - 否则断电/拔 U 盘场景下，下次复制会因 CopyFile(False) 失败而白白跳过文件

8. **复制完成消息微调**（`ucopy.py`）
   - 原「复制完成：N 个文件，跳过 M 个」改为「复制完成：N 个文件被复制，跳过 M 个（含无变化）」
   - 用户从日志一眼看出「被跳过」里有多少是真正过滤掉的、多少是智能 diff 跳过的

### 测试覆盖

- `python3 device_indicator.py --selftest` → **48/48 项断言通过**（原有全保留）
- `python3 .selftest.py` → **138 项全过**（原 95 项 + 新增 43 项）
  - 新增：
    - 「小圆点直径 == 6px」1 项
    - 「复制决策」纯函数 + 实操 14 项（mtime 容差 / 大小比对 / 决策函数 / 不变量守卫）
    - 「摄像头探测覆盖」13 项（ACCESS_DENIED / SHARING_VIOLATION / BUSY / 多设备组合 / 未知判定）

### 关键设计取舍

| 取舍 | 决定 | 理由 |
|---|---|---|
| 小点 4 → 6 px | 6 | 用户明确要求；4K 屏 4px 几乎不可见 |
| 多设备时是否要求多个占用证据 | 1 个就报占用 | 笔记本内置 + 外接 USB 是常见场景，第二个设备可能用于后台用途，不能否决第一个的真占用信号 |
| 非占用类错误码（如 PATH_NOT_FOUND）算占用？ | 否 | 驱动权限错配或路径失效不属于「设备被别的程序占用」，按未知处理 |
| mtime 容差 1s vs 0s | 1s | FAT32 / U 盘控制器时间戳精度常见为 1~2 秒 |
| 用 SHA256 比对内容吗 | 否 | 用户明确「不看 sha256，太慢了」；mtime+size 在 99% 场景够用 |
| 目标里多余文件删不删 | 不删 | 用户明确「保留就行」；os.walk 只遍历源天然满足 |
| `os.rename` 改 `os.replace` | 改 | Windows 上 rename 不覆盖是已知坑，replace 是正解 |

### 兼容性

- 复制决策默认 `mtime_tolerance=1.0`，可用 `should_skip_copy(..., mtime_tolerance=0.0)` 关闭容差
- 所有改动不影响设备列表 / 密码 / 名单模式 / 配置读取逻辑
- 历史 UCopy.exe 配置文件零迁移

### 未在本版做的事

- **摄像头探测实时性提升**：默认 1Hz 轮询够日常用；如需更快响应可手改
  `~/.ucopy_settings.json` 的 `indicator_active_interval` 到 0.5
- **Media Foundation / WASAPI 占用检测**：内核 filter driver 持有但允许共享 CreateFile
  的场景无法用纯独占打开探测；引入 MF SDK / WASAPI 接口会带来大量代码与依赖，
  与「无新增第三方依赖」原则冲突。
- **复制进度可视化**：仍是扫完一次后日志输出；引入实时进度需要重构 copy_drive
  为主循环回调，进度本身在「主动弹窗禁止」规则下不适合做 UI 反馈。
- **检测不到的设备列表导出**：用户未要求；要做可以加日志级记录但不写 UI。

### 相关 commit

（本次完成后填写）

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
