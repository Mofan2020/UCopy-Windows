# UCopy 开发笔记

## v1.7.5 — 关掉 WAL + watchdog 改 SIGKILL

### 根因线索（仍未到 sqlite3 源码级确诊）

long-running server 里 server 进程的 db handle 与磁盘 db 脱钩：API 返回
200 但 db 表里 0 行，并抛 `sqlite3.OperationalError: disk I/O error`。

两个稳定的观测点：
1. `lsof` 显示进程持有多个指向**不同 inode** 的 `server.db-wal`，其中若干
   已 unlinked 但 fd 未释放。
2. `diskutil verifyVolume /` 通过，`PRAGMA integrity_check` 返回 ok ——
   主库文件本身没坏，坏的是 WAL 与主库之间的同步关系。

复现实验排除了 PyInstaller：源码直跑启动后 60–120s 同样复现。所以这不是
打包产物的问题，是 SQLite 在 macOS 上长 fd 持有场景下的行为。

### 修复

**A. 关闭 WAL**

`open_db()` 从 `journal_mode=WAL` 改为 `journal_mode=TRUNCATE`，并加
`synchronous=FULL` + `busy_timeout=5000`。

理由：整条故障路径都指向 WAL 文件的 unlink + inode 重建。TRUNCATE 模式
每次事务结束把 journal 截断复用，不产生独立的 `-wal` / `-shm` 文件，也就
没有 WAL inode 被替换的问题。代价是读写不能完全并发；UCopy Server 是
单用户低频写入，代价可忽略。

`synchronous=FULL` 让每次提交都 fsync，避免进程被 kill 时丢掉已返回成功的
写入 —— 这一点在原 WAL + NORMAL 配置下是做不到的。

另外补了三处健壮性：

- 启动时删掉历史残留的 `-wal` / `-shm`。切到 TRUNCATE 后 SQLite 自己不再
  写这两个文件，但旧文件留在盘上会被当成有效数据合并回主库。
- 切 `journal_mode` 需要库级排它锁，`busy_timeout` 只管数据锁，所以显式
  重试最多 10s 再放弃（Web GUI 或残留进程同时开着 db 时会撞到）。
- `busy_timeout` 提升到 5s。

**B. watchdog 改 SIGKILL**

v1.7.3 的 watchdog 检测到三次连续失败后发 SIGTERM，走
`_shutdown()` → `ThreadingHTTPServer.shutdown()`。而 `shutdown()` 会阻塞等
`serve_forever` 循环退出；主线程此时就在 `serve_forever` 里，进程实际不
退出，fd 不释放，故障持续。

改用 `SIGKILL`：进程立即消失，内核回收所有 fd，靠 launchd `KeepAlive` 或
外部 supervisor 拉起新实例。同时把原先只累加 `fail_streak` 不动手的异常
分支也接上 SIGKILL —— 抛 `OperationalError` 和"RO 读不到 marker"是同一类
故障，不该区别对待。

watchdog 本身保留：它不修根因，但能在故障时快速止损，且现在真的会自杀。

### 验证

- `.selftest.py` 新增 13 项断言，含真实 sqlite3 行为验证（不是字符串匹配）：
  跑一次 `open_db()` 确认磁盘上不产生 `-wal` / `-shm`、写入立即被独立连接
  读到；再造一个 WAL 模式的 db 迁移到 TRUNCATE，确认 5 行数据不丢不重复、
  残留文件被清掉。
- 15 分钟压力测试：每 2 秒经 API 写一条 token + 一次配置写入，每次都立刻
  用独立 `sqlite3` 进程确认磁盘可见。0 次失败。
- 对照：同样时长下旧 WAL 版本在 60–120s 内就开始丢写。

### 已知未修

- sqlite3 在 macOS 长 fd 持有下的行为本身没查到底。若 TRUNCATE 也扛不住，
  下一步是换存储（Postgres / Turso），不是继续调 PRAGMA。
- 生产数据目录里现有 17 个测试 token 是调试残留，需要清掉。

## v1.7.4 — 现状记录 + 临时恢复工具（不声称修复）

### 事实（2026-10-20 22:30 ~ 23:50 复盘）

v1.7.3 加了 db handle watchdog 想"自动修"long-running server 出现的"server 内存有 key
但磁盘 db 文件里没记录"问题。**这次复盘证明 v1.7.3 没修这个问题**：

- 22:32:27 重启 server（v1.7.3 binary + watchdog 线程），login/POST /keys 立刻 200
- 22:35:21 admin 创建 key #3 REGRESSION_FINAL，server 返回 200，
  GET /api/v1/keys 也能看到 3 个
- 22:35:21 同一时刻，`sqlite3 server.db "SELECT * FROM api_keys"` 只看到 2 个（id=1, 2）
- lsof 显示 server 进程持有 **6 个指向不同 inode 的 server.db-wal 句柄**，
  其中 4 个已经 unlinked 但 fd 没释放
- 22:35:27 watchdog 抛 `OperationalError: disk I/O error`（捕获到未处理）

**结论**：v1.7.3 的 watchdog 捕获到了事故，但**当前设计无法自动恢复**——
SIGTERM 后 ThreadingHTTPServer.shutdown() 在主线程阻塞，daemon 线程不
真退出，server 还是不释放 fd，重启还是不会真发生。

### 真正暴露的事实
- long-running server 进磁盘 db 写入断流的 bug **仍在**
- 重启只是临时恢复手段，每次重启后大约 1-3 分钟内 bug 再现
- root cause 仍 100% 未确诊（高度怀疑 macOS APFS + PyInstaller onefile 抽 _MEI
  目录的 sqlite3 dylib + 长 fd 持有 的某些交互）
