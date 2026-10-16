# UCopy 远程模式 — 客户端/服务器协议契约

本文档是 `ucopy.py`（客户端）与 `ucopy-server/`（服务器）之间的**事实契约**。
双方实现都必须遵守；任一侧破坏契约，对端行为未定义。

## 0. 传输层

- **HTTPS**，TLS 1.2+。服务器使用 adhoc self-signed 证书（Python `ssl.SSLContext`）。
  - 客户端首次连接时弹出"该证书为自签，是否信任"提示，用户确认后写入 `~/.ucopy_trusted_servers.json` 持久化。
  - 服务器证书指纹（SHA-256）保存到 trusted 列表，下次连同一服务器不再提示。
  - 指纹变化 → 弹"证书已变更，是否重新信任"（强制用户重输密码确认，避免 MITM 静默换证书）。
- **双栈**：服务器监听 `::`（IPv6 wildcard，IPv4 mapped 自动兼容）。客户端支持 `https://[ipv6]:port` / `https://ipv4:port` / `https://host:port`。
- **端口**：默认 `8443`。服务器启动时若被占则报错退出（让用户改端口，不要偷偷换）。
- **路径前缀**：所有 API 都在 `/api/v1/` 下；Web GUI 在 `/`。

## 1. 鉴权

### 1.1 登录

```
POST /api/v1/login
Content-Type: application/json

{"username": "alice", "password": "hunter2"}
```

成功 → 200 + `Set-Cookie: ucopy_session=<token>; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=86400`

```json
{"ok": true, "role": "user", "username": "alice"}
```

失败 → 401：

```json
{"ok": false, "error": "invalid credentials"}
```

**密码哈希**：服务端用 `hashlib.pbkdf2_hmac('sha256', password, salt, 200_000)` 存 `(salt, hash)`。比对用 `hmac.compare_digest`。

### 1.2 受保护端点

所有 `/api/v1/*` 端点（除 `/api/v1/login`）都必须带 cookie `ucopy_session`。
服务端校验失败 → 401：

```json
{"ok": false, "error": "unauthenticated"}
```

### 1.3 Logout

```
POST /api/v1/logout
```

清 session。返回 200 + `Set-Cookie` 过期。

## 2. 用户角色与权限

| 角色 | 配置 | 文件 | 上传 | 用户管理 |
|---|---|---|---|---|
| `admin` | 读/写所有 | 读/写所有 | ✓ | ✓ |
| `user` | 读/写自己 | 读/写自己 | ✓ | ✗ |
| `readonly` | 读自己 | 读自己 | ✗ | ✗ |

每用户配额 `quota_mb`（默认 10240 = 10GB）。上传前服务端校验 `当前用量 + 文件大小 <= quota_mb`。

## 3. 配置同步

### 3.1 拉取

```
GET /api/v1/config
Cookie: ucopy_session=...
```

返回：

```json
{
  "ok": true,
  "config": { ...完整的 UCopy 配置 JSON（已脱敏）... },
  "last_modified": "2026-10-15T12:34:56",
  "version": 7
}
```

**脱敏**：服务端在响应前**剥除**以下键：
- `security.password_hash`
- `security.password_salt`
- `security.qa`

剥除后 `security` 字段保留为 `{}`（客户端不会因此报错）。

### 3.2 推送

```
PUT /api/v1/config
Cookie: ucopy_session=...
Content-Type: application/json

{"config": { ...客户端本地配置... }}
```

服务端**再次**剥除 `security.*`（双层防御，客户端上传前就剥一次），然后覆盖用户自己的配置记录。

**返回**：

```json
{"ok": true, "version": 8, "last_modified": "..."}
```

### 3.3 配置 schema

完整 schema 见 `ucopy.py` 的 `DEFAULT_CONFIG`。**唯一额外**与远程模式相关的键：

```jsonc
{
  "remote": {
    "enabled": true,
    "server_url": "https://[2609:abcd::1]:8443",  // 用户在 SettingsWindow 填的地址
    "username": "alice",
    "auto_upload": true,
    "trusted_fingerprint": "ab12cd34...",  // 服务器证书指纹
    "last_error": "",  // 给 UI 显示用
    "last_upload_at": "2026-10-15T12:34:56"
  }
}
```

