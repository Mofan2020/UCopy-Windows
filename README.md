# UCopy

> **A lightweight Windows background tool that automatically copies files from inserted USB drives to a folder you choose.** No system tray, no taskbar clutter — just a tiny floating dot in the top-right corner of your screen.

You insert a USB drive. Files copy themselves to a backup folder. You go on with your life. That's the whole product.

![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-blue.svg)
![Python](https://img.shields.io/badge/Python-3.11%2B-yellow.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

[中文文档](./README.zh-CN.md) · [Releases](https://github.com/Mofan2020/UCopy-Windows/releases)

> **Status:** Development is currently paused on the Windows client. The companion remote server (`ucopy_server`) is still maintained.
>
> **⚠ Legal notice:** Do not use this tool to copy or modify storage devices that do not belong to you or that you are not explicitly authorized to access. The author takes no responsibility for misuse.

## Highlights

- **Auto-detect** — Listens to Windows device notifications; no polling.
- **Silent background copy** — Preserves the original directory structure; creates `<label>_<serial>` subfolders per drive.
- **Configurable rules:**
  - Required target directory
  - Per-drive max file count / total size (skip the whole drive if exceeded)
  - Per-file max size (skip oversized files)
  - Refuse copy if target disk has less than N MB free
  - Extension filter (e.g. `.jpg,.png`)
  - Filename regex filter
- **Blacklist / Whitelist dual mode** — one list, two modes. Switchable from the settings panel.
- **Optional auto-start** via the registry; toggle on or off any time.
- **Floating dot UI** — a tiny purple dot in the top-right corner of the screen. No tray icon. **Triple-click it** to open the settings panel.
- **Smart diff copy** — On a second insertion of the same drive, files are compared by size + modification time; unchanged files are skipped, changed files are overwritten. Files already in the target that are no longer on the drive are kept (never deleted).
- **Camera / microphone activity indicator** — the dot changes color in real time: green when the camera is in use, orange when only the microphone is in use, purple when both are idle. Virtual cameras (OBS Virtual Camera, ManyCam, VCAM) and virtual microphones (VB-CABLE, VoiceMeeter) are also detected.
- **Password-protected settings panel** — first launch forces you to set a login password + 3 security questions. The panel requires the password every time it's opened. Passwords and answers are stored as **SHA-256 + per-record random salt** — never plaintext.
- **Single-file build** — PyInstaller packages it into one EXE, no Python runtime required.

## Screenshots

> To be added: settings panel + dot indicator.

## Install & Run

### Option 1: Run the Python script directly

1. Make sure Python 3.8+ is installed.
2. Install the dependency:

   ```sh
   pip install pywin32
   ```

3. Run:

   ```sh
   python ucopy.py
   ```

On first launch, a **Security Setup** window appears and forces you to set a login password and 3 security questions (used for password recovery). After that, the settings window appears — configure your rules and click "Save and Hide". From then on, the program only shows a small dot in the top-right corner of your screen (purple by default; turns green/orange when the camera or microphone is in use). **Triple-click the dot** to reopen the settings panel.

### Option 2: Download the prebuilt EXE

Grab `UCopy.exe` from the [Releases](https://github.com/Mofan2020/UCopy-Windows/releases) page and double-click it (Windows may show a security warning — allow it).

> The prebuilt binary targets AMD64. For ARM, please build it yourself.

## Configuration

All settings live in `%USERPROFILE%\.ucopy_settings.json` and are edited via the settings panel:

| Setting | Description |
| --- | --- |
| Target directory | Root directory for copied files — **required** |
| Max file count | If the drive's total file count exceeds this, skip the whole drive; `0` = no limit |
| Max total size (MB) | If the drive's total size exceeds this, skip the whole drive; `0` = no limit |
| Skip files larger than (MB) | Skip individual files larger than this; `0` = no limit |
| Target free space (MB) | Refuse copy if target disk has less than this many MB free; `0` = no limit |
| Extensions | Comma-separated, e.g. `.jpg,.png,.pdf`; empty = no filter |
| Filename regex | Only files matching this regex are copied; empty = no filter |
| Auto-start on boot | Toggles a registry entry for auto-start |
| List mode | `blacklist` or `whitelist` — choose one |

## USB Drive List (Blacklist / Whitelist)

One list, two modes. The active mode is selected from a dropdown at the top of the settings panel:

| Mode | Meaning | Drives in the list | Drives not in the list |
| --- | --- | --- | --- |
| **Blacklist** (default) | Listed drives are blocked | Not copied | Copied |
| **Whitelist** | Only listed drives are copied | Copied | Not copied |

Operation: select a drive in the list, then click "Add/Remove from Blacklist" or "Add/Remove from Whitelist". The right side of the list shows each drive's current disposition in real time (`[in blacklist]` / `[will copy]` / `[in whitelist]` / `[won't copy]`).

> Switching to whitelist mode means **newly inserted drives are not in the whitelist and will not be copied** — you must add them manually.
>
> Upgrading from an older version: drives in the old blacklist are automatically reinterpreted according to the current mode.
>
> Mode switch takes effect immediately and auto-saves — no need to click "Save and Hide".

## Floating Dot

No system tray icon. After launch, a small floating dot (6 px diameter, anti-aliased) sits in the top-right corner of your screen:

- **Triple-click** the dot → opens the settings panel (with password)
- The dot is borderless and transparent everywhere except the pixel; it does not block other windows
- Closing the settings panel leaves the dot in place; the program keeps watching for USB drives
- To exit the program, open the settings panel and click "Exit"

### Dot Color (Camera / Microphone Indicator)

The dot is sampled once per second and changes color accordingly:

| Color | Meaning |
| --- | --- |
| Purple | Neither camera nor microphone is in use |
| Green | **A program is using the camera** (microphone state does not affect the color in this case) |
| Orange | Camera idle, but **a program is using the microphone** |

Detection rules:

- Camera-in-use always shows green, regardless of microphone state.
- When the camera stops but the microphone is still active, the color switches to orange — it does not falsely return to purple.
- Both devices must be idle before the dot returns to purple.
- Detection covers **all system camera and recording devices**, including virtual drivers (OBS Virtual Camera, ManyCam, VCAM, VB-CABLE, VoiceMeeter, etc.). Camera enumeration queries five GUIDs (system device class + device interface + KS streaming categories VIDEO/CAPTURE/VIDEO), covering physical cameras and the vast majority of virtual drivers.
- Windows has no official API for "is the device in use", so UCopy probes by attempting exclusive access. For each camera interface path, six combinations are tried (exclusive / read-share / read-only-share × original path / device path); any return of "sharing violation / device busy / access denied" counts as in-use. Drivers that ignore sharing semantics (always return success) may slip through — UCopy errs on the side of "rather miss than falsely light up".
- After long idle (two consecutive idle samples), the polling interval slows from 1 s to 5 s by default. When a device becomes busy again, polling snaps back to 1 s, so the first color change still happens within 1 s. Toggle `indicator_slowdown` to disable throttling; `indicator_active_interval` / `indicator_idle_interval` adjust the two rates (in seconds, written to `~/.ucopy_settings.json`).
- If multiple consecutive probes fail (driver or system hiccup), UCopy writes a warning to the log and keeps the dot purple. The interval does NOT slow down during failure, so the dot reflects the real state as soon as things recover.

## Security

To prevent anyone else from editing your backup rules, the settings panel is password-gated. First launch walks you through:

1. **Login password** — at least 4 characters; required every time the panel is opened.
2. **Security questions** — 3 questions, each chosen from a preset list or written by you; answering all three correctly lets you reset the password.

Five wrong password attempts close the settings panel temporarily. USB drive background copying and the floating dot are unaffected.

### Storage

- Passwords and security answers are **never stored as plaintext**. They are hashed with **SHA-256 + 16-byte per-record random salt** into the `security` section of `%USERPROFILE%\.ucopy_settings.json`. Even if the config file leaks, the originals cannot be reversed.
- Security answers are stripped of leading/trailing whitespace and lowercased before comparison, so they are case-insensitive.

### Change Password / Security Questions

The bottom of the settings panel has two buttons:

- **Change password** — requires current password; new password must be entered twice.
- **Change security questions** — requires current password; re-pick 3 questions and answers.

### Forgot Password?

On the login screen, click "Forgot password" and answer the 3 security questions. **Keep your answers safe — this is the only recovery path.**

## Backup Folder Layout

Given a target directory `D:\Backup`, drive label `MyDrive`, serial `1234ABCD`:

```
D:\Backup\MyDrive_1234ABCD\
├── Documents\
│   └── report.docx
├── Photos\
│   └── landscape.jpg
└── ...
```

## Building from Source

PyInstaller packages it into a single EXE:

```sh
pip install pyinstaller
pyinstaller --onefile --windowed --name UCopy --icon assets/blank.ico ucopy.py
```

Output: `dist\UCopy.exe`. `assets/blank.ico` is a **fully transparent** (all-zero pixel) icon used to avoid the PyInstaller default icon.

### GitHub Actions Auto-Build

`.github/workflows/build.yml` builds on every push using an AMD64 Windows runner; artifacts are downloadable from the Actions page.

## Remote Mode (v1.5.0+)

UCopy can connect to a remote server, automatically upload the files it copies locally, and sync configuration across machines.

### Enable

1. **Start the server.** On the remote machine, run `ucopy_server` (a separate Python program, not bundled with the UCopy EXE):

   ```sh
   python3 ucopy_server/server.py
   ```

   First launch generates a self-signed certificate + initial admin account (random password printed to the terminal). Features: HTTPS (self-signed), dual-stack listening (IPv4 + IPv6), multi-user, quotas, launchd auto-start (macOS), web GUI (`static/index.html` on the server).

2. **Open the "Remote Mode" section** in UCopy's settings panel:
   - Server address (e.g. `https://[2609::1]:8443` or `https://example.com:8443`)
   - Username + server password (the password is **never written to disk**, session-only)
   - Tick "Enable Remote Mode" → three mandatory confirmation dialogs appear (all three checkboxes must be ticked to enable):
     1. I will only use this tool for lawful purposes; all liability for misuse rests with me.
     2. I am not being deceived or induced into enabling this mode.
     3. I have confirmed the server address is correct and that the server has enough storage for my uploads.

3. **First connection** shows a "Trust self-signed certificate" dialog (with the cert fingerprint). After confirmation, the fingerprint is saved to `~/.ucopy_trusted_servers.json` and not prompted again.

4. **After every USB copy**, a background thread uploads the files that pass the `extensions / regex_pattern / max_single_file_mb` filter to the server. SHA-256 dedup avoids retransfers.

### Server Configuration

| Item | Default | Notes |
| --- | --- | --- |
| Port | 8443 | HTTPS port |
| Data directory | `~/UCopyServer/` | User files + SQLite + self-signed cert |
| Initial admin | `admin` | Random password printed to terminal on first launch |
| Quota | 10 GB/user (admin unlimited) | Excess returns HTTP 413 |
| launchd auto-start | macOS: enabled on first launch | `~/Library/LaunchAgents/com.skyc8266.ucopy-server.plist` |

### Security

- Client → server: HTTPS with self-signed certificate; after the first trust, the cert fingerprint is pinned (defends against MITM).
- Config sync: password and security-question SHA-256 hashes are **never transmitted** (stripped both on the client and the server side).
- Server password: lives only in the in-memory session, lost when UCopy exits; re-enter on next enable.

### Protocol Contract

Full API spec: [`ucopy_server/PROTOCOL.md`](ucopy_server/PROTOCOL.md).

### macOS Single-File Build

The server code can be packaged into a macOS single-file binary (no Python needed):

```sh
bash build_macos.sh
```

Output: `dist/UCopy-Server-macos-arm64` (~9.5 MB). Run it with `./UCopy-Server-macos-arm64 --help`.

**Apple Silicon only (M-series).** Intel Macs are not supported — Python 3.13 + PyInstaller 6.x cannot produce x86_64 binaries from an M-series machine. To run the server on Intel, use the source: `python3 ucopy_server/server.py` (Python 3.9+ required).

CI: pushing a tag (e.g. `v1.5.0`) triggers `.github/workflows/build-server.yml`, which builds on `macos-14` (arm64) and uploads the artifact `UCopy-Server-macos-arm64` to the GitHub Release.

## Tech Stack

- Python 3.11+
- `pywin32` — Windows API calls
- `tkinter` — Settings GUI + floating dot
- `ctypes` — Camera/microphone detection (SetupAPI + winmm; no extra dependencies)
- `hashlib` + `secrets` — Password and security-answer SHA-256 salted hashing
- `http.server` + `sqlite3` + `ssl` (stdlib) — Companion remote server (no third-party deps)
- `PyInstaller` — Packaging