- v1.7.3 release assets 已经在用户手里，但 v1.7.3 binary 的"defensive fix"
  实际只在 server.log 里多了一段早期 warning，**没真正恢复持久化**

### 改动概述
1. **`docs/notes.md`**：明确 v1.7.3 = "早期检测器，未修问题"
2. **`restart-server.sh`**：临时恢复工具（不依赖 launchd 服务）——
   - 检测 server 进程是否在跑、能否响应 API
   - 如果 db 写入不可见（OperationalError），自动 `kill -TERM` 优雅停，
     等 10s → `kill -9`
   - 重新 launch detach 的 server
   - 在 `dist/UCopy-Server-macos-arm64` + `~/UCopyServer/` 路径假设下可用
3. **加 selftest 覆盖**：检查 restart 工具至少能 dry-run 通过检查。
4. **回退 v1.7.3 的 db.py 注释修改**：发现 v1.7.3 之前的 db.py 注释也合理

### 不动的边界
- **v1.7.3 binary 不撤回**：release 已经被下载；但 release notes 标成
  "experiment / early-detector"，commit aaae8ea 不变
- **不动 server.py / db.py**：任何"重启后 +1 分钟又能用"的 server 代码
  改动都是猜测性 fix，未在确诊 root cause 之前引入
- **不动 launchd plist**：launchd 服务在 v1.7.3 binary 路径之前，用户决定是否替换

### 当前可用状态（2026-10-20 22:48）
- production server (pid 85243) 在跑，监听 127.0.0.1:8443 (`--disable-https --detach`)
- 当前 GET /api/v1/ping = 200 + login 200 + GET keys 200
- bug 已再次发作：server 内存 3 keys，磁盘 db 实际只有 2 行
- 用户需在使用过程中观察；如再发生 disk I/O error，按 `restart-server.sh`
  临时恢复

### 关键 commit hash
- commit 3d88185 已 force-push amended; GitHub 上 main 的 hash 是 3d88185.
  notes.md 之前写的 1f8633a 是同 commit 的较早 hash (amend 改了 SHA).

## v1.7.3 — server 端 db handle watchdog（**未修问题，仅早期检测器**）

### 症状
- 2026-10-20 用户报告：服务器长时间跑后，「创建 token」返回 200，
  GET /api/v1/keys 也能看到，但 **SQLite 数据库里没有这条记录**。
  重启 server 后写入能恢复一段时间（约 1-3 分钟），但 bug 会再次发生。

### 排查关键观察
- GET /api/v1/keys（server 内存）= N 条
- 直接 `sqlite3 server.db "SELECT * FROM api_keys"` = 0 条
- 服务器进程的 fd 显示打开了 db + WAL（lsof 看到几 MB 的 WAL）
- 但 OS 层 ls -la 看 server.db-wal = 0 字节
- server.db mtime 间歇性更新，重启后 1-3 分钟内 mtime 不再前进
- lsof 显示 server 持有多个指向不同 inode 的 server.db-wal 句柄，
  部分 inode 已被 unlink 但 fd 未释放
- 同 db 文件外部 sqlite3 RO 读 OK；server 进程自己读 OperationalError

### 根因（仍未确诊）
**最可能假设**：macOS APFS + PyInstaller onefile 抽 _MEI 目录的 sqlite3 dylib
+ 长 fd 持有，在某些时序下 server 进程的 sqlite3 handle 跟磁盘 db 脱钩。

代码层未发现 bug —— 隔离环境（同步 sqlite3）行为正常，问题只在 PyInstaller
打包的 server 内部长跑后出现。

### 改动概述（实事求是的版本）
1. **加了 db handle watchdog**：30s 一次，独立连接测一次"写可见性"。
   失败时写 WARNING 日志 + 连续 3 次发 SIGTERM（**无法在生产环境优雅退出**，
   见 v1.7.4 复盘）。
2. 重打包 server binary

### 不动的边界
- launchd plist 不改：v1.7.3 binary 没真正解决问题，重启仍可能再次发作
- db.py 没改：未确诊 root cause 之前任何 PRAGMA 调整都是猜测

### 验证（实事求是的版本）
- 隔离环境（短跑 5 分钟）写入正常
- **生产 server 重启后 1-3 分钟内** bug 仍会复发（见 v1.7.4）

### 关键 commit hash
- `aaae8ea` — feat(server v1.7.3): db handle watchdog +事故复盘文档

## v1.7.2 — 远程模式 HTTP 协议适配（fingerprint 探测跳过）

### 症状
- v1.7.1 引入 HTTP 明文模式后，**客户端「测试连接」仍然报错「无法获取服务器
  指纹（地址错？HTTPS 端口错？）」**，虽然服务器 HTTP 端是通的
  （curl / 浏览器 200 OK）。这是因为 `test_remote` 调用了
  `RemoteClient.get_current_fingerprint()` —— 即便服务器是明文 HTTP，
  该方法仍试图用一个 SSL ctx 发 HTTPS 探测包，永远拿不到证书，返回 None，
  触发报错弹窗。

### 改动概述
1. **`RemoteClient.get_current_fingerprint()`（`remote_client.py`）**：当
   `self.is_http == True` 时**直接返回 `None`**，不发任何网络探测。HTTP 协议
   没有证书 —— 没有「指纹」概念。