`remote.trusted_fingerprint` 是 SHA-256(server cert DER)，**不是密码**，可以本地存。

## 4. 文件操作

所有文件路径用**相对路径**（相对于用户根目录 `/data/<username>/`）。
**禁止**用绝对路径或 `..` 越过根目录 —— 服务端用 `os.path.normpath` + 前缀检查拦截。

### 4.1 列目录

```
GET /api/v1/files?path=subdir
```

```json
{
  "ok": true,
  "entries": [
    {"name": "photo.jpg", "type": "file", "size": 12345, "mtime": 1700000000.0, "sha256": "ab12..."},
    {"name": "videos", "type": "dir", "size": null, "mtime": 1700000000.0, "sha256": null}
  ],
  "path": "subdir"
}
```

### 4.2 下载

```
GET /api/v1/files/download?path=subdir/photo.jpg
```

返回二进制流，`Content-Disposition: attachment; filename="photo.jpg"`。

### 4.3 检查（sha256 比对）

```
GET /api/v1/files/check?path=subdir/photo.jpg&sha256=ab12...
```

服务端查文件 → 200 if 命中 + sha256 相同（**不用上传**），404 otherwise（**请上传**）。

```json
{"ok": true, "match": true, "size": 12345, "mtime": 1700000000.0}
{"ok": true, "match": false}
```

### 4.4 上传（multipart）

```
POST /api/v1/files/upload?path=subdir/new.jpg
Cookie: ucopy_session=...
Content-Type: multipart/form-data; boundary=...

--boundary
Content-Disposition: form-data; name="sha256"

ab12cd34...
--boundary
Content-Disposition: form-data; name="size"

12345
--boundary
Content-Disposition: form-data; name="mtime"

1700000000.0
--boundary
Content-Disposition: form-data; name="file"; filename="new.jpg"
Content-Type: application/octet-stream

<binary>
--boundary--
```

服务端流程：
1. 校验 path 不越界
2. 校验 size 不超过配额剩余
3. 写临时文件 `<dest>.upload.tmp`
4. 流式计算 sha256（边读边算，不全量加载内存）
5. 与表单 `sha256` 字段比对 → 不一致返回 400
6. 与现有文件 sha256 比对 → 相同返回 200 `{"ok": true, "unchanged": true}`
7. 移动临时文件到目标 → `os.replace`
8. 扣减配额计数器
9. 返回 200 `{"ok": true, "unchanged": false, "size": 12345, "sha256": "..."}`

**配额不足** → 413：

```json
{"ok": false, "error": "quota exceeded"}
```

### 4.5 删除

```
DELETE /api/v1/files?path=subdir/photo.jpg
```

user 只能删自己的；admin 能删任意。

## 5. 用户管理（admin only）

```
GET    /api/v1/admin/users                # 列出所有用户
POST   /api/v1/admin/users                # 创建用户 {username, password, role, quota_mb}
PATCH  /api/v1/admin/users/<username>     # 修改角色 / 配额 / 重置密码
DELETE /api/v1/admin/users/<username>     # 删除用户（同时删除其文件）
```

返回结构与服务端管理页配套。所有 admin 接口对非 admin 返回 403。

## 6. 错误约定

所有 4xx/5xx 都返回：

```json
{"ok": false, "error": "<短描述>", "code": "<短代号>"}
```

常见 code：
- `unauthenticated` (401)
- `forbidden` (403)
- `not_found` (404)
- `quota_exceeded` (413)
- `invalid_path` (400, 越界)
- `size_mismatch` (400, 上传 sha256 与表单 sha256 不符)
- `internal` (500)

## 7. 客户端与服务器版本兼容

- 协议版本字段 `X-UCopy-Protocol: 1` 在所有请求/响应头里。
- 客户端发现服务器版本不匹配 → 报错"协议不兼容，请升级"。
- 服务器发现客户端协议字段缺失或版本过高 → 同上。
- **当前版本 = 1**。