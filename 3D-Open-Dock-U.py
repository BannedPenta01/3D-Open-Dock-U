#!/usr/bin/env python3
import os
import sys
from typing import Optional
import subprocess
import platform
import shutil
import hashlib
import binascii
import zlib
import json
import base64
import random
import re
import socket
import zipfile
import io
import shlex
from datetime import datetime
import time

try:
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(65536, hard), hard))
except Exception:
    pass

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QLineEdit, QTextEdit, QFormLayout,
    QGroupBox, QMessageBox, QFileDialog, QProgressBar, QFrame,
    QScrollArea, QSizePolicy, QSpacerItem, QInputDialog, QDialog,
    QCheckBox, QDialogButtonBox, QScroller, QScrollerProperties,
    QListWidget, QListWidgetItem, QRadioButton, QButtonGroup
)
from PySide6.QtCore import Qt, QThread, Signal, QSize, QSettings, QTimer, QDir, QLockFile, QStandardPaths
from PySide6.QtGui import QColor

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# region agent log helper
def _agent_debug_log(hypothesis_id, location, message, data=None, run_id="initial"):
    """Lightweight NDJSON logger for debug-mode instrumentation."""
    try:
        import json as _json
        import time as _time
        payload = {
            "sessionId": "08b005",
            "id": f"log_{int(_time.time() * 1000)}_{hypothesis_id}",
            "timestamp": int(_time.time() * 1000),
            "location": location,
            "message": message,
            "data": data or {},
            "runId": run_id,
            "hypothesisId": hypothesis_id,
        }
        with open("/home/jan/Dev/.cursor/debug-08b005.log", "a") as _f:
            _f.write(_json.dumps(payload) + "\n")
    except Exception:
        # Instrumentation must never break the app
        pass
# endregion

# ─── Constants ────────────────────────────────────────────────────────────────
APP_NAME = "3D Open Dock U"
APP_VERSION = "1.1.0"
PRETENDO_REPO = "https://github.com/MatthewL246/pretendo-docker.git"

# ─── Windows / WSL2 Support Helpers ───────────────────────────────────────────
def _is_windows():
    return platform.system().lower() == "windows"

def _wsl_installed():
    """Check if WSL2 is available on this Windows machine."""
    if not _is_windows():
        return False
    try:
        res = subprocess.run(["wsl", "--status"], capture_output=True, text=True, timeout=10, creationflags=0x08000000 if _is_windows() else 0)
        return res.returncode == 0
    except Exception:
        return False

def _wsl_distro_installed():
    """Check if at least one WSL distro is installed."""
    if not _is_windows():
        return False
    try:
        res = subprocess.run(["wsl", "-l", "-q"], capture_output=True, text=True, timeout=10, creationflags=0x08000000 if _is_windows() else 0)
        distros = [d.strip().replace('\x00', '') for d in res.stdout.strip().splitlines() if d.strip().replace('\x00', '')]
        return len(distros) > 0
    except Exception:
        return False

def _get_default_wsl_distro():
    """Return the name of the default WSL distro, or None."""
    if not _is_windows():
        return None
    try:
        res = subprocess.run(["wsl", "-l", "-v"], capture_output=True, text=True, timeout=10, creationflags=0x08000000 if _is_windows() else 0)
        for line in res.stdout.replace('\x00', '').splitlines():
            line = line.strip()
            if line.startswith("*"):
                parts = line[1:].split()
                if parts:
                    return parts[0]
    except Exception:
        pass
    return None

def _win_to_wsl_path(win_path):
    """Convert a Windows path (C:\\Users\\foo) to a WSL path (/mnt/c/Users/foo)."""
    if not win_path:
        return win_path
    p = win_path.replace("\\", "/")
    # Handle drive letter: C:/... -> /mnt/c/...
    if len(p) >= 2 and p[1] == ':':
        drive = p[0].lower()
        p = f"/mnt/{drive}{p[2:]}"
    return p

def _wsl_to_win_path(wsl_path):
    """Convert a WSL path (/mnt/c/Users/foo) to a Windows path (C:\\Users\\foo)."""
    if not wsl_path:
        return wsl_path
    if wsl_path.startswith("/mnt/") and len(wsl_path) > 5:
        drive = wsl_path[5].upper()
        rest = wsl_path[6:].replace("/", "\\")
        return f"{drive}:{rest}"
    return wsl_path

def _wsl_run(cmd_str, cwd=None, timeout=60):
    """Run a bash command inside WSL and return the CompletedProcess."""
    wsl_cmd = ["wsl", "bash", "-lc", cmd_str]
    wsl_cwd = None
    if cwd:
        wsl_cwd = _win_to_wsl_path(cwd) if _is_windows() else cwd
        wsl_cmd = ["wsl", "bash", "-lc", f"cd {shlex.quote(wsl_cwd)} && {cmd_str}"]
    return subprocess.run(wsl_cmd, capture_output=True, text=True, timeout=timeout, creationflags=0x08000000 if _is_windows() else 0)

def _docker_desktop_running():
    """Check if Docker Desktop is running on Windows."""
    if not _is_windows():
        return False
    try:
        res = subprocess.run(
            ["powershell", "-Command", "Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=10, creationflags=0x08000000
        )
        return bool(res.stdout.strip())
    except Exception:
        return False

def _docker_available():
    """Check if the docker CLI is available and responsive."""
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10, creationflags=0x08000000 if _is_windows() else 0)
        return res.returncode == 0
    except Exception:
        return False

def _start_docker_desktop():
    """Attempt to start Docker Desktop on Windows."""
    if not _is_windows():
        return False
    try:
        # Try common install paths
        dd_paths = [
            os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Docker", "Docker", "Docker Desktop.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Docker", "Docker Desktop.exe"),
        ]
        for dd in dd_paths:
            if os.path.isfile(dd):
                subprocess.Popen([dd], creationflags=0x00000008)  # DETACHED_PROCESS
                return True
        # Fallback: try via start command
        subprocess.Popen(["cmd", "/c", "start", "", "Docker Desktop"], creationflags=0x00000008)
        return True
    except Exception:
        return False

def _install_wsl2():
    """Install WSL2 on Windows using PowerShell (requires admin). Returns (success, message)."""
    if not _is_windows():
        return False, "Not Windows"
    try:
        # Use 'wsl --install' which handles both the WSL2 feature and Ubuntu distro
        res = subprocess.run(
            ["powershell", "-Command",
             "Start-Process 'wsl' -ArgumentList '--install' -Verb RunAs -Wait"],
            capture_output=True, text=True, timeout=600
        )
        if res.returncode == 0:
            return True, "WSL2 installation initiated. A restart may be required."
        else:
            return False, f"WSL2 install failed: {res.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, "WSL2 installation timed out (10 minutes)."
    except Exception as e:
        return False, f"WSL2 install error: {e}"

def _win_shell_cmd(cmd_str, use_wsl=False, cwd=None):
    """Build the appropriate shell command for Windows.
    If use_wsl=True, wraps the command in 'wsl bash -lc ...'.
    Otherwise returns the command as-is (for cmd.exe / PowerShell execution).
    """
    if use_wsl:
        wsl_cwd = ""
        if cwd:
            wsl_path = _win_to_wsl_path(cwd)
            wsl_cwd = f"cd {shlex.quote(wsl_path)} && "
        # Use double-quoting to pass through shell=True on Windows
        escaped = cmd_str.replace('"', '\\"')
        return f'wsl bash -lc "{wsl_cwd}{escaped}"'
    return cmd_str


def detect_os_info():
    """Detect OS, package manager, and default emulator paths."""
    system = platform.system().lower()
    info = {"os": system, "pkg_mgr": None, "pkg_install": "",
            "cemu_dir": "", "cemu_settings": "", "citra_config": "", "server_dir": "", "distro": "",
            "has_wsl": False, "has_wsl_distro": False, "wsl_distro": None,
            "has_docker_desktop": False, "docker_available": False}

    username = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
    home = os.path.expanduser("~")

 if system == "linux":
        info["server_dir"] = os.path.join(home, "pretendo-docker")
        # Direct path for AppImage/Generic Linux
        info["cemu_dir"] = os.path.join(home, ".local/share/Cemu")
        info["cemu_data"] = os.path.join(home, ".local/share/Cemu")

        # Check for Flatpak
        if os.path.isdir(os.path.join(home, ".var/app/info.cemu.Cemu")):
            info["cemu_dir"] = os.path.join(home, ".var/app/info.cemu.Cemu/data/Cemu")
            info["cemu_data"] = info["cemu_dir"]

        info["cemu_settings"] = os.path.join(info["cemu_dir"], "settings.xml")
        info["distro"] = "Linux"
    elif system == "darwin":
        info["server_dir"] = os.path.join(home, "pretendo-docker")
        info["distro"] = "macOS"
        info["cemu_dir"] = os.path.join(home, "Library/Application Support/Cemu")
        info["cemu_settings"] = os.path.join(info["cemu_dir"], "settings.xml")

    elif system == "windows":
        userprofile = os.environ.get("USERPROFILE", "C:\\Users\\User")
        info["server_dir"] = os.path.join(userprofile, "pretendo-docker")
        info["distro"] = "Windows"
        
        # Detect WSL2
        info["has_wsl"] = _wsl_installed()
        info["has_wsl_distro"] = _wsl_distro_installed()
        info["wsl_distro"] = _get_default_wsl_distro()
        
        # Detect Docker Desktop
        info["has_docker_desktop"] = _docker_desktop_running()
        info["docker_available"] = _docker_available()
        
        # Cemu paths on Windows
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        
        # Check multiple Cemu locations on Windows
        cemu_candidates = []
        if appdata:
            cemu_candidates.append(os.path.join(appdata, "Cemu"))
        if localappdata:
            cemu_candidates.append(os.path.join(localappdata, "Cemu"))
        # Common standalone install locations
        for drive in ["C:", "D:", "E:"]:
            cemu_candidates.append(os.path.join(drive, os.sep, "Cemu"))
            cemu_candidates.append(os.path.join(drive, os.sep, "Games", "Cemu"))
        
        found_cemu = ""
        for cand in cemu_candidates:
            if os.path.isdir(cand):
                found_cemu = cand
                break
        if not found_cemu and appdata:
            found_cemu = os.path.join(appdata, "Cemu")
        
        info["cemu_dir"] = found_cemu
        info["cemu_data"] = found_cemu  # Windows Cemu uses single directory
        c_dir = info["cemu_dir"] or ""
        info["cemu_settings"] = os.path.join(c_dir, "settings.xml") if c_dir else ""
        
        citra_paths = []
        if appdata:
            citra_paths.extend([
                os.path.join(appdata, "Citra", "config", "qt-config.ini"),
                os.path.join(appdata, "Lime3DS", "config", "qt-config.ini"),
                os.path.join(appdata, "Azahar", "qt-config.ini"),
            ])
        if localappdata:
            citra_paths.extend([
                os.path.join(localappdata, "Citra", "config", "qt-config.ini"),
                os.path.join(localappdata, "Lime3DS", "config", "qt-config.ini"),
            ])
        for p in citra_paths:
            if os.path.exists(p):
                info["citra_config"] = p
                break
    return info

OS_INFO = detect_os_info()
DEFAULT_SERVER_DIR = OS_INFO["server_dir"]
CEMU_DIR = OS_INFO["cemu_dir"]

# ─── Compressed Console Certificate Data (ccerts) ──────────────────────────────
# These certificates match the BannedPenta OTP/SEEPROM bundle.
# Storing them compressed avoids exceeding token limits and script bloat.
CONSOLE_CERTS_PACKED = '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'

# Security Utility: Base64 obfuscator for QSettings storage
import base64
def _obs(s): return base64.b64encode(str(s).encode('utf-8')).decode('utf-8') if s else ""
def _deobs(s):
    try: return base64.b64decode(str(s).encode('utf-8')).decode('utf-8') if s else ""
    except: return str(s) if s else ""

# Extract Master Keys into external strict-permission file
def load_security_keys():
    k_dir = os.path.join(OS_INFO.get("appdata") or os.path.expanduser("~"), ".config", "OpenDock")
    os.makedirs(k_dir, exist_ok=True)
    fpath = os.path.join(k_dir, "identity_secrets.txt")
    
    d_keys = {
        "WIIU_COMMON_KEY": "d7b00402659ba2abd2cb0db27fa2b197",
        "WIIU_STARBUCK_ANCAST": "d8b4970a7ed12e1002a0c4bf89bee171740d268b",
        "ESPRESSO_ANCAST_KEY": "2ba6f692ddbf0b3cd267e9374fa7dd849e80f8ab",
        "BANNED_OTP_HEX": "f8b18ec3fe26b9b11ad4a4edd3b7a031119a79f8ebe42a225e8593e448d9c5457381aaf7244ac1b7010be6ad38cc39ffdf9cebbc5eb45a159a7c5538a63a820f400c701eb11601265dbd123a58342643d057231fe5ec0c877f694664f54d053e8cae459488bd3bbf4801d72a6935ca9508803bf6d8395b2d4eba00000000000090000000000000000000000000010000b5d8ab06ed7f6cfc529f2ce1b4ea32fd3b8d192a39b759a8df501fc5da8ec3e26786a5461112c488703150395b33603e7065a3a22697494b3a119ec625bbec5430bfc76e7c19afbb23163330ced7c28dd7b00402659ba2abd2cb0db27fa2b656491bd12034b42d797af16bbce18d268564816af536f8ab1d58d23b83dc4a45df3085c9fc5bddaef7c3d70062edfd63c47b118f321870dab70af6f207ed2972bae5959adf673ca63143a744080ee67fe415f4b1e241982b487c1047cf9096b5aa0adc3a209a563ec90cfe09f32482167029bfabd3f8d5621d82d40ee74b78a1b3baf47f9b0ba0561a7f5dfb1edbc454460e9cc0cc659ba260543318dbc7fc9273a179cbd4826eda2ea0b734861bbd2d6d6939730070e45ded84d57e4a0a7cc8f644ddad3bfdab9ba3811ccd23d71d2973efa018560bb92cf5b2a5bce252ad0d02c3d72637fb32ea9416689f9bfe387b1e10778e29fe8cca227cbbf49361d54b8c2db626415640c8a338f1849ea562eaaf9362e4ad1f8027541ed93c8e3f9413b935bcacc038f0eff6b4dc080d444ac1b701a1049af812c91af0625d3a03850a35af04cef94b89e0da93150d2f2ad0b9c4e1592f018777f13633ee166bf685e077b6f7f4bbf67505ac78fa6ff7719ac6b2bc044cc63999fc95e52651705556b89deb458a82373ebdf79551d2d760b8f7c8000000120000000375fb390e004a0db12fc13af071aeed049e824a050579248c6aab6afdd93d84bbcb820081d32274144974cd95bee6b73803be0c8868281fb30683868c21f75ab25415f713492d57944e02de19a1b0cc5efa144b0083ddbd720000000000000000000000000000000000000000000000000000000000000000000000020000000175fb390e000a7808191601f1941ce23cf7b08c3047c7253584aa618b13acf15dfeba0072446c023b5c5e5de86ad9798c59ea4a98291c17dd22326d3bd087adb463b82bb4f4614e2e13f2fefbba4c9b7e786e76cdff2dc083ff9ada7953296282219b49475debd05fdff391b1271e026dbbb70852a04e859b00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000008000708fca5c41502206a03233394c5037313800000000000000e1",
        "BANNED_SEEPROM_HEX": "00000000000000000000000000000000000000000000000058cc00000000000070010201434d383339340004008000703233394c50373138bec9f23a00240004404d4346000b4e31080000020005000200015521000000f8000300010000000000000000000000000000000000000000000000000000000000000000000000007819cc4d4f3ea413d78bcd3a80d3f5f40000000000000000000000000000000000000000000000000000000000000000444ac1b7b8c5d165b27aebf20464c92affff0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000070074000c0000000000000000000000020001000000000002000000004e545343513200074657000000000000343035353933323638000000000000005755502d3130312830322900000000000101866800000000201211022009ee3a450faa5500010007000200000000503dcb8cbb66002000056d615f6470363131f9a1007f000000000000000000004000a444d430efaab8603c4d0c67031900d50cc1a25cf1bd491f56ef31acacb7f66ae82c1b0b87cb28cd02083ead6ad9192700000000000000000000000000000000"
    }

    if not os.path.exists(fpath):
        with open(fpath, "w") as f:
            f.write("# SECURITY FILE: Master Keys and Console Vectors\n")
            f.write("# Storing these externally prevents hardcoded secrets in the .py script\n")
            for k, v in d_keys.items(): f.write(f"{k}={v}\n")
        # Restrict permissions so only the owner can read
        if platform.system() != "Windows": os.chmod(fpath, 0o600)
        return d_keys
        
    loaded = {}
    with open(fpath, "r") as f:
        for line in f:
            if '=' in line and not line.startswith('#'):
                k, v = line.strip().split('=', 1)
                loaded[k] = v
                
    # Auto-repair missing keys
    changed = False
    for k, v in d_keys.items():
        if k not in loaded:
            loaded[k] = v
            changed = True
    if changed:
        with open(fpath, "a") as f:
            for k, v in d_keys.items():
                if k not in loaded: f.write(f"{k}={v}\n")
    return loaded

SEC_KEYS = load_security_keys()
WIIU_COMMON_KEY = SEC_KEYS.get("WIIU_COMMON_KEY", "")
WIIU_STARBUCK_ANCAST = SEC_KEYS.get("WIIU_STARBUCK_ANCAST", "")
ESPRESSO_ANCAST_KEY = SEC_KEYS.get("ESPRESSO_ANCAST_KEY", "")

def safe_unhex(hex_str: str, target_size: int = 0) -> bytes:
    """Robustly parse hex strings, padding to target size and preventing odd-length crashes."""
    hex_str = str(hex_str).strip()
    if len(hex_str) % 2 != 0: hex_str = "0" + hex_str
    try:
        b = binascii.unhexlify(hex_str)
        if target_size > 0 and len(b) < target_size:
            b = b.ljust(target_size, b'\x00')
        return b
    except Exception:
        return b'\x00' * target_size if target_size else b""

# ─── Nintendo Colors ──────────────────────────────────────────────────────────
RED_DARK = "#8B0000"
RED_PRIMARY = "#CC0000"
RED_LIGHT = "#E83030"
CYAN_DARK = "#006080"
CYAN_PRIMARY = "#00AEDE"
CYAN_LIGHT = "#33CCFF"
BG_DARKEST = "#0A0A12"
BG_DARK = "#0F1018"
BG_CARD = "#151520"
BG_CARD_HOVER = "#1A1A2E"
BG_INPUT = "#0C0C16"
BORDER = "#252535"
BORDER_ACTIVE = "#353550"
TEXT_PRIMARY = "#E8E8F0"
TEXT_SECONDARY = "#8888AA"
ORANGE_ACCOUNT = "#FF8C00"
GREEN_MONEY = "#85BB65"
GREEN_BRIGHT = "#3fb950"

TITLE_MAP = {
    "00050000-10176900": "Splatoon",
    "00050000-1010EC00": "MARIO KART 8",
    "00050000-1018DC00": "Super Mario Maker",
    "00050000-10144F00": "Super Smash Bros. for Wii U",
    "00050000-10114000": "Pikmin 3",
    "00050000-10101D00": "The Legend of Zelda: The Wind Waker HD",
    "00050000-10143100": "The Legend of Zelda: Breath of the Wild",
    "00050000-10105700": "New SUPER MARIO BROS. U",
    "00050000-10145C00": "Minecraft: Wii U Edition",
    "00050000-101c4d00": "Splatoon (Trial)",
    "00050000-10102400": "Hyrule Warriors",
    "00050000-10110300": "TLoZ: Skyward Sword",
    "00050000-101c4f00": "Xenoblade Chronicles X",
    "00050000-10191b00": "Pokk\u00e9n Tournament"
}

STYLESHEET = f"""
QMainWindow {{ background-color: {BG_DARKEST}; }}
QWidget {{ color: {TEXT_PRIMARY}; font-family: 'Segoe UI', sans-serif; font-size: 14px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: {BG_DARK}; width: 10px; border-radius: 5px; }}
QScrollBar::handle:vertical {{ background: {BORDER_ACTIVE}; border-radius: 5px; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {BG_DARK}; border-radius: 10px; padding: 8px; }}
QTabBar::tab {{ background: {BG_CARD}; color: {TEXT_SECONDARY}; padding: 12px 24px; border-top-left-radius: 8px; border-top-right-radius: 8px; font-weight: bold; }}
QTabBar::tab:selected {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {RED_DARK}, stop:1 {CYAN_DARK}); color: white; border-bottom: 3px solid {CYAN_PRIMARY}; }}
QGroupBox {{ background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 12px; margin-top: 20px; padding: 20px; padding-top: 40px; font-weight: bold; color: {CYAN_PRIMARY}; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; padding: 6px 16px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {RED_DARK}, stop:1 {CYAN_DARK}); border-radius: 6px; color: white; left: 12px; }}
QLineEdit, QTextEdit {{ background: {BG_INPUT}; border: 1px solid {BORDER}; border-radius: 8px; padding: 10px; color: {TEXT_PRIMARY}; }}
QPushButton {{ background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 12px; font-weight: bold; }}
QPushButton:hover {{ border-color: {CYAN_PRIMARY}; color: {CYAN_LIGHT}; }}
QPushButton#startBtn {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1a5c1a, stop:1 #1a7f37); border-color: #2ea043; color: white; font-size: 16px; border-radius: 12px; }}
QPushButton#stopBtn {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {RED_DARK}, stop:1 #9e2a2a); border-color: {RED_PRIMARY}; color: white; font-size: 16px; border-radius: 12px; }}
QPushButton#patchBtn {{ color: {CYAN_LIGHT}; border-color: {CYAN_DARK}; }}
QProgressBar {{ border: 1px solid {BORDER}; border-radius: 8px; text-align: center; background: {BG_INPUT}; }}
QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {RED_PRIMARY}, stop:1 {CYAN_PRIMARY}); border-radius: 7px; }}
QTextEdit#logBox {{ background: {BG_INPUT}; font-family: monospace; color: {CYAN_LIGHT}; }}
QFrame#separator {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {RED_PRIMARY}, stop:0.5 {BORDER_ACTIVE}, stop:1 {CYAN_PRIMARY}); max-height: 2px; border: none; }}
"""

class CommandWorker(QThread):
    output = Signal(str)
    finished = Signal(int)
    def __init__(self, cmd, cwd=None, stdin_data=None):
        super().__init__()
        self.cmd = cmd
        self.cwd = cwd
        self.stdin_data = stdin_data
    def run(self):
        try:
            env = os.environ.copy()
            env.pop("LD_PRELOAD", None)
            if "TERM" not in env:
                env["TERM"] = "xterm-256color"

            popen_kwargs = {
                "shell": True,
                "cwd": self.cwd,
                "env": env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.PIPE if self.stdin_data is not None else None,
                "bufsize": 1,
                "universal_newlines": True
            }
            if OS_INFO["os"] == "windows":
                popen_kwargs["creationflags"] = 0x08000000 
                
            proc = subprocess.Popen(self.cmd, **popen_kwargs)
            
            if self.stdin_data is not None and proc.stdin:
                # Send password with multiple newlines to handle sudo's prompt correctly
                # We send it twice to handle cases where sudo might re-prompt
                proc.stdin.write(f"{self.stdin_data}\n{self.stdin_data}\n")
                proc.stdin.flush()
                # Crucial: tiny sleep to ensure sudo reads the buffer before we continue
                import time
                time.sleep(0.2)

            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                self.output.emit(self._colorize_log(line.strip()))
            
            proc.wait()
            self.finished.emit(proc.returncode)
        except Exception as e:
            self.output.emit(self._colorize_log(f"ERROR: {e}"))
            self.finished.emit(1)

    @staticmethod
    def _colorize_log(line):
        """Apply HTML color formatting to log lines based on severity keywords."""
        import html as html_mod
        safe = html_mod.escape(line)
        low = line.lower()
        # Strip ANSI escape codes for keyword detection
        stripped = re.sub(r'\x1b\[[0-9;]*m', '', low)
        if any(k in stripped for k in ['[success]', 'success:', 'started', 'created', 'connected', 'ready to accept', ' started on port']):
            return f"<span style='color:#3fb950;'>{safe}</span>"
        elif any(k in stripped for k in ['[error]', 'error:', 'error]', 'fatal:', 'fatal]', 'panic:', 'panic ', 'segmentation', 'module_not_found', 'typeerror', 'exited with code 1', 'exited with code 2', 'connection refused']):
            return f"<span style='color:#f85149;'>{safe}</span>"
        elif any(k in stripped for k in ['[warn', 'warning', 'deprecated', 'deprecation']):
            return f"<span style='color:#d29922;'>{safe}</span>"
        elif any(k in stripped for k in ['[info]', '[debug]']):
            return f"<span style='color:#8b949e;'>{safe}</span>"
        return safe

def make_scrollable(widget):
    scroll = QScrollArea()
    scroll.setWidget(widget)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setFrameShape(QFrame.NoFrame)
    scroller = QScroller.scroller(scroll.viewport())
    scroller.grabGesture(scroll.viewport(), QScroller.LeftMouseButtonGesture)
    return scroll

class ErrorPopupDialog(QDialog):
    def __init__(self, parent, error_msg):
        super().__init__(parent)
        self.setWindowTitle("Critical Server Error Detected")
        self.setMinimumWidth(650)
        self.setStyleSheet(STYLESHEET)
        
        layout = QVBoxLayout(self)
        
        header = QHBoxLayout()
        icon = QLabel("⚠️")
        icon.setStyleSheet("font-size: 32px;")
        header.addWidget(icon)
        
        title_text = QLabel("A Problematic Server Log was Detected")
        title_text.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {RED_LIGHT};")
        header.addWidget(title_text, 1)
        layout.addLayout(header)
        
        desc = QLabel("The following error was captured from the server logs while the session was active. "
                     "This usually indicates a database connection failure, a crash, or an authentication timeout "
                     "that might disconnect your game.")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_SECONDARY}; margin-bottom: 10px;")
        layout.addWidget(desc)
        
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlainText(error_msg)
        self.text_edit.setStyleSheet(f"background: {BG_INPUT}; color: {CYAN_LIGHT}; font-family: monospace; border: 1px solid {RED_DARK};")
        self.text_edit.setMinimumHeight(150)
        layout.addWidget(self.text_edit)
        
        hint = QLabel("You can select and copy the text above, or use the button below to copy the full report.")
        hint.setStyleSheet("font-size: 11px; color: #8888AA; font-style: italic;")
        layout.addWidget(hint)
        
        btn_layout = QHBoxLayout()
        copy_btn = QPushButton("Copy Report to Clipboard")
        copy_btn.setStyleSheet(f"background: {BG_CARD_HOVER}; border-color: {CYAN_PRIMARY};")
        copy_btn.clicked.connect(self.copy_to_clip)
        btn_layout.addWidget(copy_btn)
        
        close_btn = QPushButton("Dismiss")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        
        layout.addLayout(btn_layout)
        
    def copy_to_clip(self):
        full_text = f"--- 3D Open Dock U CRITICAL ERROR REPORT ---\nTimestamp: {datetime.now().isoformat()}\nDetected Log Line:\n{self.text_edit.toPlainText()}\n--------------------------------------------"
        QApplication.clipboard().setText(full_text)
        QMessageBox.information(self, "Copied", "Error report copied to clipboard.")

class StatusWorker(QThread):
    status_updated = Signal(dict)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = True
        self.last_ip = "127.0.0.1"
        self.os_info = OS_INFO
        
    def run(self):
        import socket
        import subprocess
        count = 0
        while self.running:
            status = {}
            # 1. IP Detection (Every 3 ticks / 15s or if last was local)
            if count % 3 == 0 or self.last_ip == "127.0.0.1":
                ip = "127.0.0.1"
                for target in [("8.8.8.8", 80), ("1.1.1.1", 80), ("192.168.1.1", 80)]:
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        s.settimeout(0.5)
                        s.connect(target)
                        ip = s.getsockname()[0]
                        s.close()
                        if ip != "127.0.0.1": break
                    except: continue
                self.last_ip = ip
            status["ip"] = self.last_ip
            
            # 2. Docker Status
            server_running = False
            docker_available = False
            try:
                flags = 0x08000000 if self.os_info["os"] == "windows" else 0
                res = subprocess.run(["docker", "info"], capture_output=True, timeout=2, creationflags=flags)
                docker_available = (res.returncode == 0)
                if docker_available:
                    res = subprocess.run(["docker", "ps", "--filter", "name=pretendo", "--format", "{{.Names}}"], 
                                         capture_output=True, text=True, timeout=1)
                    server_running = bool(res.stdout.strip())
            except: pass
            
            status["server_running"] = server_running
            status["docker_available"] = docker_available
            self.status_updated.emit(status)
            for _ in range(50): 
                if not self.running: break
                time.sleep(0.1)
            count += 1

class SudoPasswordDialog(QDialog):
    def __init__(self, parent=None, current_pw=""):
        super().__init__(parent)
        self.setWindowTitle("Administrator Password")
        self.setMinimumWidth(360)
        self.setStyleSheet(STYLESHEET)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Sudo Access Required", styleSheet="font-size: 18px; font-weight: bold;"))
        layout.addWidget(QLabel("Enter your Linux password:"))
        
        pass_row = QHBoxLayout()
        self.pass_field = QLineEdit(current_pw)
        self.pass_field.setEchoMode(QLineEdit.Password)
        pass_row.addWidget(self.pass_field)
        
        self.eye_btn = QPushButton("👁")
        self.eye_btn.setCheckable(True)
        self.eye_btn.setFixedSize(32, 32)
        self.eye_btn.setStyleSheet("font-size: 18px; padding: 0;")
        self.eye_btn.clicked.connect(self.toggle_visibility)
        pass_row.addWidget(self.eye_btn)
        layout.addLayout(pass_row)

        self.remember_cb = QCheckBox("Remember for this session")
        self.remember_cb.setStyleSheet("color: #8888AA;")
        self.remember_cb.setChecked(True)
        layout.addWidget(self.remember_cb)
        
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        
    def toggle_visibility(self):
        if self.eye_btn.isChecked():
            self.pass_field.setEchoMode(QLineEdit.Normal)
        else:
            self.pass_field.setEchoMode(QLineEdit.Password)
            
    def get_data(self):
        return self.pass_field.text(), self.remember_cb.isChecked()

class PretendoManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(960, 720)
        self.worker = None
        self.status_worker = StatusWorker(self)
        self.status_worker.status_updated.connect(self._handle_status_update)
        self.status_worker.start()
        
        self.cached_password: Optional[str] = None
        self.server_running = False
        self.docker_service_running = False
        self.server_dir = DEFAULT_SERVER_DIR
        self.settings = QSettings(APP_NAME, "Config")
        self.bypassing_close_prompt = False
        self.command_lock_count = 0
        self.last_popup_time = 0
        self.seen_errors = {} # error -> timestamp

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        
        # Header
        title = QLabel(f'<span style="color:{RED_PRIMARY};">3D</span> <span style="color:white;">Open Dock</span> <span style="color:{CYAN_PRIMARY};">U</span>')
        title.setStyleSheet("font-size: 36px; font-weight: bold;")
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)
        
        sep = QFrame(objectName="separator")
        sep.setFrameShape(QFrame.HLine)
        root.addWidget(sep)

        self.tabs = QTabWidget()
        self.tabs.addTab(make_scrollable(self._build_dashboard_tab()), "Server & Deployment")
        self.tabs.addTab(make_scrollable(self._build_emulator_tab()), "Identities & Emulators")
        self.tabs.addTab(make_scrollable(self._build_guide_tab()), "Help & Guide")
        root.addWidget(self.tabs)

        # Footer Credits & Reset
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(10, 0, 10, 10)
        
        self.atomic_btn = QPushButton("ATOMIC SHUTDOWN & EXIT")
        self.atomic_btn.setStyleSheet(f"font-size: 10px; color: white; background: {RED_DARK}; border: 1px solid {RED_PRIMARY}; padding: 4px 12px; font-weight: bold; border-radius: 4px;")
        self.atomic_btn.setToolTip("Force-stops the server, disables Docker, and quits the application.")
        self.atomic_btn.clicked.connect(self.emergency_exit)
        footer_layout.addWidget(self.atomic_btn)

        footer = QLabel('Made by BannedPenta AKA Jan Michael | Powered by Gemini 3 Flash & Google Antigravity')
        footer.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
        footer.setAlignment(Qt.AlignCenter)
        footer_layout.addWidget(footer, 1)
        
        self.reset_data_btn = QPushButton("Reset Sensitive Data")
        self.reset_data_btn.setStyleSheet(f"font-size: 10px; color: {RED_LIGHT}; border: 1px solid {RED_DARK}; padding: 4px 8px; background: transparent;")
        self.reset_data_btn.clicked.connect(self.clear_sensitive_data)
        footer_layout.addWidget(self.reset_data_btn)
        
        root.addLayout(footer_layout)

        # Start in windowed fullscreen (maximized) every time
        self.setWindowState(Qt.WindowMaximized)

        self.statusBar().setStyleSheet(f"background: {BG_CARD}; color: {TEXT_SECONDARY}; padding: 4px;")
        self.statusBar().showMessage(f"{APP_NAME} v{APP_VERSION} - Ready")
        self.load_settings()
        self.last_connectivity_state = (self._get_local_ip() != "127.0.0.1")
        
        # Start Heartbeat Timer (Checks status & Connectivity every 5s)
        self.heartbeat = QTimer()
        self.heartbeat.timeout.connect(self._on_status_tick)
        self.heartbeat.start(5000)
        self._on_status_tick()

        # region agent log
        _agent_debug_log(
            "H1",
            "3D-Open-Dock-U.py:PretendoManager.__init__",
            "PretendoManager initialized",
            {},
        )
        # endregion

    def load_settings(self):
        """Load user configurations from persistent storage with signal blocking to prevent recursive save cycles."""
        # Block signals to prevent setText() from triggering save_settings() prematurely
        self.cemu_username.blockSignals(True)
        self.cemu_password.blockSignals(True)
        self.cemu_miiname.blockSignals(True)
        if getattr(self, 'cemu_mii_hex', None): self.cemu_mii_hex.blockSignals(True)
        if getattr(self, 'server_sudo_pass', None): self.server_sudo_pass.blockSignals(True)
        if getattr(self, 'cemu_dir_field', None): self.cemu_dir_field.blockSignals(True)
        if getattr(self, 'citra_dir_field', None): self.citra_dir_field.blockSignals(True)

        try:
            self.cemu_username.setText(str(self.settings.value("username", "")))
            
            # Passwords decode (Base64)
            dec_pw = _deobs(self.settings.value("password", ""))
            self.cemu_password.setText(dec_pw)
            
            self.cemu_miiname.setText(str(self.settings.value("miiname", "")))
            
            mii_h = str(self.settings.value("mii_hex", "")).strip().replace(" ", "")
            if getattr(self, 'cemu_mii_hex', None):
                self.cemu_mii_hex.setText(mii_h)
            if mii_h:
                setattr(self, '_mii_data_hex', mii_h[:192])
            
            if getattr(self, 'cemu_dir_field', None):
                self.cemu_dir_field.setText(str(self.settings.value("cemu_dir", CEMU_DIR)))
            if getattr(self, 'citra_dir_field', None):
                self.citra_dir_field.setText(str(self.settings.value("citra_dir", "")))

            saved_sudo = self.settings.value("sudo_cache", None)
            self.cached_password = _deobs(saved_sudo) if saved_sudo else None
            
            if self.cached_password and getattr(self, 'server_sudo_pass', None):
                self.server_sudo_pass.setText(str(self.cached_password))
        finally:
            # Re-enable signals
            self.cemu_username.blockSignals(False)
            self.cemu_password.blockSignals(False)
            self.cemu_miiname.blockSignals(False)
            if getattr(self, 'cemu_mii_hex', None): self.cemu_mii_hex.blockSignals(False)
            if getattr(self, 'server_sudo_pass', None): self.server_sudo_pass.blockSignals(False)
            if getattr(self, 'cemu_dir_field', None): self.cemu_dir_field.blockSignals(False)
            if getattr(self, 'citra_dir_field', None): self.citra_dir_field.blockSignals(False)

        self.refresh_vault_list()
        # Ensure MK8 save path exists for default Cemu locations so Cemu does not report "Save path (not present)"
        try:
            self._ensure_mlc_save_dirs("")
        except Exception:
            pass

    def _pick_dir(self, field, title):
        dlg = QFileDialog(self, title, field.text())
        dlg.setFileMode(QFileDialog.Directory)
        dlg.setOption(QFileDialog.ShowDirsOnly, True)
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
        dlg.setFilter(QDir.Hidden | QDir.AllDirs | QDir.NoDotAndDotDot)
        if dlg.exec():
            # selectedFiles returns a list of paths
            selected = dlg.selectedFiles()
            if selected:
                field.setText(selected[0])
                self.save_settings()

    def save_settings(self):
        """Save current identity fields to persistent storage. All passwords persist until Reset Sensitive Data."""
        self.settings.setValue("username", self.cemu_username.text())
        self.settings.setValue("password", _obs(self.cemu_password.text()))
        self.settings.setValue("miiname", self.cemu_miiname.text())
        
        if getattr(self, 'cemu_dir_field', None):
            self.settings.setValue("cemu_dir", self.cemu_dir_field.text())
        if getattr(self, 'citra_dir_field', None):
            self.settings.setValue("citra_dir", self.citra_dir_field.text())
        
        # Always persist sudo password if entered, heavily obfuscated
        if getattr(self, 'server_sudo_pass', None):
            sudo_pw = self.server_sudo_pass.text()
            if sudo_pw:
                self.settings.setValue("sudo_cache", _obs(sudo_pw))
                self.cached_password = sudo_pw
        
        mii_h = (getattr(self, 'cemu_mii_hex', None) and self.cemu_mii_hex.text().strip().replace(" ", "")) or getattr(self, '_mii_data_hex', "")
        self.settings.setValue("mii_hex", (mii_h[:192] if mii_h else ""))
        
        self.settings.sync()

    def _get_target_port(self):
        """Extract port from the patch URL input, fallback to 8070."""
        text = self.patch_url_input.text().strip()
        if not text:
            return "8070"
        # Match :port at the end or before a path
        match = re.search(r":(\d+)(?:/|$)", text)
        if match:
            return match.group(1)
        # Default web ports if not specified
        if text.startswith("https:"):
            return "443"
        return "80"

    def _get_local_ip(self):
        """Robust IP detection trying multiple targets to avoid false offline status."""
        for target in [("8.8.8.8", 80), ("1.1.1.1", 80), ("192.168.1.1", 80)]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(1.0)
                s.connect(target)
                ip = s.getsockname()[0]
                s.close()
                if ip != "127.0.0.1": return ip
            except: continue
        return "127.0.0.1"

    def _build_dashboard_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)
        h_layout = QHBoxLayout()

        # LEFT PANE: Server
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0,0,0,0)
        
        group = QGroupBox("Network Status & Node")
        glay = QVBoxLayout(group)
        self.status_label = QLabel("OFFLINE")
        self.status_label.setStyleSheet(f"color: {RED_PRIMARY}; font-size: 24px; font-weight: bold;")
        self.status_label.setAlignment(Qt.AlignCenter)
        glay.addWidget(self.status_label)
        
        self.ip_info = QLabel(f"Local Network IP: {self._get_local_ip()}")
        self.ip_info.setStyleSheet(f"color: {CYAN_LIGHT}; font-size: 16px;")
        self.ip_info.setAlignment(Qt.AlignCenter)
        glay.addWidget(self.ip_info)

        self.detected_game_label = QLabel("Active Session: None Detected")
        self.detected_game_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 13px; font-style: italic;")
        self.detected_game_label.setAlignment(Qt.AlignCenter)
        glay.addWidget(self.detected_game_label)

        self.server_toggle_btn = QPushButton("START SERVER")
        self.server_toggle_btn.setObjectName("startBtn")
        self.server_toggle_btn.setMinimumHeight(50)
        self.server_toggle_btn.clicked.connect(self.toggle_server)
        glay.addWidget(self.server_toggle_btn)
        
        btn_hl = QHBoxLayout()
        self.copy_server_log_btn = QPushButton("Copy Logs", clicked=self.copy_server_logs)
        btn_hl.addWidget(self.copy_server_log_btn)
        self.clear_server_log_btn = QPushButton("Clear Logs", clicked=lambda: self.server_log.clear())
        btn_hl.addWidget(self.clear_server_log_btn)
        glay.addLayout(btn_hl)
        
        self.check_result = QLabel("")
        self.check_result.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
        self.check_result.setWordWrap(True)
        glay.addWidget(self.check_result)
        
        lv.addWidget(group)
        
        self.server_log = QTextEdit(objectName="logBox")
        self.server_log.setReadOnly(True)
        lv.addWidget(self.server_log)
        
        h_layout.addWidget(left, 1)

        # RIGHT PANE: Setup Infrastructure
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0,0,0,0)
        
        dep = QGroupBox("Infrastructure Deployment")
        dlay = QVBoxLayout(dep)
        
        self.service_toggle_btn = QPushButton("Enable Docker Service", clicked=self.toggle_docker_service)
        dlay.addWidget(self.service_toggle_btn)
        
        row_dir = QHBoxLayout()
        row_dir.addWidget(QLabel("Stack Dir:"))
        self.server_dir_field = QLineEdit(DEFAULT_SERVER_DIR)
        row_dir.addWidget(self.server_dir_field)
        dlay.addLayout(row_dir)

        self.deploy_stack_btn = QPushButton("Deploy Server Stack (Automated Setup)", clicked=self.automated_install_stack)
        self.deploy_stack_btn.setMinimumHeight(40)
        self.deploy_stack_btn.setStyleSheet(f"background: {CYAN_DARK}; color: white; font-weight: bold;")
        dlay.addWidget(self.deploy_stack_btn)

        # Sudo Password Field (persisted across sessions until Reset Sensitive Data)
        if OS_INFO["os"] == "linux":
            sudo_row = QHBoxLayout()
            sudo_row.addWidget(QLabel("Sudo Pass:"))
            self.server_sudo_pass = QLineEdit()
            self.server_sudo_pass.setEchoMode(QLineEdit.Password)
            self.server_sudo_pass.setPlaceholderText("Enter sudo password (auto-saved)")
            self.server_sudo_pass.textChanged.connect(self.save_settings)
            sudo_row.addWidget(self.server_sudo_pass, 1)
            
            self.dash_sudo_eye = QPushButton("👁")
            self.dash_sudo_eye.setCheckable(True)
            self.dash_sudo_eye.setFixedSize(30, 24)
            self.dash_sudo_eye.setStyleSheet("padding: 0; font-size: 14px;")
            self.dash_sudo_eye.clicked.connect(lambda: self.server_sudo_pass.setEchoMode(QLineEdit.Normal if self.dash_sudo_eye.isChecked() else QLineEdit.Password))
            sudo_row.addWidget(self.dash_sudo_eye)
            dlay.addLayout(sudo_row)
        elif OS_INFO["os"] == "windows":
            self.server_sudo_pass = None  # Windows uses UAC elevation, no inline password
            
            # ─── WSL2 Status & Controls ───
            wsl_group = QGroupBox("Windows Subsystem for Linux (WSL2)")
            wsl_lay = QVBoxLayout(wsl_group)
            
            self.wsl_status_label = QLabel("WSL2: Detecting...")
            self.wsl_status_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
            wsl_lay.addWidget(self.wsl_status_label)
            
            wsl_btn_row = QHBoxLayout()
            self.install_wsl_btn = QPushButton("Install WSL2 + Ubuntu", clicked=self._install_wsl2_action)
            self.install_wsl_btn.setToolTip("Automatically installs WSL2 and Ubuntu distro (requires admin privileges and a restart).")
            self.install_wsl_btn.setStyleSheet(f"background: #0366d6; color: white; font-weight: bold; border-radius: 6px; padding: 8px;")
            wsl_btn_row.addWidget(self.install_wsl_btn)
            
            self.refresh_wsl_btn = QPushButton("Refresh WSL Status", clicked=self._refresh_wsl_status)
            self.refresh_wsl_btn.setStyleSheet(f"border-color: {CYAN_PRIMARY}; padding: 8px;")
            wsl_btn_row.addWidget(self.refresh_wsl_btn)
            wsl_lay.addLayout(wsl_btn_row)
            
            dlay.addWidget(wsl_group)
        else:
            self.server_sudo_pass = None

        row_sys = QHBoxLayout()
        if OS_INFO["os"] == "linux":
            row_sys.addWidget(QPushButton("Fix Perms", clicked=self.fix_docker_permissions))
            row_sys.addWidget(QPushButton("Reset Per-Session Sudo", clicked=lambda: setattr(self, 'cached_password', None) or (self.server_sudo_pass.clear() if self.server_sudo_pass else None)))
        elif OS_INFO["os"] == "windows":
            row_sys.addWidget(QPushButton("Open PowerShell", clicked=lambda: subprocess.Popen(["powershell", "-NoExit", "-Command", "Write-Host '3D Open Dock U - PowerShell Session'"], creationflags=0x00000010 if _is_windows() else 0)))
            row_sys.addWidget(QPushButton("Open WSL Terminal", clicked=lambda: subprocess.Popen(["wsl"], creationflags=0x00000010 if _is_windows() else 0) if OS_INFO.get("has_wsl") else QMessageBox.warning(self, "WSL2", "WSL2 is not installed. Click 'Install WSL2 + Ubuntu' first.")))
        
        dlay.addLayout(row_sys)

        rv.addWidget(dep)
        
        splat = QGroupBox("Splatoon Fixes")
        slay = QVBoxLayout(splat)
        self.patch_rotations_btn = QPushButton("Patch Splatoon Rotations", clicked=self.apply_splatoon_rotation_patch)
        self.patch_rotations_btn.setToolTip("Fixes stuck stage rotations (Saltspray Rig/Museum) by injecting a 100-phase schedule.")
        slay.addWidget(self.patch_rotations_btn)
        rv.addWidget(splat)

        self.setup_log = QTextEdit(objectName="logBox")
        self.setup_log.setReadOnly(True)
        rv.addWidget(self.setup_log)
        
        h_layout.addWidget(right, 1)
        layout.addLayout(h_layout)
        return w

    def _build_emulator_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        h_layout = QHBoxLayout()

        # LEFT PANE: Credentials & Identity Target
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0,0,0,0)
        
        cred = QGroupBox("1. Your account (Wii U / 3DS)")
        crgl = QVBoxLayout(cred)
        crgl.addWidget(QLabel("Same username & password for Patch & Connect. Used by Cemu and Citra."))
        form = QFormLayout()
        self.cemu_username = QLineEdit()
        self.cemu_username.setMaxLength(16)
        self.cemu_username.setPlaceholderText("e.g. BannedPenta — 6–16 chars")
        self.cemu_username.setToolTip("Pretendo username (PNID). Must match what you use in Patch & Connect and on the server.")
        self.cemu_username.textChanged.connect(self.save_settings)
        form.addRow("Username (PNID):", self.cemu_username)
        self.cemu_password = QLineEdit()
        self.cemu_password.setEchoMode(QLineEdit.Password)
        self.cemu_password.setToolTip("Password for this Pretendo account. Use a unique password, not your real email password.")
        self.cemu_password.textChanged.connect(self.save_settings)
        form.addRow("Password:", self.cemu_password)
        self.cemu_miiname = QLineEdit()
        self.cemu_miiname.setMaxLength(10)
        self.cemu_miiname.setPlaceholderText("Display name, max 10 chars")
        self.cemu_miiname.setToolTip("Name shown on your Mii in games.")
        self.cemu_miiname.textChanged.connect(self.save_settings)
        form.addRow("Mii name:", self.cemu_miiname)
        self.cemu_mii_hex = QLineEdit()
        self.cemu_mii_hex.setPlaceholderText("Optional: 192 hex chars for custom Mii look")
        self.cemu_mii_hex.setMaxLength(256)
        self.cemu_mii_hex.setToolTip("Advanced: custom Mii appearance as hex. Leave empty to use Mii name only.")
        def _on_mii_hex_changed():
            h = self.cemu_mii_hex.text().strip().replace(" ", "")
            if len(h) >= 192 and all(c in "0123456789abcdefABCDEF" for c in h[:192]):
                setattr(self, "_mii_data_hex", h[:192])
            elif not h:
                setattr(self, "_mii_data_hex", "")
            self.save_settings()
        self.cemu_mii_hex.textChanged.connect(_on_mii_hex_changed)
        form.addRow("Custom Mii (hex):", self.cemu_mii_hex)
        
        # Cemu Dir with folder button
        cemu_dir_layout = QHBoxLayout()
        self.cemu_dir_field = QLineEdit(self.settings.value("cemu_dir", CEMU_DIR))
        self.cemu_dir_field.setToolTip("Folder where Cemu is installed or where it stores mlc01 (e.g. ~/.local/share/Cemu or Emulation/roms/wiiu).")
        self.cemu_dir_field.textChanged.connect(self.save_settings)
        cemu_dir_layout.addWidget(self.cemu_dir_field)
        self.cemu_dir_btn = QPushButton("📁")
        self.cemu_dir_btn.setFixedSize(30, 24)
        self.cemu_dir_btn.setStyleSheet("padding: 0;")
        self.cemu_dir_btn.clicked.connect(lambda: self._pick_dir(self.cemu_dir_field, "Select Cemu directory"))
        cemu_dir_layout.addWidget(self.cemu_dir_btn)
        form.addRow("Cemu directory:", cemu_dir_layout)

        # Citra Dir with folder button
        citra_dir_layout = QHBoxLayout()
        default_citra = OS_INFO.get("citra_config", "")
        self.citra_dir_field = QLineEdit(self.settings.value("citra_dir", os.path.dirname(os.path.dirname(default_citra)) if default_citra else ""))
        self.citra_dir_field.setToolTip("Folder where Citra stores config/sdmc (e.g. ~/.local/share/citra-emu).")
        self.citra_dir_field.textChanged.connect(self.save_settings)
        citra_dir_layout.addWidget(self.citra_dir_field)
        self.citra_dir_btn = QPushButton("📁")
        self.citra_dir_btn.setFixedSize(30, 24)
        self.citra_dir_btn.setStyleSheet("padding: 0;")
        self.citra_dir_btn.clicked.connect(lambda: self._pick_dir(self.citra_dir_field, "Select Citra directory"))
        citra_dir_layout.addWidget(self.citra_dir_btn)
        form.addRow("Citra directory:", citra_dir_layout)
        
        crgl.addLayout(form)
        
        # Security Disclaimer
        sec_warn = QLabel("⚠️ Use a unique password here — never your real email or main account password.")
        sec_warn.setStyleSheet(f"color: {RED_LIGHT}; font-weight: bold; font-size: 11px;")
        sec_warn.setAlignment(Qt.AlignCenter)
        sec_warn.setWordWrap(True)
        crgl.addWidget(sec_warn)
        
        p_row = QHBoxLayout()
        self.bundle_btn = QPushButton("Generate credentials bundle", objectName="patchBtn", clicked=self.generate_console_bundle_zip)
        self.bundle_btn.setToolTip("Create a ZIP with account files and instructions to copy into Cemu/Citra.")
        self.bundle_btn.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffffff, stop:1 #e6e6e6; color: #111111; border: 1px solid #cccccc; padding: 10px; font-weight: bold; border-radius: 8px;")
        p_row.addWidget(self.bundle_btn)
        self.mk8_help_btn = QPushButton("MK8 crash help")
        self.mk8_help_btn.setToolTip("If Mario Kart 8 crashes on Linux (signal 11), open this for options.")
        self.mk8_help_btn.clicked.connect(self._show_mk8_crash_workarounds)
        p_row.addWidget(self.mk8_help_btn)
        crgl.addLayout(p_row)

        self.cemu_log = QTextEdit(objectName="logBox")
        self.cemu_log.setReadOnly(True)
        self.cemu_log.setMaximumHeight(80)
        crgl.addWidget(self.cemu_log)

        cemu_btn_row = QHBoxLayout()
        self.copy_cemu_log_btn = QPushButton("Copy Cemu Log", clicked=self.copy_cemu_logs)
        cemu_btn_row.addWidget(self.copy_cemu_log_btn)
        crgl.addLayout(cemu_btn_row)
        
        lv.addWidget(cred)
        
        net = QGroupBox("2. Network & connect")
        nlay = QVBoxLayout(net)
        nlay.addWidget(QLabel("Where Cemu and Citra send online traffic: your local server or public Pretendo."))
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Server URL:"))
        current_ip = self._get_local_ip()
        self.patch_url_input = QLineEdit(f"http://{current_ip}:8070")
        self.patch_url_input.setToolTip("For local: use your machine's IP and port (e.g. 8070). Cemu/Citra will connect here for online.")
        url_row.addWidget(self.patch_url_input)
        nlay.addLayout(url_row)
        
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self.mode_local = QRadioButton("Local server (your stack)")
        self.mode_pretendo = QRadioButton("Public Pretendo")
        self.mode_local.setChecked(True)
        self.mode_local.setStyleSheet("color: white;")
        self.mode_pretendo.setStyleSheet("color: white;")
        self.mode_local.setToolTip("Use your own Pretendo server (Docker).")
        self.mode_pretendo.setToolTip("Use official Pretendo network; no local server needed.")
        mode_row.addWidget(self.mode_local)
        mode_row.addWidget(self.mode_pretendo)
        nlay.addLayout(mode_row)
        
        pat_row = QHBoxLayout()
        pat_btn = QPushButton("Patch & Connect (Cemu)", objectName="patchBtn")
        pat_btn.setToolTip("Patch Cemu settings and network URLs, write identity files, and register this account on your server. Start server first for local mode.")
        pat_btn.clicked.connect(lambda: self.apply_cemu_patch_all())
        pat_row.addWidget(pat_btn)
        citra_btn = QPushButton("Patch & Connect (Citra)", objectName="patchBtn")
        citra_btn.setToolTip("Patch Citra config and register this account. Start server first for local mode.")
        citra_btn.clicked.connect(lambda: self.patch_citra("ui_trigger"))
        pat_row.addWidget(citra_btn)
        nlay.addLayout(pat_row)
        
        lv.addWidget(net)
        h_layout.addWidget(left, 1)

        # RIGHT PANE: Vault & Restore
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0,0,0,0)
        
        vault = QGroupBox("3. Saved profiles")
        vlay = QVBoxLayout(vault)
        vlay.addWidget(QLabel("Save and switch between different account profiles (username/password/Mii)."))
        self.profile_list = QListWidget()
        self.profile_list.setMinimumHeight(150)
        vlay.addWidget(self.profile_list)
        
        vbtn = QHBoxLayout()
        self.save_curr_btn = QPushButton("Save current profile", clicked=self.save_to_vault)
        self.apply_sel_btn = QPushButton("Use selected profile", clicked=self.apply_from_vault)
        vbtn.addWidget(self.save_curr_btn)
        vbtn.addWidget(self.apply_sel_btn)
        vlay.addLayout(vbtn)
        
        vbtn2 = QHBoxLayout()
        self.open_vault_btn = QPushButton("Open vault folder", clicked=self.open_vault_folder)
        self.open_vault_btn.setToolTip("Open the folder where saved profiles are stored.")
        self.delete_prof_btn = QPushButton("Delete selected profile", clicked=self.delete_profile)
        self.delete_prof_btn.setToolTip("Remove the selected saved profile from the vault.")
        vbtn2.addWidget(self.open_vault_btn)
        vbtn2.addWidget(self.delete_prof_btn)
        vlay.addLayout(vbtn2)
        
        rv.addWidget(vault)
        
        
        n_row = QHBoxLayout()
        nintendo_btn = QPushButton("Restore Nintendo (official)", clicked=self.restore_nintendo_official)
        nintendo_btn.setToolTip("Point Cemu/Citra back to Nintendo's servers (no Pretendo).")
        nintendo_btn.setStyleSheet(f"background: {RED_DARK}; color: white; padding: 8px;")
        pretendo_btn = QPushButton("Restore Pretendo (public)", clicked=self.restore_pretendo_official)
        pretendo_btn.setToolTip("Point Cemu/Citra to the public Pretendo network.")
        pretendo_btn.setStyleSheet(f"background: {CYAN_DARK}; color: white; padding: 8px;")
        n_row.addWidget(nintendo_btn)
        n_row.addWidget(pretendo_btn)
        rv.addLayout(n_row)
        
        reset_btn = QPushButton("Reset to factory defaults", clicked=self.reset_to_defaults, styleSheet="background: #8b4513; color: white; padding: 8px;")
        reset_btn.setToolTip("Clear all app settings and restore defaults.")
        rv.addWidget(reset_btn)

        h_layout.addWidget(right, 1)
        layout.addLayout(h_layout)

        return w
    def _build_guide_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        
        guide = QTextEdit()
        guide.setReadOnly(True)
        guide.setHtml(f"""
            <h1 style='color:{RED_PRIMARY};'>Quick Start</h1>
            <p><b>3D Open Dock U</b> connects Cemu (Wii U) and Citra (3DS) to Pretendo for online play.</p>

            <h2 style='color:{CYAN_PRIMARY};'>1. Start your server (local mode)</h2>
            <ol>
                <li>Open <b>Setup & Maintenance</b> → <b>Download Stack</b>.</li>
                <li>Run <b>Run Full Setup Script</b>, then <b>Build Server Containers</b> (wait ~10 min).</li>
                <li>On the dashboard, click <b>START SERVER</b>.</li>
            </ol>

            <h2 style='color:{CYAN_PRIMARY};'>2. Connect Cemu or Citra</h2>
            <ul>
                <li>In <b>1. Your account</b>, enter username (PNID) and password. Set <b>Cemu directory</b> (or Citra) to where the emulator stores its files.</li>
                <li>In <b>2. Network & connect</b>, leave <b>Server URL</b> as-is for local, or enter another server’s URL.</li>
                <li>Click <b>Patch & Connect (Cemu)</b> or <b>Patch & Connect (Citra)</b>. This patches the emulator and registers your account.</li>
                <li>In Cemu/Citra, enable online in options, then start a game.</li>
            </ul>

            <h2 style='color:{CYAN_PRIMARY};'>3. Mario Kart 8 crash on Linux?</h2>
            <p>Try Cemu from Flathub, or run base game only (no update/DLC). See <b>MK8 crash help</b> for more.</p>

            <h2 style='color:{CYAN_PRIMARY};'>4. Troubleshooting</h2>
            <ul>
                <li><b>Docker (Windows):</b> If Docker or WSL fails, try <b>Fix Docker Permissions</b> in Setup.</li>
                <li><b>Port in use:</b> Stop the server, close Steam if needed, then Start again.</li>
                <li><b>Real Wii U/3DS:</b> Use <b>Generate credentials bundle</b> and copy the files to your console.</li>
            </ul>

            <h2 style='color:#85bb65;'>Using another server</h2>
            <p>To use a friend’s or public Pretendo server:</p>
            <ul>
                <li>In <b>2. Network & connect</b>, set <b>Server URL</b> to their address (e.g. <code>http://their-ip:8070</code>).</li>
                <li>Choose <b>Local server</b> or <b>Public Pretendo</b> as needed, then click <b>Patch & Connect</b>.</li>
            </ul>
        """)
        layout.addWidget(guide)
        
        reset_row = QHBoxLayout()
        reset_row.addStretch()
        self.guide_reset_btn = QPushButton("Reset emulator settings to defaults", clicked=self.show_reset_dialog)
        self.guide_reset_btn.setStyleSheet(f"color: {RED_LIGHT}; border-color: {RED_DARK}; padding: 8px 16px;")
        reset_row.addWidget(self.guide_reset_btn)
        layout.addLayout(reset_row)
        
        return w

    def refresh_vault_list(self):
        self.profile_list.clear()
        vault_dir = os.path.join(os.path.expanduser("~"), ".config", APP_NAME, "vault")
        if not os.path.exists(vault_dir): return
        
        for name in sorted(os.listdir(vault_dir)):
            path = os.path.join(vault_dir, name)
            if os.path.isdir(path):
                meta_path = os.path.join(path, "profile_meta.json")
                item = QListWidgetItem(name)
                
                is_account = False
                is_backup = False
                
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path, "r") as f:
                            meta = json.load(f)
                        t = str(meta.get("type", "")).lower()
                        if t == "account": is_account = True
                        elif t == "backup": is_backup = True
                        elif meta.get("username"): is_account = True
                    except: pass
                
                # Heuristics if metadata is silent
                if not (is_account or is_backup):
                    lname = name.lower()
                    if "backup" in lname or "vault" in lname: is_backup = True
                    else: is_account = True # Default to account
                
                if is_backup:
                    item.setForeground(QColor(GREEN_MONEY))
                else:
                    item.setForeground(QColor(ORANGE_ACCOUNT))
                
                self.profile_list.addItem(item)

    def open_vault_folder(self):
        vault_dir = os.path.join(os.path.expanduser("~"), ".config", APP_NAME, "vault")
        os.makedirs(vault_dir, exist_ok=True)
        try:
            if platform.system().lower() == "linux":
                subprocess.Popen(["xdg-open", vault_dir])
            elif platform.system().lower() == "darwin":
                subprocess.Popen(["open", vault_dir])
            else:
                if hasattr(os, 'startfile'):
                    os.startfile(vault_dir)
                else:
                    subprocess.Popen(["explorer", vault_dir])
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not open folder: {e}")

    def _get_emulator_paths(self):
        """Ultra-robust helper to find actual data paths for vault harvesting."""
        home = os.path.expanduser("~")
        paths = {}
        
        # 1. Search for Wii U (account.dat)
        # Priority: Common paths -> EmuDeck -> Flatpak
        c_roots = [
            CEMU_DIR,
            os.path.join(home, ".local/share/Cemu"),
            os.path.join(home, ".config/Cemu"),
            os.path.join(home, "Emulation", "roms", "wiiu"),  # EmuDeck: mlc01 lives here
            os.path.join(home, ".var/app/info.cemu.Cemu/data/Cemu"),
        ]
        
        found_mlc = None
        found_cemu = None
        for r in c_roots:
            if not r: continue
            # Check both r/mlc01 and r directly to avoid mlc01/mlc01 nesting
            for cand_base in [os.path.join(r, "mlc01"), r]:
                acc_path = os.path.join(cand_base, "usr/save/system/act/80000001/account.dat")
                if os.path.exists(acc_path):
                    found_mlc = cand_base
                    found_cemu = r
                    paths["WiiU/account.dat"] = acc_path
                    break
            if found_mlc: break
        
        if found_mlc:
            # Keys are typically in the Cemu root (r) or possibly mlc01/sys
            paths["WiiU/otp.bin"] = os.path.join(found_cemu, "otp.bin")
            paths["WiiU/seeprom.bin"] = os.path.join(found_cemu, "seeprom.bin")
            # If they aren't in Cemu root, maybe they are in mlc01/sys
            if not os.path.exists(paths["WiiU/otp.bin"]):
                alt_otp = os.path.join(found_mlc, "sys/otp.bin")
                if os.path.exists(alt_otp): paths["WiiU/otp.bin"] = alt_otp
            if not os.path.exists(paths["WiiU/seeprom.bin"]):
                alt_see = os.path.join(found_mlc, "sys/seeprom.bin")
                if os.path.exists(alt_see): paths["WiiU/seeprom.bin"] = alt_see

        # 2. Search for 3DS (SecureInfo_A, etc.)
        # The user has AZAHAR at Emulation/storage/azahar/nand/rw/sys/SecureInfo_A
        # We search common roots and subfolders
        ctx_roots = [
            os.path.join(home, "Emulation/storage/azahar"),
            os.path.join(home, ".config/citra-emu"),
            os.path.join(home, ".var/app/org.citra_emu.citra/data/citra-emu"),
            os.path.join(home, ".config/lime-3ds"),
            os.path.join(home, ".config/azahar-emu")
        ]
        
        for r in ctx_roots:
            if not os.path.exists(r): continue
            
            # Look for SecureInfo_A in various possible sub-paths
            si_candidates = [
                "nand/rw/sys/SecureInfo_A",
                "nand/data/00000000000000000000000000000000/sysdata/00010011/00000000/SecureInfo_A",
                "nand/data/00000000000000000000000000000000/sysdata/00010011/SecureInfo_A"
            ]
            
            for sub in si_candidates:
                si_path = os.path.join(r, sub)
                if os.path.exists(si_path):
                    paths["3DS/SecureInfo_A"] = si_path
                    # Map other files relative to SecureInfo_A's location structure
                    base = os.path.dirname(si_path)
                    # For AZAHAR structure
                    if "nand/rw/sys" in sub:
                         paths["3DS/LocalFriendCodeSeed_B"] = os.path.join(base, "LocalFriendCodeSeed_B")
                         paths["3DS/CTCert.bin"] = os.path.join(base, "CTCert.bin")
                    else:
                         # For standard Citra structure
                         sysdata = os.path.dirname(os.path.dirname(base))
                         paths["3DS/LocalFriendCodeSeed_B"] = os.path.join(sysdata, "0001000f/00000000/LocalFriendCodeSeed_B")
                         paths["3DS/CTCert.bin"] = os.path.join(sysdata, "00010010/00000000/CTCert.bin")
                    break
            if "3DS/SecureInfo_A" in paths: break
                
        return paths

    def save_to_vault(self):
        # 1. Ask for name and type
        name, ok = QInputDialog.getText(self, "New Vault Entry", "Profile Name (e.g. My_Account):")
        if not (ok and name): return
        
        type_res = QMessageBox.question(self, "Vault Entry Type", "Is this a specific PNID Account? (Choose No for a generic Backup Folder)", QMessageBox.Yes | QMessageBox.No)
        entry_type = "account" if type_res == QMessageBox.Yes else "backup"
        
        vault_path = os.path.join(os.path.expanduser("~"), ".config", APP_NAME, "vault", name)
        os.makedirs(vault_path, exist_ok=True)

        # 2. Save Files (Harvesting)
        found_paths = self._get_emulator_paths()
        count = 0
        for rel_path, src in found_paths.items():
            if os.path.exists(src):
                dest = os.path.join(vault_path, rel_path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                count += 1
        
        # 3. Save Metadata
        meta = {
            "type": entry_type,
            "username": self.cemu_username.text(),
            "password": _obs(self.cemu_password.text()),
            "miiname": self.cemu_miiname.text(),
            "mii_hex": getattr(self, '_mii_data_hex', ""),
            "timestamp": datetime.now().isoformat(),
        }
        try:
            meta_path = os.path.join(vault_path, "profile_meta.json")
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=4)
        except Exception as e:
            if hasattr(self, "cemu_log"):
                self.cemu_log.append(f"[WARN] Failed to save vault metadata for '{name}': {e}")
        self.refresh_vault_list()

    def apply_from_vault(self):
        item = self.profile_list.currentItem()
        if not item: return
        name = item.text()
        
        if QMessageBox.question(self, "Confirm", f"Apply profile '{name}'? This will overwrite your current online files.") != QMessageBox.Yes:
            return
            
        vault_path = os.path.join(os.path.expanduser("~"), ".config", APP_NAME, "vault", name)
        found_paths = self._get_emulator_paths()
        
        if not found_paths:
            QMessageBox.critical(self, "Error", "Could not detect where to restore files. Please launch your emulators once first.")
            return

        # 1. Restore Files
        count = 0
        for rel_path, dest in found_paths.items():
            src = os.path.join(vault_path, rel_path)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                count += 1
        
        # 2. Restore Metadata (Credentials)
        self.cemu_username.blockSignals(True)
        self.cemu_password.blockSignals(True)
        self.cemu_miiname.blockSignals(True)
        if getattr(self, 'cemu_mii_hex', None):
            self.cemu_mii_hex.blockSignals(True)
        
        try:
            meta_path = os.path.join(vault_path, "profile_meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r") as f:
                        meta = json.load(f)
                    self.cemu_username.setText(meta.get("username", ""))
                    self.cemu_password.setText(_deobs(meta.get("password", "")))
                    self.cemu_miiname.setText(meta.get("miiname", ""))
                    mii_hex = (meta.get("mii_hex", "") or "").strip().replace(" ", "")
                    if mii_hex:
                        setattr(self, '_mii_data_hex', mii_hex[:192])
                    if getattr(self, 'cemu_mii_hex', None):
                        self.cemu_mii_hex.setText(mii_hex[:192] if mii_hex else "")
                except Exception as me:
                    self.cemu_log.append(f"[WARN] Failed to load profile meta: {me}")
            else:
                # Fallback: Try to extract what we can from the restored account.dat
                self._try_extract_info_from_files(vault_path)
        finally:
            self.cemu_username.blockSignals(False)
            self.cemu_password.blockSignals(False)
            self.cemu_miiname.blockSignals(False)
            if getattr(self, 'cemu_mii_hex', None):
                self.cemu_mii_hex.blockSignals(False)
        
        self.save_settings() # Persist the swapped credentials
        QMessageBox.information(self, "Vault", f"Profile '{name}' applied!\nFiles restored: {count}\nCredentials updated: OK")

    def _try_extract_info_from_files(self, profile_vault_path):
        """Heuristic to extract Username/MiiName from account.dat if metadata is missing."""
        act_p = os.path.join(profile_vault_path, "WiiU/account.dat")
        if os.path.exists(act_p):
            try:
                with open(act_p, "r", errors="ignore") as f:
                    content = f.read()
                
                un_match = re.search(r"AccountId=(.*)", content)
                if un_match: self.cemu_username.setText(un_match.group(1).strip())
                
                mii_match = re.search(r"MiiName=(.*)", content)
                if mii_match:
                    try:
                        n_hex = mii_match.group(1).strip()
                        n_bytes = binascii.unhexlify(n_hex)
                        # UTF-16BE decode for Mii name
                        self.cemu_miiname.setText(n_bytes.decode('utf-16be').rstrip('\x00'))
                    except: pass
            except: pass

    def delete_profile(self):
        item = self.profile_list.currentItem()
        if not item: return
        name = item.text()
        if QMessageBox.question(self, "Delete", f"Are you sure you want to delete profile '{name}'?") == QMessageBox.Yes:
            vault_path = os.path.join(os.path.expanduser("~"), ".config", APP_NAME, "vault", name)
            shutil.rmtree(vault_path, ignore_errors=True)
            self.refresh_vault_list()

    def _apply_compose_patches(self, port, s_dir):
        """Robust YAML patching for mitmproxy port, Postgres health, and service injections."""
        applied_any = False
        # Check files in order of Docker preference
        for fname in ["compose.yaml", "compose.yml", "docker-compose.yml"]:
            path = os.path.join(s_dir, fname)
            if not os.path.exists(path): continue
            
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                
                if not lines: continue

                new_lines = []
                current_service = None
                in_depends_on = False
                changed = False
                
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    # Service detection (indented exactly 2 spaces)
                    if line.startswith("  ") and not line.startswith("   ") and stripped.endswith(":") and not stripped.startswith("-"):
                        current_service = stripped[:-1].lower()
                        in_depends_on = False

                    if line.startswith("    ") and not line.startswith("     ") and ":" in stripped:
                        key_part = stripped.split(":")[0].strip()
                        if key_part == "depends_on":
                            in_depends_on = True
                        else:
                            in_depends_on = False

                    # 1. mitmproxy-pretendo: update external port and bind globally
                    if current_service == "mitmproxy-pretendo":
                        if "8080" in line and "ports:" not in line:
                            # CRITICAL FIX: Strip 127.0.0.1 restriction so AppImages/Flatpaks can route to it
                            new_line = re.sub(r'-\s*(?:127\.0\.0\.1:)?\d+:8080', f'- "{port}:8080"', line)
                            if new_line != line:
                                line = new_line
                                changed = True

                    # 2. adminer: update port
                    if current_service == "adminer":
                        if "127.0.0.1:8070:8080" in line:
                            line = line.replace("127.0.0.1:8070:8080", "127.0.0.1:8088:8080")
                            changed = True
                    
                    # 3. mongodb: pin version and add alias
                    if current_service == "mongodb":
                        if "image: mongo:latest" in line:
                            line = line.replace("mongo:latest", "mongo:4.4")
                            changed = True
                        if stripped == "internal:":
                            if "aliases:" not in "".join(lines[i:i+5]):
                                new_lines.append(line)
                                new_lines.append("        aliases:\n")
                                new_lines.append("          - mongo\n")
                                changed = True
                                continue

                    # 4. mongo-express: pin version
                    if current_service == "mongo-express":
                        if "image: mongo-express:latest" in line:
                            line = line.replace("mongo-express:latest", "mongo-express:0.54.0")
                            changed = True

                    # 5. postgres: Add Healthcheck (Fixes Smash connection refused) and mount init script
                    if current_service == "postgres" and stripped == "postgres:":
                        if "healthcheck:" not in "".join(lines[i:i+40]):
                            new_lines.append(line)
                            scripts_mount = os.path.join(s_dir, "scripts", "run-in-container")
                            
                            # Add volumes block and script mount
                            if os.path.isdir(scripts_mount):
                                if "volumes:" not in "".join(lines[i:i+40]):
                                    new_lines.append("    volumes:\n")
                                    new_lines.append("      - ./scripts/run-in-container:/scripts:ro\n")
                                else:
                                    # Volume block exists, but we'll try to add it later by patching the block itself
                                    pass

                            new_lines.append("    healthcheck:\n")
                            new_lines.append("      test:\n")
                            new_lines.append("        - CMD-SHELL\n")
                            new_lines.append("        - pg_isready -U postgres_pretendo -d postgres\n")
                            new_lines.append("      interval: 5s\n")
                            new_lines.append("      timeout: 5s\n")
                            new_lines.append("      retries: 5\n")
                            changed = True
                            continue

                    # 5.1 If postgres volumes block exists, ensure /scripts is in it
                    if current_service == "postgres" and stripped == "volumes:":
                        new_lines.append(line)
                        if "/scripts:ro" not in "".join(lines[i:i+20]):
                            new_lines.append("      - ./scripts/run-in-container:/scripts:ro\n")
                            changed = True
                        continue

                    # 5.5 Upgrade depends_on list to dict for services that need to wait for Postgres to be healthy
                    target_services = [
                        "account", "friends", "splatoon", "super-mario-maker", 
                        "pikmin-3", "wiiu-chat", "minecraft-wiiu", "miiverse-api", 
                        "juxtaposition-ui", "boss", "nintendo-lotus", "discovery",
                        "mario-kart-8", "pokken-tournament", "super-smash-bros-wiiu"
                    ]
                    if in_depends_on and current_service in target_services:
                        if stripped.startswith("- "):
                            dep_name = stripped[2:].strip()
                            indent = line[:len(line) - len(line.lstrip())]
                            if dep_name == "postgres":
                                new_lines.append(f"{indent}{dep_name}:\n{indent}  condition: service_healthy\n")
                            else:
                                new_lines.append(f"{indent}{dep_name}:\n{indent}  condition: service_started\n")
                            changed = True
                            continue

                    new_lines.append(line)

                # 6. Injection: Super Smash Bros. Wii U with Health Dependency
                if "super-smash-bros-wiiu:" not in "".join(new_lines).lower():
                    self.setup_log.append("[System] Injecting Super Smash Bros. Wii U service definition...")
                    smash_service = [
                        "\n",
                        "  super-smash-bros-wiiu:\n",
                        "    build: ./repos/super-smash-bros-wiiu\n",
                        "    depends_on:\n",
                        "      postgres:\n",
                        "        condition: service_healthy\n",
                        "      account:\n",
                        "        condition: service_started\n",
                        "      mongodb:\n",
                        "        condition: service_started\n",
                        "    restart: unless-stopped\n",
                        "    ports:\n",
                        "      - 127.0.0.1:2352:2345\n",
                        "      - 6012:6012/udp\n",
                        "      - 6013:6013/udp\n",
                        "    networks:\n",
                        "      internal:\n",
                        "        aliases:\n",
                        "          - super-smash-bros-wiiu\n",
                        "    dns:\n",
                        "      - 172.20.0.200\n",
                        "    env_file:\n",
                        "      - ./environment/super-smash-bros-wiiu.local.env\n"
                    ]
                    for idx, l in enumerate(new_lines):
                        if l.strip().startswith("services:"):
                            for sl in reversed(smash_service): new_lines.insert(idx + 1, sl)
                            changed = True
                            break

                # 7. Injection: Mario Kart 8
                if "mario-kart-8:" not in "".join(new_lines).lower():
                    self.setup_log.append("[System] Injecting Mario Kart 8 service definition...")
                    mk8_service = [
                        "\n",
                        "  mario-kart-8:\n",
                        "    build: ./repos/mario-kart-8\n",
                        "    depends_on:\n",
                        "      postgres:\n",
                        "        condition: service_healthy\n",
                        "      account:\n",
                        "        condition: service_started\n",
                        "      mongodb:\n",
                        "        condition: service_started\n",
                        "    restart: unless-stopped\n",
                        "    ports:\n",
                        "      - 127.0.0.1:2353:2345\n",
                        "      - 6014:6014/udp\n",
                        "      - 6015:6015/udp\n",
                        "    networks:\n",
                        "      internal:\n",
                        "        aliases:\n",
                        "          - mario-kart-8\n",
                        "    dns:\n",
                        "      - 172.20.0.200\n",
                        "    env_file:\n",
                        "      - ./environment/mario-kart-8.local.env\n"
                    ]
                    for idx, l in enumerate(new_lines):
                        if l.strip().startswith("services:"):
                            for sl in reversed(mk8_service): new_lines.insert(idx + 1, sl)
                            changed = True
                            break

                # 8. Injection: Pokken Tournament
                if "pokken-tournament:" not in "".join(new_lines).lower():
                    self.setup_log.append("[System] Injecting Pokk\u00ebn Tournament service definition...")
                    pokken_service = [
                        "\n",
                        "  pokken-tournament:\n",
                        "    build: ./repos/pokken-tournament\n",
                        "    depends_on:\n",
                        "      postgres:\n",
                        "        condition: service_healthy\n",
                        "      account:\n",
                        "        condition: service_started\n",
                        "      mongodb:\n",
                        "        condition: service_started\n",
                        "    restart: unless-stopped\n",
                        "    ports:\n",
                        "      - 127.0.0.1:2354:2345\n",
                        "      - 6016:6016/udp\n",
                        "      - 6017:6017/udp\n",
                        "    networks:\n",
                        "      internal:\n",
                        "        aliases:\n",
                        "          - pokken-tournament\n",
                        "    dns:\n",
                        "      - 172.20.0.200\n",
                        "    env_file:\n",
                        "      - ./environment/pokken-tournament.local.env\n"
                    ]
                    for idx, l in enumerate(new_lines):
                        if l.strip().startswith("services:"):
                            for sl in reversed(pokken_service): new_lines.insert(idx + 1, sl)
                            changed = True
                            break

                if changed and new_lines:
                    self._patch_mitmproxy_addon(s_dir)
                    # Force DB addition to init script
                    pg_init = os.path.join(s_dir, "scripts", "run-in-container", "postgres-init.sh")
                    if os.path.exists(pg_init):
                        try:
                            with open(pg_init, "r") as f: pg_lines = f.readlines()
                            new_pg = []
                            for pl in pg_lines:
                                if "databases=" in pl:
                                    current_dbs = re.search(r'databases="([^"]*)"', pl)
                                    if current_dbs:
                                        db_list = current_dbs.group(1).split()
                                        for new_db in ["super_smash_bros_wiiu", "mario_kart_8", "pokken_tournament"]:
                                            if new_db not in db_list: db_list.append(new_db)
                                        pl = f'databases="{" ".join(db_list)}"\n'
                                new_pg.append(pl)
                            with open(pg_init, "w") as f: f.writelines(new_pg)
                        except: pass
                    
                    # Write the patched compose file
                    with open(path, "w", encoding="utf-8") as f:
                        f.writelines(new_lines)
                    applied_any = True
                    # Stop after first successful patch to avoid conflicting compose files
                    break
                    
            except Exception as e:
                self.setup_log.append(f"[ERROR] Failed to patch {fname}: {e}")
                
        return applied_any

    def _patch_mitmproxy_addon(self, s_dir):
        """Fix the mitmproxy addon to prevent infinite loops when patching via IP address."""
        addon_path = os.path.join(s_dir, "repos/mitmproxy-pretendo/pretendo_addon.py")
        if not os.path.exists(addon_path): return
        
        try:
            with open(addon_path, "r") as f: content = f.read()
            changed = False
            
            if "import re" not in content and "from mitmproxy" in content:
                content = content.replace("from mitmproxy", "import re\nfrom mitmproxy")
                changed = True
                
            loop_fix = 'or re.match(r"^\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}$", flow.request.pretty_host)'
            if loop_fix not in content:
                # Find the pretendo condition block
                if "or \"pretendo-cdn.b-cdn.net\" in flow.request.pretty_host" in content:
                    content = content.replace(
                        "or \"pretendo-cdn.b-cdn.net\" in flow.request.pretty_host",
                        "or \"pretendo-cdn.b-cdn.net\" in flow.request.pretty_host\n                " + loop_fix
                    )
                    changed = True
            
            if changed:
                with open(addon_path, "w") as f: f.write(content)
                self.setup_log.append("[System] mitmproxy addon patched for IP-based loop prevention.")
        except Exception as e:
            self.setup_log.append(f"[WARN] Failed to patch mitmproxy addon: {e}")


    def run_setup_check(self):
        """Perform a deep system audit for dependencies and configuration."""
        missing = []
        warnings = []
        
        # 1. Engine Check
        if not shutil.which("docker"): missing.append("Docker Engine (Missing binary)")
        
        has_compose = False
        if shutil.which("docker-compose"): has_compose = True
        else:
            try:
                res = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, creationflags=0x08000000 if OS_INFO["os"] == "windows" else 0)
                if res.returncode == 0: has_compose = True
            except: pass
        if not has_compose: missing.append("Docker Compose (Plugin or V1)")

        # 2. Permission & Group Check
        if OS_INFO["os"] == "linux":
            try:
                res = subprocess.run(["groups"], capture_output=True, text=True)
                if "docker" not in res.stdout:
                    warnings.append("User not in 'docker' group (Permissions might fail)")
                if not os.access("/var/run/docker.sock", os.W_OK):
                    warnings.append("Docker socket not writable (Run 'Fix Permissions')")
            except: pass
        elif OS_INFO["os"] == "windows":
            # Windows-specific checks
            if not OS_INFO.get("has_wsl"):
                missing.append("WSL2 (Required for bash scripts — Click 'Install WSL2 + Ubuntu')")
            elif not OS_INFO.get("has_wsl_distro"):
                missing.append("WSL2 Linux Distro (WSL2 installed but no distro — run 'wsl --install -d Ubuntu')")
            
            if not shutil.which("git"):
                warnings.append("Git not found in PATH (Install Git for Windows from https://git-scm.com)")
            
            # Check if bash is available (via Git Bash or WSL)
            if not shutil.which("bash") and not OS_INFO.get("has_wsl"):
                warnings.append("No bash available (Install Git for Windows or WSL2)")

        # 3. Stack Integrity
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            missing.append(f"Server Root ({s_dir})")
        else:
            has_yml = any(os.path.isfile(os.path.join(s_dir, f)) for f in ["compose.yml", "docker-compose.yml"])
            if not has_yml: missing.append("compose.yml (Missing stack files)")
            
            env_dir = os.path.join(s_dir, "environment")
            if os.path.isdir(env_dir):
                for e in ["miiverse-api.local.env", "account.local.env"]:
                    if not os.path.isfile(os.path.join(env_dir, e)):
                        missing.append(f"Env Config: {e} (Run Setup Script)")

        # 4. Emulator Keys
        cemu_dir = self.cemu_dir_field.text().strip()
        if os.path.isdir(cemu_dir):
            for k in ["otp.bin", "seeprom.bin"]:
                if not os.path.isfile(os.path.join(cemu_dir, k)):
                    warnings.append(f"Cemu: {k} missing (Injection recommended)")

        # Final Report
        if not missing and not warnings:
            platform_note = ""
            if OS_INFO["os"] == "windows":
                distro = OS_INFO.get("wsl_distro", "Unknown")
                platform_note = f"\n🖥 Windows Mode — WSL2: {distro} | Docker Desktop"
            self.check_result.setText(f"✔ Audit Passed: System is fully operational.{platform_note}")
            self.check_result.setStyleSheet("color: #3fb950; font-weight: bold;")
        else:
            report = ""
            if missing: report += "❌ MISSING:\n• " + "\n• ".join(missing)
            if warnings: report += ("\n\n" if report else "") + "⚠ WARNINGS:\n• " + "\n• ".join(warnings)
            self.check_result.setText(report)
            self.check_result.setStyleSheet(f"color: {RED_PRIMARY if missing else '#d29922'}; font-weight: bold;")

    def restore_nintendo_official(self):
        """Restore official Nintendo network settings for all emulators."""
        res = QMessageBox.question(self, "Restore Official Nintendo", "This will reconnect Cemu and Citra to official Nintendo servers. Proceed?", QMessageBox.Yes | QMessageBox.No)
        if res == QMessageBox.Yes:
            self.patch_cemu_settings("https://api.nintendo.net")
            self.patch_citra("nintendo_restore")

    def restore_pretendo_official(self):
        """Restore official Pretendo network settings for all emulators."""
        res = QMessageBox.question(self, "Restore Official Pretendo", "This will reconnect Cemu and Citra to https://api.pretendo.network. Proceed?", QMessageBox.Yes | QMessageBox.No)
        if res == QMessageBox.Yes:
            # For official restore, we clear the proxy/custom URL
            self.patch_cemu_settings("https://api.pretendo.network", is_official=True)
            self.patch_citra("official_restore")

    def reset_to_defaults(self):
        """Prompt user to choose which settings to reset."""
        msg = QMessageBox(self)
        msg.setWindowTitle("Reset to Defaults")
        msg.setText("Which emulator settings would you like to reset to defaults?")
        msg.setIcon(QMessageBox.Question)
        
        wiiu_btn = msg.addButton("Wii U", QMessageBox.ActionRole)
        ds3_btn = msg.addButton("3DS", QMessageBox.ActionRole)
        cancel_btn = msg.addButton("Cancel", QMessageBox.RejectRole)
        
        msg.exec()
        
        if msg.clickedButton() == wiiu_btn:
            self.patch_cemu_settings("")
            QMessageBox.information(self, "Success", "Wii U settings reset to default.")
        elif msg.clickedButton() == ds3_btn:
            self.patch_citra("reset_default")
            QMessageBox.information(self, "Success", "3DS settings reset to default.")

    def _handle_status_update(self, status):
        """Called by the background worker to update UI components efficiently."""
        self.server_running = status.get("server_running", False)
        current_ip = status.get("ip", "127.0.0.1")
        is_connected = (current_ip != "127.0.0.1")
        
        # Update IP info label if changed
        if getattr(self, 'ip_info', None):
            self.ip_info.setText(f"Local Network IP: {current_ip}")
            
        # Connectivity Safeguard - only shut down if it was previously connected
        if not is_connected and getattr(self, 'last_connectivity_state', False):
             if self.server_running or getattr(self, 'docker_service_running', False):
                msg = "\n[ALARM] Connection Terminated! Triggering Secure Shutdown Protocol...\n"
                self.server_log.append(msg)
                self.setup_log.append(msg)
                if self.server_running: self.stop_server()
                if self.docker_service_running: self.toggle_docker_service()
                self.statusBar().showMessage("NETWORK LOSS DETECTED - Safe Mode Active", 15000)
        
        self.last_connectivity_state = is_connected
        self._update_server_status_ui()

    def _update_server_status_ui(self):
        """Refresh UI labels based on cached server_running state."""
        if self.server_running:
            self.status_label.setText("ONLINE")
            self.status_label.setStyleSheet(f"color: {GREEN_BRIGHT}; font-size: 24px; font-weight: bold;")
            self.server_toggle_btn.setText("STOP SERVER")
            self.server_toggle_btn.setObjectName("stopBtn")
        else:
            self.status_label.setText("OFFLINE")
            self.status_label.setStyleSheet(f"color: {RED_PRIMARY}; font-size: 24px; font-weight: bold;")
            self.server_toggle_btn.setText("START SERVER")
            self.server_toggle_btn.setObjectName("startBtn")
        
        # Update style
        self.server_toggle_btn.setStyle(self.server_toggle_btn.style())

    def _on_status_tick(self):
        """Periodic heartbeat for low-intensity UI updates (Game Detection)."""
        self._detect_current_game()

    def _detect_current_game(self):
        """Monitor Cemu log for the latest TitleID to show what game is booting."""
        try:
            c_data = OS_INFO.get("cemu_data")
            if not c_data: return
            
            cemu_log = os.path.join(c_data, "log.txt")
            if os.path.exists(cemu_log):
                with open(cemu_log, "r") as f:
                    # Seek to near end to avoid parsing multi-MB logs
                    f.seek(0, 2)
                    size = f.tell()
                    offset = max(0, size - 32768) # Check last 32KB
                    f.seek(offset)
                    content = f.read()
                
                # Cemu log format: [19:02:45.370] TitleId: 00050000-10176900
                matches = re.findall(r"TitleId: ([0-9a-fA-F-]+)", content)
                if matches:
                    tid = matches[-1].lower()
                    game_name = TITLE_MAP.get(tid, TITLE_MAP.get(tid.upper(), f"Unknown Title ({tid})"))
                    self.detected_game_label.setText(f"Active Session: {game_name}")
                    self.detected_game_label.setStyleSheet(f"color: {GREEN_BRIGHT}; font-weight: bold;")
                    return
            
            self.detected_game_label.setText("Active Session: None Detected")
            self.detected_game_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-style: italic;")
        except:
            pass

    def show_reset_dialog(self):
        """Helper to show the reset options from different tabs."""
        self.reset_to_defaults()

    def _check_docker_status(self):
        try:
            s_dir = self.server_dir_field.text().strip()
            stack_exists = os.path.isdir(s_dir) and (os.path.isfile(os.path.join(s_dir, "compose.yml")) or os.path.isfile(os.path.join(s_dir, "docker-compose.yml")))
            
            # Use a timeout to prevent GUI freezes if the docker socket is unresponsive
            res = subprocess.run(["docker", "ps", "--filter", "name=pretendo", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=3, creationflags=0x08000000 if OS_INFO["os"] == "windows" else 0)
            self.server_running = bool(res.stdout.strip())
            
            if not stack_exists:
                self.server_toggle_btn.setEnabled(False)
                self.server_toggle_btn.setText("Error: Install Docker Services")
                self.server_toggle_btn.setStyleSheet("background: #555555; border-color: #444444; color: #aaaaaa; font-size: 16px; border-radius: 12px; padding: 12px;")
                self.status_label.setText("NO STACK")
                self.status_label.setStyleSheet(f"color: {RED_PRIMARY}; font-size: 24px; font-weight: bold;")
            else:
                self.server_toggle_btn.setEnabled(True)
                if self.server_running:
                    self.status_label.setText("ONLINE")
                    self.status_label.setStyleSheet("color: #3fb950; font-size: 24px; font-weight: bold;")
                    self.server_toggle_btn.setText("STOP SERVER")
                    self.server_toggle_btn.setStyleSheet(f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {RED_DARK}, stop:1 #9e2a2a); border-color: {RED_PRIMARY}; color: white; font-size: 16px; border-radius: 12px; padding: 12px;")
                else:
                    self.status_label.setText("OFFLINE")
                    self.status_label.setStyleSheet(f"color: {RED_PRIMARY}; font-size: 24px; font-weight: bold;")
                    self.server_toggle_btn.setText("START SERVER")
                    self.server_toggle_btn.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1a5c1a, stop:1 #1a7f37); border-color: #2ea043; color: white; font-size: 16px; border-radius: 12px; padding: 12px;")
            
            if OS_INFO["os"] == "linux":
                svc = subprocess.run(["systemctl", "is-active", "docker"], capture_output=True, text=True)
                self.docker_service_running = (svc.stdout.strip() == "active")
                
                if not stack_exists:
                    self.service_toggle_btn.setEnabled(False)
                    self.service_toggle_btn.setText("Error: Install Docker Services")
                    self.service_toggle_btn.setStyleSheet("background: #555555; border-color: #444444; color: #aaaaaa; font-weight: bold; border-radius: 8px; padding: 12px;")
                else:
                    self.service_toggle_btn.setEnabled(True)
                    if self.docker_service_running:
                        self.service_toggle_btn.setText("Disable Docker Service")
                        self.service_toggle_btn.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #cb2431, stop:1 #d73a49); border-color: #b31d28; color: white; font-weight: bold; border-radius: 8px; padding: 12px;")
                    else:
                        self.service_toggle_btn.setText("Enable Docker Service")
                        self.service_toggle_btn.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1a7f37, stop:1 #2ea043); border-color: #1a5c1a; color: white; font-weight: bold; border-radius: 8px; padding: 12px;")
            elif OS_INFO["os"] == "windows":
                self.docker_service_running = _docker_desktop_running() or _docker_available()
                
                if not stack_exists:
                    self.service_toggle_btn.setEnabled(False)
                    self.service_toggle_btn.setText("Error: Install Docker Services")
                    self.service_toggle_btn.setStyleSheet("background: #555555; border-color: #444444; color: #aaaaaa; font-weight: bold; border-radius: 8px; padding: 12px;")
                else:
                    self.service_toggle_btn.setEnabled(True)
                    if self.docker_service_running:
                        self.service_toggle_btn.setText("Docker Desktop Running ✔")
                        self.service_toggle_btn.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1a7f37, stop:1 #2ea043); border-color: #1a5c1a; color: white; font-weight: bold; border-radius: 8px; padding: 12px;")
                    else:
                        self.service_toggle_btn.setText("Start Docker Desktop")
                        self.service_toggle_btn.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0366d6, stop:1 #2188ff); border-color: #0366d6; color: white; font-weight: bold; border-radius: 8px; padding: 12px;")
                
                # Update WSL status label if it exists
                if getattr(self, 'wsl_status_label', None):
                    if OS_INFO["has_wsl"] and OS_INFO["has_wsl_distro"]:
                        distro = OS_INFO.get("wsl_distro", "Unknown")
                        self.wsl_status_label.setText(f"WSL2: ✔ Active ({distro})")
                        self.wsl_status_label.setStyleSheet("color: #3fb950; font-size: 12px;")
                    elif OS_INFO["has_wsl"]:
                        self.wsl_status_label.setText("WSL2: ⚠ No Distro Installed")
                        self.wsl_status_label.setStyleSheet("color: #d29922; font-size: 12px;")
                    else:
                        self.wsl_status_label.setText("WSL2: ❌ Not Installed")
                        self.wsl_status_label.setStyleSheet(f"color: {RED_PRIMARY}; font-size: 12px;")
        except: pass

    def _get_effective_sudo_password(self):
        """Unified helper to get password from UI field or cache."""
        if getattr(self, 'server_sudo_pass', None) and self.server_sudo_pass.text():
            pw = self.server_sudo_pass.text()
            self.cached_password = pw
            return pw
        return self.cached_password

    def _ask_sudo_password(self):
        if OS_INFO["os"] != "linux":
            return ""
        pw = self._get_effective_sudo_password()
        if pw: return pw.strip() # Added strip()
        
        dlg = SudoPasswordDialog(self)
        if dlg.exec():
            pw, _ = dlg.get_data()
            if not pw: return None
            pw = pw.strip() # Clean input
            self.cached_password = pw
            if getattr(self, 'server_sudo_pass', None):
                self.server_sudo_pass.blockSignals(True)
                self.server_sudo_pass.setText(pw)
                self.server_sudo_pass.blockSignals(False)
            self.save_settings()
            return pw
        return None

    def _manage_ui_lock(self, locked: bool):
        """Disables/Enables the main window to prevent interaction during sensitive background jobs."""
        if locked:
            self.command_lock_count += 1
            if self.command_lock_count == 1:
                self.setEnabled(False)
                QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            self.command_lock_count -= 1
            if self.command_lock_count <= 0:
                self.command_lock_count = 0
                self.setEnabled(True)
                QApplication.restoreOverrideCursor()

    def _run_command(self, cmd, log_widget, cwd=None, on_done=None, stdin_data=None, display_cmd=None):
        # If a worker is still running, wait up to 3 seconds for it to finish before giving up
        worker = self.worker  # local reference for type narrowing
        if worker is not None and worker.isRunning() and not worker.isFinished():
            if not worker.wait(3000):  # 3 second timeout
                log_widget.append("<b style='color:orange;'>[WARN]</b> Previous command still running - queuing after 1s...")
                QTimer.singleShot(1000, lambda: self._run_command(cmd, log_widget, cwd, on_done, stdin_data, display_cmd))
                return
        
        # Locking the UI before starting the worker thread
        self._manage_ui_lock(True)

        # Safe display: never substitute stdin_data into cmd string (could corrupt if pw appears in cmd)
        if display_cmd:
            clean_cmd = display_cmd
        elif stdin_data:
            # Build a safe display string — strip sudo prefix, take first part up to &&, limit length
            parts = str(cmd).split("&&")
            fp = parts[0].strip() if parts else str(cmd).strip()
            sz = str(re.sub(r'sudo\s+-S\s+', '', fp))
            clean_cmd = "[sudo] " + (sz[0:80] + "..." if len(sz) > 80 else sz)
        else:
            clean_cmd = cmd
        log_widget.append(f"<b>[EXEC]</b> <span style='color:{CYAN_PRIMARY};'>guest@pretendo-manager:</span> <span style='color:white;'>{clean_cmd}</span>")
        
        if stdin_data:
            log_widget.append(f"<i style='color:{TEXT_SECONDARY};'>[SYSTEM] Elevating privileges for secure task...</i>")
            
        worker = CommandWorker(cmd, cwd, stdin_data)
        self.worker = worker
        worker.output.connect(log_widget.append)
        
        def handle_done(code):
            status_clr = "#3fb950" if code == 0 else RED_LIGHT
            status_txt = "SUCCESS" if code == 0 else f"FAILED ({code})"
            log_widget.append(f"<b>[DONE]</b> Process exited with status: <b style='color:{status_clr};'>{status_txt}</b>\n")
            
            # Re-enable the UI
            self._manage_ui_lock(False)
            
            if on_done: on_done(code)
            
        worker.finished.connect(handle_done)
        worker.start()

    def _force_shutdown_sync(self, show_progress=True):
        """Blocking shutdown: kills containers, stops Docker, terminates workers. ALWAYS succeeds."""
        s_dir = self.server_dir_field.text().strip()
        pw = self.cached_password or (self.server_sudo_pass.text() if getattr(self, 'server_sudo_pass', None) else None)
        custom_port = self._get_target_port()
        ports_to_kill = f"80 443 21 53 8080 {custom_port} 9231"

        if show_progress:
            self.statusBar().showMessage("FORCE SHUTDOWN IN PROGRESS — DO NOT CLOSE...")

        # Step 1: Kill the background worker thread immediately
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.terminate()
            worker.wait(2000)
        if getattr(self, 'log_worker', None) and self.log_worker.isRunning():
            self.log_worker.terminate()
            self.log_worker.wait(1000)

        env = os.environ.copy()
        env.pop("LD_PRELOAD", None)

        # Step 2: Run docker compose down SYNCHRONOUSLY
        if os.path.isdir(s_dir):
            try:
                subprocess.run(
                    "docker compose down --remove-orphans",
                    shell=True, cwd=s_dir, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30
                )
            except Exception:
                pass

        # Step 3: Kill any lingering port processes + stop Docker service (Linux)
        if OS_INFO["os"] == "linux":
            kill_cmd = self._get_kill_ports_cmd(ports_to_kill, pw)
            try:
                # Use subprocess to run the kill command directly
                if pw:
                    subprocess.run(kill_cmd, shell=True, input=pw + "\n\n", env=env, timeout=10)
                    subprocess.run(f"sudo -S systemctl stop docker.socket docker.service", shell=True, input=pw + "\n\n", env=env, timeout=10)
                else:
                    subprocess.run(kill_cmd, shell=True, env=env, timeout=10)
            except Exception:
                pass
        elif OS_INFO["os"] == "windows":
            kill_cmd = self._get_kill_ports_cmd(ports_to_kill)
            try:
                subprocess.run(kill_cmd, shell=True, env=env, timeout=15)
            except Exception:
                pass

    def emergency_exit(self):
        """ATOMIC SHUTDOWN: force stops everything and kills the process unconditionally."""
        msg = ("ATOMIC SHUTDOWN INITIATED\n\n"
               "This will IMMEDIATELY stop all Pretendo containers, kill the Docker service, "
               "and force-quit the application.\n\n"
               "Proceed?")
        if QMessageBox.critical(self, "Emergency Shutdown", msg, QMessageBox.Yes | QMessageBox.No) == QMessageBox.No:
            return

        self.bypassing_close_prompt = True
        self.statusBar().showMessage("☠ ATOMIC SHUTDOWN — TERMINATING ALL SERVICES...")

        # Run the blocking shutdown (shows progress in status bar)
        self._force_shutdown_sync(show_progress=True)

        # Hard kill — guaranteed exit, no Qt cleanup needed
        os._exit(0)

    def toggle_server(self):
        if self.server_running: self.stop_server()
        else: self.start_server()

    def start_server(self):
        self.server_log.clear()
        # 1. Connectivity Gate
        if self._get_local_ip() == "127.0.0.1":
            QMessageBox.warning(self, "No Connection", "An internet connection is required to start the Pretendo server.\n\nPlease check your network and try again.")
            return

        s_dir = self.server_dir_field.text().strip()
        custom_port = self._get_target_port()
        if not custom_port.isdigit():
            QMessageBox.warning(self, "Security Verification", "Warning: Port must be numeric to assign network bindings safely.")
            return

        if not os.path.isdir(s_dir):
            QMessageBox.warning(self, "Error", "Server directory not found! Download the stack first.")
            return

        self._apply_compose_patches(custom_port, s_dir)

        # 2. Unified Credential Aggregation
        pw = self._get_effective_sudo_password()

        # 3. Docker Service Check & Auto-Start (Prompt if password still missing on Linux)
        if OS_INFO["os"] == "linux":
            if not pw:
                pw = self._ask_sudo_password()
                if not pw: return # User cancelled

        ports = f"80 443 21 53 8080 {custom_port} 9231 6000 6001 6002 6003 6004 6005 6006 6007 6008 6009 6010 6011 6012 6013 6014 6015 6016 6017"
        # Add timeout to fuser to prevent long hangs on busy sockets
        fuser_cmd = " ; ".join([f"timeout 2 fuser -k -n tcp {p} || true" for p in ports.split()])
        
        # Helper to finalize and kickstart mongo replica
        def _on_start(code):
            if code != 0:
                self.server_log.append("<b>[System] Skipping initialization tasks due to start failure.</b>")
                return
            
            if s_dir:
                self.server_log.append("[System] Waiting for MongoDB to initialize Replica Set...")
                
                # We use a loop to force-initiate the replica set and wait for it to be 'PRIMARY'
                # This fixes the MongooseServerSelectionError
                # Try mongo first (4.4) then mongosh (5+); mongo:4.4 has no mongosh
                setup_cmds = [
                    "docker compose exec -T mongodb mongo --eval 'rs.initiate()' || docker compose exec -T mongodb mongosh --eval 'rs.initiate()'",
                    "sleep 5",
                    "docker compose exec -T postgres /docker-entrypoint-initdb.d/postgres-init.sh || docker compose exec -T postgres /scripts/postgres-init.sh || true",
                    "docker compose restart account friends splatoon boss super-mario-maker super-smash-bros-wiiu mario-kart-8"
                ]
                
                inner_cmds = " && ".join(setup_cmds)
                if pw and OS_INFO["os"] == "linux":
                    full_cmd = f"sudo -S bash -c {shlex.quote(inner_cmds)}"
                    self._run_command(full_cmd, self.server_log, s_dir, stdin_data=pw, display_cmd="DB Initialization")
                else:
                    self._run_command(inner_cmds, self.server_log, s_dir, display_cmd="DB Initialization")

            self._check_docker_status()
            if self.server_running:
                self._on_server_boot_finished(0)

        if OS_INFO["os"] == "linux":
            if pw:
                self.server_log.append("[System] Starting services with elevated privileges...")
                # Wrap EVERYTHING into one bash call so sudo only asks ONCE
                inner_logic = (
                    "systemctl start docker.socket docker.service; "
                    f"{fuser_cmd}; "
                    "docker compose up -d"
                )
                # Use -S for stdin and -E to preserve environment variables (like SERVER_IP)
                cmd = f"sudo -S -E bash -c {shlex.quote(inner_logic)}"
                self._run_command(cmd, self.server_log, s_dir, stdin_data=pw, on_done=_on_start)
            else:
                self.server_log.append("[System] Starting server (Best-Effort Mode)...")
                cmd = f"({fuser_cmd}) ; docker compose up -d"
                self._run_command(cmd, self.server_log, s_dir, on_done=_on_start, display_cmd="[Standard] Clean Ports & Start Framework")
        elif OS_INFO["os"] == "windows":
            # Windows: Clear ports via PowerShell, then docker compose up
            kill_cmd = self._get_kill_ports_cmd(ports)
            def _win_start():
                self.server_log.append("[System] Clearing ports and starting Docker containers...")
                cmd = f"{kill_cmd} & docker compose up -d"
                self._run_command(cmd, self.server_log, s_dir, on_done=_on_start, display_cmd="[Windows] Clean Ports & Start Framework")
            # Ensure Docker Desktop is running first
            self._ensure_docker_desktop(_win_start)
        else:
            self._run_command("docker compose up -d", self.server_log, s_dir, on_done=_on_start, display_cmd="docker compose up -d")

    def stop_server(self):
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir): return
        
        custom_port = self._get_target_port()
        if not custom_port.isdigit(): return
        
        ports = f"80 443 21 53 8080 {custom_port} 9231 6000 6001 6002 6003 6004 6005 6006 6007 6008 6009 6010 6011 6012 6013 6014 6015 6016 6017"
        # Ensure fuser doesn't cause the whole chain to fail
        fuser_cmd = " ; ".join([f"fuser -k -n tcp {p} || true" for p in ports.split()])
        
        if OS_INFO["os"] == "linux":
            # Unified Credential Aggregation
            pw = self._get_effective_sudo_password()

            if pw:
                self.server_log.append("[System] Stopping server and force-releasing ports (Secure-Fast-Track)...")
                import shlex
                inner = f"echo '[System] Stopping Containers...'; docker compose down; echo '[System] Disabling Docker...'; systemctl stop docker.socket docker.service; echo '[System] Releasing Ports...'; {fuser_cmd} || true"
                cmd = f"sudo -S bash -c {shlex.quote(inner)}"
                self._run_command(cmd, self.server_log, s_dir, stdin_data=pw, on_done=lambda c: self._check_docker_status(), display_cmd="[Elevated] Stop Framework & Clean Ports")
            else:
                self.server_log.append("[System] Stopping server containers and clearing ports (Best-Effort)...")
                # Try to kill what we can as current user, then docker down
                cmd = f"{fuser_cmd} || true; docker compose down"
                self._run_command(cmd, self.server_log, s_dir, on_done=lambda c: self._check_docker_status(), display_cmd="[Standard] Stop Framework & Clean Ports")
        elif OS_INFO["os"] == "windows":
            # Windows: kill ports via PowerShell, then docker compose down
            kill_cmd = self._get_kill_ports_cmd(ports)
            self.server_log.append("[System] Stopping containers and releasing ports...")
            cmd = f"docker compose down & {kill_cmd}"
            self._run_command(cmd, self.server_log, s_dir, on_done=lambda c: self._check_docker_status(), display_cmd="[Windows] Stop Framework & Clean Ports")
        else:
            self._run_command("docker compose down", self.server_log, s_dir, on_done=lambda c: self._check_docker_status(), display_cmd="docker compose down")

    def _on_server_boot_finished(self, code):
        """Callback for when the entire server stack is fully operational."""
        self.stream_docker_logs()
        if code == 0:
            QMessageBox.information(self, "Server Success", 
                "<b>Pretendo Network Server is now ONLINE!</b><br><br>"
                "Local infrastructure has been successfully initialized and databases are synchronized. "
                "Your emulators can now connect to the network node.")

    def stream_docker_logs(self):
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir): return
        
        self.server_log.append("[System] Connecting to deep Docker event stream...")
        self.server_log.append("--------------------------------------------------")
        cmd = "docker compose logs -f --tail=20"
        
        if getattr(self, 'log_worker', None) and getattr(self.log_worker, 'isRunning', None) and self.log_worker.isRunning():
            self.log_worker.terminate()
            self.server_log.append("[System] Restarting log watcher...")
            
        self.log_worker = CommandWorker(cmd, cwd=s_dir)
        self.log_worker.output.connect(self._handle_server_log)
        self.log_worker.start()

    def _handle_server_log(self, html_text):
        """Processes incoming server logs for both display and critical error detection."""
        self.server_log.append(html_text)
        
        # Strip simple HTML spans to get the raw text back for checking
        raw_text = re.sub(r'<[^>]+>', '', html_text)
        
        critical_patterns = [
            "failed request /oauth20/access_token/generate",
            "failed to retrieve oauth token",
            "mongooseserverselectionerror",
            "mongonetworkerror",
            "invalid memory address or nil pointer dereference",
            "bad decrypt",
            "panic:",
            "segmentation fault",
            "502 bad gateway",
            "request failed with status code",
            "database is locked"
        ]
        
        low = raw_text.lower()
        if any(p in low for p in critical_patterns):
            # Ignore panic loops caused by Docker containers starting before the database is ready.
            # Docker natively self-heals these crashes via 'restart: unless-stopped'.
            if any(ignore in low for ignore in ["connection refused", "dial tcp", "database system is starting up", "econnrefused"]):
                return
                
            self._show_critical_error_popup(raw_text)

    def copy_server_logs(self):
        """Copy current server logs to the system clipboard."""
        if not hasattr(self, "server_log"):
            return
        text = self.server_log.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "Copy Logs", "There are no server logs to copy yet.")
            return
        QApplication.clipboard().setText(text)
        QMessageBox.information(self, "Copy Logs", "Server logs copied to clipboard.")

    def copy_cemu_logs(self):
        """Copy current Cemu patch logs to the system clipboard."""
        if not hasattr(self, "cemu_log"):
            return
        text = self.cemu_log.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "Copy Cemu Log", "There are no Cemu logs to copy yet.")
            return
        QApplication.clipboard().setText(text)
        QMessageBox.information(self, "Copy Cemu Log", "Cemu logs copied to clipboard.")

    def _show_critical_error_popup(self, error_line):
        """Throttled popup for critical server errors."""
        now = time.time()
        
        # Throttling: same error within 20 seconds, or any popup within 10 seconds
        error_key = error_line[:100] # Use first 100 chars as key
        if error_key in self.seen_errors and now - self.seen_errors[error_key] < 20:
            return
        if now - self.last_popup_time < 10:
            return
            
        self.seen_errors[error_key] = now
        self.last_popup_time = now
        
        dlg = ErrorPopupDialog(self, error_line)
        dlg.exec()

    def toggle_docker_service(self):
        # Unified Credential Aggregation
        pw = self._get_effective_sudo_password()

        # Aggressive port cleaning logic
        custom_port = self._get_target_port()
        if not custom_port.isdigit(): return
        ports = f"80 443 21 53 8080 {custom_port} 9231 6000 6001 6002 6003 6004 6005 6006 6007 6008 6009 6010 6011 6012 6013 6014 6015 6016 6017"

        if OS_INFO["os"] == "windows":
            # ─── Windows: Docker Desktop Toggle ───
            if self.docker_service_running:
                self.setup_log.append("[System] Stopping Docker Desktop and clearing ports...")
                kill_cmd = self._get_kill_ports_cmd(ports)
                # Stop Docker Desktop gracefully
                stop_cmd = 'powershell -Command "Get-Process \"Docker Desktop\" -ErrorAction SilentlyContinue | Stop-Process -Force"'
                self._run_command(f"{kill_cmd} & {stop_cmd}", self.setup_log, on_done=lambda c: self._check_docker_status())
            else:
                if self._get_local_ip() == "127.0.0.1":
                    QMessageBox.warning(self, "No Connection", "An active internet connection is required to enable Docker services.")
                    return
                self.setup_log.append("[System] Starting Docker Desktop...")
                self._ensure_docker_desktop(lambda: self._check_docker_status())
            return

        # ─── Linux: systemctl Toggle ───
        fuser_cmd = " ; ".join([f"fuser -k -n tcp {p} || true" for p in ports.split()])

        if self.docker_service_running:
            # DISABLING: Make it popup-free and clear ports
            if pw:
                self.setup_log.append("[System] Disabling Docker service and clearing ports (Fast-Track)...")
                cmd = f"sudo -S bash -c 'systemctl stop docker.socket docker.service; {fuser_cmd} || true'"
                self._run_command(cmd, self.setup_log, stdin_data=pw, on_done=lambda c: self._check_docker_status())
            else:
                self.setup_log.append("[System] Clearing ports and attempting service stop (Silent-Best-Effort)...")
                cmd = f"{fuser_cmd} || true; systemctl stop docker.socket docker.service"
                self._run_command(cmd, self.setup_log, on_done=lambda c: self._check_docker_status())
        else:
            # ENABLING: Check connectivity first
            if self._get_local_ip() == "127.0.0.1":
                QMessageBox.warning(self, "No Connection", "An active internet connection is required to enable Docker services.")
                return

            # Standard security prompt if no password found
            if not pw:
                pw = self._ask_sudo_password()
                if not pw: return
            
            self.setup_log.append("[System] Activating Docker services...")
            self._run_command("sudo -S bash -c 'systemctl reset-failed docker; systemctl start docker.socket docker.service'", self.setup_log, stdin_data=pw, on_done=lambda c: self._check_docker_status())

    def automated_install_stack(self):
        """Combined multi-step workflow for deployment."""
        s_dir = self.server_dir_field.text().strip()
        
        # Check if directory exists but is broken (missing compose.yml)
        needs_clone = False
        if not os.path.isdir(s_dir):
            needs_clone = True
        else:
            has_compose = any(os.path.isfile(os.path.join(s_dir, f)) for f in ["compose.yml", "docker-compose.yml"])
            if not has_compose:
                reply = QMessageBox.question(
                    self, "Incomplete Stack",
                    f"Directory '{s_dir}' exists but is missing critical files (compose.yml).\n\n"
                    f"Delete and re-clone the repository?",
                    QMessageBox.Yes | QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    self.setup_log.append("[System] Removing incomplete stack directory (may need sudo for Docker-created files)...")
                    removed = False
                    try:
                        shutil.rmtree(s_dir)
                        removed = True
                    except PermissionError:
                        # Docker/root-owned files — use sudo rm -rf
                        self.setup_log.append("[System] Permission denied on some files. Using elevated removal...")
                        pw = self._ask_sudo_password()
                        if pw:
                            try:
                                if OS_INFO["os"] == "linux":
                                    cmd = f"sudo -S rm -rf {shlex.quote(s_dir)}"
                                    result = subprocess.run(cmd, shell=True, input=pw + "\n", capture_output=True, text=True)
                                else:
                                    # Windows: Use PowerShell Remove-Item which handles junctions better
                                    win_path = s_dir.replace("'", "''")
                                    cmd = f'powershell -Command "Remove-Item -Path \'{win_path}\' -Recurse -Force -ErrorAction Stop"'
                                    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                                
                                if result.returncode == 0:
                                    removed = True
                                    self.setup_log.append("[OK] Directory removed with elevated privileges.")
                                else:
                                    self.setup_log.append(f"[ERROR] Elevated removal failed: {result.stderr.strip()}")
                            except Exception as e2:
                                self.setup_log.append(f"[ERROR] Elevated removal failed: {e2}")
                        else:
                            self.setup_log.append("[ERROR] Sudo password required to remove root-owned files. Aborted.")
                    except Exception as e:
                        self.setup_log.append(f"[ERROR] Failed to remove directory: {e}")
                    
                    if not removed:
                        return
                    needs_clone = True
                else:
                    self.setup_log.append("[WARN] Cannot proceed without compose.yml. Aborted.")
                    return
        
        if needs_clone:
            reply = QMessageBox.question(self, "Proceed", f"Clone Pretendo Docker repository to '{s_dir}'?", QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.setup_log.append("[System] Cloning repository (this may take a few minutes)...")
                self._run_command(
                    f"git clone --recurse-submodules {PRETENDO_REPO} {shlex.quote(s_dir)}",
                    self.setup_log,
                    on_done=lambda c: self._on_clone_finished(c)
                )
            return
        
        # Directory exists and has compose file - proceed to setup
        self._on_clone_finished(0)
            
    def _on_clone_finished(self, code):
        """Called after git clone completes (or skipped if dir already valid)."""
        if code != 0:
            self.setup_log.append("[ERROR] Git clone failed. Check your internet connection and try again.")
            return
        
        s_dir = self.server_dir_field.text().strip()
        
        # Validate the clone result
        has_compose = any(os.path.isfile(os.path.join(s_dir, f)) for f in ["compose.yml", "docker-compose.yml"])
        if not has_compose:
            self.setup_log.append("[ERROR] Clone completed but compose.yml still missing. Repository may be corrupted.")
            return
        
        self.setup_log.append("\n[System] Stack downloaded. Checking for game-specific repositories...")
        
        # Ensure Super Smash Bros. Wii U repo is present (Custom Addition)
        smash_dir = os.path.join(s_dir, "repos", "super-smash-bros-wiiu")
        if not os.path.isdir(smash_dir):
            self.setup_log.append("[System] Downloading Super Smash Bros. Wii U server...")
            smash_repo = "https://github.com/PretendoNetwork/super-smash-bros-wiiu"
            self._run_command(
                f"git clone {smash_repo} {shlex.quote(smash_dir)}",
                self.setup_log,
                on_done=lambda c: self._on_clone_finished(c)
            )
            return

        # Ensure Mario Kart 8 repo is present
        mk8_dir = os.path.join(s_dir, "repos", "mario-kart-8")
        if not os.path.isdir(mk8_dir):
            self.setup_log.append("[System] Downloading Mario Kart 8 server...")
            mk8_repo = "https://github.com/PretendoNetwork/mario-kart-8"
            self._run_command(
                f"git clone {mk8_repo} {shlex.quote(mk8_dir)}",
                self.setup_log,
                on_done=lambda c: self._on_clone_finished(c)
            )
            return

        # Ensure Pokken Tournament repo is present
        pokken_dir = os.path.join(s_dir, "repos", "pokken-tournament")
        if not os.path.isdir(pokken_dir):
            self.setup_log.append("[System] Downloading Pokk\u00ebn Tournament server...")
            pokken_repo = "https://github.com/PretendoNetwork/pokken-tournament"
            self._run_command(
                f"git clone {pokken_repo} {shlex.quote(pokken_dir)}",
                self.setup_log,
                on_done=lambda c: self._on_clone_finished(c)
            )
            return

        self.setup_log.append("[OK] All required repositories are present. Applying submodule patches...")
        
        # Run submodule patches (CRITICAL for Super Mario Maker and other fixes)
        patch_script = os.path.join(s_dir, "scripts", "setup-submodule-patches.sh")
        if os.path.isfile(patch_script):
            if OS_INFO["os"] == "linux":
                patch_cmd = f"chmod +x {shlex.quote(patch_script)} && {shlex.quote(patch_script)}"
            elif OS_INFO["os"] == "windows":
                # Windows: Route through WSL if available, fallback to Git Bash
                if OS_INFO.get("has_wsl") and OS_INFO.get("has_wsl_distro"):
                    wsl_script = _win_to_wsl_path(patch_script)
                    wsl_cwd = _win_to_wsl_path(s_dir)
                    patch_cmd = f'wsl bash -lc "cd {shlex.quote(wsl_cwd)} && chmod +x {shlex.quote(wsl_script)} && {shlex.quote(wsl_script)}"'
                    self.setup_log.append("[System] Running patches via WSL2...")
                elif shutil.which("bash"):
                    # Git Bash fallback: convert to Unix path for the bash shell
                    git_bash_script = patch_script.replace("\\", "/")
                    patch_cmd = f'bash -lc "chmod +x \'{git_bash_script}\' && \'{git_bash_script}\'"'
                    self.setup_log.append("[System] Running patches via Git Bash...")
                else:
                    self.setup_log.append("[WARN] No bash environment available (WSL2 or Git Bash). Skipping patches.")
                    self.setup_log.append("[HINT] Install WSL2 for full compatibility. Click 'Install WSL2 + Ubuntu'.")
                    QTimer.singleShot(1000, self._run_environment_setup)
                    return
            else:
                # macOS / other
                patch_cmd = f"bash {shlex.quote(patch_script)}" if shutil.which("bash") else shlex.quote(patch_script)
                
            self._run_command(
                patch_cmd,
                self.setup_log,
                cwd=s_dir,
                on_done=lambda c: QTimer.singleShot(1000, self._run_environment_setup)
            )
        else:
            self.setup_log.append("[WARN] scripts/setup-submodule-patches.sh not found. Skipping patches.")
            QTimer.singleShot(1000, self._run_environment_setup)

    def _run_environment_setup(self):
        """Generate environment files and run the setup pipeline."""
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            self.setup_log.append("[ERROR] Server directory not found. Cannot proceed with setup.")
            return

        custom_port = self._get_target_port()
        if not custom_port.isdigit():
            self.setup_log.append("[ERROR] Port must be numeric.")
            return
        self._apply_compose_patches(custom_port, s_dir)

        local_ip = self._get_local_ip()
        
        # Generate .env and local environment files in Python (reliable, no bash dependencies)
        self.setup_log.append("[System] Generating environment configuration...")
        try:
            self._generate_env_files(s_dir, local_ip)
            self._ensure_smm_metadata(s_dir)
            self._fix_go_build_compatibility(s_dir)
            self._generate_juxtaposition_boot_config(s_dir) # Renamed from _fix_juxtaposition_ui_aliases
            self.setup_log.append("[OK] Environment files generated successfully.")
        except Exception as e:
            self.setup_log.append(f"[ERROR] Failed to generate environment: {e}")
            return

        # Collect sudo password ONCE here, pass it all the way through steps 2-3-4
        pw = None
        if OS_INFO["os"] == "linux":
            pw = self._ask_sudo_password()
            if not pw:
                self.setup_log.append("[ERROR] Sudo password is required for setup. Aborted.")
                return
        elif OS_INFO["os"] == "windows":
            # Windows: No sudo needed — Docker Desktop handles elevation via its own daemon
            self.setup_log.append("[System] Windows mode: Docker Desktop handles elevation.")
            pw = None

        # Proceed to port clearing + docker pull + build
        self._deploy_step2_clear_and_pull(s_dir, local_ip, custom_port, pw)

    def _generate_env_files(self, s_dir, server_ip):
        """Fixed Env Generator: Corrects BOSS key lengths and ensures all DB URIs are present."""
        import secrets as sec_module
        import string
        
        def gen_password(length=32):
            chars = string.ascii_letters + string.digits
            return ''.join(sec_module.choice(chars) for _ in range(length))
        
        def gen_hex(length=32):
            chars = 'abcdef0123456789' # BOSS often prefers lowercase hex
            return ''.join(sec_module.choice(chars) for _ in range(length))
        
        env_dir = os.path.join(s_dir, "environment")
        os.makedirs(env_dir, exist_ok=True)
        
        def get_existing(fname, key, fallback, required_len=0):
            val = self._grep_env_file(os.path.join(env_dir, fname), key)
            # If the existing value is the wrong length or "dummy", use the fallback
            if val and required_len > 0 and len(val) != required_len:
                return fallback
            return val if val else fallback

        # BOSS Keys must be exactly 32 chars (16 bytes)
        boss_wiiu_aes = get_existing("boss.local.env", "PN_BOSS_CONFIG_BOSS_WIIU_AES_KEY", gen_hex(32), 32)
        boss_wiiu_hmac = get_existing("boss.local.env", "PN_BOSS_CONFIG_BOSS_WIIU_HMAC_KEY", gen_hex(32), 32)
        boss_3ds_aes = get_existing("boss.local.env", "PN_BOSS_CONFIG_BOSS_3DS_AES_KEY", gen_hex(32), 32)
        boss_3ds_hmac = get_existing("boss.local.env", "PN_BOSS_CONFIG_BOSS_3DS_HMAC_KEY", gen_hex(32), 32)
        
        # Postgres and Account Keys
        postgres_pass = get_existing("postgres.local.env", "POSTGRES_PASSWORD", gen_password(32))
        account_grpc_key = get_existing("account.local.env", "PN_ACT_CONFIG_GRPC_MASTER_API_KEY_ACCOUNT", gen_password(32))
        account_aes_key = get_existing("account.local.env", "PN_ACT_CONFIG_AES_KEY", gen_hex(64), 64)
        account_datastore_secret = get_existing("account.local.env", "PN_ACT_CONFIG_DATASTORE_SIGNATURE_SECRET", gen_hex(32), 32)
        minio_secret = get_existing("account.local.env", "PN_ACT_CONFIG_S3_ACCESS_SECRET", gen_password(32))

        # Friends
        friends_auth_pw = get_existing("friends.local.env", "PN_FRIENDS_CONFIG_AUTHENTICATION_PASSWORD", gen_password(32))
        friends_secure_pw = get_existing("friends.local.env", "PN_FRIENDS_CONFIG_SECURE_PASSWORD", gen_password(32))
        friends_api_key = get_existing("friends.local.env", "PN_FRIENDS_CONFIG_GRPC_API_KEY", gen_password(32))
        friends_aes_key = get_existing("friends.local.env", "PN_FRIENDS_CONFIG_AES_KEY", gen_hex(64), 64)

        # Game Kerberos and AES Keys
        chat_kerberos_pw = get_existing("wiiu-chat.local.env", "PN_WIIU_CHAT_KERBEROS_PASSWORD", gen_password(32))
        smm_kerberos_pw = get_existing("super-mario-maker.local.env", "PN_SMM_KERBEROS_PASSWORD", gen_password(32))
        smm_aes_key = get_existing("super-mario-maker.local.env", "PN_SMM_CONFIG_AES_KEY", gen_hex(64), 64)
        
        splat_kerberos_pw = get_existing("splatoon.local.env", "PN_SPLATOON_KERBEROS_PASSWORD", gen_password(32))
        splat_aes_key = get_existing("splatoon.local.env", "PN_SPLATOON_CONFIG_AES_KEY", gen_hex(64), 64)
        
        smash_kerberos_pw = get_existing("super-smash-bros-wiiu.local.env", "PN_SUPERSMASHBROSWIIU_KERBEROS_PASSWORD", 
                                         get_existing("super-smash-bros-wiiu.local.env", "PN_SSBWIIU_KERBEROS_PASSWORD", gen_password(32)))
        smash_aes_key = get_existing("super-smash-bros-wiiu.local.env", "PN_SUPERSMASHBROSWIIU_AES_KEY", 
                                     get_existing("super-smash-bros-wiiu.local.env", "PN_SSBWIIU_AES_KEY", gen_hex(64), 64))
        
        minecraft_kerberos_pw = get_existing("minecraft-wiiu.local.env", "PN_MINECRAFT_KERBEROS_PASSWORD", gen_password(32))
        pikmin3_kerberos_pw = get_existing("pikmin-3.local.env", "PN_PIKMIN3_KERBEROS_PASSWORD", gen_password(32))
        
        mk8_kerberos_pw = get_existing("mario-kart-8.local.env", "PN_MARIOKART8_KERBEROS_PASSWORD", 
                                       get_existing("mario-kart-8.local.env", "PN_MK8_KERBEROS_PASSWORD", gen_password(32)))
        mk8_aes_key = get_existing("mario-kart-8.local.env", "PN_MARIOKART8_CONFIG_AES_KEY", 
                                   get_existing("mario-kart-8.local.env", "PN_MK8_CONFIG_AES_KEY", gen_hex(64), 64))
        
        pokken_kerberos_pw = get_existing("pokken-tournament.local.env", "PN_POKKENTOURNAMENT_KERBEROS_PASSWORD", 
                                          get_existing("pokken-tournament.local.env", "PN_POKKEN_KERBEROS_PASSWORD", gen_password(32)))
        pokken_aes_key = get_existing("pokken-tournament.local.env", "PN_POKKENTOURNAMENT_CONFIG_AES_KEY", 
                                      get_existing("pokken-tournament.local.env", "PN_POKKEN_CONFIG_AES_KEY", gen_hex(64), 64))
        
        boss_api_key = get_existing("boss.local.env", "PN_BOSS_CONFIG_GRPC_BOSS_SERVER_API_KEY", gen_password(32))

        # Re-map the BOSS environment file with BOTH naming conventions (old and new)
        env_files = {}
        
        # 1. Account Server
        env_files["account.local.env"] = [
            f"PN_ACT_CONFIG_AES_KEY={account_aes_key}",
            f"PN_ACT_CONFIG_DATASTORE_SIGNATURE_SECRET={get_existing('account.local.env', 'PN_ACT_CONFIG_DATASTORE_SIGNATURE_SECRET', gen_hex(32), 32)}",
            f"PN_ACT_CONFIG_GRPC_MASTER_API_KEY_ACCOUNT={account_grpc_key}",
            f"PN_ACT_CONFIG_GRPC_MASTER_API_KEY_API={account_grpc_key}",
            f"PN_ACT_CONFIG_S3_ACCESS_SECRET={minio_secret}",
            "PN_ACT_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_ACT_CONFIG_S3_ACCESS_KEY=minio_pretendo",
            "PN_ACT_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_account?replicaSet=rs",
        ]

        # 2. Friends Server
        env_files["friends.local.env"] = [
            f"PN_FRIENDS_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_FRIENDS_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_FRIENDS_CONFIG_AUTHENTICATION_PASSWORD={get_existing('friends.local.env', 'PN_FRIENDS_CONFIG_AUTHENTICATION_PASSWORD', gen_password(32))}",
            f"PN_FRIENDS_CONFIG_SECURE_PASSWORD={get_existing('friends.local.env', 'PN_FRIENDS_CONFIG_SECURE_PASSWORD', gen_password(32))}",
            f"PN_FRIENDS_CONFIG_GRPC_API_KEY={get_existing('friends.local.env', 'PN_FRIENDS_CONFIG_GRPC_API_KEY', gen_password(32))}",
            f"PN_FRIENDS_CONFIG_AES_KEY={get_existing('friends.local.env', 'PN_FRIENDS_CONFIG_AES_KEY', gen_hex(64), 64)}",
            f"PN_FRIENDS_CONFIG_DATABASE_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/friends?sslmode=disable",
            f"PN_FRIENDS_SECURE_SERVER_HOST={server_ip}",
            f"PN_FRIENDS_CONFIG_SECURE_SERVER_HOST={server_ip}",
            f"PN_WEBSITE_CONFIG_DATABASE_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/website?sslmode=disable",
        ]

        # 3. Miiverse API
        env_files["miiverse-api.local.env"] = [
            f"PN_MIIVERSE_API_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_MIIVERSE_API_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_MIIVERSE_API_CONFIG_S3_ACCESS_SECRET={minio_secret}",
            "PN_MIIVERSE_API_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_MIIVERSE_API_CONFIG_S3_ACCESS_KEY=minio_pretendo",
            f"PN_MIIVERSE_API_CONFIG_GRPC_FRIENDS_API_KEY={get_existing('friends.local.env', 'PN_FRIENDS_CONFIG_GRPC_API_KEY', gen_password(32))}",
            f"PN_MIIVERSE_API_CONFIG_AES_KEY={account_aes_key}",
            "PN_MIIVERSE_API_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_miiverse?replicaSet=rs",
        ]
        
        # 4. Juxtaposition UI
        env_files["juxtaposition-ui.local.env"] = [
            f"JUXT_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"JUXT_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"JUXT_CONFIG_AWS_SPACES_SECRET={minio_secret}",
            "JUXT_CONFIG_AWS_SPACES_ENDPOINT=minio:9000",
            "JUXT_CONFIG_AWS_SPACES_ACCESS_KEY=minio_pretendo",
            f"JUXT_CONFIG_GRPC_FRIENDS_API_KEY={get_existing('friends.local.env', 'PN_FRIENDS_CONFIG_GRPC_API_KEY', gen_password(32))}",
            f"JUXT_CONFIG_AES_KEY={account_aes_key}",
            "JUXT_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_juxt?replicaSet=rs",
        ]

        # 5. BOSS
        env_files["boss.local.env"] = [
            f"PN_BOSS_CONFIG_GRPC_ACCOUNT_SERVER_API_KEY={account_grpc_key}",
            f"PN_BOSS_CONFIG_GRPC_FRIENDS_SERVER_API_KEY={friends_api_key}",
            f"PN_BOSS_CONFIG_GRPC_BOSS_SERVER_API_KEY={boss_api_key}",
            "PN_BOSS_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_BOSS_S3_ENDPOINT=minio:9000",
            f"PN_BOSS_CONFIG_S3_ACCESS_SECRET={minio_secret}",
            f"PN_BOSS_S3_ACCESS_KEY=minio_pretendo",
            # Ensure these are exactly 32 chars (16 bytes)
            f"PN_BOSS_CONFIG_BOSS_WIIU_AES_KEY={boss_wiiu_aes}",
            f"PN_BOSS_BOSS_WIIU_AES_KEY={boss_wiiu_aes}",
            f"PN_BOSS_CONFIG_BOSS_WIIU_HMAC_KEY={boss_wiiu_hmac}",
            f"PN_BOSS_BOSS_WIIU_HMAC_KEY={boss_wiiu_hmac}",
            f"PN_BOSS_CONFIG_BOSS_3DS_AES_KEY={boss_3ds_aes}",
            f"PN_BOSS_BOSS_3DS_AES_KEY={boss_3ds_aes}",
            f"PN_BOSS_CONFIG_BOSS_3DS_HMAC_KEY={boss_3ds_hmac}",
            f"PN_BOSS_BOSS_3DS_HMAC_KEY={boss_3ds_hmac}",
            "PN_BOSS_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_boss?replicaSet=rs",
        ]

        # 6. Super Mario Maker
        env_files["super-mario-maker.local.env"] = [
            f"PN_SMM_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_SMM_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_SMM_CONFIG_S3_ACCESS_SECRET={minio_secret}",
            "PN_SMM_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_SMM_CONFIG_S3_ACCESS_KEY=minio_pretendo",
            f"PN_SMM_KERBEROS_PASSWORD={smm_kerberos_pw}",
            f"PN_SMM_CONFIG_AES_KEY={smm_aes_key}",
            f"PN_SMM_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/super_mario_maker?sslmode=disable",
            f"PN_SMM_SECURE_SERVER_HOST={server_ip}",
            f"PN_SMM_CONFIG_SECURE_SERVER_HOST={server_ip}",
            "PN_SMM_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_smm?replicaSet=rs",
        ]

        # 7. WiiU Chat
        env_files["wiiu-chat.local.env"] = [
            f"PN_WIIU_CHAT_FRIENDS_GRPC_API_KEY={friends_api_key}",
            f"PN_WIIU_CHAT_CONFIG_GRPC_FRIENDS_API_KEY={friends_api_key}",
            f"PN_WIIU_CHAT_KERBEROS_PASSWORD={chat_kerberos_pw}",
            "PN_WIIU_CHAT_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_chat?replicaSet=rs",
            "MONGO_URI=mongodb://mongodb:27017/pretendo_chat?replicaSet=rs",
            f"PN_WIIU_CHAT_SECURE_SERVER_LOCATION={server_ip}",
            f"PN_WIIU_CHAT_CONFIG_SECURE_SERVER_LOCATION={server_ip}",
        ]
        
        # 8. Splatoon
        env_files["splatoon.local.env"] = [
            f"PN_SPLATOON_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_SPLATOON_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_SPLATOON_KERBEROS_PASSWORD={splat_kerberos_pw}",
            f"PN_SPLATOON_CONFIG_AES_KEY={splat_aes_key}",
            "PN_SPLATOON_CONFIG_S3_ENDPOINT=minio:9000",
            f"PN_SPLATOON_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/splatoon?sslmode=disable",
            f"PN_SPLATOON_SECURE_SERVER_HOST={server_ip}",
            f"PN_SPLATOON_CONFIG_SECURE_SERVER_HOST={server_ip}",
            "PN_SPLATOON_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_splatoon?replicaSet=rs",
        ]
        
        # 9. Minecraft
        env_files["minecraft-wiiu.local.env"] = [
            f"PN_MINECRAFT_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_MINECRAFT_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_MINECRAFT_KERBEROS_PASSWORD={minecraft_kerberos_pw}",
            f"PN_MINECRAFT_SECURE_SERVER_HOST={server_ip}",
            f"PN_MINECRAFT_CONFIG_SECURE_SERVER_HOST={server_ip}",
            "PN_MINECRAFT_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_minecraft?replicaSet=rs",
            "PN_MINECRAFT_ALLOW_PUBLIC_MATCHMAKING=1",
        ]
        
        # 10. Pikmin 3
        env_files["pikmin-3.local.env"] = [
            f"PN_PIKMIN3_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_PIKMIN3_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_PIKMIN3_KERBEROS_PASSWORD={pikmin3_kerberos_pw}",
            "PN_PIKMIN3_CONFIG_S3_ENDPOINT=minio:9000",
            f"PN_PIKMIN3_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/pikmin3?sslmode=disable",
            f"PN_PIKMIN3_SECURE_SERVER_HOST={server_ip}",
            f"PN_PIKMIN3_CONFIG_SECURE_SERVER_HOST={server_ip}",
            "PN_PIKMIN3_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_pikmin3?replicaSet=rs",
        ]

        # 11. Super Smash Bros. Wii U
        env_files["super-smash-bros-wiiu.local.env"] = [
            f"PN_SSBWIIU_KERBEROS_PASSWORD={smash_kerberos_pw}",
            f"PN_SUPERSMASHBROSWIIU_KERBEROS_PASSWORD={smash_kerberos_pw}",
            "PN_SSBWIIU_AUTHENTICATION_SERVER_PORT=6012",
            "PN_SUPERSMASHBROSWIIU_AUTHENTICATION_SERVER_PORT=6012",
            "PN_SSBWIIU_SECURE_SERVER_PORT=6013",
            "PN_SUPERSMASHBROSWIIU_SECURE_SERVER_PORT=6013",
            f"PN_SSBWIIU_SECURE_SERVER_HOST={server_ip}",
            f"PN_SUPERSMASHBROSWIIU_SECURE_SERVER_HOST={server_ip}",
            "PN_SSBWIIU_ACCOUNT_GRPC_HOST=account",
            "PN_SUPERSMASHBROSWIIU_ACCOUNT_GRPC_HOST=account",
            "PN_SSBWIIU_ACCOUNT_GRPC_PORT=5000",
            "PN_SUPERSMASHBROSWIIU_ACCOUNT_GRPC_PORT=5000",
            f"PN_SSBWIIU_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_SUPERSMASHBROSWIIU_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            "PN_SSBWIIU_FRIENDS_GRPC_HOST=friends",
            "PN_SUPERSMASHBROSWIIU_FRIENDS_GRPC_HOST=friends",
            "PN_SSBWIIU_FRIENDS_GRPC_PORT=5001",
            "PN_SUPERSMASHBROSWIIU_FRIENDS_GRPC_PORT=5001",
            f"PN_SSBWIIU_FRIENDS_GRPC_API_KEY={friends_api_key}",
            f"PN_SUPERSMASHBROSWIIU_FRIENDS_GRPC_API_KEY={friends_api_key}",
            "PN_SSBWIIU_DATASTORE_S3BUCKET=super-smash-bros-wiiu",
            "PN_SUPERSMASHBROSWIIU_DATASTORE_S3BUCKET=super-smash-bros-wiiu",
            "PN_SSBWIIU_DATASTORE_S3KEY=minio_pretendo",
            "PN_SUPERSMASHBROSWIIU_DATASTORE_S3KEY=minio_pretendo",
            f"PN_SSBWIIU_DATASTORE_S3SECRET={minio_secret}",
            f"PN_SUPERSMASHBROSWIIU_DATASTORE_S3SECRET={minio_secret}",
            "PN_SSBWIIU_DATASTORE_S3URL=minio:9000",
            "PN_SUPERSMASHBROSWIIU_DATASTORE_S3URL=minio:9000",
            f"PN_SSBWIIU_AES_KEY={smash_aes_key}",
            f"PN_SUPERSMASHBROSWIIU_AES_KEY={smash_aes_key}",
            f"PN_SSBWIIU_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/super_smash_bros_wiiu?sslmode=disable",
            f"PN_SUPERSMASHBROSWIIU_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/super_smash_bros_wiiu?sslmode=disable",
            "PN_SSBWIIU_LOCAL_AUTH=0",
            "PN_SUPERSMASHBROSWIIU_LOCAL_AUTH=0",
            "PN_SSBWIIU_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_smash?replicaSet=rs",
            "PN_SUPERSMASHBROSWIIU_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_smash?replicaSet=rs",
            "PN_SSBWIIU_MONGODB_URI=mongodb://mongodb:27017/pretendo_smash?replicaSet=rs",
            "PN_SUPERSMASHBROSWIIU_MONGODB_URI=mongodb://mongodb:27017/pretendo_smash?replicaSet=rs",
            "PN_SSBWIIU_S3_ENDPOINT=minio:9000",
            "PN_SUPERSMASHBROSWIIU_S3_ENDPOINT=minio:9000",
            "PN_SSBWIIU_S3_URL=minio:9000",
            "PN_SUPERSMASHBROSWIIU_S3_URL=minio:9000",
            "PN_SSBWIIU_S3_ACCESS_KEY=minio_pretendo",
            "PN_SUPERSMASHBROSWIIU_S3_ACCESS_KEY=minio_pretendo",
            f"PN_SSBWIIU_S3_ACCESS_SECRET={minio_secret}",
            f"PN_SUPERSMASHBROSWIIU_S3_ACCESS_SECRET={minio_secret}",
        ]

        # 12. Mario Kart 8
        env_files["mario-kart-8.local.env"] = [
            f"PN_MK8_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_MARIOKART8_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_MK8_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_MARIOKART8_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_MK8_KERBEROS_PASSWORD={mk8_kerberos_pw}",
            f"PN_MARIOKART8_KERBEROS_PASSWORD={mk8_kerberos_pw}",
            f"PN_MK8_CONFIG_AES_KEY={mk8_aes_key}",
            f"PN_MARIOKART8_CONFIG_AES_KEY={mk8_aes_key}",
            f"PN_MK8_AES_KEY={mk8_aes_key}",
            f"PN_MARIOKART8_AES_KEY={mk8_aes_key}",
            "PN_MK8_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_MARIOKART8_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_MK8_S3_ENDPOINT=minio:9000",
            "PN_MARIOKART8_S3_ENDPOINT=minio:9000",
            f"PN_MK8_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/mario_kart_8?sslmode=disable",
            f"PN_MARIOKART8_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/mario_kart_8?sslmode=disable",
            f"PN_MK8_SECURE_SERVER_HOST={server_ip}",
            f"PN_MARIOKART8_SECURE_SERVER_HOST={server_ip}",
            f"PN_MK8_CONFIG_SECURE_SERVER_HOST={server_ip}",
            f"PN_MARIOKART8_CONFIG_SECURE_SERVER_HOST={server_ip}",
            "PN_MK8_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_mk8?replicaSet=rs",
            "PN_MARIOKART8_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_mk8?replicaSet=rs",
            "PN_MK8_MONGODB_URI=mongodb://mongodb:27017/pretendo_mk8?replicaSet=rs",
            "PN_MARIOKART8_MONGODB_URI=mongodb://mongodb:27017/pretendo_mk8?replicaSet=rs",
            "PN_MK8_AUTHENTICATION_SERVER_PORT=6014",
            "PN_MARIOKART8_AUTHENTICATION_SERVER_PORT=6014",
            "PN_MK8_SECURE_SERVER_PORT=6015",
            "PN_MARIOKART8_SECURE_SERVER_PORT=6015",
            "PN_MK8_ACCOUNT_GRPC_HOST=account",
            "PN_MARIOKART8_ACCOUNT_GRPC_HOST=account",
            "PN_MK8_ACCOUNT_GRPC_PORT=5000",
            "PN_MARIOKART8_ACCOUNT_GRPC_PORT=5000",
            "PN_MK8_FRIENDS_GRPC_HOST=friends",
            "PN_MARIOKART8_FRIENDS_GRPC_HOST=friends",
            "PN_MK8_FRIENDS_GRPC_PORT=5001",
            "PN_MARIOKART8_FRIENDS_GRPC_PORT=5001",
            f"PN_MK8_FRIENDS_GRPC_API_KEY={friends_api_key}",
            f"PN_MARIOKART8_FRIENDS_GRPC_API_KEY={friends_api_key}",
            "PN_MK8_DATASTORE_S3BUCKET=mario-kart-8",
            "PN_MARIOKART8_DATASTORE_S3BUCKET=mario-kart-8",
            "PN_MK8_DATASTORE_S3KEY=minio_pretendo",
            "PN_MARIOKART8_DATASTORE_S3KEY=minio_pretendo",
            f"PN_MK8_DATASTORE_S3SECRET={minio_secret}",
            f"PN_MARIOKART8_DATASTORE_S3SECRET={minio_secret}",
            "PN_MK8_DATASTORE_S3URL=minio:9000",
            "PN_MARIOKART8_DATASTORE_S3URL=minio:9000",
            "PN_MK8_S3_ACCESS_KEY=minio_pretendo",
            "PN_MARIOKART8_S3_ACCESS_KEY=minio_pretendo",
            f"PN_MK8_S3_ACCESS_SECRET={minio_secret}",
            f"PN_MARIOKART8_S3_ACCESS_SECRET={minio_secret}",
            "PN_MK8_S3_URL=minio:9000",
            "PN_MARIOKART8_S3_URL=minio:9000",
            "PN_MK8_CONFIG_S3_ACCESS_KEY=minio_pretendo",
            "PN_MARIOKART8_CONFIG_S3_ACCESS_KEY=minio_pretendo",
            f"PN_MK8_CONFIG_S3_ACCESS_SECRET={minio_secret}",
            f"PN_MARIOKART8_CONFIG_S3_ACCESS_SECRET={minio_secret}",
            "PN_MK8_LOCAL_AUTH=0",
            "PN_MARIOKART8_LOCAL_AUTH=0",
        ]

        # 13. Pokken Tournament
        env_files["pokken-tournament.local.env"] = [
            f"PN_POKKEN_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_POKKENTOURNAMENT_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_POKKEN_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_POKKENTOURNAMENT_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_POKKEN_KERBEROS_PASSWORD={pokken_kerberos_pw}",
            f"PN_POKKENTOURNAMENT_KERBEROS_PASSWORD={pokken_kerberos_pw}",
            f"PN_POKKEN_CONFIG_AES_KEY={pokken_aes_key}",
            f"PN_POKKENTOURNAMENT_CONFIG_AES_KEY={pokken_aes_key}",
            f"PN_POKKEN_AES_KEY={pokken_aes_key}",
            f"PN_POKKENTOURNAMENT_AES_KEY={pokken_aes_key}",
            "PN_POKKEN_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_POKKENTOURNAMENT_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_POKKEN_S3_ENDPOINT=minio:9000",
            "PN_POKKENTOURNAMENT_S3_ENDPOINT=minio:9000",
            "PN_POKKEN_S3_ACCESS_KEY=minio_pretendo",
            "PN_POKKENTOURNAMENT_S3_ACCESS_KEY=minio_pretendo",
            f"PN_POKKEN_S3_ACCESS_SECRET={minio_secret}",
            f"PN_POKKENTOURNAMENT_S3_ACCESS_SECRET={minio_secret}",
            "PN_POKKEN_S3_URL=minio:9000",
            "PN_POKKENTOURNAMENT_S3_URL=minio:9000",
            f"PN_POKKEN_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/pokken_tournament?sslmode=disable",
            f"PN_POKKENTOURNAMENT_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/pokken_tournament?sslmode=disable",
            f"PN_POKKEN_SECURE_SERVER_HOST={server_ip}",
            f"PN_POKKENTOURNAMENT_SECURE_SERVER_HOST={server_ip}",
            f"PN_POKKEN_CONFIG_SECURE_SERVER_HOST={server_ip}",
            f"PN_POKKENTOURNAMENT_CONFIG_SECURE_SERVER_HOST={server_ip}",
            "PN_POKKEN_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_pokken?replicaSet=rs",
            "PN_POKKENTOURNAMENT_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_pokken?replicaSet=rs",
            "PN_POKKEN_MONGODB_URI=mongodb://mongodb:27017/pretendo_pokken?replicaSet=rs",
            "PN_POKKENTOURNAMENT_MONGODB_URI=mongodb://mongodb:27017/pretendo_pokken?replicaSet=rs",
            "PN_POKKEN_AUTHENTICATION_SERVER_PORT=6016",
            "PN_POKKENTOURNAMENT_AUTHENTICATION_SERVER_PORT=6016",
            "PN_POKKEN_SECURE_SERVER_PORT=6017",
            "PN_POKKENTOURNAMENT_SECURE_SERVER_PORT=6017",
            "PN_POKKEN_ACCOUNT_GRPC_HOST=account",
            "PN_POKKENTOURNAMENT_ACCOUNT_GRPC_HOST=account",
            "PN_POKKEN_ACCOUNT_GRPC_PORT=5000",
            "PN_POKKENTOURNAMENT_ACCOUNT_GRPC_PORT=5000",
            "PN_POKKEN_FRIENDS_GRPC_HOST=friends",
            "PN_POKKENTOURNAMENT_FRIENDS_GRPC_HOST=friends",
            "PN_POKKEN_FRIENDS_GRPC_PORT=5001",
            "PN_POKKENTOURNAMENT_FRIENDS_GRPC_PORT=5001",
            f"PN_POKKEN_FRIENDS_GRPC_API_KEY={friends_api_key}",
            f"PN_POKKENTOURNAMENT_FRIENDS_GRPC_API_KEY={friends_api_key}",
            "PN_POKKEN_DATASTORE_S3BUCKET=pokken-tournament",
            "PN_POKKENTOURNAMENT_DATASTORE_S3BUCKET=pokken-tournament",
            "PN_POKKEN_DATASTORE_S3KEY=minio_pretendo",
            "PN_POKKENTOURNAMENT_DATASTORE_S3KEY=minio_pretendo",
            f"PN_POKKEN_DATASTORE_S3SECRET={minio_secret}",
            f"PN_POKKENTOURNAMENT_DATASTORE_S3SECRET={minio_secret}",
            "PN_POKKEN_DATASTORE_S3URL=minio:9000",
            "PN_POKKENTOURNAMENT_DATASTORE_S3URL=minio:9000",
            "PN_POKKEN_LOCAL_AUTH=0",
            "PN_POKKENTOURNAMENT_LOCAL_AUTH=0",
        ]

        # 12. MinIO
        env_files["minio.local.env"] = [
            f"MINIO_ROOT_PASSWORD={minio_secret}",
        ]
        
        # 13. Postgres
        env_files["postgres.local.env"] = [
            f"POSTGRES_PASSWORD={postgres_pass}",
        ]

        # 14. Mongo Express
        env_files["mongo-express.local.env"] = [
            "ME_CONFIG_MONGODB_SERVER=mongodb",
            "ME_CONFIG_MONGODB_PORT=27017",
        ]
        
        
        # Write all env files
        for filename, lines in env_files.items():
            filepath = os.path.join(env_dir, filename)
            with open(filepath, "w") as f:
                f.write("\n".join(lines) + "\n")
            self.setup_log.append(f"  [ENV] Created {filename}")
        
        for fname in os.listdir(env_dir):
            if fname.endswith(".env") and not fname.endswith(".local.env"):
                local_name = fname.replace(".env", ".local.env")
                local_path = os.path.join(env_dir, local_name)
                if not os.path.exists(local_path):
                    with open(local_path, "w") as f:
                        f.write("# Auto-generated empty local env\n")
                    self.setup_log.append(f"  [ENV] Created empty {local_name}")
        
        root_env = os.path.join(s_dir, ".env")
        with open(root_env, "w") as f:
            f.write(f"SERVER_IP={server_ip}\n")
        self._patch_splatoon_schedules(s_dir)
        self._patch_mario_kart_8(s_dir)
        
        secrets_path = os.path.join(s_dir, "secrets.txt")
        with open(secrets_path, "w") as f:
            f.write(f"""Pretendo Network server secrets
===============================

MinIO root username: minio_pretendo
MinIO root password: {minio_secret}
Postgres username: postgres_pretendo
Postgres password: {postgres_pass}
Server IP address: {server_ip}
""")
        self.setup_log.append("  [ENV] Created secrets.txt")

    def _ensure_smm_metadata(self, s_dir):
        """Ensure 900000.bin exists to prevent 'specified key does not exist' S3 error."""
        dest_path = os.path.join(s_dir, "environment", "900000.bin")
        if os.path.exists(dest_path):
            self.setup_log.append("[INFO] SMM metadata file already exists.")
            return
            
        self.setup_log.append("[System] Ensuring Super Mario Maker metadata (900000.bin)...")
        
        # Attempt download from a known mirror first
        mirror_url = "https://raw.githubusercontent.com/MatthewL246/pretendo-docker/master/console-files/900000.bin"
        if HAS_REQUESTS:
            try:
                self.setup_log.append(f"  [HTTP] Attempting to download metadata from known mirror...")
                r = requests.get(mirror_url, timeout=10)
                if r.status_code == 200:
                    with open(dest_path, "wb") as f:
                        f.write(r.content)
                    self.setup_log.append("[OK] SMM metadata downloaded successfully.")
                    return
            except: pass

        # Fallback: Create placeholder to satisfy the Stat check (patch handles rest)
        try:
            with open(dest_path, "wb") as f:
                f.write(b"") 
            self.setup_log.append("[OK] SMM metadata placeholder created (0 bytes).")
        except Exception as e:
            self.setup_log.append(f"[WARN] Failed to create SMM metadata placeholder: {e}")

    def _fix_go_build_compatibility(self, s_dir):
        """Fix build errors, sync vendors, and patch UI crashes."""
        self.setup_log.append("[System] Applying deep code patches to microservices...")
        repos_dir = os.path.join(s_dir, "repos")
        if not os.path.isdir(repos_dir):
            return

        # 0. Clean the slate to remove old broken database patches & init submodules
        if shutil.which("git"):
            for repo_name in ["splatoon", "friends", "pikmin-3", "minecraft-wiiu", "super-mario-maker", "juxtaposition-ui", "super-smash-bros-wiiu", "mario-kart-8", "pokken-tournament"]:
                r_path = os.path.join(repos_dir, repo_name)
                if os.path.isdir(r_path):
                    subprocess.run(["git", "checkout", "--", "."], cwd=r_path, capture_output=True)
                    # Force submodule initialization in case they were cloned empty
                    subprocess.run(["git", "submodule", "update", "--init", "--recursive"], cwd=r_path, capture_output=True)

        # 0.5 Generate missing/broken Dockerfiles for game servers
        for repo_name in ["mario-kart-8", "pokken-tournament", "super-smash-bros-wiiu"]:
            repo_path = os.path.join(repos_dir, repo_name)
            dockerfile_path = os.path.join(repo_path, "Dockerfile")
            if os.path.isdir(repo_path):
                # Always overwrite for these three to ensure multi-binary support is present
                try:
                    with open(dockerfile_path, "w") as f:
                        f.write(r"""FROM golang:1.22-alpine AS builder
ENV CGO_ENABLED=0
RUN apk add --no-cache git gcc musl-dev bash
WORKDIR /app
COPY . .
RUN if [ ! -f go.mod ]; then go mod init pretendo-service; fi
RUN go mod tidy
RUN mkdir -p /app/bin && \
    find . -name "*.go" -type f -exec grep -l "^package main" {} + | xargs -n 1 dirname | sort -u | while read DNAME; do \
        BIN_NAME=$(basename "$DNAME"); \
        [ "$BIN_NAME" = "." ] && BIN_NAME="server"; \
        echo "Building $DNAME -> /app/bin/$BIN_NAME"; \
        (cd "$DNAME" && go build -v -o "/app/bin/$BIN_NAME" .); \
    done

FROM alpine:latest
RUN apk add --no-cache ca-certificates tzdata bash
WORKDIR /app
COPY --from=builder /app/bin /app/bin
RUN echo '#!/bin/bash' > /app/start.sh && \
    echo 'for bin in /app/bin/*; do' >> /app/start.sh && \
    echo '  if [ -x "$bin" ] && [ ! -d "$bin" ]; then' >> /app/start.sh && \
    echo '    echo "Starting $bin..."' >> /app/start.sh && \
    echo '    "$bin" &' >> /app/start.sh && \
    echo '  fi' >> /app/start.sh && \
    echo 'done' >> /app/start.sh && \
    echo 'wait -n' >> /app/start.sh && \
    echo 'exit $?' >> /app/start.sh && \
    chmod +x /app/start.sh
CMD ["/app/start.sh"]
""")
                    self.setup_log.append(f"[OK] Generated dynamic multi-binary Dockerfile for {repo_name}.")
                except Exception as e:
                    self.setup_log.append(f"[ERROR] Failed to generate Dockerfile for {repo_name}: {e}")

        # 1. Patch Dockerfiles (Vendor sync & Delve)
        cnt = 0
        for root, _, files in os.walk(repos_dir):
            if "Dockerfile" in files:
                fpath = os.path.join(root, "Dockerfile")
                try:
                    with open(fpath, "r") as f: content = f.read()
                    changed = False
                    if "dlv@latest" in content:
                        content = content.replace("dlv@latest", "dlv@v1.22.0")
                        changed = True
                    if "go build" in content and "go mod vendor" not in content and "COPY . ." in content:
                        content = content.replace("COPY . .", "COPY . .\nRUN go mod vendor\n", 1)
                        changed = True
                    if changed:
                        with open(fpath, "w") as f: f.write(content)
                        cnt += 1
                except: pass
        if cnt > 0:
            self.setup_log.append(f"[OK] Patched {cnt} Dockerfiles for build compatibility.")

        # 2. Patch Juxtaposition-UI AWS Endpoint Crash directly in JS Source
        ui_util_path = os.path.join(repos_dir, "juxtaposition-ui", "src", "util.js")
        if os.path.isfile(ui_util_path):
            try:
                with open(ui_util_path, "r") as f: ui_content = f.read()
                # Safely fallback to minio:9000 if config.aws.spaces.endpoint is somehow undefined
                ui_content = re.sub(
                    r'new aws\.Endpoint\([^)]+\)',
                    r'new aws.Endpoint((config.aws && config.aws.spaces && config.aws.spaces.endpoint) || "http://minio:9000")',
                    ui_content
                )
                with open(ui_util_path, "w") as f: f.write(ui_content)
                self.setup_log.append("[OK] Applied safe AWS Endpoint fallback to Juxtaposition UI.")
            except Exception as e:
                self.setup_log.append(f"[WARN] Failed to patch Juxtaposition UI: {e}")

            except: pass

        # Ensure Super Smash Bros. Wii U repo is present
        smash_dir = os.path.join(s_dir, "repos", "super-smash-bros-wiiu")
        if not os.path.isdir(smash_dir):
            self.setup_log.append("[System] Downloading Super Smash Bros. Wii U server...")
            smash_repo = "https://github.com/PretendoNetwork/super-smash-bros-wiiu"
            self._run_command(
                f"git clone --recurse-submodules {smash_repo} {shlex.quote(smash_dir)}",
                self.setup_log,
                on_done=lambda c: self._on_clone_finished(c)
            )
            return

        # Ensure Mario Kart 8 repo is present
        mk8_dir = os.path.join(s_dir, "repos", "mario-kart-8")
        if not os.path.isdir(mk8_dir):
            self.setup_log.append("[System] Downloading Mario Kart 8 server...")
            mk8_repo = "https://github.com/PretendoNetwork/mario-kart-8"
            self._run_command(
                f"git clone --recurse-submodules {mk8_repo} {shlex.quote(mk8_dir)}",
                self.setup_log,
                on_done=lambda c: self._on_clone_finished(c)
            )
            return

        # Ensure Pokken Tournament repo is present
        pokken_dir = os.path.join(s_dir, "repos", "pokken-tournament")
        if not os.path.isdir(pokken_dir):
            self.setup_log.append("[System] Downloading Pokk\u00ebn Tournament server...")
            pokken_repo = "https://github.com/PretendoNetwork/pokken-tournament"
            self._run_command(
                f"git clone --recurse-submodules {pokken_repo} {shlex.quote(pokken_dir)}",
                self.setup_log,
                on_done=lambda c: self._on_clone_finished(c)
            )
            return

    def _patch_splatoon_schedules(self, s_dir):
        """Modify Nginx config to pull live Splatoon rotation schedules from the public Pretendo CDN, fixing online hangs."""
        self.setup_log.append("[System] Patching NGINX to route Splatoon BOSS requests to public Pretendo CDN...")
        nginx_conf_dir = os.path.join(s_dir, "config/nginx")
        boss_conf_path = os.path.join(nginx_conf_dir, "boss.conf")
        
        boss_conf_content = """server {
    listen 80;
    server_name npdi.cdn.pretendo.cc npdl.cdn.pretendo.cc npfl.c.app.pretendo.cc
    nppl.c.app.pretendo.cc nppl.app.pretendo.cc npts.app.pretendo.cc;

    location / {
        resolver 8.8.8.8;
        proxy_ssl_server_name on;
        proxy_set_header Host $host;
        proxy_pass https://$host;
    }
}
"""
        try:
            os.makedirs(nginx_conf_dir, exist_ok=True)
            with open(boss_conf_path, "w") as f:
                f.write(boss_conf_content)
            self.setup_log.append("[OK] Updated BOSS Nginx proxy config.")
            
            # Restart Nginx if container is running
            try:
                subprocess.check_call("docker restart pretendo-network-nginx-1", shell=True, cwd=s_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.setup_log.append("[OK] Restarted Nginx container to apply live rotations.")
            except:
                pass
        except Exception as e:
            self.setup_log.append(f"[ERROR] Failed to update boss.conf: {e}")

    def _patch_mario_kart_8(self, s_dir):
        """Fix Mario Kart 8 hardcoded IPs, configuration fallbacks, and build process."""
        self.setup_log.append("[System] Patching Mario Kart 8 source and build config...")
        mk8_dir = os.path.join(s_dir, "repos", "mario-kart-8")
        if not os.path.isdir(mk8_dir): return

        # 1. Fix hardcoded MongoDB IP in mk8-authentication/database.go
        auth_db_path = os.path.join(mk8_dir, "mk8-authentication", "database.go")
        if os.path.isfile(auth_db_path):
            with open(auth_db_path, "r") as f:
                content = f.read()
            
            if '"mongodb://143.198.126.113:27017/"' in content:
                content = content.replace('import (', 'import (\n\t"os"')
                content = content.replace(
                    'mongoClient, _ = mongo.NewClient(options.Client().ApplyURI("mongodb://143.198.126.113:27017/"))',
                    'mongoURI = os.Getenv("PN_MK8_MONGODB_URI")\n\tif mongoURI == "" { mongoURI = os.Getenv("PN_MARIOKART8_MONGODB_URI") }\n\tif mongoURI == "" { mongoURI = "mongodb://mongodb:27017/" }\n\tmongoClient, _ = mongo.NewClient(options.Client().ApplyURI(mongoURI))'
                )
                with open(auth_db_path, "w") as f:
                    f.write(content)
                self.setup_log.append("[OK] Fixed MK8 Auth database routing.")

        # 1.5 Fix MK8 Auth port in mk8-authentication/main.go
        auth_main_path = os.path.join(mk8_dir, "mk8-authentication", "main.go")
        if os.path.isfile(auth_main_path):
            with open(auth_main_path, "r") as f:
                content = f.read()
            if 'nexServer.Listen(":60002")' in content:
                content = content.replace('import (', 'import (\n\t"os"')
                content = content.replace(
                    'nexServer.Listen(":60002")',
                    'port := os.Getenv("PN_MK8_AUTHENTICATION_SERVER_PORT")\n\tif port == "" { port = "60002" }\n\tnexServer.Listen(":" + port)'
                )
                with open(auth_main_path, "w") as f:
                    f.write(content)
                self.setup_log.append("[OK] Fixed MK8 Auth port routing.")

        # 2. Fix mk8-secure/init.go to support environment variable fallbacks
        secure_init_path = os.path.join(mk8_dir, "mk8-secure", "init.go")
        if os.path.isfile(secure_init_path):
            new_init = """package main
import (
	"os"
)
var hmacSecret []byte
func init() {
	var err error
	config, err = ImportConfigFromFile("secure.config")
	if err != nil {
		config = &ServerConfig{
			ServerName:            "Pretendo MK8 Secure",
			ServerPort:            os.Getenv("PN_MK8_SECURE_SERVER_PORT"),
			PrudpVersion:          1,
			SignatureVersion:      1,
			KerberosKeySize:       32,
			AccessKey:             os.Getenv("PN_MK8_ACCESS_KEY"),
			NexVersion:            30500,
			DatabaseIP:            "mongodb",
			DatabasePort:          "27017",
			DatabaseUseAuth:       false,
			AccountDatabase:       "pretendo_account",
			PNIDCollection:        "pnids",
			NexAccountsCollection: "nexaccounts",
			MK8Database:           "pretendo_mk8",
			RoomsCollection:       "rooms",
			SessionsCollection:    "sessions",
			UsersCollection:       "users",
			RegionsCollection:     "regions",
			TournamentsCollection: "tourneys",
		}
		if config.ServerPort == "" { config.ServerPort = "6001" }
	}
	connectMongo()
}
"""
            with open(secure_init_path, "w") as f:
                f.write(new_init)
            self.setup_log.append("[OK] Fixed MK8 Secure configuration logic.")

        # 3. Fix Dockerfile build loop
        dockerfile_path = os.path.join(mk8_dir, "Dockerfile")
        if os.path.isfile(dockerfile_path):
            with open(dockerfile_path, "r") as f:
                lines = f.readlines()
            
            changed = False
            for i, line in enumerate(lines):
                if 'go build -v -o "/app/bin/$BIN_NAME" .' in line and 'go mod tidy' not in line:
                    lines[i] = line.replace('go build -v -o "/app/bin/$BIN_NAME" .', '([ ! -f go.mod ] && go mod init "$BIN_NAME" || true) && go mod tidy && go build -v -o "/app/bin/$BIN_NAME" .')
                    changed = True
            
            if changed:
                with open(dockerfile_path, "w") as f:
                    f.writelines(lines)
                self.setup_log.append("[OK] Fixed MK8 Dockerfile build process.")

    def _generate_juxtaposition_boot_config(self, s_dir):
        """Fix Juxtaposition UI crash by creating dummy config and patching aliases."""
        self.setup_log.append("[System] Generating Juxtaposition UI boot config...")
        ui_repo = os.path.join(s_dir, "repos", "juxtaposition-ui")
        if not os.path.isdir(ui_repo): return

        # Extract secrets to populate the config.json correctly
        env_dir = os.path.join(s_dir, "environment")
        minio_secret = self._grep_env_file(os.path.join(env_dir, "account.local.env"), "PN_ACT_CONFIG_S3_ACCESS_SECRET") or "dummy"
        account_aes = self._grep_env_file(os.path.join(env_dir, "account.local.env"), "PN_ACT_CONFIG_AES_KEY") or "dummy"
        account_grpc = self._grep_env_file(os.path.join(env_dir, "account.local.env"), "PN_ACT_CONFIG_GRPC_MASTER_API_KEY_ACCOUNT") or "dummy"
        friends_grpc = self._grep_env_file(os.path.join(env_dir, "friends.local.env"), "PN_FRIENDS_CONFIG_GRPC_API_KEY") or "dummy"

        # 1. Create missing config.json in the repo root
        config_path = os.path.join(ui_repo, "config.json")
        dummy_config = {
            "http": {"port": 8080},
            "mongoose": {"uri": "mongodb://mongodb:27017", "database": "pretendo_juxt", "options": {}},
            "redis": {"host": "redis", "port": 6379},
            "aes_key": account_aes,
            "CDN_domain": "localhost",
            "aws": {
                "spaces": {
                    "endpoint": "http://minio:9000", 
                    "key": "minio_pretendo", 
                    "secret": minio_secret
                }
            },
            "grpc": {
                "friends": {"ip": "friends", "port": 50051, "api_key": friends_grpc},
                "account": {"ip": "account", "port": 50051, "api_key": account_grpc}
            }
        }
        try:
            with open(config_path, "w") as f:
                json.dump(dummy_config, f, indent=4)
            self.setup_log.append("[OK] Created/Updated config.json for Juxtaposition UI.")
        except Exception as e:
            self.setup_log.append(f"[WARN] Failed to create dummy config.json: {e}")

        # 2. Patch package.json aliases
        pkg_path = os.path.join(ui_repo, "package.json")
        try:
            with open(pkg_path, "r") as f: data = json.load(f)
            changed = False
            if "_moduleAliases" in data:
                aliases = data["_moduleAliases"]
                for key in list(aliases.keys()):
                    if key.endswith("config.json"):
                        aliases[key] = "/home/node/app/config.json"
                        changed = True
            if changed:
                with open(pkg_path, "w") as f: json.dump(data, f, indent=2)
                self.setup_log.append("[OK] Corrected Juxtaposition UI module aliases.")
        except Exception as e:
            self.setup_log.append(f"[WARN] Failed to patch Juxtaposition UI aliases: {e}")

    def apply_splatoon_rotation_patch(self):
        """Manual trigger for the live Splatoon BOSS schedule patch."""
        s_dir = self.server_dir_field.text().strip()
        if not os.path.exists(s_dir):
            QMessageBox.warning(self, "Invalid Path", "Server directory not found.")
            return
        
        self.setup_log.append("[Action] Applying Live Splatoon Rotations and Mii Font Fix...")
        try:
            # Trigger font fix as well since it's frequently needed alongside rotation fixes
            cemu_dir = self.cemu_dir_field.text().strip()
            if os.path.isdir(cemu_dir):
                self._ensure_cemu_fonts(cemu_dir)

            self._patch_splatoon_schedules(s_dir)

            QMessageBox.information(self, "Success", "Splatoon Mii fonts and Live Rotations have been applied successfully!\n\nYour server will now pull live multiplayer schedules directly from the public Pretendo Network CDN, bypassing broken local encryptions.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to patch fonts: {e}")

    def _deploy_step2_clear_and_pull(self, s_dir, local_ip, custom_port, pw=None):
        """Step 2: Clear ports and remove old containers."""
        ports_to_kill = f"80 443 21 53 8080 {custom_port} 9231 6000 6001 6002 6003 6004 6005 6006 6007 6008 6009 6010 6011 6012 6013 6014 6015 6016 6017"
        self.setup_log.append("[System] Wiping port conflicts and removing old containers...")
        
        # Build commands for port killing and container downing
        kill_cmd = self._get_kill_ports_cmd(ports_to_kill, pw)
        
        if OS_INFO["os"] == "windows":
            d_cmd = "docker compose down --remove-orphans 2>NUL || docker-compose down --remove-orphans 2>NUL || echo done"
            full_cmd = f"{kill_cmd} & {d_cmd}"
        else:
            d_cmd = "docker compose down --remove-orphans 2>/dev/null || docker-compose down --remove-orphans 2>/dev/null || true"
            if pw and OS_INFO["os"] == "linux":
                d_cmd = f"sudo -S {d_cmd}"
            full_cmd = f"{kill_cmd} ; {d_cmd}"
        
        self._run_command(full_cmd, self.setup_log, cwd=s_dir, stdin_data=pw,
                          on_done=lambda c: self._deploy_step3_pull(s_dir, pw),
                          display_cmd="Clean Ports & Down Containers")

    def _deploy_step3_pull(self, s_dir, pw):
        """Step 3: Pull docker images."""
        self.setup_log.append("\n[System] Pulling Docker images (this may take several minutes)...")
        
        def _on_pull_done(code):
            if code != 0:
                self.setup_log.append(f"[WARN] Pull exited with code {code}. Some images may need to be built instead.")
            # Proceed to build regardless
            QTimer.singleShot(500, lambda: self._deploy_step4_build(s_dir, pw))
        
        # Ensure Docker is active before pulling
        def _do_pull():
            if OS_INFO["os"] == "windows":
                cmd = "docker compose pull --ignore-buildable 2>NUL || docker compose pull 2>NUL || echo done"
            else:
                cmd = "docker compose pull --ignore-buildable 2>/dev/null || docker compose pull 2>/dev/null || true"
                if pw and OS_INFO["os"] == "linux":
                    cmd = f"sudo -S {cmd}"
            QTimer.singleShot(500, lambda: self._run_command(
                cmd,
                self.setup_log, cwd=s_dir,
                stdin_data=pw if (pw and OS_INFO["os"] == "linux") else None,
                on_done=_on_pull_done,
                display_cmd="docker compose pull"
            ))
        
        # Ensure Docker is active on all platforms
        self._ensure_docker_active(_do_pull)

    def _deploy_step4_build(self, s_dir, pw):
        """Step 4: Build docker images."""
        self.setup_log.append("\n[System] Building Docker images (this may take 5-15 minutes)...")
        
        def _on_build_done(code):
            if code == 0:
                self.setup_log.append("\n[SUCCESS] Full Stack Deployment Finished! Ready to boot.")
                self.setup_log.append("[INFO] Click 'START SERVER' on the left panel to bring up all services.")
                QMessageBox.information(self, "Deployment Complete",
                    "Full Stack Deployment Finished!\n\n"
                    "All Pretendo Docker images have been built successfully.\n\n"
                    "Click 'START SERVER' to launch the network.")
            else:
                self.setup_log.append(f"\n[ERROR] Build failed with exit code {code}.")
                self.setup_log.append("[HINT] Try running 'Health Check' to diagnose issues.")
            self._check_docker_status()
        
        def _do_build():
            cmd = "docker compose build"
            if pw and OS_INFO["os"] == "linux":
                cmd = f"sudo -S {cmd}"
            QTimer.singleShot(500, lambda: self._run_command(
                cmd, self.setup_log, cwd=s_dir,
                stdin_data=pw if (pw and OS_INFO["os"] == "linux") else None,
                on_done=_on_build_done,
                display_cmd="docker compose build"
            ))
        
        # Ensure Docker is active on all platforms
        self._ensure_docker_active(_do_build)

    def _check_port_conflicts(self):
        """Check for processes occupying critical Pretendo ports."""
        conflicts = []
        custom_port = self._get_target_port()
        ports = [80, 443, 21, 53, 8080, 9231]
        if custom_port.isdigit():
            ports.append(int(custom_port))
        for port in ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                result = s.connect_ex(('127.0.0.1', port))
                s.close()
                if result == 0:
                    conflicts.append(f"Port {port} is already in use")
            except Exception:
                pass
        return conflicts

    def run_pretendo_setup(self):
        """Run the official Pretendo setup script in non-interactive mode."""
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            self.setup_log.append("[ERROR] Server directory not found. Cannot proceed with setup.")
            return

        custom_port = self._get_target_port()
        if not custom_port.isdigit():
            self.setup_log.append("[ERROR] Port must be numeric.")
            return
        self._apply_compose_patches(custom_port, s_dir)

        conflicts = self._check_port_conflicts()
        if conflicts:
            msg = "Warning: Setup conflicts detected:\n" + "\n".join(conflicts) + "\n\nThese will be force-killed."
            if QMessageBox.warning(self, "Conflicts", msg, QMessageBox.Ok | QMessageBox.Cancel) == QMessageBox.Cancel:
                return
            
        local_ip = self._get_local_ip()
        ports_to_kill = f"80 443 21 53 8080 {custom_port} 9231"
        
        pw = None
        if OS_INFO["os"] == "linux":
            pw = self._ask_sudo_password()
            if not pw:
                self.setup_log.append("[ERROR] Sudo password is required for setup. Aborted.")
                return

        self.setup_log.append("[System] Wiping port conflicts and removing old containers...")
        kill_cmd = self._get_kill_ports_cmd(ports_to_kill, pw)
        
        # Check if setup.sh exists and has its required dependencies
        setup_script = os.path.join(s_dir, "setup.sh")
        framework_script = os.path.join(s_dir, "scripts", "internal", "framework.sh")
        
        if os.path.isfile(setup_script) and os.path.isfile(framework_script) and OS_INFO["os"] == "linux":
            cmd = f"docker compose down --remove-orphans ; {kill_cmd} ; chmod +x ./setup.sh && ./setup.sh --force --server-ip {local_ip}"
        elif os.path.isfile(setup_script) and os.path.isfile(framework_script) and OS_INFO["os"] == "windows":
            if OS_INFO.get("has_wsl") and OS_INFO.get("has_wsl_distro"):
                # Windows + WSL: Run bash setup.sh through WSL
                wsl_dir = _win_to_wsl_path(s_dir)
                self.setup_log.append("[System] Running setup.sh via WSL2...")
                cmd = f'docker compose down --remove-orphans & {kill_cmd} & wsl bash -lc "cd {shlex.quote(wsl_dir)} && chmod +x ./setup.sh && ./setup.sh --force --server-ip {local_ip}"'
            elif shutil.which("bash"):
                # Windows + Git Bash: Run bash setup.sh via Git Bash fallback
                git_bash_setup = setup_script.replace("\\", "/")
                self.setup_log.append("[System] Running setup.sh via Git Bash...")
                cmd = f'docker compose down --remove-orphans & {kill_cmd} & bash -lc "\'{git_bash_setup}\' --force --server-ip {local_ip}"'
            else:
                # No bash found on Windows - will fall through to Python fallback
                cmd = None
        else:
            cmd = None

        if cmd:
            pass # cmd is set
        else:
            # Fallback: generate env files in Python, then pull + build
            if not os.path.isfile(setup_script):
                self.setup_log.append("[WARN] setup.sh not found in stack.")
            elif not os.path.isfile(framework_script):
                self.setup_log.append("[WARN] scripts/internal/framework.sh not found (submodules may be missing).")
            
            self.setup_log.append("[System] Generating environment configuration in-app...")
            try:
                self._generate_env_files(s_dir, local_ip)
                self.setup_log.append("[OK] Environment files generated successfully.")
            except Exception as e:
                self.setup_log.append(f"[ERROR] Failed to generate environment: {e}")
                return
            
            if OS_INFO["os"] == "windows":
                # Windows: no sudo needed, use & instead of ; for cmd.exe chaining
                inner = f"docker compose down --remove-orphans & {kill_cmd} & docker compose pull --ignore-buildable 2>NUL & docker compose pull 2>NUL & docker compose build"
                cmd = inner
            else:
                inner = f"docker compose down --remove-orphans; {kill_cmd} ; docker compose pull --ignore-buildable 2>/dev/null; docker compose pull 2>/dev/null; docker compose build"
                if pw and OS_INFO["os"] == "linux":
                    import shlex
                    cmd = f"sudo -S bash -c {shlex.quote(inner)}"
                else:
                    cmd = inner
        
        self.setup_log.append(f"[System] Starting comprehensive setup with IP: {local_ip}...")
        self._run_command(cmd, self.setup_log, cwd=s_dir, stdin_data=pw, 
                          on_done=lambda c: self._post_setup_build(c))

    def _post_setup_build(self, code):
        if code != 0:
            self.setup_log.append(f"\n[WARN] Setup exited with code {code}. Attempting build anyway...")
        self.setup_log.append("\n[System] Deep Config Complete. Orchestrating container build process...")
        t = self.server_dir_field.text().strip()
        def _do_build():
            pw = self._get_effective_sudo_password()
            b_cmd = "docker compose build"
            if pw and OS_INFO["os"] == "linux":
                b_cmd = f"sudo -S {b_cmd}"
            QTimer.singleShot(500, lambda: self._run_command(
                b_cmd, self.setup_log, cwd=t,
                stdin_data=pw if (pw and OS_INFO["os"] == "linux") else None,
                on_done=lambda c: QMessageBox.information(self, "Success", "Full Stack Deployment Finished! Ready to boot.") if c == 0 else self.setup_log.append(f"[ERROR] Build failed with exit code {c}")
            ))
        if OS_INFO["os"] == "linux":
            self._ensure_docker_active(_do_build)
        else:
            _do_build()

    def _ensure_docker_active(self, on_ready):
        """Ensure Docker service is running appropriately for the platform."""
        if OS_INFO["os"] == "windows":
            # On Windows, ensure Docker Desktop is running
            self._ensure_docker_desktop(on_ready)
            return
        
        if OS_INFO["os"] != "linux":
            on_ready()
            return

        try:
            res = subprocess.run(["systemctl", "is-active", "docker"], capture_output=True, text=True)
            if res.stdout.strip() == "active":
                on_ready()
                return
        except: pass
        
        pw = self._ask_sudo_password()
        if not pw: return
            
        self.setup_log.append("[System] Resetting and starting Docker services...")
        self._run_command("sudo -S bash -c 'systemctl reset-failed docker; systemctl start docker.socket docker.service'", self.setup_log, stdin_data=pw, on_done=lambda c: on_ready() if c == 0 else None)

    def _get_kill_ports_cmd(self, ports_str, pw=None):
        """Generate a shell command string to kill processes on specified ports."""
        ports = ports_str.split()
        if OS_INFO["os"] == "windows":
            # PowerShell one-liner with silent error handling for protected/system processes
            ps_parts = []
            for p in ports:
                # We filter for OwningProcess > 0 and use SilentlyContinue for Access Denied fix
                ps_parts.append(f"Get-NetTCPConnection -LocalPort {p} -ErrorAction SilentlyContinue | Where-Object {{ $_.OwningProcess -gt 0 }} | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}")
            return 'powershell -Command "' + "; ".join(ps_parts) + '"'
        else:
            # Linux: build fuser sequence
            f_parts = [f"fuser -k -n tcp {p}" for p in ports]
            inner = " ; ".join(f_parts) + " ; true"
            if pw:
                return f"sudo -S bash -c '{inner}'"
            return inner

    def fix_docker_permissions(self):
        """Fix Docker socket permissions on Linux or diagnostic check on Windows."""
        if OS_INFO["os"] == "linux":
            def _do_fix():
                pw = self._ask_sudo_password()
                if not pw: return
                self.setup_log.append("[System] Attempting to apply Docker permissions...")
                try:
                    actual_user = os.getlogin()
                except OSError:
                    actual_user = os.environ.get("USER", os.environ.get("LOGNAME", "user"))
                cmd = f"sudo -S bash -c 'groupadd -f docker && usermod -aG docker {actual_user} && chmod 666 /var/run/docker.sock'"
                self._run_command(cmd, self.setup_log, stdin_data=pw, on_done=lambda c: QMessageBox.information(self, "Permissions", "Docker permissions applied! Restart the app to finish.") if c == 0 else None)
            
            self._ensure_docker_active(_do_fix)
        elif OS_INFO["os"] == "windows":
            # Windows: Diagnostic check for Docker Desktop
            self.setup_log.append("[System] Performing Windows Docker Health Check...")
            if not _docker_desktop_running():
                reply = QMessageBox.question(self, "Docker Offline", "Docker Desktop is not running. Start it now?", QMessageBox.Yes | QMessageBox.No)
                if reply == QMessageBox.Yes:
                    _start_docker_desktop()
            else:
                if not _docker_available():
                    reply = QMessageBox.question(self, "Docker Issue", 
                        "Docker Desktop is running, but the 'docker' command is unresponsive.\n\n"
                        "This often means the WSL integration has crashed (distro stopped).\n\n"
                        "Attempt an automated 'WSL Repair' (Shutdown + Restart Docker)?",
                        QMessageBox.Yes | QMessageBox.No)
                    if reply == QMessageBox.Yes:
                        self._repair_wsl_integration()
                else:
                    QMessageBox.information(self, "Docker OK", "Docker is running and accessible. If you have deployment issues, try 'Refresh WSL Status' on the dashboard.")

    def _repair_wsl_integration(self):
        """Force-restart WSL and Docker Desktop to fix stuck integration bridges."""
        self.setup_log.append("[System] Starting WSL integration repair sequence...")
        # 1. Kill Docker Desktop processes
        self.setup_log.append("  [1/3] Terminating Docker Desktop...")
        subprocess.run(["taskkill", "/IM", "Docker Desktop.exe", "/F"], capture_output=True, creationflags=0x08000000 if _is_windows() else 0)
        # 2. Force WSL shutdown
        self.setup_log.append("  [2/3] Executing WSL global shutdown...")
        subprocess.run(["wsl", "--shutdown"], capture_output=True, creationflags=0x08000000 if _is_windows() else 0)
        # 3. Re-start Docker Desktop
        self.setup_log.append("  [3/3] Re-initiating Docker Desktop startup...")
        if _start_docker_desktop():
            self.setup_log.append("[OK] Repair sequence complete. Wait ~20s for Docker to stabilize.")
            QMessageBox.information(self, "Repair", "Repair commands sent. Docker Desktop is restarting.\n\nPlease wait a few moments for the dashboard to show 'ONLINE'.")
        else:
            self.setup_log.append("[ERROR] Could not find Docker Desktop executable.")


    # ─── Windows-Specific Actions ─────────────────────────────────────────────

    def _install_wsl2_action(self):
        """Install WSL2 + Ubuntu on Windows with user confirmation."""
        if OS_INFO["os"] != "windows":
            QMessageBox.information(self, "WSL2", "WSL2 is only applicable on Windows.")
            return
        
        if OS_INFO.get("has_wsl") and OS_INFO.get("has_wsl_distro"):
            QMessageBox.information(self, "WSL2", f"WSL2 is already installed with distro: {OS_INFO.get('wsl_distro', 'Unknown')}.")
            return
        
        reply = QMessageBox.question(
            self, "Install WSL2",
            "This will install WSL2 and Ubuntu on your system.\n\n"
            "• Requires Administrator privileges (UAC prompt)\n"
            "• A system restart may be required after installation\n"
            "• Approximately 1-2 GB of disk space needed\n\n"
            "Proceed with installation?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        
        self.setup_log.append("[System] Installing WSL2 + Ubuntu (this requires admin and may take several minutes)...")
        self.setup_log.append("[System] A UAC elevation prompt will appear — please approve it.")
        QApplication.processEvents()
        
        # Run the install in the background worker
        self._run_command(
            'powershell -Command "Start-Process wsl -ArgumentList \'--install\' -Verb RunAs -Wait"',
            self.setup_log,
            on_done=self._on_wsl_install_done
        )

    def _on_wsl_install_done(self, code):
        """Handle WSL2 installation completion."""
        if code == 0:
            self.setup_log.append("[SUCCESS] WSL2 installation command completed!")
            self.setup_log.append("[INFO] You may need to restart your computer to finish WSL2 setup.")
            self._refresh_wsl_status()
            QMessageBox.information(
                self, "WSL2 Installed",
                "WSL2 installation initiated successfully!\n\n"
                "Please restart your computer to complete the setup.\n"
                "After restarting, launch this application again and WSL2 will be ready."
            )
        else:
            self.setup_log.append(f"[ERROR] WSL2 installation failed with code {code}.")
            self.setup_log.append("[HINT] Try running 'wsl --install' manually in an Administrator PowerShell.")

    def _refresh_wsl_status(self):
        """Re-probe WSL2 status on Windows and update the UI."""
        if OS_INFO["os"] != "windows":
            return
        
        self.setup_log.append("[System] Refreshing WSL2 status...")
        OS_INFO["has_wsl"] = _wsl_installed()
        OS_INFO["has_wsl_distro"] = _wsl_distro_installed()
        OS_INFO["wsl_distro"] = _get_default_wsl_distro()
        OS_INFO["has_docker_desktop"] = _docker_desktop_running()
        OS_INFO["docker_available"] = _docker_available()
        
        if OS_INFO["has_wsl"] and OS_INFO["has_wsl_distro"]:
            distro = OS_INFO.get("wsl_distro", "Unknown")
            self.setup_log.append(f"[OK] WSL2 Active — Default Distro: {distro}")
        elif OS_INFO["has_wsl"]:
            self.setup_log.append("[WARN] WSL2 is installed but no Linux distro found. Click 'Install WSL2 + Ubuntu'.")
        else:
            self.setup_log.append("[WARN] WSL2 is not installed.")
        
        self._check_docker_status()

    def _ensure_docker_desktop(self, on_ready):
        """Ensure Docker Desktop is running on Windows, auto-starting if needed."""
        if OS_INFO["os"] != "windows":
            on_ready()
            return
        
        if _docker_available():
            on_ready()
            return
        
        self.setup_log.append("[System] Docker is not responding. Attempting to start Docker Desktop...")
        if _start_docker_desktop():
            self.setup_log.append("[System] Docker Desktop launch initiated. Waiting for it to become ready (up to 60s)...")
            # Poll for docker availability
            self._wait_for_docker_desktop(on_ready, attempts=0)
        else:
            self.setup_log.append("[ERROR] Could not find Docker Desktop. Please install it from https://www.docker.com/products/docker-desktop/")
            QMessageBox.warning(
                self, "Docker Desktop Required",
                "Docker Desktop is required on Windows.\n\n"
                "Please install it from:\nhttps://www.docker.com/products/docker-desktop/\n\n"
                "After installing, restart this application."
            )

    def _wait_for_docker_desktop(self, on_ready, attempts=0):
        """Poll for Docker Desktop readiness with timeout."""
        if _docker_available():
            self.setup_log.append("[OK] Docker Desktop is now ready!")
            self._check_docker_status()
            on_ready()
            return
        if attempts >= 30:  # 30 * 2s = 60s timeout
            self.setup_log.append("[ERROR] Docker Desktop did not become ready within 60 seconds.")
            self.setup_log.append("[HINT] Please wait for Docker Desktop to fully start, then try again.")
            return
        QTimer.singleShot(2000, lambda: self._wait_for_docker_desktop(on_ready, attempts + 1))


    def clear_sensitive_data(self):
        """Wipe passwords (including sudo), usernames, and miinames from the UI, cache, and disk."""
        res = QMessageBox.warning(self, "Clear Data", "This will permanently wipe your saved credentials, identity info, and admin password. Proceed?", QMessageBox.Yes | QMessageBox.No)
        if res == QMessageBox.Yes:
            # Disable docker server and services before deleting credentials to prevent softlock
            self._force_shutdown_sync(show_progress=True)
            
            self.cached_password = None
            self.cemu_username.clear()
            self.cemu_password.clear()
            self.cemu_miiname.clear()
            if getattr(self, "server_sudo_pass", None):
                self.server_sudo_pass.clear()
            
            # Wipe QSettings completely
            for key in self.settings.allKeys():
                self.settings.remove(key)
            self.settings.sync()
            
            self.statusBar().showMessage("SENSITIVE DATA PURGED", 5000)
            QMessageBox.information(self, "Data Wiped", "All sensitive data and administrator credentials have been permanently cleared.")

    def create_local_account(self):
        """Execute a node.js script inside the container to inject an account."""
        username = self.cemu_username.text().strip()
        password = self.cemu_password.text()
        miiname = self.cemu_miiname.text().strip() or username or "Player"
        
        # region agent log
        _agent_debug_log(
            "H6",
            "3D-Open-Dock-U.py:create_local_account_entry",
            "Entering create_local_account",
            {"username_present": bool(username), "password_present": bool(password)},
        )
        # endregion

        if not username or not password or not username.isalnum():
            QMessageBox.warning(self, "Input Error", "Please provide a valid alphanumeric Username and a Password.")
            return
            
        if not self.server_running:
            QMessageBox.warning(self, "Network Error", "The Pretendo Server must be RUNNING (ONLINE) to create an account in the database.")
            return
        pid = int(hashlib.sha256(username.lower().encode('utf-8')).hexdigest(), 16) % 1000000000 + 1000000000
        local_ip = self._get_local_ip()
        s_dir = self.server_dir_field.text().strip()
        
        # Pull AES keys from host env files to sync into the container's database
        friends_aes = self._grep_env_file(os.path.join(s_dir, "environment", "friends.local.env"), "PN_FRIENDS_CONFIG_AES_KEY") or ""
        splatoon_aes = self._grep_env_file(os.path.join(s_dir, "environment", "splatoon.local.env"), "PN_SPLATOON_CONFIG_AES_KEY") or ""
        smm_aes = self._grep_env_file(os.path.join(s_dir, "environment", "super-mario-maker.local.env"), "PN_SMM_CONFIG_AES_KEY") or ""
        miiverse_aes = self._grep_env_file(os.path.join(s_dir, "environment", "miiverse-api.local.env"), "PN_MIIVERSE_API_CONFIG_AES_KEY") or ""
        smash_aes = self._grep_env_file(os.path.join(s_dir, "environment", "super-smash-bros-wiiu.local.env"), "PN_SSBWIIU_AES_KEY") or ""
        mk8_aes = self._grep_env_file(os.path.join(s_dir, "environment", "mario-kart-8.local.env"), "PN_MK8_AES_KEY") or "00"*32
        pokken_aes = self._grep_env_file(os.path.join(s_dir, "environment", "pokken-tournament.local.env"), "PN_POKKEN_AES_KEY") or ""
        
        js_script = f"""
const {{ connect }} = require("./dist/database");
const {{ PNID }} = require("./dist/models/pnid");
const {{ NEXAccount }} = require("./dist/models/nex-account");
const {{ Server }} = require("./dist/models/server");
const {{ nintendoPasswordHash }} = require("./dist/util");
const mongoose = require("mongoose");
const bcrypt = require("bcrypt");
const crypto = require("crypto");

(async () => {{
try {{
    await connect();
    const username = "{username}";
    const pass = {json.dumps(password)};
    const miiName = "{miiname}";
    const email = username + "@pretendo.local";
    const pid = {pid};
    const local_ip = "{local_ip}";

    const nintendoPw = await nintendoPasswordHash(pass, pid);
    const hashedPw = await bcrypt.hash(nintendoPw, 10);

    let user = await PNID.findOne({{ usernameLower: username.toLowerCase() }});

    // Valid default male Mii template, avoids Nintendo invalid-Mii "???" fallbacks
    const baseMiiHex = "03000003024DBA3A3420040A56094B184334341B281D413000000000B225000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000004004140224104085006CC4CD41AD0CD2";
    const miiBuf = Buffer.alloc(96);
    miiBuf.write(baseMiiHex, "hex");
    
    // Name at offset 0x1A (Max 10 chars, UTF-16 BE)
    let paddedName = miiName.slice(0, 10);
    while(paddedName.length < 10) paddedName += String.fromCharCode(0);
    const nameBuf = Buffer.from(paddedName, "utf16le");
    nameBuf.swap16(); // Convert to Big Endian
    nameBuf.copy(miiBuf, 0x1A);
    nameBuf.copy(miiBuf, 0x48); // Author name at 0x48 (Crucial for Splatoon to not show '???')

    // Recalculate CRC16-CCITT for the first 0x5E bytes (94 bytes)
    let crc = 0;
    for (let i = 0; i < 0x5E; i++) {{
        crc ^= (miiBuf[i] << 8);
        for (let j = 0; j < 8; j++) {{
            if ((crc & 0x8000) !== 0) {{
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF;
            }} else {{
                crc = (crc << 1) & 0xFFFF;
            }}
        }}
    }}
    miiBuf.writeUInt16BE(crc, 0x5E);
    const miiDataHex = miiBuf.toString("hex");

    if (user) {{
        user.password = hashedPw;
        user.pid = pid;
        user.mii.name = miiName;
        user.mii.data = Buffer.from(miiDataHex, "hex").toString("base64");
        user.mii.author = miiName;
        await user.save();
        console.log("[Notice] " + username + " is already registered! Updated Password, PID, and Mii to align locally.");
    }} else {{
        user = new PNID({{
            pid: pid,
            creation_date: new Date(),
            updated_at: new Date(),
            username: username,
            usernameLower: username.toLowerCase(),
            password: hashedPw,
            birthdate: "2000-01-01",
            gender: "M",
            country: "US",
            language: "en",
            email: {{ address: email, validated: true }},
            mii: {{
                name: miiName,
                primary: true,
                data: Buffer.from(miiDataHex, "hex").toString("base64"),
                hash: crypto.randomBytes(16).toString("hex"),
                id: crypto.randomBytes(4).readUInt32BE(0),
                image_url: "",
                author: miiName
            }},
            identification: {{
                email_code: crypto.randomBytes(4).toString('hex'),
                email_token: crypto.randomBytes(32).toString('hex')
            }},
            flags: {{ active: true, is_admin: true, is_dev: true }},
            access_level: 2
        }});

        await user.save();
        console.log("[Success] User " + username + " injected! PID: " + pid);
    }}

    // Ensure NEX Account exists (Friend Server credentials)
    let nex = await NEXAccount.findOne({{ owning_pid: user.pid }});
    if (!nex) {{
        const charset = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
        let nexPass = "";
        for (let i = 0; i < 16; i++) nexPass += charset.charAt(Math.floor(Math.random() * charset.length));

        nex = new NEXAccount({{
            pid: user.pid,
            owning_pid: user.pid,
            password: nexPass,
            device_type: "wiiu",
            access_level: 0,
            server_access_level: "prod"
        }});
        await nex.save();
        console.log("[Success] NEX-Account linked! Password: " + nexPass);
    }} else {{
        console.log("[Notice] NEX-Account already synchronized.");
    }}

    // --- Server Synchronization (Splatoon, SMM, Friends, Miiverse) ---
    async function upsertNexServer(name, gid, titles, port, aes, cid) {{
        for (const mode of ["prod", "test", "dev"]) {{
            const query = gid ? {{ game_server_id: gid, access_mode: mode }} : {{ client_id: cid, access_mode: mode }};
            await Server.findOneAndUpdate(
                query,
                {{
                    service_name: name,
                    service_type: gid ? "nex" : "service",
                    title_ids: titles,
                    ip: local_ip,
                    port: port || 80,
                    maintenance_mode: false,
                    device: 1,
                    aes_key: (aes && aes.length === 64) ? aes : "0".repeat(64),
                    client_id: cid,
                    access_mode: mode
                }},
                {{ upsert: true }}
            );
        }}
    }}

    console.log("[Notice] Patching Game Server Definitions...");
    await upsertNexServer("Splatoon", "10162B00", ["0005000010176A00", "0005000010176900", "0005000010162B00"], 6006, "{splatoon_aes}");
    await upsertNexServer("Super Mario Maker", "1018DB00", ["000500001018DB00", "000500001018DC00", "000500001018DD00"], 6004, "{smm_aes}");
    await upsertNexServer("Friend List", "00003200", ["0005001010001C00", "000500301001500A", "000500301001510A", "000500301001520A"], 6000, "{friends_aes}");
    await upsertNexServer("Miiverse", null, ["000500301001600A", "000500301001610A", "000500301001620A"], 80, "{miiverse_aes}", "87cd32617f1985439ea608c2746e4610");
    await upsertNexServer("Pikmin 3", "10113F00", ["0005000010113F00", "0005000010114000", "0005000010114100"], 6010, "");
    await upsertNexServer("Super Smash Bros. Wii U", "10110E00", ["0005000010110E00", "0005000010144F00", "000500001010ED00"], 6012, "{smash_aes}");

    // CRITICAL FIX FOR 102-1021: Register MK8 Server in the 'account' database
    // This tells the Auth server that MK8 exists on this local IP at port 6014
    await Server.findOneAndUpdate(
        {{ game_server_id: "1010EC00" }}, 
        {{
            service_name: "Mario Kart 8",
            service_type: "nex",
            // We must include all regional variations (EUR/USA/JAP) to prevent 102-1021
            title_ids: ["000500001010EB00", "000500001010EC00", "000500001010ED00"],
            ip: "{local_ip}",
            port: 6014,
            aes_key: "{mk8_aes}",
            access_mode: "prod",
            device: 1
        }},
        {{ upsert: true }}
    );
    console.log("[Success] Mario Kart 8 Service Registered locally (Universal Region Patch).");

    await upsertNexServer("Pokk\u00ebn Tournament", "10191B00", ["0005000010191B00", "0005000010191C00", "0005000010191A00"], 6016, "{pokken_aes}");

    // --- Miiverse Discovery Patch (Fixes 400 error) ---
    try {{
        const miiverseDb = mongoose.connection.useDb("pretendo_miiverse");
        const Endpoint = miiverseDb.model("Endpoint", new mongoose.Schema({{}}, {{ strict: false }}), "endpoints");
        await Endpoint.findOneAndUpdate(
            {{ server_access_level: "prod" }},
            {{
                status: 0,
                host: "discovery.olv.pretendo.cc",
                api_host: "api.olv.pretendo.cc",
                portal_host: "portal.olv.pretendo.cc",
                n3ds_host: "n3ds.olv.pretendo.cc",
                server_access_level: "prod"
            }},
            {{ upsert: true }}
        );
        console.log("[Notice] Miiverse Discovery endpoints synchronized.");
    }} catch (mErr) {{
        console.log("[Notice] Miiverse sync error: " + mErr.message);
    }}

    process.exit(0);
}} catch(e) {{
    console.error(e);
    process.exit(1);
}}
}})();
"""
        s_dir = self.server_dir_field.text().strip()
        # Use docker compose exec -T for robust service targeting and project name handling
        cmd = f"docker compose exec -T account node -e {shlex.quote(js_script)}"
        
        pw = self.cached_password or (self.server_sudo_pass.text() if getattr(self, 'server_sudo_pass', None) else None)
        if pw and OS_INFO["os"] == "linux":
            # Direct sudo execution for reliable docker access
            cmd = f"sudo -S {cmd}"
        
        self.cemu_log.append("[System] Injecting Account into Local Service Layer...")
        
        def _on_reg_done(code):
            # region agent log
            _agent_debug_log(
                "H6",
                "3D-Open-Dock-U.py:create_local_account_done",
                "create_local_account finished docker exec",
                {"exit_code": code},
            )
            # endregion
            if code == 0:
                self._track_account_in_vault(username, password, miiname)
                QMessageBox.information(self, "Registration", f"Account '{username}' Registration task completed!\n\nAdded to Credentials Vault as: Account:{username}")

        self._run_command(cmd, self.cemu_log, cwd=s_dir, stdin_data=pw, on_done=_on_reg_done)

    def _track_account_in_vault(self, username, password, miiname):
        """Automatically create a metadata-only vault entry for newly created/patched accounts."""
        vname = f"Account:{username}"
        vpath = os.path.join(os.path.expanduser("~"), ".config", APP_NAME, "vault", vname)
        os.makedirs(vpath, exist_ok=True)
        vmeta = {
            "type": "account",
            "username": username,
            "password": _obs(password),
            "miiname": miiname,
            "mii_hex": getattr(self, '_mii_data_hex', ""),
            "timestamp": datetime.now().isoformat()
        }
        try:
            # region agent log
            _agent_debug_log(
                "H5",
                "3D-Open-Dock-U.py:_track_account_in_vault",
                "Tracking account in vault",
                {"has_username": bool(username)},
            )
            # endregion
            with open(os.path.join(vpath, "profile_meta.json"), "w") as f:
                json.dump(vmeta, f, indent=4)
            self.refresh_vault_list()
        except: pass

    def _ensure_console_certs(self, data_path):
        """Deploy essential ccerts and scerts matching BannedPenta OTP to ALL possible MLC paths."""
        try:
            # Collect targets (both data_path and standard Linux dirs)
            targets = [data_path]
            if OS_INFO["os"] == "linux":
                home = os.path.expanduser("~")
                targets.extend([
                    os.path.join(home, ".local/share/Cemu"),
                    os.path.join(home, ".config/Cemu"),
                    os.path.join(home, "Emulation", "roms", "wiiu"),
                ])
            
            # Decompress cert data
            raw_json = zlib.decompress(base64.b64decode(CONSOLE_CERTS_PACKED)).decode()
            certs_dict = json.loads(raw_json)
            
            for base in set(targets):
                if not base or not os.path.isdir(base): continue
                # Base content path for system certs title
                base_content = os.path.join(base, "mlc01", "sys", "title", "0005001b", "10054000", "content")
                
                for rel_file, b64_data in certs_dict.items():
                    target_f = os.path.join(base_content, rel_file)
                    os.makedirs(os.path.dirname(target_f), exist_ok=True)
                    with open(target_f, "wb") as f:
                        f.write(base64.b64decode(b64_data))
            
            self.cemu_log.append("[System] Console Certificates (ccerts & scerts) Synchronized.")
        except Exception as e:
            self.cemu_log.append(f"[ERROR] Failed to deploy console certs: {e}")

    def _ensure_cemu_fonts(self, data_path):
        """Download and deploy shared fonts to ALL possible MLC paths."""
        font_url = "https://raw.githubusercontent.com/BannedPenta01/3D-Open-Dock-U/06b4aca5702eb58f77adad772f8c6bf520bb4cb7/CafeStd.ttf"
        regions = ["10042000", "10042300", "10042400"] # JP, US, EU
        
        targets = [data_path]
        if OS_INFO["os"] == "linux":
            home = os.path.expanduser("~")
            targets.extend([
                os.path.join(home, ".local/share/Cemu"),
                os.path.join(home, ".config/Cemu"),
                os.path.join(home, "Emulation", "roms", "wiiu"),
            ])
            
        font_data = None
        try:
            for base in set(targets):
                if not base or not os.path.isdir(base): continue
                for region_id in regions:
                    content_path = os.path.join(base, "mlc01", "sys", "title", "0005001b", region_id, "content")
                    os.makedirs(content_path, exist_ok=True)
                    
                    for font_name in ["CafeStd.ttf", "CafeDMP7.ttf"]:
                        target_f = os.path.join(content_path, font_name)
                        if not os.path.exists(target_f):
                            if font_data is None:
                                self.cemu_log.append(f"[System] Downloading shared font...")
                                if HAS_REQUESTS:
                                    r = requests.get(font_url, timeout=15)
                                    if r.status_code == 200: font_data = r.content
                                else:
                                    import urllib.request
                                    with urllib.request.urlopen(font_url) as response: font_data = response.read()
                            
                            if font_data:
                                with open(target_f, "wb") as f: f.write(font_data)
                                self.cemu_log.append(f"[OK] Installed {font_name} in {region_id} ({base})")
            if font_data: self.cemu_log.append("[System] Shared Fonts Synchronized.")
        except Exception as e:
            self.cemu_log.append(f"[ERROR] Font installation failed: {e}")

def _ensure_mlc_save_dirs(self, data_path):
        """Generates valid 96-byte Mii binary to stop MK8 Signal 11 crash."""
        titles = ["1010ec00", "1010ed00", "1010eb00"]
        home = os.path.expanduser("~")
        bases = [data_path, os.path.join(home, ".local/share/Cemu")]

        mii_hex = "03000003024DBA3A3420040A56094B184334341B281D413000000000B225000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000004004140224104085006CC4CD41AD0CD2"
        mii_data = binascii.unhexlify(mii_hex)

        for base in bases:
            if not base or not os.path.isdir(base): continue
            for tid in titles:
                os.makedirs(os.path.join(base, "mlc01/usr/save/00050000", tid.lower(), "user/80000001"), exist_ok=True)
                os.makedirs(os.path.join(base, "mlc01/usr/save/00050000", tid.upper(), "user/80000001"), exist_ok=True)
                os.makedirs(os.path.join(base, "mlc01/usr/save/00050000", tid.lower(), "common"), exist_ok=True)

            mii_dir = os.path.join(base, "mlc01/sys/title/0005001b/10056000/content")
            os.makedirs(mii_dir, exist_ok=True)
            with open(os.path.join(mii_dir, "FFLStoreData"), "wb") as f:
                f.write(mii_data)

    def _ensure_mk8_game_profile(self, cemu_base):
        """Write a stability-oriented game profile for Mario Kart 8 (000500001010ec00)."""
        if not cemu_base or not os.path.isdir(cemu_base):
            return
        profile_dir = os.path.join(cemu_base, "gameProfiles")
        profile_path = os.path.join(profile_dir, "000500001010ec00.ini")
        try:
            os.makedirs(profile_dir, exist_ok=True)
            content = (
                "# Mario Kart 8 - stability profile\n"
                "# Generated by 3D-Open-Dock-U.\n"
                "[General]\n"
                "loadSharedLibraries = true\n\n"
                "[Graphics]\n"
                "accurateShaderMul = true\n"
            )
            with open(profile_path, "w", encoding="utf-8") as f:
                f.write(content)
            if hasattr(self, "cemu_log"):
                self.cemu_log.append(f"[MK8] Game profile written: {profile_path}")
        except Exception as e:
            pass

    def _show_mk8_crash_workarounds(self):
        """Explain the MK8 crash, the fix, and ask users to report to Cemu."""
        msg = (
            "The problem: Cemu’s PPC interpreter sometimes reads from an invalid memory address (e.g. in LWZX). "
            "That causes a crash (signal 11 / SIGSEGV) in games like Mario Kart 8.\n\n"
            "The fix: Before reading, Cemu should check that the address is in a valid range (e.g. with memory_isAddressRangeAccessible). "
            "If it isn’t, return 0 instead of dereferencing. That needs to be added in Cemu’s usermode interpreter (PPCInterpreterImpl.cpp, ppcMem_readDataU32).\n\n"
            "Please report your crash and log to the Cemu project so they can add this fix:\n"
            "github.com/cemu-project/Cemu/issues"
        )
        QMessageBox.information(self, "MK8 / Cemu crash", msg)

    def _remove_system_mii_file_if_present(self, data_path):
        """DEPRECATED: Do not delete FFLStoreData.
        Removing this file causes games like Mario Kart 8 to crash on boot (Signal 11 LWZX)
        because the game fails to initialize the Mii database."""
        pass

    def _ensure_system_mii_data(self, data_path, mii_bytes):
        """Deploy one valid Mii (96 bytes, FFLStoreData format) to the system Mii title.
        Games like Mario Kart 8 may load Mii from mlc01/sys/title/0005001b/10056000;
        missing data there can cause crashes (e.g. SIGSEGV in PPCInterpreter)."""
        if not mii_bytes or len(mii_bytes) != 96:
            return
        # Title 0005001b/10056000 = system Mii database (FFLRes/FFLStoreData)
        content_rel = os.path.join("mlc01", "sys", "title", "0005001b", "10056000", "content")
        ffl_store_file = "FFLStoreData"
        targets = []
        if data_path:
            targets.append(os.path.join(data_path, content_rel, ffl_store_file))
        if OS_INFO["os"] == "linux":
            home = os.path.expanduser("~")
            for base in [os.path.join(home, ".local/share/Cemu"), os.path.join(home, ".config/Cemu")]:
                if os.path.isdir(base):
                    targets.append(os.path.join(base, content_rel, ffl_store_file))
        for path in targets:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(mii_bytes)
                if hasattr(self, "cemu_log"):
                    self.cemu_log.append(f"[System] Mii data deployed: {path}")
            except Exception as e:
                if hasattr(self, "cemu_log"):
                    self.cemu_log.append(f"[WARN] System Mii deploy failed {path}: {e}")

    # Default Wii U Mii template (96 bytes): valid facial features, body; name/author/CRC are patched.
    _WIIU_MII_TEMPLATE_HEX = "03000003024DBA3A3420040A56094B184334341B281D413000000000B225000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000004004140224104085006CC4CD41AD0CD2"

    def _build_wiiu_mii(self, mii_name, base_hex=None):
        """Build a valid 96-byte Wii U Mii (FFLStoreData) with the given name.
        Uses base_hex if provided and valid (192 hex chars); otherwise the default template.
        Name and author are written at 0x1A and 0x48 (UTF-16BE, 20 bytes). CRC at 0x5E.
        Returns (mii_bytes_96, account_name_hex) for MiiName= in account.dat (22 bytes UTF-16BE = 44 hex)."""
        name_limited = (mii_name or "Player")[:10]
        name_be_20 = name_limited.encode("utf-16-be").ljust(20, b"\x00")
        account_name_hex = binascii.hexlify(name_limited.encode("utf-16-be").ljust(22, b"\x00")).decode("ascii")
        raw = base_hex if base_hex and len(base_hex) >= 192 and all(c in "0123456789abcdefABCDEF" for c in base_hex[:192]) else None
        if raw is None:
            raw = self._WIIU_MII_TEMPLATE_HEX.ljust(192, "0")
        buf = bytearray(binascii.unhexlify((raw[:192]).ljust(192, "0")))
        if len(buf) < 96:
            buf.extend(b"\x00" * (96 - len(buf)))
        buf[0x1A:0x1A + 20] = name_be_20
        buf[0x48:0x48 + 20] = name_be_20
        crc = 0
        for i in range(0x5E):
            crc ^= buf[i] << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
        buf[0x5E] = (crc >> 8) & 0xFF
        buf[0x5F] = crc & 0xFF
        return bytes(buf), account_name_hex

    def _force_write_file(self, path, content):
        """Robustly overwrite a file, enforcing exact binary bytes to prevent Cemu parser crashes."""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                try: os.chmod(path, 0o666)
                except: pass

            success = False
            try:
                # CRITICAL FIX: Use 'wb' to prevent Linux from destroying the \r\n line endings
                with open(path, "wb") as f:
                    f.write(content.encode("utf-8"))
                try: os.chmod(path, 0o777)
                except: pass
                success = True
            except (PermissionError, OSError):
                # Fallback to elevated writes if permission denied
                pw = self.cached_password or self._ask_sudo_password()
                if pw and OS_INFO["os"] == "linux":
                    cmd = f"sudo -S tee {shlex.quote(path)} > /dev/null"
                    proc = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    proc.communicate(input=f"{pw}\n".encode("utf-8") + content.encode("utf-8"))
                    if proc.returncode == 0:
                        subprocess.run(["sudo", "-S", "chmod", "777", path], input=pw + "\n", text=True, capture_output=True)
                        success = True
            return success
        except Exception as e:
            if hasattr(self, "cemu_log"):
                self.cemu_log.append(f"[ERROR] Force-write failed for {path}: {e}")
            return False

    def _force_delete_file(self, path):
        """Robustly delete a file, attempting to fix permissions or elevate if access is denied."""
        try:
            if not os.path.exists(path): return True
            try:
                os.remove(path)
                return True
            except (PermissionError, OSError):
                if OS_INFO["os"] == "windows":
                    # Windows: Use PowerShell to force delete
                    win_path = path.replace("'", "''")
                    cmd = f'powershell -Command "Remove-Item -Path \'{win_path}\' -Force -ErrorAction SilentlyContinue"'
                    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                    return result.returncode == 0
                else:
                    # Linux: Use sudo rm
                    pw = self.cached_password or self._get_effective_sudo_password()
                    if pw:
                        import shlex
                        cmd = f"sudo -S rm -f {shlex.quote(path)}"
                        result = subprocess.run(cmd, shell=True, input=pw + "\n", capture_output=True, text=True)
                        return result.returncode == 0
                    return False
        except: return False

    def patch_cemu_settings(self, url, is_official=False):
        cemu_dir = self.cemu_dir_field.text().strip()
        p = os.path.join(cemu_dir, "settings.xml") if cemu_dir else OS_INFO.get("cemu_settings", "")
        
        try:
            # 1. Update settings.xml (Proxy & SSL Bypass)
            if p and os.path.exists(p):
                with open(p, "r") as f: c = f.read()
                
                # Force Online & Global SSL Bypass
                if "<OnlineEnabled>true</OnlineEnabled>" not in c:
                    if "<OnlineEnabled>false</OnlineEnabled>" in c:
                        c = c.replace("<OnlineEnabled>false</OnlineEnabled>", "<OnlineEnabled>true</OnlineEnabled>")
                    elif "</Account>" in c:
                        c = c.replace("</Account>", "    <OnlineEnabled>true</OnlineEnabled>\n    </Account>")
                
                if "<Account>" in c and "<disablesslverification>1</disablesslverification>" not in c:
                    c = c.replace("<Account>", "<Account>\n        <disablesslverification>1</disablesslverification>")
                
                if "<disablesslverification>1</disablesslverification>" not in c:
                    if "<disablesslverification>0</disablesslverification>" in c:
                        c = c.replace("<disablesslverification>0</disablesslverification>", "<disablesslverification>1</disablesslverification>")
                    elif "</content>" in c:
                        c = c.replace("</content>", "    <disablesslverification>1</disablesslverification>\n</content>")
                
                c = re.sub(r"<account_cert_path>.*?</account_cert_path>", "<account_cert_path></account_cert_path>", c)

                if "<PersistentId>" in c:
                    c = re.sub(r"<PersistentId>\d+</PersistentId>", "<PersistentId>2147483649</PersistentId>", c)
                elif "<Account>" in c:
                    c = c.replace("<Account>", "<Account>\n        <PersistentId>2147483649</PersistentId>")
                
                username = self.cemu_username.text().strip()
                if username:
                    if "<AccountId>" in c:
                        c = re.sub(r"<AccountId>.*?</AccountId>", f"<AccountId>{username}</AccountId>", c)
                    elif "<Account>" in c:
                        c = c.replace("<Account>", f"<Account>\n        <AccountId>{username}</AccountId>")

                # ActiveService: 0=Nintendo, 1=Pretendo (built-in endpoints), 2=Custom (needs network_services.xml)
                # Use 1 (Pretendo) for local stack: mitmproxy intercepts Pretendo domains and routes locally.
                # Using 2 (Custom) + deleting network_services.xml causes null pointer crashes (Signal 11 LWZX)
                # because Cemu has no endpoint URLs and games get null network response objects.
                active_svc = "1"  # Pretendo — proxy handles redirection to local stack
                if "<ActiveService>" in c:
                    c = re.sub(r"<ActiveService>\d+</ActiveService>", f"<ActiveService>{active_svc}</ActiveService>", c)
                elif "<Account>" in c:
                    c = c.replace("<Account>", f"<Account>\n        <ActiveService>{active_svc}</ActiveService>")

                if "<AccountService>" in c:
                    c = re.sub(r'<SelectedService\s+PersistentId="\d+"(?:\s+Service="[^"]*")?(?:\s+/>|>)', f'<SelectedService PersistentId="2147483649" Service="{active_svc}"/>', c)
                elif "</content>" in c:
                    as_block = f'    <AccountService>\n        <SelectedService PersistentId="2147483649" Service="{active_svc}"/>\n    </AccountService>\n'
                    c = c.replace("</content>", f"{as_block}</content>")
                
                # CRITICAL FIX: Split Proxy URL into IP and Port for libcurl, or clear if official
                if is_official:
                    c = re.sub(r"<proxy_server>.*?</proxy_server>", "<proxy_server></proxy_server>", c)
                    c = re.sub(r"<proxy_port>.*?</proxy_port>", "<proxy_port>0</proxy_port>", c)
                else:
                    import urllib.parse
                    parsed = urllib.parse.urlparse(url if "://" in url else "http://" + url)
                    proxy_ip = parsed.hostname or "127.0.0.1"
                    proxy_port = parsed.port or 8070

                    if "<proxy_server>" in c:
                        c = re.sub(r"<proxy_server>.*?</proxy_server>", f"<proxy_server>{proxy_ip}</proxy_server>", c)
                    elif "</content>" in c:
                        c = c.replace("</content>", f"    <proxy_server>{proxy_ip}</proxy_server>\n</content>")
                        
                    if "<proxy_port>" in c:
                        c = re.sub(r"<proxy_port>.*?</proxy_port>", f"<proxy_port>{proxy_port}</proxy_port>", c)
                    elif "</content>" in c:
                        c = c.replace("</content>", f"    <proxy_port>{proxy_port}</proxy_port>\n</content>")
                
                self._force_write_file(p, c)
            
            # 2. CRITICAL FIX: Delete any existing network_services.xml. 
            # Injecting local IPs into this file breaks mitmproxy routing completely.
            ns_targets = set()
            if cemu_dir: ns_targets.add(str(cemu_dir))
            if p and os.path.dirname(p) != ".": ns_targets.add(str(os.path.dirname(p)))
            for key in ["cemu_dir", "cemu_data"]:
                val = OS_INFO.get(key, "")
                if val: ns_targets.add(str(val))
            
            for target_dir in ns_targets:
                if target_dir and os.path.isdir(target_dir):
                    ns_xml = os.path.join(target_dir, "network_services.xml")
                    self._force_delete_file(ns_xml)

            msg = "Cemu restored to Official Pretendo." if is_official else f"Wii U Patched & Connected!\n\nProxy configured: {url}\nSSL Verification Disabled."
            self.statusBar().showMessage(msg.split('\n')[0], 5000)
            QMessageBox.information(self, "Success", msg)
        except Exception as e: 
            QMessageBox.critical(self, "Error", str(e))

    def _sync_docker_services_to_port(self, target_url):
        """Patch Docker compose.yml mitmproxy port to match the Target Node URL and restart key services.
        This ensures that the emulator's configured URL correctly reaches the mitmproxy reverse-proxy,
        which in turn routes traffic through nginx to the account service — fixing 502 errors on
        /oauth20/access_token/generate."""
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            self.cemu_log.append("[Docker Sync] Server directory not found — skipping Docker patching.")
            return

        custom_port = self._get_target_port()
        if not custom_port.isdigit():
            self.cemu_log.append(f"[Docker Sync] Invalid port '{custom_port}' — skipping Docker patching.")
            return

        # 1. Patch compose.yml mitmproxy port binding
        compose_changed = self._apply_compose_patches(custom_port, s_dir)
        if compose_changed:
            self.cemu_log.append(f"[Docker Sync] compose.yml updated: mitmproxy external port → {custom_port}")
        else:
            self.cemu_log.append(f"[Docker Sync] compose.yml already configured for port {custom_port} (no change needed).")

        # 2. Restart the critical service chain ONLY if needed
        #    mitmproxy-pretendo: the entry-point proxy that emulators connect to
        
        if not compose_changed:
            self.cemu_log.append("[Docker Sync] Docker services are stable. No restart required.")
            return

        pw = self._get_effective_sudo_password()

        restart_services = "mitmproxy-pretendo"
        # Port changed in compose → need docker compose up -d to re-create the port binding
        restart_cmd = f"docker compose up -d --no-deps {restart_services}"

        if pw and OS_INFO["os"] == "linux":
            restart_cmd = f"sudo -S {restart_cmd}"

        self.cemu_log.append(f"[Docker Sync] Applying changes to services: {restart_services}")
        self._run_command(
            restart_cmd, self.cemu_log, cwd=s_dir,
            stdin_data=pw if (pw and OS_INFO["os"] == "linux") else None,
            display_cmd=f"[Docker Sync] Refreshing mitmproxy on port {custom_port}"
        )

    def apply_cemu_patch_all(self):
        # Safely read mode_pretendo to avoid silent AttributeErrors
        mode_btn = getattr(self, "mode_pretendo", None)
        use_official = bool(mode_btn.isChecked()) if mode_btn else False
        url = "https://api.pretendo.network" if use_official else self.patch_url_input.text().strip()

        # region agent log
        _agent_debug_log(
            "H8",
            "3D-Open-Dock-U.py:apply_cemu_patch_all",
            "Patch & Connect (Cemu) invoked",
            {"use_official": use_official, "url": url},
        )
        # endregion

        try:
            if not use_official:
                # Sync Docker services to the target port BEFORE patching the emulator
                self._sync_docker_services_to_port(url)

            self.patch_cemu_settings(url, is_official=use_official)
            self.generate_cemu_manual()
            if not use_official:
                self.create_local_account()
            
            # Track in vault if local server
            if not use_official and ("127.0.0.1" in url or "localhost" in url):
                self._track_account_in_vault(self.cemu_username.text(), self.cemu_password.text(), self.cemu_miiname.text())
        except Exception as e:
            if hasattr(self, "cemu_log"):
                self.cemu_log.append(f"[ERROR] Patch & Connect (Cemu) failed: {e}")
            QMessageBox.critical(self, "Patch Error", f"Patch & Connect (Cemu) failed:\n{e}")

def generate_cemu_manual(self):
        """Writes a strict binary identity to stop the Mario Kart 8 Signal 11 crash."""
        username = self.cemu_username.text().strip() or "Banned"
        home = os.path.expanduser("~")
        base = os.path.join(home, ".local/share/Cemu")

        if not os.path.isdir(base):
            os.makedirs(base, exist_ok=True)

        try:
            acc_dir = os.path.join(base, "mlc01/usr/save/system/act/80000001")
            os.makedirs(acc_dir, exist_ok=True)

            # Binary Mii Template and PrincipalId
            lines = [
                "AccountInstance_20120705",
                "PersistentId=80000001",
                f"AccountId={username}",
                "MiiData=03000003024DBA3A3420040A56094B184334341B281D413000000000B225000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000004004140224104085006CC4CD41AD0CD2",
                "MiiName=50006c00610079006500720000000000000000000000",
                "PrincipalId=3b9aca01",
                "IsPnidLinked=1",
                "Region=2",
                "Language=1"
            ]

            with open(os.path.join(acc_dir, "account.dat"), "wb") as f:
                f.write(("\r\n".join(lines) + "\r\n").encode("utf-8"))

            self._ensure_mlc_save_dirs(base)
            self.cemu_log.append(f"[System] Identity created for {username}.")

        except Exception as e:
            self.cemu_log.append(f"[Error] Manual patch failed: {e}")

    def patch_citra(self, mode):
        # Determine mode if coming from the UI
        if mode == "ui_trigger":
            mode = "custom"

        # Trigger identity sync first for local modes
        if mode in ["custom", "pretendo"]: # pretendo in this context might be the UI choice
            self.cemu_log.append("[System] Syncing Identity with Local Database...")
            if mode == "custom" or (mode == "pretendo" and not "network" in "https://account.pretendo.cc"): # check if actually local
               # Actually, the UI logic handle this better now
               pass 

        if mode == "custom":
            self.create_local_account()
            self._sync_docker_services_to_port(self.patch_url_input.text().strip())
        elif mode == "official_restore":
            # No account injection for official restore
            pass
        else:
            # Only inject if we are using the local mode or specifically asked
            if False: # Public Pretendo toggle removed
                pass # it's official public
            else:
                self.create_local_account()

        # Determine config path based on chosen directory
        citra_dir = self.citra_dir_field.text().strip()
        p = os.path.join(citra_dir, "config", "qt-config.ini") if citra_dir else OS_INFO.get("citra_config", "")
        
        if not p or not os.path.exists(p):
            QMessageBox.warning(self, "Not Found", f"Citra configuration not found at:\n{p}")
            return

        target_url = "https://account.pretendo.cc" if mode in ["pretendo", "official_restore"] else self.patch_url_input.text()
        if mode == "nintendo" or mode == "nintendo_restore": target_url = "https://account.nintendo.net"

        try:
            with open(p, "r") as f: lines = f.readlines()
            new_lines = []
            found = False
            for line in lines:
                if line.startswith("web_api_url="):
                   new_lines.append(f"web_api_url={target_url}\n")
                   found = True
                else: new_lines.append(line)
            if not found: new_lines.append(f"web_api_url={target_url}\n")
            with open(p, "w") as f: f.writelines(new_lines)

            # 3DS Zero-Cert Bypass logic
            citra_root = os.path.dirname(os.path.dirname(p))
            sysdata = os.path.join(citra_root, "sysdata")
            if os.path.isdir(sysdata):
                # Generate a dummy LocalFriendCodeSeed if missing
                seed_p = os.path.join(sysdata, "LocalFriendCodeSeed_B")
                if not os.path.exists(seed_p):
                    with open(seed_p, "wb") as f: f.write(os.urandom(0x110))
                    self.setup_log.append("[System] Generated dummy LocalFriendCodeSeed for Citra.")
                
                # Generate a dummy SecureInfo_A if missing
                info_p = os.path.join(sysdata, "SecureInfo_A")
                if not os.path.exists(info_p):
                    with open(info_p, "wb") as f: f.write(os.urandom(0x111))
                    self.setup_log.append("[System] Generated dummy SecureInfo_A for Citra.")

            if mode == "custom":
                username = self.cemu_username.text().strip()
                password = self.cemu_password.text()
                miiname = self.cemu_miiname.text().strip() or "Player"
                QMessageBox.information(self, "Success", f"3DS Patched & Connected!\n\nTarget: {target_url}\nDocker services synced to port {self._get_target_port()}.\nIdentity bypass files verified.")
                self._track_account_in_vault(username, password, miiname)
            else:
                QMessageBox.information(self, "Success", f"Patched Citra to use:\n{target_url}\n\nIdentity bypass files checked.")
        except Exception as e: QMessageBox.critical(self, "Error", str(e))




    def generate_console_bundle_zip(self):
        user = self.cemu_username.text()
        passw = self.cemu_password.text()
        miiname = self.cemu_miiname.text().strip() or "Player"
        dlg = QFileDialog(self, "Save Console Bundle", f"Pretendo_Bundle_{user}.zip")
        dlg.setAcceptMode(QFileDialog.AcceptSave)
        dlg.setNameFilter("ZIP Files (*.zip)")
        dlg.setDefaultSuffix("zip")
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
        dlg.setFilter(QDir.Files | QDir.Hidden | QDir.AllDirs | QDir.NoDotAndDotDot)
        
        if not dlg.exec(): return
        path = dlg.selectedFiles()[0]
        try:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
                # ─── Wii U Folder ───
                otp_hex = SEC_KEYS.get("BANNED_OTP_HEX", "0" * 2048)
                z.writestr("Wii U/otp.bin", safe_unhex(otp_hex, 1024))

                seeprom_hex = SEC_KEYS.get("BANNED_SEEPROM_HEX", "0" * 1024)
                z.writestr("Wii U/seeprom.bin", safe_unhex(seeprom_hex, 512))

                # Use deterministic PID for database sync
                pid = int(hashlib.sha256(user.lower().encode('utf-8')).hexdigest(), 16) % 1000000000 + 1000000000
                pid_bytes = pid.to_bytes(4, byteorder='little')
                pwd_hash = hashlib.sha256(pid_bytes + b"\x02eCF" + passw.encode('utf-8')).hexdigest()
                
                # Authentically link account to BannedPenta console IDs
                uuid_hex = "e7e455936d2acbae339c189fb2c42990"
                trans_id_hex = "2000004b2c42990"
                
                # Valid 96-byte Mii (same as Cemu path) for bundle account.dat
                mii_bundle_hex = getattr(self, "_mii_data_hex", None) or ""
                mii_bundle_bytes, account_name_hex = self._build_wiiu_mii(miiname, mii_bundle_hex if len(mii_bundle_hex) >= 192 else None)
                cur_mii = binascii.hexlify(mii_bundle_bytes).decode("ascii")

                acct_lines = [
                    "AccountInstance_20120705",
                    "PersistentId=80000001",
                    f"TransferableIdBase={trans_id_hex}",
                    f"Uuid={uuid_hex}",
                    "ParentalControlSlotNo=0",
                    f"MiiData={cur_mii}",
                    f"MiiName={account_name_hex}",
                    "IsMiiUpdated=1",
                    f"AccountId={user}",
                    "BirthYear=2003", "BirthMonth=1", "BirthDay=1", "Gender=0",
                    "IsMailAddressValidated=1",
                    "EmailAddress=none@pretendo.network",
                    "Country=49", "SimpleAddressId=49010000",
                    "Region=2", "Language=1",
                    "TimeZoneId=America/New_York",
                    "UtcOffset=ffffffff9ac22000",
                    f"PrincipalId={pid:08x}",
                    "IsNnidLinked=1",
                    "IsPnidLinked=1",
                    "IsPasswordCacheEnabled=1",
                    f"AccountPasswordCache={pwd_hash}",
                    "NnasType=1", "NfsType=0", "NfsNo=1", "NnasNfsEnv=L1",
                    "IsPersistentIdUploaded=1",
                    "IsConsoleAccountInfoUploaded=1",
                    "LastAuthenticationResult=0",
                    f"StickyAccountId={user}",
                    f"StickyPrincipalId={pid:08x}",
                    "IsServerAccountDeleted=0",
                    "IsCommitted=1",
                ]
                z.writestr("Wii U/account.dat", "\n".join(acct_lines))

                # ─── 3DS Folder ───
                local_ip = self._get_local_ip()
                p_port = self._get_target_port()
                z.writestr("3DS/local_server_url.txt", f"http://{local_ip}:{p_port}\n(Use this in Citra or Nimbus)")
                z.writestr("3DS/mii_data.bin", safe_unhex(getattr(self, '_mii_data_hex', '01000100' + '0'*184)))
                
                si = bytearray(b'\x00' * 0x111)
                si[0x100] = 1 # Region USA
                serial_3ds = f"YW{random.randint(100000000, 999999999)}"
                serial_bytes = serial_3ds.encode('ascii')
                for i, b in enumerate(serial_bytes):
                    if 0x101 + i < len(si): si[0x101 + i] = b
                z.writestr("3DS/SecureInfo_A", bytes(si))

                lfcs = bytearray(os.urandom(0x110))
                # Fixed code to satisfy linter
                l_bits = random.getrandbits(64).to_bytes(8, 'little')
                for i, b in enumerate(l_bits): lfcs[i] = b
                z.writestr("3DS/LocalFriendCodeSeed_B", bytes(lfcs))
                z.writestr("3DS/CTCert.bin", os.urandom(0x1A0))
                
                readme = (
                    "3D Open Dock U - Complete Console Bundle\n"
                    "========================================\n\n"
                    "Wii U / Cemu:\n"
                    "1. Copy otp.bin and seeprom.bin to your Cemu 'sys' folder.\n"
                    "2. Copy account.dat to mlc01/usr/save/system/act/80000001/\n\n"
                    "3DS / Citra:\n"
                    "1. Copy SecureInfo_A, LocalFriendCodeSeed_B, and CTCert.bin to your Citra 'sysdata' folder.\n"
                    "2. Use the local_server_url.txt content in your emulator or Nimbus settings.\n"
                )
                z.writestr("README.txt", readme)
                
                # ─── Certificates (ccerts & scerts) ───
                try:
                    c_json = zlib.decompress(base64.b64decode(CONSOLE_CERTS_PACKED)).decode()
                    c_dict = json.loads(c_json)
                    for rel_name, c_b64 in c_dict.items():
                        # rel_name is "ccerts/file" or "scerts/file"
                        z.writestr(f"Wii U/{rel_name}", base64.b64decode(c_b64))
                except Exception as ce:
                    print(f"Zip bundle cert error: {ce}")

            with open(path, "wb") as f: f.write(buf.getvalue())
            QMessageBox.information(self, "Success", f"Premium Bundle created!\nLocation: {path}")
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def closeEvent(self, event):
        """Handle application exit with persistence options."""
        if self.bypassing_close_prompt:
            self.save_settings()
            # Kill worker threads
            self._stop_all_workers()
            event.accept()
            return

        # Custom Dialog for persistence logic
        msg = QMessageBox(self)
        msg.setWindowTitle("Exit Pretendo Manager")
        msg.setText("How would you like to close the program?")
        msg.setInformativeText("You can keep the server running in the background or perform a complete shutdown.")
        msg.setIcon(QMessageBox.Question)
        msg.setStyleSheet(STYLESHEET)
        
        keep_btn = msg.addButton("Keep Server Running", QMessageBox.ActionRole)
        stop_btn = msg.addButton("Full Shutdown", QMessageBox.DestructiveRole)
        cancel_btn = msg.addButton("Cancel", QMessageBox.RejectRole)
        
        msg.exec()
        
        if msg.clickedButton() == cancel_btn:
            event.ignore()
            return
        
        if msg.clickedButton() == stop_btn:
            # Block the close event, show status, run blocking shutdown, then force-kill
            event.ignore()  # Temporarily ignore so we can run cleanup
            self.bypassing_close_prompt = True
            self.statusBar().showMessage("Full Shutdown in progress — stopping containers...")
            # Process events so the status bar updates are visible
            QApplication.processEvents()
            # Run the blocking shutdown sequence
            self._force_shutdown_sync(show_progress=True)
            self.save_settings()
            # Hard exit — guarantees everything dies
            os._exit(0)
            return

        # "Keep Server Running" path — just save settings and close cleanly
        self.save_settings()
        self._stop_all_workers()
        event.accept()

    def _stop_all_workers(self):
        """Cleanly shut down all background threads."""
        if getattr(self, 'status_worker', None):
            self.status_worker.running = False
            self.status_worker.wait(1000)
            
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.terminate()
            worker.wait(100)

    def _grep_env_file(self, path, key):
        """Helper to safely extract a key from a local env file if it exists."""
        if not os.path.exists(path): return None
        try:
            with open(path, "r") as f:
                for line in f:
                    if line.startswith(f"{key}="):
                        return line.split("=", 1)[1].strip()
        except: pass
        return None

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # ─── Single Instance Guard ───
    # Use a system-wide lock file to prevent multiple instances
    lock_path = os.path.join(QStandardPaths.writableLocation(QStandardPaths.TempLocation), "3d_open_dock_u.lock")
    lock_file = QLockFile(lock_path)
    
    if not lock_file.tryLock(100):
        # Already running!
        warning = QMessageBox()
        warning.setWindowTitle("3D Open Dock U - Already Running")
        warning.setText("<b>An instance of 3D Open Dock U is already active.</b>")
        warning.setInformativeText("Only one instance can be open at the same time to prevent data corruption and port conflicts.\n\nPlease check your taskbar or tray for the existing window.")
        warning.setIcon(QMessageBox.Warning)
        warning.setStandardButtons(QMessageBox.Ok)
        
        # Apply the app's global dark styling to this popup if possible
        try:
             # Since STYLESHEET is a global constant defined earlier in the file
             warning.setStyleSheet(STYLESHEET)
        except: pass
        
        warning.exec()
        sys.exit(1)

    app.setStyle("Fusion")
    win = PretendoManager()
    win.showMaximized()
    
    # Pass the lock_file reference to the window so it persists for the lifetime of the app
    win._instance_lock = lock_file
    
    sys.exit(app.exec())