2. **`SettingsWindow.test_remote`（`ucopy.py`）**：HTTP 明文模式下直接跳过
   整段 cert 探测与「信任此服务器？」弹窗，走 `login_with_key` + `me()` 流程。
   HTTP 路径写回 cfg 时 `trusted_fingerprint = ""`（与默认一致）。
3. **`RemoteClient.upload_file`（`remote_client.py`）**：原本无论 scheme 都
   走 `http.client.HTTPSConnection`。HTTP 路径会触发 TLS handshake 失败。
   改为按 `self.is_http` 分支：HTTP → `http.client.HTTPConnection`
   （端口默认 80、无 SSL ctx、跳过 `_pin_after_connect`）；HTTPS → 原路径。
4. **`.selftest.py`**：新增 v1.7.2 测试 8 条 —— `get_current_fingerprint` 对 HTTP
   返回 None、`test_remote` 走 `if not is_http_url:` 条件分支、`upload_file` 导入
   `HTTPConnection` 且按 scheme 选默认端口。

### 不动的边界
- v1.7.1 已加的 HTTP 明文警告弹窗保留 —— 测试连接 / 启用前仍弹一次。
- HTTPS 路径不变：自签 cert trust flow + fingerprint pin 逻辑全部保留。
- `TrustedServerStore` 不清空 —— 已存的旧 HTTPS 指纹保留；HTTP 模式下不读也不写。

### 关键 commit hash
- `dfa240f` — fix(remote): HTTP 明文模式跳过证书/指纹探测（test_remote + upload_file）

### 待办（用户 2026-10-20 反馈，未做改动，仅记录）
- **多 token / 多客户端文件区分**：当前 `file_index` 不存 `source_client_id` /
  `key_id`，`storage` 路径只到 `<data_dir>/files/<username>/<rel_path>`。
  后果：同一 U 盘在两台客户端上传 → `rel_path` 撞 → 后上传覆盖前者；
  list API 返回 `name / type / size / mtime / sha256`，无上传来源元数据；
  多 token 用户之间互相看不到对方上传记录。
- **config 多版本**：v1.5.0 设计就没存历史版本，`put_user_config` 只覆写
  `config_json` + `version`（单调递增但不 serve）+ `last_modified`，无法回溯。
  这两项今天暂不动，等用户提优先级时再议。
- **U 盘坏文件弹窗**：用户测 10 个 U 盘只 1 个复现，影响小，决定先观察不修。

## v1.7.1 — 自适应 UI + HTTP 明文模式警告 + 多客户端识别

### 改动概述

#### 1. 设置窗口自适应（小屏 / 高 DPI 完整显示）

- **症状**：v1.7.0 之前的 SettingsWindow 固定尺寸 (`resizable(False, False)`)、固定列宽
  (`Entry(width=45)` / `Listbox(width=60)` / `wraplength=380`)，在低分辨率（1024×768）
  或 Windows DPI 缩放（150% / 200%）下，内容超出窗口右边界或底部被截断。
- **修法**（`ucopy.py::SettingsWindow`）：
  - 外层 `Canvas + vertical Scrollbar`，内容超长时滚动显示
  - `self.win.resizable(True, True)` + `self.win.minsize(480, 420)` —— 可拖拽、最小尺寸
  - 列权重 `columnconfigure(1, weight=1)`，`Entry` 用 `sticky="ew"` —— 横向拉伸
  - 远程状态 Label 用 `bind("<Configure>")` 跟随窗口宽度调整 `wraplength`
  - 鼠标滚轮在 canvas 上自动滚动（仅内容超出时生效）
- **不动的边界**：`FloatingDot` / `LoginWindow` / `RemoteConsentWindow` 等其它窗口不动
  （用户没有反馈，且它们尺寸本身就小）

#### 2. HTTP 明文模式安全警告

- **场景**：v1.7.0 服务端新增 `--disable-https`（CLI + Web GUI 开关），让用户在内网
  部署时可以走明文 HTTP。客户端之前 **强制 https** —— 用户改服务端 HTTP 后，客户端
  一填 URL 就抛 `ValueError("远程模式仅支持 https")`，连不上。
- **设计选择**：用户原话「支持 http 服务器，但是弹出一个安全性警告，不完全阻止用户
  使用 http 服务器」。落地形态：
  - `normalize_server_address` 接受 `http://`（其它非法协议仍拒绝：ftp/ws/file）
  - `RemoteClient.scheme` / `is_http` 属性，UI 层用 `is_http` 决定是否弹警告
  - `_build_opener`：HTTP 走 urllib HTTPHandler（明文，不做证书校验）；HTTPS 走原
    fingerprint pin 路径
  - `_http_insecure_warning(parent, url)`：首次启用 / 测试连接时弹模态警告，含「不再
    提示此服务器」checkbox。已确认过的 URL 不重复弹。
  - 记忆文件：`~/.ucopy_http_acknowledged.json`，按 server_url 维度持久化（与
    `~/.ucopy_trusted_servers.json` 独立，互不影响）
  - 启用路径检查顺序：HTTP 警告 → RemoteConsentWindow 三项 → 写 cfg
- **不动的边界**：服务端没改 —— 用户保留启用 HTTP 的能力，客户端不强制 HTTPS 但要警告

#### 3. 多客户端 / 多 U 盘识别

- **场景**：v1.6.0 引入 `client_id = hostname`，服务器端用 client_id 做独占锁。但同
  hostname 的多台机器（如两台都叫 "DESKTOP-ABC123" 的机器）会互踢独占锁。
- **修法**（`ucopy.py::_default_client_id`）：
  - 默认值改为 `{hostname}-{6hex random}`，每次启动随机后缀不同
  - `SettingsWindow._build_remote_frame` 默认值改用 `_default_client_id()`
  - 已保存到 cfg 的 `client_name` 不变（用户主动改的保留）
- **多 U 盘区分**：`get_drive_info` 已有 GUID 优先 + serial+label fallback，不变。
  已覆盖 FAT32 / exFAT / NTFS 各文件系统唯一标识。
- **不动的边界**：`client_id` 没扩展成「每进程 / 每用户独立」——服务器端独占锁的语义
  不变（同 key 同时只服务一个 client_id）。

### 兼容性

- 旧配置（`client_name` 仍是 hostname）继续工作：默认值用 `_default_client_id()`，
  cfg 里有值就用 cfg 里的值，不主动覆盖用户设置
- `~/.ucopy_http_acknowledged.json` 是新文件，不存在就当全部未确认
- `normalize_server_address` 接受 `http://` 是行为变更：之前会抛错，现在接受
  + UI 弹警告。已存在的 https URL 行为完全不变。

### 测试覆盖（282+ 项全绿）

- `.selftest.py` 第 12 节远程模式测试扩充：
  - `normalize_server_address` 接受 http 多种格式（含 IPv6）
  - `normalize_server_address` 拒绝 ftp/ws/空
  - `RemoteClient.scheme / is_http` 属性正确
  - `TrustedServerStore` 与 HTTP 警告记忆文件独立持久化
  - `SettingsWindow` 源码层断言（`resizable(True, True)` / `minsize` /
    `columnconfigure` / `sticky="ew"` / 外层 Canvas + Scrollbar）
  - `_default_client_id` 两次调用结果不同（带随机后缀）
- `device_indicator.py --selftest` 48/48 项不受影响

### 关键 commit hash

- `03ed968` — feat(v1.7.1): 设置窗口自适应 + HTTP 明文模式警告 + 多客户端识别


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

---

## v1.5.0 — 远程模式 + 配套服务器

### 改动概述

1. **新增 `remote_client.py`**：纯标准库实现（urllib + ssl + http.cookiejar + hashlib）。
   - **不引入 requests**，UCopy.exe 体积不变。
   - HTTPS + TLS 1.2+ + 自签证书 fingerprint pinning。
   - `TrustedServerStore` 把 SHA-256(server cert DER) 持久化到 `~/.ucopy_trusted_servers.json`。
   - `sanitize_config_for_remote`：**双层剥除** `security.password_hash / password_salt / qa`，确保上传/拉取不会泄漏密码。
   - `normalize_server_address`：支持 `[ipv6]:port` / `ipv4:port` / `host:port`，拒绝 `http://` / `ftp://`。
   - `RemoteClient.upload_file` 走 `http.client.HTTPSConnection` 直连（不走 urllib 池）—— 因为 BaseHTTPRequestHandler + `Connection: close` + urllib keep-alive 池实测 30s+ 不返回响应（实测发现）。

2. **新增 `ucopy_server/`** 独立 Python 包（不跟 UCopy 主程序一起打包）：
   - 纯标准库 server（`http.server` + sqlite3 + `ssl`），不引第三方依赖。
   - `server.py` HTTPS + REST API（login / me / config / files CRUD / admin users）+ 静态文件服务 Web GUI。
   - `auth.py` PBKDF2-SHA256 × 200k 密码哈希 + sha256 session token（不存原密码）。
   - `db.py` SQLite (users / sessions / user_configs / file_index)，WAL 模式 + foreign_keys。
   - `storage.py` 路径校验（拒绝 `..` / 绝对路径 / 盘符），原子写入 `.tmp` + `os.replace`，配额检查。
   - `cert.py` 自动检测本机 IPv4/IPv6 地址写 SAN（首次启动调用 openssl 生成自签证书，7 天内续签）。
   - `launchd.py` macOS 自启动：写 `~/Library/LaunchAgents/com.skyc8266.ucopy-server.plist` + `launchctl load/unload`。Linux 上 no-op。
   - `config.py` 服务器配置（监听端口 / 数据目录 / 初始管理员）。
   - `static/index.html` 简单 Web GUI（登录 + 用户管理 + launchd 开关 + 文件浏览 + 配置 JSON 编辑）。

3. **macOS 服务器单文件打包**（PyInstaller `--onefile`）：
   - `build_macos.sh`：本地构建脚本，只构建 arm64（Apple Silicon）
   - `.github/workflows/build-server.yml`：CI 在 macos-14（arm64）上构建 + 产物上 Release
   - 产物名：`UCopy-Server-macos-arm64`（带执行权限）
   - **只支持 Apple Silicon（M 系列芯片）**，不再支持 Intel Mac。原因：Python 3.13 + PyInstaller 6.x 不支持在 arm64 机器上交叉构建 x86_64（`_struct.cpython-313-darwin.so` 是 arm64-only，会抛 `IncompatibleBinaryArchError`）；打 universal binary 需要 x86_64 版 Python 环境。Intel Mac 用户如需跑服务器，走源码 `python3 ucopy_server/server.py`。
   - 端到端验证：打包后二进制 9.5MB，跑 `--help` / 实跑 + curl `/api/v1/ping` / 与 remote_client 完整流程（login/push_config/upload/list/download/unchanged/delete）全部通过
   - **关键 fix**：模块内 `import db as _db` 等相对 import 在 PyInstaller onefile 下失败，改为 `from . import db as _db`（带 try 兜底兼容源码运行）

4. **`ucopy.py` 集成**：
   - `DEFAULT_CONFIG` 新增 `remote` 段（enabled / server_url / username / auto_upload / trusted_fingerprint / last_error / last_upload_at / last_config_sync_at）。
   - `get_config()` 加 remote 段类型归一化。
   - `RemoteUploader` 后台守护线程：从 queue 拿任务 → `RemoteClient` 上传。先 sha256 check 再 upload（服务端返回 200 / unchanged 短路）。
   - `copy_drive` 增加可选 `on_file_copied` 回调，每复制成功一个文件入队 → `remote_uploader.enqueue`。
   - `RemoteConsentWindow` 强制确认弹窗：**三项 checkbox 必须全部勾选**才能启用远程模式（满足用户硬性偏好）：
     1. 我仅将本工具用于合法用途，因滥用产生的全部责任由我本人承担
     2. 我没有受到欺骗或诱导而启用此模式
     3. 我已确认服务器地址正确，且服务器有足够空间存储上传的文件
   - `SettingsWindow` 加 "远程模式（v1.5.0+）" LabelFrame：服务器地址 / 用户名 / 密码（不存盘） / 启用 / 自动上传 / 证书指纹 / 状态 / 测试连接 / 拉取配置 / 推送配置 按钮。
   - 密码**绝不存盘**，每次 UI 输入后注入到 `UCopyApp._remote_password`（会话级）。
   - `UCopyApp.quit_app` 退出时 `remote_uploader.stop()` 干净停。

5. **mac 自启动管理（服务器侧）**：
   - 服务器首次启动在 macOS 上自动注册 launchd plist 到 `~/Library/LaunchAgents/com.skyc8266.ucopy-server.plist`。
   - Web GUI "launchd 自启动" 按钮可手动 install / uninstall。
   - Linux 上全 no-op。

6. **摄像头/麦克风隐私检测**：**未做改动**，保持 v1.4.3 实现。漏检场景（kernel filter driver / 三种 share_mode 全 SUCCESS）超出范围，按用户原话"修不了就算了"，写进已知边界。

### 测试覆盖

- `python3 device_indicator.py --selftest` → **48/48 项断言通过**（未变）
- `python3 .selftest.py` → **全部通过**（原 150 项 + 新增 30 项远程模式）
  - sanitize_config_for_remote（2 项）：security 顶层剥 / 非 security 块不剥
  - normalize_server_address（10 项）：合法格式 + 拒绝非法
  - TrustedServerStore（2 项）：trust/persist/case-insensitive/forget + 损坏文件容错
  - DEFAULT_CONFIG.remote 字段完整性（8 项）
  - RemoteConsentWindow 三项 checkbox（1 项）
  - RemoteUploader 类存在 + 关键方法（6 项）
- 端到端联调（手动）：源码版 server + client 全流程跑通
- **打包版端到端联调（手动）**：用 PyInstaller onefile 产出的 `dist/ucopy-server` 跑 + `remote_client.py` 客户端全流程再跑一次，全部通过
- CI smoke test：`.github/workflows/build-server.yml` 在 macos-14 跑 `--help` + 实跑 + `curl /api/v1/ping` 双重冒烟

### 关键设计取舍

| 取舍 | 决定 | 理由 |
|---|---|---|
| 客户端 transport 库 | 纯 stdlib (urllib) | 不引 requests → UCopy.exe 体积不变；延续"无新增第三方依赖"原则 |
| 客户端 HTTPS 验证 | self-signed + fingerprint pin | adhoc SSL + 用户首次确认后存盘指纹；避免 MITM |
| 双层密码脱敏 | 客户端 sanitize + 服务端 sanitize | 任一环节出错都不会泄漏 |
| 配置文件传输 | 客户端不存密码 → 服务端无密码可看 | 远程密码只活在本会话内存 |
| 启动 multipart 路径 | 直连 http.client.HTTPSConnection（不走 urllib 池） | 反复实测：urllib keep-alive 池在 server Connection: close 下 hang 30s+；直连每次新 TLS 0.01s |
| 上传 body 构造 | 一次性读 file + bytes 拼接 | 流式 + urllib 池化不稳定；一次性接受内存开销换稳定（一次复制几十文件，单文件 <100MB 完全可接受） |
| 配额模型 | 用户 quota_mb（默认 10GB，admin 0=不限） | 多用户 / 防滥用；admin 默认不限方便自托管 |
| 强制确认弹窗 | 三项 checkbox 全勾才能启用 | 用户硬性偏好；非用户主动进入设置时不弹窗（保留"主动弹窗禁止"原则） |
| 自启动 | macOS launchd + Linux no-op | 用户要求 macOS 专属功能；Windows 用户不需要（UCopy 自带注册表自启动） |
| 服务器摄像头指纹 | 仅存 SHA-256 DER hex | 非密码，可本地存；指纹变更要求重新信任 + 重输密码 |
| 服务器 GUI | 纯 HTML/JS 单文件 | 不引前端框架；Web GUI 在 static/index.html |
| **macOS 服务器打包** | **PyInstaller `--onefile`，仅 arm64（Apple Silicon）** | 单文件直接跑；Intel Mac 不再支持（PyInstaller 6.x + Python 3.13 无法交叉构建 x86_64）；CI 在 macos-14 单 arch 构建 |
| **macOS 打包相对 import 兼容** | **`from . import db` + try 兜底为 `import db`** | onefile 模式下 sys.path 不含当前目录；源码跑用相对 import 仍兼容 |

### 兼容性

- `~/.ucopy_settings.json` 缺 `remote` 段时 `get_config()` 自动回填默认值（与其它新字段同等处理）。
- 服务器配置 `~/UCopyServer/server.json` 缺 `initial_admin.password` 时首次启动随机生成并**打印到终端**（用户必须从终端看，不能从 Web GUI）。
- 服务器 `data_dir/files/<username>/<rel_path>`：admin / user / readonly 各有自己根目录。
- 历史 UCopy.exe 配置文件零迁移（remote 段可选填）。
- 打包服务器兼容 macOS 10.15+（PyInstaller 6.x 最低支持版本），**不兼容 Windows / Linux**（那是源代码运行的事）。

### 未在本版做的事（已知边界）

- **WebSocket 上传进度推送**：当前 upload progress 回调只在 client 内部，无 UI 进度条显示。可后续加。
- **客户端 sha256 缓存**：每次上传前都重算 sha256（~100MB/s，无 IO 瓶颈但 CPU 浪费）。可后续加 file_hash_cache。
- **服务端媒体预览**：Web GUI 文件列表不显示缩略图 / 视频预览（raw video / mp4 不在范围内）。
- **摄像头/麦克风检测改进**：kernel filter driver 漏检（pitfall #17）未处理，超出范围；按用户原话"修不了就算了，一切问题都怪微软！"。
- **v1.5.0 → v1.5.1 macOS 绑定 fix**：
  - **默认 `bind-host` 改回 `'::'`（IPv6 双栈）**，通过 `IPV6_V6ONLY=0` 让 `[::]` 通配同时接受 IPv4 mapped
  - macOS 上 `getaddrinfo('[::]', port)` 报 `Errno 8`（用户原报错路径），所以 `ThreadingHTTPServer.__init__` 重写：检测 `bind_host` 是 `[::]` 时先剥成裸 `::` 再喂给 `TCPServer.__init__`
  - `ThreadingHTTPServer.server_bind` 之前开 `IPV6_V6ONLY=0`
  - 绑定错误提示改成 5 行可读（含 v4-only、v6-only、双栈、具体地址场景）
  - 启动日志里 IPv6 hostname 自动加方括号
- **v1.5.2 Web UI 重写 + ReferenceError 修复**：
  - **根因**：原 `index.html` 用 `onclick="login()"` 等 inline event handler，调用 `async function login() { ... }`；在 Safari 严格模式 + 异步上下文里 inline handler 偶尔找不到 global async 函数，抛 `ReferenceError: Can't find variable: login`
  - **修法**：所有按钮改用 `addEventListener` 绑定；函数全部包在 IIFE `(function() { 'use strict'; ... })()` 里避免全局污染；通过 `data-*` 属性 + 事件代理处理动态生成元素的回调（用户列表 / 文件列表 / 面包屑）
  - **UI 美观（同步重做）**：
    - 配色：`#8B5CF6` 紫色主题（沿用 UCopy 浮点 + DeepSleep / TorSOCKS5 等项目风格），深色模式自适应 `prefers-color-scheme`
    - 卡片：14px 圆角 + `backdrop-filter: blur(20px)` 毛玻璃 + 细描边
    - 状态：进度条（配额用满率）、toast 提示、role-badge 徽章、面包屑导航
    - 安全：`escapeHtml()` 防 XSS；用户输入 `parseInt` + 边界检查
    - 文件大小自适应 B/KB/MB/GB；`formatSize()` 工具函数
  - **触发问题**：用户实测 v1.5.1 release 二进制点击登录按钮无反应 + 浏览器 console 报 `ReferenceError`
  - **为什么 v1.5.1 release 漏检**：之前端到端只测了 API (`/api/v1/ping`)，没在浏览器实测 UI 行为
- **v1.5.4 修改密码 + admin 改他人 + do_PATCH bug fix**：
  - **自己改密码**：`PUT /api/v1/users/me/password`，需 `old_password` 验证（PBKDF2-SHA256）+ `new_password` ≥ 6 字符；成功后清 session + `Set-Cookie: ... Max-Age=0` 强制重登
  - **admin 改他人**：`PATCH /api/v1/admin/users/<name>` `{password: ...}`（已存在，admin 角色可改）
  - **Web UI 同步**：me 卡片下加「修改我的密码」表单（3 字段 + 提交）；admin 表格每行加「改密码」按钮
  - **顺手修历史 bug**：`server.py` 之前没有 `do_PATCH` handler（`BaseHTTPRequestHandler` 默认不接 PATCH），所以 `PATCH /api/v1/admin/users/<name>` 从 v1.5.0 起就一直 501。补上后 admin 改他人密码/角色/配额才真正可用
  - **响应头顺序坑**：`send_header("Set-Cookie", ...)` 必须在 `send_response` 之后调，否则 header 落空；改自己密码用 inline 写 response 而不是 `_json + _clear_session_cookie` 两步
  - **不动的边界**：旧密码忘记 → 仍需 admin 重置；服务端没有「邮箱找回」功能
- **v1.5.5 后台模式（--detach / --stop）**：
  - **用户需求**：「启动时加参数就能后台运行 + 关掉命令行窗口服务不中断」
  - **方案**：fork / spawn 子进程后父进程 exit，子进程继续服务
    - macOS / Linux：标准 double-fork daemonize（`os.fork` + `os.setsid` + IO 重定向）
    - Windows：`subprocess.Popen` + `DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`
  - **新模块**：`ucopy_server/detach.py`
    - `read_pid / write_pid / remove_pid / is_pid_alive`（跨平台：Unix 用 `kill -0`，Windows 用 `OpenProcess + GetExitCodeProcess`）
    - `stop(pid_file, timeout=5)`：SIGTERM 等 5s → SIGKILL 强杀，过程中清 stale pid 文件
    - `detach(log_file, pid_file)`：平台分发
    - `is_already_running(pid_file)`：重复 detach 检测
  - **新 CLI 参数**（`config.py`）：`--detach` / `--stop` / `--log-file` / `--pid-file`
  - **默认路径**：log = `<data-dir>/server.log`，pid = `<data-dir>/server.pid`
  - **detach 模式不注册 launchd**：避免和 launchd 重复管理
  - **detach 模式 static 404 根因（v1.5.5 后修）**：
    - **症状**：detach 模式启动后 `GET /` → 404「Web GUI 未安装（static/index.html 不存在）」
    - **根因**：PyInstaller onefile 把 `ucopy_server/static/` 解压到临时 `_MEIPASS/ucopy_server/static/`；模块加载时算 STATIC_DIR 指向这个临时目录；detach 模式 double-fork 后 macOS 系统 TMPDIR 自动清理 unlink 整个 _MEIPASS 目录（即使 daemon 进程 fd 还指向文件，路径字符串已失效）→ `os.path.exists` 走 lstat 拿不到 inode → 报 404
    - **修法**：模块加载时 `_load_static_files()` 一次性把所有 static/* 读到内存 dict（`STATIC_FILES`），`_serve_index` / `_serve_static_or_index` 从 dict 读 → 完全绕开文件系统
    - **代价**：HTML 27KB + JS/CSS 多一点，~50KB 内存常驻（不可接受就别用 detach 模式）
  - **zombie 状态（v1.5.5 后修）**：
    - **症状**：`--stop` 后 `is_pid_alive(daemon_pid)` 仍 True（daemon 进程已死但 init 还没回收 zombie）
    - **修法**：`stop()` 三阶段：SIGTERM 等 5s → SIGKILL 等 5s → 再 fail；zombie 阶段 5s 内 init 一定回收
  - **重复 --stop 视为 OK**：无 PID 文件 = `OK: 未在运行（无 PID 文件）` exit=0
  - **实测**（macOS arm64 本地 build）：
    - `--detach` 父进程 exit=0，子进程 PID 写入 pid_file，端口 LISTEN，curl 200
    - 等 15s（让 macOS 清理 _MEIPASS）→ curl 仍 200（内存缓存生效）
    - `--stop` SIGKILL 阶段 + 自动清 stale pid
  - **文档**：中英 README 各加「macOS 服务器」独立段（默认数据路径表 + 完整 CLI 参数表 + 后台模式 vs 前台模式对比）
- **v1.5.0 → v1.5.1 Web GUI 404 fix**（**严重**，未在 v1.5.1 release 内修）：
  - `server.py` 原来用 `os.path.join(THIS_DIR, "static")` 找 `index.html`，但在 PyInstaller onefile 模式下 `__file__` 指向解压临时目录（只有 `ucopy_server/server.py`），`ucopy_server/static/` 在 `sys._MEIPASS/ucopy_server/static/`
  - 修法：加 `_resolve_static_dir()` 优先查 `sys._MEIPASS/ucopy_server/static`，fallback 源码运行的 `THIS_DIR/static`；`STATIC_DIR` 模块级常量
  - 之前 v1.5.1 的端到端测试只测 API（`/api/v1/ping`），没测 Web GUI `/`，所以漏检
  - 教训：onefile 打包后必须测**全路径**（HTML / 静态资源 / API），不能只看 API 通就认为 OK
- **v1.5.0 旧二进制下载的用户**：先 `./UCopy-Server-macos-arm64 --data-dir ~/UCopyServer --print-admin-password` 拿回旧 admin 密码，下载新二进制覆盖后再起；或 `--init-only` 重生数据。
- **v1.6.0 API Key 认证（替代服务器账密）**：
  - **动机**：服务器账号密码不应该出现在每台客户端上；key 可吊销/轮换、可审计、可独占绑定设备
  - **服务端**：
    - `db.py`：新增 `api_keys` 表（id / username / token_hash / secret_hash / secret_salt / name / current_client_id / last_heartbeat_at / created_at）+ 7 个 CRUD 函数（add / get_by_token / get_by_id / list / delete / claim_lock / update_heartbeat / release_lock / purge_stale / rename）
    - `auth.py`：`new_key_token()`（32B url-safe）、`hash_key_token()`（sha256）、`hash_key_secret()`（PBKDF2-SHA256 200k + 独立 salt）、`verify_key_secret()`
    - `server.py` 新路由：`GET/POST /api/v1/keys`、`DELETE /api/v1/keys/{id}`、`DELETE /api/v1/keys/{id}/release`、`POST /api/v1/auth/key`、`POST /api/v1/auth/heartbeat`
    - **key 格式**：`<token>.<secret>`（87 字符，中间一个点）—— 客户端只存这一串
    - **key 认证也发 session cookie**：这样 `/api/v1/me`、`/api/v1/files/*`、`/api/v1/config` 等现有业务 API 全部复用 session 认证，无需改造
    - **独占锁**：`current_client_id` + `last_heartbeat_at`；登录时抢占式接管（新 client 顶掉旧 client）；心跳 client_id 不匹配 → `410 key_lock_lost`
    - **stale 锁清理**：每个请求顺手 `purge_stale_api_key_locks(120s)`
    - 常量：`KEY_LOCK_TIMEOUT_SECONDS=120`（2 分钟）、`KEY_HEARTBEAT_INTERVAL_SECONDS=40`
  - **客户端**：
    - 新模块 `ucopy_credentials.py`：跨平台凭据加密
      - Windows：DPAPI (`CryptProtectData`/`CryptUnprotectData`，`CRYPTPROTECT_UI_FORBIDDEN` 不弹窗)
      - macOS：Keychain（`security add/find/delete-generic-password`，service=`com.skyc8266.ucopy.remote-key`）
      - Linux：`/etc/machine-id` + 用户名 + 固定 salt 派生 SHA-256 密钥做 XOR
      - **都不支持时返回 None（拒绝明文落盘）**，调用方弹警告说 key 只在内存里
    - `remote_client.py`：`login_with_key(api_key, client_id)` / `heartbeat()` / `heartbeat_interval` property / `key_id` / `client_id`
    - `ucopy.py`：
      - `load_remote_key(cfg)` / `save_remote_key(cfg, key)` / `clear_remote_key(cfg)`（读写加密 blob）
      - `_remote_key_account(cfg)`：Keychain/DPAPI 的 account 名 = `server_url|client_name`（多服务器不串）
      - `RemoteUploader`：`username/password` → `api_key/client_id`；新增心跳线程 `_heartbeat_loop`（40s 一次，失败 5s 重试，`key_lock_lost` 标记）
      - UI：`用户名`+`密码` 两行 → `API Key`（密码框显示 + 「已保存/未设置」提示）+ `本机名称`（默认 hostname）
      - `test_remote`：`login_with_key` + 抢占时提示「已接管原占用设备 X」；`key_lock_lost` / `invalid_key` 各有专门错误弹窗
      - `DEFAULT_CONFIG.remote`：去掉 `username`，加 `client_name` / `api_key_enc`；`get_config()` 引导迁移（`pop("username")` / `pop("password")`）
  - **Web GUI**：新「API Keys」卡片（所有登录用户可见，不只是 admin）
    - 列表：名称 / 状态（● 使用中 + client_id / ○ 空闲）/ 创建时间 / 操作
    - 创建：填名称 → 显示完整 key 一次 + 「复制到剪贴板」按钮
    - 删除（danger）/ 踢下线（仅占用时显示）
  - **实测**（本地 build + 真实 server）：
    - 25 项服务端 API 测试全过（创建/列表/登录/心跳/抢占/踢/删除/越权 alice 看不到 admin 的 key/未登录 401）
    - 14 组客户端 E2E 全过（cert 信任 → 建 key → key 登录 → 心跳 → 上传文件（验证 session 复用）→ B 抢占 → A 被踢 410 → 强制释放 → 重登 → 凭据加密往返 → 心跳线程 3.5s 发 4 次 → 清理）
  - **安全性对比**：
    | 项 | v1.5.x | v1.6.0 |
    |---|---|---|
    | 客户端持有 | 用户名 + 密码（密码仅内存） | API key（加密落盘） |
    | 服务端存 key | — | token=sha256, secret=PBKDF2 200k + 独立 salt |
    | 密码泄露影响 | 账号被盗 | 单个 key 失效，账号不受影响 |
    | 吊销粒度 | 无（只能改密码） | 按 key |
    | 设备绑定 | 无 | 独占锁 + client_id |
- **v1.5.5.2 UCopy 客户端首次连接 self-signed cert 处理修**：
  - **症状**：「测试连接」点一次后弹「连接失败 [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate」
  - **根因**：`test_remote` 旧版直接 `client.login()` —— `RemoteClient` 看到 store 里没存 fingerprint → 用 `CERT_REQUIRED`（标准 CA 校验）→ self-signed cert 必抛 `SSLCertVerificationError`，但 `test_remote` 的 `except Exception` 把异常包成 `str(e)` 弹窗，**没有「信任此服务器？」入口**
  - **修法**（`ucopy.py` `SettingsWindow.test_remote`）：
    1. 先 `client.get_current_fingerprint()` 用 CERT_NONE ctx 拿 fingerprint（绕开 CA 校验）
    2. 拿 `store.get_fingerprint(url)`：未存 → 弹 `messagebox.askyesno("信任此服务器？", fp + 防 MITM 说明)` → 用户点是 → `store.trust(url, fp)` → 重新 new client（store 有 fp 走 CERT_NONE + pin）
    3. 已存但 fingerprint 不一致 → 弹「证书指纹不匹配」错误（已信任 + 当前 fp + 警告：服务器换证书 / 中间人攻击）
    4. 已存且一致 → 直接 `client.login(user, pw)` 走通
  - **不动的边界**：RemoteUploader 后台线程撞到 self-signed cert 没有「信任」弹窗入口（GUI 必须在主线程），只能写日志让用户去设置面板点「测试连接」完成首次信任
  - **不动的边界**：fingerprint 不一致时需要用户手动删 `~/.ucopy_trusted_servers.json`（没做 UI 入口）—— v1.5.0 文档里有提及但没修；下次再补
- **v1.5.5.3 UCopy 客户端 test_remote 不存 server_url/username 修**：
  - **症状**：用户在「测试连接」填了 server_url + username + password → 弹「信任此服务器？」点「是」→ 登录成功 → 关掉设置面板再打开 → server_url / username 输入框是空的（password 设计上就不存）
  - **根因**：`test_remote` 成功路径**只**写 `trusted_fingerprint` 到 cfg，没写 server_url / username / auto_upload。设置面板下次打开从 cfg 读 `""` → 输入框空
  - **修法**：`test_remote` 成功路径加 `update({server_url, username, auto_upload, trusted_fingerprint})` 一并写盘
  - **password 仍然不存**（用户硬性偏好：「仅本会话使用，重启后要重新输入」），源码断言守住 `_src.count('"password"') == 0`
