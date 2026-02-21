#!/usr/bin/env python3
import os
import sys
import subprocess
import platform
import shutil
import hashlib
import binascii
import json
import random
import re
import socket
import zipfile
import io

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QLineEdit, QTextEdit, QFormLayout,
    QGroupBox, QMessageBox, QFileDialog, QProgressBar, QFrame,
    QScrollArea, QSizePolicy, QSpacerItem, QInputDialog, QDialog,
    QCheckBox, QDialogButtonBox, QScroller, QScrollerProperties,
    QListWidget, QListWidgetItem
)
from PySide6.QtCore import Qt, QThread, Signal, QSize, QSettings, QTimer
from PySide6.QtGui import QFont, QColor, QPalette, QLinearGradient, QPainter

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ─── Constants ────────────────────────────────────────────────────────────────
APP_NAME = "3D Open Dock U"
APP_VERSION = "1.0.1"
PRETENDO_REPO = "https://github.com/MatthewL246/pretendo-docker.git"
PNID_API = "https://pnidlt.gab.net.eu.org/api/v1/pnid/"

def detect_os_info():
    """Detect OS, package manager, and default emulator paths."""
    system = platform.system().lower()
    info = {"os": system, "pkg_mgr": None, "pkg_install": "",
            "cemu_dir": "", "cemu_settings": "", "citra_config": "", "server_dir": "", "distro": ""}

    username = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
    home = os.path.expanduser("~")

    if system == "linux":
        info["server_dir"] = os.path.join(home, "pretendo-docker")
        if shutil.which("pacman"):
            info["pkg_mgr"], info["pkg_install"], info["distro"] = "pacman", "pacman -S --noconfirm docker docker-compose", "Arch Linux"
        elif shutil.which("apt"):
            info["pkg_mgr"], info["pkg_install"], info["distro"] = "apt", "apt install -y docker.io docker-compose", "Debian/Ubuntu"
        elif shutil.which("dnf"):
            info["pkg_mgr"], info["pkg_install"], info["distro"] = "dnf", "dnf install -y docker docker-compose", "Fedora/RHEL"
        
        flatpak_cemu = os.path.join(home, ".var/app/info.cemu.Cemu/data/Cemu")
        config_cemu = os.path.join(home, ".config/Cemu")
        local_cemu = os.path.join(home, ".local/share/Cemu")
        # Check paths in priority order: Flatpak > ~/.config > ~/.local/share
        if os.path.isdir(flatpak_cemu):
            info["cemu_dir"] = flatpak_cemu
        elif os.path.isdir(config_cemu):
            info["cemu_dir"] = config_cemu
        else:
            info["cemu_dir"] = local_cemu
        
        c_dir = info["cemu_dir"] or ""
        info["cemu_settings"] = os.path.join(c_dir, "settings.xml") if c_dir else ""
        
        citra_paths = [
            os.path.join(home, ".config/citra-emu/config/qt-config.ini"),
            os.path.join(home, ".var/app/org.citra_emu.citra/config/citra-emu/config/qt-config.ini"),
            os.path.join(home, ".config/lime-3ds/config/qt-config.ini"),
            os.path.join(home, ".config/azahar-emu/qt-config.ini"),
            os.path.join(home, ".config/EmuDeck/backend/configs/citra-emu/qt-config.ini"),
            os.path.join(home, ".config/EmuDeck/backend/configs/azahar/qt-config.ini")
        ]
        for p in citra_paths:
            if os.path.exists(p):
                info["citra_config"] = p
                break
    elif system == "darwin":
        info["server_dir"] = os.path.join(home, "pretendo-docker")
        info["distro"] = "macOS"
        info["cemu_dir"] = os.path.join(home, "Library/Application Support/Cemu")
        info["cemu_settings"] = os.path.join(info["cemu_dir"], "settings.xml")
    elif system == "windows":
        info["server_dir"] = os.path.join(os.environ.get("USERPROFILE", "C:"), "pretendo-docker")
        info["distro"] = "Windows"
        appdata = os.environ.get("APPDATA", "")
        info["cemu_dir"] = os.path.join(appdata, "Cemu") if appdata else "C:/Cemu"
        c_dir = info["cemu_dir"] or ""
        info["cemu_settings"] = os.path.join(c_dir, "settings.xml") if c_dir else ""
        
        citra_paths = [
            os.path.join(appdata, "Citra/config/qt-config.ini"),
            os.path.join(appdata, "Lime3DS/config/qt-config.ini")
        ] if appdata else []
        for p in citra_paths:
            if os.path.exists(p):
                info["citra_config"] = p
                break
    return info

OS_INFO = detect_os_info()
DEFAULT_SERVER_DIR = OS_INFO["server_dir"]
CEMU_DIR = OS_INFO["cemu_dir"]

WIIU_COMMON_KEY = "d7b00402659ba2abd2cb0db27fa2b197"
WIIU_STARBUCK_ANCAST = "d8b4970a7ed12e1002a0c4bf89bee171740d268b"
ESPRESSO_ANCAST_KEY = "2ba6f692ddbf0b3cd267e9374fa7dd849e80f8ab"

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
            # Sanitize environment: Remove LD_PRELOAD to avoid leakage from AppImages/etc.
            # Add TERM variable to satisfy scripts using tput/ncurses
            env = os.environ.copy()
            env.pop("LD_PRELOAD", None)
            if "TERM" not in env or not env["TERM"]:
                env["TERM"] = "xterm-256color"

            proc = subprocess.Popen(
                self.cmd, shell=True, cwd=self.cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if self.stdin_data is not None else None,
                text=True, bufsize=1
            )
            if self.stdin_data is not None and proc.stdin:
                # Write password immediately with flush
                proc.stdin.write(self.stdin_data + "\n")
                proc.stdin.flush()
                # Do NOT block output reading with sleep here. 
                # Instead, we rely on the pipe staying open until we close it after a brief spin
            
            # Use a non-blocking way to eventually close stdin if it's still open
            stdin_closed = False

            for line in iter(proc.stdout.readline, ''):
                # We no longer close stdin here to avoid race conditions with sudo.
                # It will be closed after the process finishes or the loop ends.

                stripped = line.rstrip()
                # Only filter out the specific password prompt, show other errors
                if 'password for' in stripped.lower() or '[sudo] password' in stripped.lower():
                    continue
                if stripped:
                    self.output.emit(stripped)
            
            if proc.stdin:
                try: proc.stdin.close()
                except: pass
            proc.wait()
            self.finished.emit(proc.returncode)
        except Exception as e:
            self.output.emit(f"ERROR: {e}")
            self.finished.emit(1)

def make_scrollable(widget):
    scroll = QScrollArea()
    scroll.setWidget(widget)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setFrameShape(QFrame.NoFrame)
    scroller = QScroller.scroller(scroll.viewport())
    scroller.grabGesture(scroll.viewport(), QScroller.LeftMouseButtonGesture)
    return scroll

class SudoPasswordDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Administrator Password")
        self.setMinimumWidth(360)
        self.setStyleSheet(STYLESHEET)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Sudo Access Required", styleSheet="font-size: 18px; font-weight: bold;"))
        layout.addWidget(QLabel("Enter your Linux password:"))
        self.pass_field = QLineEdit()
        self.pass_field.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.pass_field)
        self.remember_cb = QCheckBox("Remember for this session")
        self.remember_cb.setStyleSheet("color: #8888AA;")
        layout.addWidget(self.remember_cb)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
    def get_data(self):
        return self.pass_field.text().strip(), self.remember_cb.isChecked()

class PretendoManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(960, 720)
        self.worker = None
        self.cached_password = None
        self.server_running = False
        self.docker_service_running = False
        self.server_dir = DEFAULT_SERVER_DIR
        self.settings = QSettings(APP_NAME, "Config")

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
        self.tabs.addTab(make_scrollable(self._build_server_tab()), "Server")
        self.tabs.addTab(make_scrollable(self._build_vault_tab()), "Vault")
        self.tabs.addTab(make_scrollable(self._build_patch_tab()), "Patching")
        self.tabs.addTab(make_scrollable(self._build_setup_tab()), "Setup")
        self.tabs.addTab(make_scrollable(self._build_guide_tab()), "Guide")
        root.addWidget(self.tabs)

        # Footer Credits & Reset
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(10, 0, 10, 10)
        
        footer = QLabel('Made by BannedPenta AKA Jan Michael | Powered by Gemini 3 Flash & Google Antigravity')
        footer.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
        footer.setAlignment(Qt.AlignCenter)
        footer_layout.addWidget(footer, 1)
        
        self.reset_data_btn = QPushButton("Reset Sensitive Data")
        self.reset_data_btn.setStyleSheet(f"font-size: 10px; color: {RED_LIGHT}; border: 1px solid {RED_DARK}; padding: 4px 8px; background: transparent;")
        self.reset_data_btn.clicked.connect(self.clear_sensitive_data)
        footer_layout.addWidget(self.reset_data_btn)
        
        root.addLayout(footer_layout)

        self.statusBar().setStyleSheet(f"background: {BG_CARD}; color: {TEXT_SECONDARY}; padding: 4px;")
        self.statusBar().showMessage(f"{APP_NAME} v{APP_VERSION} - Ready")
        self.load_settings()
        self._check_docker_status()

    def load_settings(self):
        """Load user configurations from persistent storage."""
        self.cemu_username.setText(str(self.settings.value("username", "")))
        self.cemu_password.setText(str(self.settings.value("password", "")))
        self.cemu_miiname.setText(str(self.settings.value("miiname", "")))
        self.host_port.setText(str(self.settings.value("host_port", "8070")))
        self.cached_password = self.settings.value("sudo_cache", None)
        
        # Load the new password field if remembered
        if self.cached_password:
            self.server_sudo_pass.setText(str(self.cached_password))
            self.server_remember_pass.setChecked(True)
            
        self.refresh_vault_list()

    def save_settings(self):
        """Save current identity fields to persistent storage."""
        self.settings.setValue("username", self.cemu_username.text())
        self.settings.setValue("password", self.cemu_password.text())
        self.settings.setValue("miiname", self.cemu_miiname.text())
        self.settings.setValue("host_port", self.host_port.text())
        
        if self.server_remember_pass.isChecked() and self.server_sudo_pass.text().strip():
             self.settings.setValue("sudo_cache", self.server_sudo_pass.text().strip())
        else:
             self.settings.remove("sudo_cache")
             self.cached_password = None

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except: return "127.0.0.1"

    def _build_server_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(20)

        group = QGroupBox("Server Controls")
        glay = QVBoxLayout(group)
        
        self.status_label = QLabel("OFFLINE")
        self.status_label.setStyleSheet(f"color: {RED_PRIMARY}; font-size: 24px; font-weight: bold;")
        self.status_label.setAlignment(Qt.AlignCenter)
        glay.addWidget(self.status_label)

        self.ip_info = QLabel(f"Local Network IP: {self._get_local_ip()}")
        self.ip_info.setStyleSheet(f"color: {CYAN_LIGHT}; font-size: 16px;")
        self.ip_info.setAlignment(Qt.AlignCenter)
        glay.addWidget(self.ip_info)

        # Sudo Password Input directly in UI
        pass_layout = QHBoxLayout()
        pass_layout.addWidget(QLabel("Admin Password:"))
        self.server_sudo_pass = QLineEdit()
        self.server_sudo_pass.setEchoMode(QLineEdit.Password)
        self.server_sudo_pass.setPlaceholderText("Enter sudo password...")
        pass_layout.addWidget(self.server_sudo_pass)
        self.server_remember_pass = QCheckBox("Remember")
        self.server_remember_pass.stateChanged.connect(lambda: setattr(self, 'cached_password', self.server_sudo_pass.text() if self.server_remember_pass.isChecked() else None))
        pass_layout.addWidget(self.server_remember_pass)
        glay.addLayout(pass_layout)

        self.server_toggle_btn = QPushButton("START SERVER")
        self.server_toggle_btn.setObjectName("startBtn")
        self.server_toggle_btn.setMinimumHeight(64)
        self.server_toggle_btn.clicked.connect(self.toggle_server)
        glay.addWidget(self.server_toggle_btn)
        layout.addWidget(group)

        check_group = QGroupBox("Setup Integrity Check")
        cl = QVBoxLayout(check_group)
        self.check_btn = QPushButton("Check Setup Status", clicked=self.run_setup_check)
        cl.addWidget(self.check_btn)
        self.check_result = QLabel("Setup status not verified.")
        self.check_result.setStyleSheet(f"color: {TEXT_SECONDARY};")
        self.check_result.setWordWrap(True)
        cl.addWidget(self.check_result)
        layout.addWidget(check_group)

        self.server_log = QTextEdit(objectName="logBox")
        self.server_log.setReadOnly(True)
        layout.addWidget(self.server_log)
        return w

    def _build_vault_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        
        vault_group = QGroupBox("Console Profile Vault")
        vlay = QVBoxLayout(vault_group)
        vlay.addWidget(QLabel("Manage multiple online identities. Swap your account.dat, otp.bin, and SecureInfo_A instantly."))
        
        self.profile_list = QListWidget()
        self.profile_list.setMinimumHeight(200)
        vlay.addWidget(self.profile_list)
        
        btn_row = QHBoxLayout()
        self.save_curr_btn = QPushButton("Backup Current Emulator Files", clicked=self.save_to_vault)
        self.apply_sel_btn = QPushButton("Restore Selected Profile", clicked=self.apply_from_vault)
        self.open_vault_btn = QPushButton("Open Vault Folder", clicked=self.open_vault_folder)
        self.delete_prof_btn = QPushButton("Delete Profile", clicked=self.delete_profile)
        btn_row.addWidget(self.save_curr_btn)
        btn_row.addWidget(self.apply_sel_btn)
        btn_row.addWidget(self.open_vault_btn)
        btn_row.addWidget(self.delete_prof_btn)
        vlay.addLayout(btn_row)
        layout.addWidget(vault_group)
        
        # Compatibility Settings
        port_group = QGroupBox("Compatibility & Network Tuning")
        play = QFormLayout(port_group)
        self.host_port = QLineEdit("8070")
        self.host_port.setToolTip("Steam uses 8080. We use 8070 by default to avoid interference.")
        play.addRow("Mitmproxy Host Port:", self.host_port)
        self.apply_port_btn = QPushButton("Save & Patch Server Port", clicked=self.apply_port_tuning)
        play.addRow(self.apply_port_btn)
        layout.addWidget(port_group)
        
        layout.addStretch()
        return w

    def _build_guide_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        
        guide = QTextEdit()
        guide.setReadOnly(True)
        guide.setHtml(f"""
            <h1 style='color:{RED_PRIMARY};'>Pretendo Docker Setup Guide</h1>
            <p>Welcome to the <b>3D Open Dock U</b> server manager! This guide will walk you through setting up your own private 
            Pretendo Network stack using Docker.</p>
            
            <h2 style='color:{CYAN_PRIMARY};'>Phase 1: Prerequisites</h2>
            <ul>
                <li><b>Docker & Docker Compose:</b> Ensure Docker is installed. On Linux, use the <i>Setup</i> tab to fix permissions.</li>
                <li><b>Admin Rights:</b> You need a sudo password for Linux setups (to manage system services and network ports).</li>
            </ul>

            <h2 style='color:{CYAN_PRIMARY};'>Phase 2: Installation</h2>
            <ol>
                <li>Go to the <b>Setup</b> tab.</li>
                <li>Choose a folder (e.g., <code>~/pretendo-docker</code>).</li>
                <li>Click <b>Download Stack</b>. This clones the official Pretendo Docker repository.</li>
                <li>Click <b>Run Full Setup Script</b>. This generates the necessary configuration and <code>.env</code> files.</li>
                <li>Click <b>Build Containers</b>. This fetches the server images (this can take 5-10 minutes).</li>
            </ol>

            <h2 style='color:{CYAN_PRIMARY};'>Phase 3: Network & Ports</h2>
            <p>Steam often uses port <b>8080</b>, which clashes with Pretendo. We have automatically tuned your 
            installation to use port <b>8070</b> to avoid crashes.</p>
            <ul>
                <li>When patching your emulator, use: <code>http://localhost:8070</code></li>
                <li>The <i>Start Server</i> button will automatically clear any port conflicts before launching.</li>
            </ul>

            <h2 style='color:{CYAN_PRIMARY};'>Phase 4: Patching Emulators</h2>
            <h3>Wii U (Cemu)</h3>
            <ul>
                <li>In the <b>Patching</b> tab, enter your PNID credentials.</li>
                <li>Ensure your Cemu folder is correctly detected.</li>
                <li>Click <b>Patch Cemu (Wii U)</b>. The manager will update your <code>settings.xml</code> to point to your local server.</li>
            </ul>
            <h3>3DS (Citra/Lime3DS)</h3>
            <ul>
                <li>Click <b>Patch Citra (3DS)</b> with the Local Server option selected.</li>
            </ul>

            <h2 style='color:{CYAN_PRIMARY};'>Phase 5: The Vault (Profile Management)</h2>
            <p>The <b>Vault</b> is used to manage multiple "identities". You can backup your current 3DS/Wii U online files 
            into named profiles. This allows you to swap between your Official, Pretendo-Public, and Local-Dev 
            accounts with a single click!</p>
            
            <h2 style='color:{RED_LIGHT};'>Common Issues</h2>
            <ul>
                <li><b>"Address already in use":</b> Close Steam or click <i>Stop Server</i> and then <i>Start Server</i> again to force-clear ports.</li>
                <li><b>Docker not active:</b> Ensure the Docker service is enabled in the Setup tab.</li>
                <li><b>"account.dat not found":</b> Make sure you have run the emulator at least once so it creates the file structure.</li>
            </ul>
        """)
        layout.addWidget(guide)
        return w

    def refresh_vault_list(self):
        self.profile_list.clear()
        vault_dir = os.path.join(os.path.expanduser("~"), ".config", APP_NAME, "vault")
        if not os.path.exists(vault_dir): return
        for name in sorted(os.listdir(vault_dir)):
            if os.path.isdir(os.path.join(vault_dir, name)):
                self.profile_list.addItem(name)

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
        # Priority: Common paths -> Custom search
        c_roots = [
            CEMU_DIR,
            os.path.join(home, ".local/share/Cemu"),
            os.path.join(home, ".config/Cemu"),
            os.path.join(home, ".var/app/info.cemu.Cemu/data/Cemu")
        ]
        
        found_cemu = None
        for r in c_roots:
            if r and os.path.exists(os.path.join(r, "mlc01/usr/save/system/act/80000001/account.dat")):
                found_cemu = r
                break
        
        if found_cemu:
            paths["WiiU/account.dat"] = os.path.join(found_cemu, "mlc01/usr/save/system/act/80000001/account.dat")
            paths["WiiU/otp.bin"] = os.path.join(found_cemu, "otp.bin")
            paths["WiiU/seeprom.bin"] = os.path.join(found_cemu, "seeprom.bin")

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
        name, ok = QInputDialog.getText(self, "New Vault Entry", "Profile Name (e.g. My_Backup):")
        if not (ok and name): return
        
        vault_path = os.path.join(os.path.expanduser("~"), ".config", APP_NAME, "vault", name)
        found_paths = self._get_emulator_paths()
        
        count = 0
        for rel_path, src in found_paths.items():
            if os.path.exists(src):
                dest = os.path.join(vault_path, rel_path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                count += 1
        
        self.refresh_vault_list()
        if count > 0:
            QMessageBox.information(self, "Vault", f"Successfully backed up {count} files to '{name}'\nOrganized by console subfolders.")
        else:
            QMessageBox.warning(self, "Vault", "No emulator files found to backup! Check your Cemu/Citra path settings.")

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

        count = 0
        for rel_path, dest in found_paths.items():
            src = os.path.join(vault_path, rel_path)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                count += 1
        
        QMessageBox.information(self, "Vault", f"Profile '{name}' applied ({count} files restored across 3DS/WiiU)!")

    def delete_profile(self):
        item = self.profile_list.currentItem()
        if not item: return
        name = item.text()
        if QMessageBox.question(self, "Delete", f"Are you sure you want to delete profile '{name}'?") == QMessageBox.Yes:
            vault_path = os.path.join(os.path.expanduser("~"), ".config", APP_NAME, "vault", name)
            shutil.rmtree(vault_path, ignore_errors=True)
            self.refresh_vault_list()

    def _apply_port_to_compose(self, port, s_dir):
        """Robust YAML patching for mitmproxy port."""
        for fname in ["compose.yml", "docker-compose.yml"]:
            path = os.path.join(s_dir, fname)
            if not os.path.exists(path): continue
            try:
                with open(path, "r") as f: lines = f.readlines()
                new_lines = []
                in_mitm = False
                changed = False
                for line in lines:
                    if "mitmproxy-pretendo:" in line: in_mitm = True
                    elif line.strip() == "" or (line.lstrip().startswith("-") == False and line.lstrip() != line and "ports:" not in line and "image:" not in line):
                        # Heuristic: if we see a non-indented or new service, we might be out of mitmproxy
                        if not line.startswith(" ") and line.strip() != "": in_mitm = False
                    
                    if in_mitm and "8080:8080" in line:
                        line = line.replace("8080:8080", f"{port}:8080")
                        changed = True
                    new_lines.append(line)
                
                if changed:
                    with open(path, "w") as f: f.writelines(new_lines)
                    return True
            except: pass
        return False

    def apply_port_tuning(self):
        port = self.host_port.text().strip()
        s_dir = self.server_dir_field.text().strip()
        if self._apply_port_to_compose(port, s_dir):
             self.save_settings()
             QMessageBox.information(self, "Success", f"Port updated to {port}.\nRestart server to apply.")
        else:
             QMessageBox.warning(self, "Error", "Could not find or patch compose file. Is the stack downloaded?")

    def run_setup_check(self):
        """Check for missing dependencies and configuration files."""
        missing = []
        if not shutil.which("docker"): missing.append("Docker Engine")
        
        # Check docker-compose (v1 or v2)
        has_compose = False
        if shutil.which("docker-compose"): has_compose = True
        else:
            try:
                res = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
                if res.returncode == 0: has_compose = True
            except: pass
        if not has_compose: missing.append("Docker Compose Plugin")

        # Check docker buildx
        try:
            res = subprocess.run(["docker", "buildx", "version"], capture_output=True, text=True)
            if res.returncode != 0: missing.append("Docker Buildx Plugin")
        except: missing.append("Docker Buildx Plugin")

        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            missing.append(f"Server Directory ({s_dir})")
        else:
            has_yml = os.path.isfile(os.path.join(s_dir, "compose.yml")) or \
                      os.path.isfile(os.path.join(s_dir, "docker-compose.yml"))
            if not has_yml:
                missing.append("compose.yml / docker-compose.yml (Run 'Download Stack' in Setup)")
            
            # Check for required environment files (Pretendo needs .local.env files)
            env_dir = os.path.join(s_dir, "environment")
            if os.path.isdir(env_dir):
                critical_envs = ["miiverse-api.local.env", "account.local.env", "boss.local.env"]
                missing_envs = [e for e in critical_envs if not os.path.isfile(os.path.join(env_dir, e))]
                if missing_envs:
                    missing.append("Missing Environment Configs (Run 'Run Full Setup Script' below)")

        cemu_dir = self.cemu_dir_field.text().strip()
        if not os.path.isdir(cemu_dir):
            missing.append(f"Cemu Directory ({cemu_dir})")

        if not missing:
            self.check_result.setText("System check passed! Nothing is missing.")
            self.check_result.setStyleSheet("color: #3fb950; font-weight: bold;")
        else:
            self.check_result.setText("Missing Items:\n• " + "\n• ".join(missing))
            self.check_result.setStyleSheet(f"color: {RED_PRIMARY}; font-weight: bold;")

    def _build_setup_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        
        group = QGroupBox("Installation")
        glay = QVBoxLayout(group)
        
        self.service_toggle_btn = QPushButton("Enable Docker Service")
        self.service_toggle_btn.clicked.connect(self.toggle_docker_service)
        glay.addWidget(self.service_toggle_btn)

        row = QHBoxLayout()
        self.server_dir_field = QLineEdit(DEFAULT_SERVER_DIR)
        row.addWidget(QLabel("Folder:"))
        row.addWidget(self.server_dir_field)
        glay.addLayout(row)

        glay.addWidget(QPushButton("Download Stack", clicked=self.clone_pretendo))
        
        if OS_INFO["os"] == "linux":
            glay.addWidget(QPushButton("Fix Docker Permissions (Socket)", clicked=self.fix_docker_permissions))
            glay.addWidget(QPushButton("Install Buildx Plugin", clicked=self.install_buildx))
            glay.addWidget(QPushButton("Run Full Setup Script (Fix Env Files)", clicked=self.run_pretendo_setup))
            
        glay.addWidget(QPushButton("Build Containers", clicked=self.build_pretendo))
        layout.addWidget(group)

        self.setup_progress = QProgressBar()
        self.setup_progress.setVisible(False)
        layout.addWidget(self.setup_progress)

        self.setup_log = QTextEdit(objectName="logBox")
        self.setup_log.setReadOnly(True)
        layout.addWidget(self.setup_log)
        return w

    def _build_patch_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        
        # ─── Console Credentials & Files ───
        cred_group = QGroupBox("Console Identity & Credentials")
        crgl = QVBoxLayout(cred_group)
        form = QFormLayout()
        self.cemu_username = QLineEdit()
        form.addRow("Username:", self.cemu_username)
        self.cemu_password = QLineEdit()
        self.cemu_password.setEchoMode(QLineEdit.Password)
        form.addRow("Password:", self.cemu_password)
        self.cemu_miiname = QLineEdit()
        form.addRow("Mii Name:", self.cemu_miiname)
        self.cemu_dir_field = QLineEdit(CEMU_DIR)
        form.addRow("Cemu Folder:", self.cemu_dir_field)
        crgl.addLayout(form)
        
        # PNID Search Section
        pnid_row = QHBoxLayout()
        self.pnid_input = QLineEdit()
        self.pnid_input.setPlaceholderText("Enter PNID to Search...")
        pnid_row.addWidget(self.pnid_input)
        self.pnid_fetch_btn = QPushButton("Fetch PNID Info", clicked=self.fetch_pnid)
        pnid_row.addWidget(self.pnid_fetch_btn)
        crgl.addLayout(pnid_row)
        
        self.pnid_results = QLabel("Search for a PNID to import Mii data.")
        self.pnid_results.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
        self.pnid_results.setWordWrap(True)
        crgl.addWidget(self.pnid_results)
        
        gen_row = QHBoxLayout()
        gen_row.addWidget(QPushButton("Generate All-in-One ZIP Bundle", objectName="patchBtn", clicked=self.generate_console_bundle_zip))
        crgl.addLayout(gen_row)

        self.cemu_log = QTextEdit()
        self.cemu_log.setObjectName("logBox")
        self.cemu_log.setReadOnly(True)
        self.cemu_log.setMaximumHeight(100)
        crgl.addWidget(self.cemu_log)
        layout.addWidget(cred_group)

        # ─── Universal Server Router ───
        group = QGroupBox("Universal Server Patcher")
        glay = QVBoxLayout(group)
        
        # Default to localhost:8070 which matches our anti-conflict tuning
        self.patch_url_input = QLineEdit("http://localhost:8070")
        glay.addWidget(QLabel("Target Server URL (Host Port):"))
        glay.addWidget(self.patch_url_input)



        patch_row = QHBoxLayout()
        patch_row.addWidget(QPushButton("Patch Cemu (Wii U)", objectName="patchBtn", clicked=lambda: self.patch_cemu_settings(self.patch_url_input.text())))
        patch_row.addWidget(QPushButton("Patch Citra/Forks (3DS)", objectName="patchBtn", clicked=lambda: self.patch_citra("custom")))
        glay.addLayout(patch_row)

        nintendo_btn = QPushButton("Quick Restore: Official Nintendo Network", clicked=self.restore_nintendo_official)
        nintendo_btn.setStyleSheet(f"background: {RED_DARK}; border-color: {RED_PRIMARY}; color: white; padding: 12px; font-size: 14px;")
        glay.addWidget(nintendo_btn)

        pretendo_btn = QPushButton("Quick Restore: Official Pretendo Network", clicked=self.restore_pretendo_official)
        pretendo_btn.setStyleSheet(f"background: {CYAN_DARK}; border-color: {CYAN_PRIMARY}; color: white; padding: 12px; font-size: 14px;")
        glay.addWidget(pretendo_btn)

        reset_btn = QPushButton("Reset to Default Settings", clicked=self.reset_to_defaults)
        reset_btn.setStyleSheet(f"background: #8b4513; border-color: #d2691e; color: white; padding: 12px; font-size: 14px;")
        glay.addWidget(reset_btn)
        
        layout.addWidget(group)
        layout.addStretch()
        return w



    def restore_nintendo_official(self):
        """Restore official Nintendo network settings for all emulators."""
        res = QMessageBox.question(self, "Restore Official Nintendo", "This will patch Cemu and Citra to point to official Nintendo servers. Proceed?", QMessageBox.Yes | QMessageBox.No)
        if res == QMessageBox.Yes:
            self.patch_cemu_settings("https://api.nintendo.net")
            self.patch_citra("nintendo_restore")

    def restore_pretendo_official(self):
        """Restore official Pretendo network settings for all emulators."""
        res = QMessageBox.question(self, "Restore Official Pretendo", "This will patch Cemu and Citra to point to https://api.pretendo.network. Proceed?", QMessageBox.Yes | QMessageBox.No)
        if res == QMessageBox.Yes:
            self.patch_cemu_settings("https://api.pretendo.network")
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



    def _check_docker_status(self):
        try:
            res = subprocess.run(["docker", "ps", "--filter", "name=pretendo", "--format", "{{.Names}}"], capture_output=True, text=True)
            self.server_running = bool(res.stdout.strip())
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
                
                # Update Setup tab toggle button with premium gradients
                if self.docker_service_running:
                    self.service_toggle_btn.setText("Disable Docker Service")
                    # System Red Gradient
                    self.service_toggle_btn.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #cb2431, stop:1 #d73a49); border-color: #b31d28; color: white; font-weight: bold; border-radius: 8px; padding: 12px;")
                else:
                    self.service_toggle_btn.setText("Enable Docker Service")
                    # Money Green Gradient
                    self.service_toggle_btn.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1a7f37, stop:1 #2ea043); border-color: #1a5c1a; color: white; font-weight: bold; border-radius: 8px; padding: 12px;")
        except: pass

    def _ask_sudo_password(self):
        # 1. Check the dedicated UI field first
        if hasattr(self, 'server_sudo_pass') and self.server_sudo_pass.text():
            pw = self.server_sudo_pass.text()
            # If they checked remember, sync it to the cache
            if self.server_remember_pass.isChecked():
                self.cached_password = pw
            return pw

        # 2. Check cache
        if self.cached_password: return self.cached_password
        
        # 3. Fallback to dialog
        dlg = SudoPasswordDialog(self)
        if dlg.exec():
            pw, rem = dlg.get_data()
            if not pw: return None
            if rem: 
                self.cached_password = pw
                self.save_settings()
            return pw
        return None

    def _run_command(self, cmd, log_widget, cwd=None, on_done=None, stdin_data=None):
        if self.worker and self.worker.isRunning(): return
        log_widget.append(f"$ {cmd}\n")
        if stdin_data:
            log_widget.append("[System] Submitting credentials to secure process...\n")
        self.worker = CommandWorker(cmd, cwd, stdin_data)
        self.worker.output.connect(log_widget.append)
        def handle_done(code):
            log_widget.append("\nDone." if code == 0 else f"\nFailed ({code})")
            if on_done: on_done(code)
        self.worker.finished.connect(handle_done)
        self.worker.start()

    def toggle_server(self):
        if self.server_running: self.stop_server()
        else: self.start_server()

    def start_server(self):
        s_dir = self.server_dir_field.text().strip()
        custom_port = self.host_port.text().strip()
        
        # Ensure file is patched before starting
        self._apply_port_to_compose(custom_port, s_dir)

        # Aggressive port cleaning
        ports = f"80 443 21 53 8080 {custom_port} 9231"
        fuser_cmd = "; ".join([f"fuser -k -n tcp {p}" for p in ports.split()])
        
        if OS_INFO["os"] == "linux":
            # Attempt to get password from UI or cache without triggering a dialog
            pw = None
            if hasattr(self, 'server_sudo_pass') and self.server_sudo_pass.text():
                pw = self.server_sudo_pass.text()
            elif self.cached_password:
                pw = self.cached_password

            if pw:
                self.server_log.append("[System] Clearing network binds and starting server (Fast-Track)...")
                cmd = f"sudo -S bash -c 'systemctl reset-failed docker; systemctl start docker.socket docker.service; {fuser_cmd} || true'; docker compose up -d"
                self._run_command(cmd, self.server_log, s_dir, stdin_data=pw, on_done=lambda c: self._check_docker_status())
            else:
                self.server_log.append("[System] Starting server containers and clearing ports (Best-Effort)...")
                # Try to kill what we can as current user, then docker up
                cmd = f"{fuser_cmd} || true; docker compose up -d"
                self._run_command(cmd, self.server_log, s_dir, on_done=lambda c: self._check_docker_status())
        else:
            self._run_command("docker compose up -d", self.server_log, s_dir, on_done=lambda c: self._check_docker_status())

    def stop_server(self):
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir): return
        
        custom_port = self.host_port.text().strip()
        ports = f"80 443 21 53 8080 {custom_port} 9231"
        # Optional fuser command for deep cleaning
        fuser_cmd = "; ".join([f"fuser -k -n tcp {p}" for p in ports.split()])
        
        if OS_INFO["os"] == "linux":
            # Attempt to get password from UI or cache without triggering a dialog
            pw = None
            if hasattr(self, 'server_sudo_pass') and self.server_sudo_pass.text():
                pw = self.server_sudo_pass.text()
            elif self.cached_password:
                pw = self.cached_password

            if pw:
                self.server_log.append("[System] Stopping server and force-releasing ports (Fast-Track)...")
                cmd = f"docker compose down; sudo -S bash -c 'systemctl stop docker.socket docker.service; {fuser_cmd} || true'"
                self._run_command(cmd, self.server_log, s_dir, stdin_data=pw, on_done=lambda c: self._check_docker_status())
            else:
                self.server_log.append("[System] Stopping server containers and clearing ports (Best-Effort)...")
                # Try to kill what we can as current user, then docker down
                cmd = f"{fuser_cmd} || true; docker compose down"
                self._run_command(cmd, self.server_log, s_dir, on_done=lambda c: self._check_docker_status())
        else:
            self._run_command("docker compose down", self.server_log, s_dir, on_done=lambda c: self._check_docker_status())

    def toggle_docker_service(self):
        pw = self._ask_sudo_password()
        if not pw: return
        if self.docker_service_running:
            self._run_command("sudo -S bash -c 'systemctl stop docker.socket docker.service'", self.setup_log, stdin_data=pw, on_done=lambda c: self._check_docker_status())
        else:
            self._run_command("sudo -S bash -c 'systemctl reset-failed docker; systemctl start docker.socket docker.service'", self.setup_log, stdin_data=pw, on_done=lambda c: self._check_docker_status())

    def clone_pretendo(self):
        target = self.server_dir_field.text()
        self._run_command(f"git clone --recurse-submodules {PRETENDO_REPO} {target}", self.setup_log)

    def _check_port_conflicts(self):
        """Check for common port conflicts and return a list of process descriptions."""
        conflicts = []
        c_port = 8080
        try: c_port = int(self.host_port.text().strip())
        except: pass
        
        for port in [80, 443, 8080, c_port, 53, 21]:
            if port < 1: continue
            try:
                # Use lsof to find listeners on the port
                res = subprocess.run(f"lsof -i :{port} -sTCP:LISTEN -t", shell=True, capture_output=True, text=True)
                if res.stdout.strip():
                    pids = sorted(list(set(res.stdout.strip().split('\n'))))
                    for pid in pids:
                        name_res = subprocess.run(f"ps -p {pid} -o comm=", shell=True, capture_output=True, text=True)
                        name = name_res.stdout.strip()
                        if name:
                            conflicts.append(f"Port {port}: {name} (PID {pid})")
            except: pass
        return conflicts

    def run_pretendo_setup(self):
        """Run the official Pretendo setup script in non-interactive mode."""
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            QMessageBox.warning(self, "Error", "Server directory not found. Download the stack first.")
            return

        # 1. Patch the port AUTOMATICALLY before setup
        custom_port = self.host_port.text().strip()
        self._apply_port_to_compose(custom_port, s_dir)

        # 2. Check for conflicts
        conflicts = self._check_port_conflicts()
        if conflicts:
            msg = "Warning: The following processes are using ports required by the server:\n\n" + \
                  "\n".join(conflicts) + \
                  "\n\nClose these applications (especially Steam) before continuing, or they may be force-killed."
            if QMessageBox.warning(self, "Port Conflicts Detected", msg, QMessageBox.Ok | QMessageBox.Cancel) == QMessageBox.Cancel:
                return
            
        local_ip = self._get_local_ip()
        # Aggressive port killing including the custom port
        # We use -n tcp and -k to be more thorough
        ports_to_kill = f"80 443 21 53 8080 {custom_port} 9231"
        
        # We need sudo to kill ports like 80/443
        pw = self._ask_sudo_password()
        if not pw: return

        # Pre-cleanup ports AND remove orphans to avoid 'address already in use' errors
        self.setup_log.append("[System] Wiping port conflicts and removing old containers...")
        # Note: we use ; instead of && so setup.sh runs even if docker down fails due to missing net
        # Use a more aggressive fuser command: fuser -k -n tcp <port>
        fuser_cmd = "; ".join([f"fuser -k -n tcp {p}" for p in ports_to_kill.split()])
        fuser_cmd += "; " + "; ".join([f"fuser -k -n udp {p}" for p in ["53", "9231"]])
        
        cmd = f"docker compose down --remove-orphans; sudo -S bash -c '{fuser_cmd} || true' && ./setup.sh --force --server-ip {local_ip}"
        
        self.setup_log.append(f"[System] Starting comprehensive setup with IP: {local_ip}...")
        self._run_command(cmd, self.setup_log, cwd=s_dir, stdin_data=pw, 
                          on_done=lambda c: QMessageBox.information(self, "Setup", "Setup script finished!") if c == 0 else None)

    def build_pretendo(self):
        def _do_build():
            t = self.server_dir_field.text()
            self._run_command("docker compose build", self.setup_log, cwd=t)
        
        if OS_INFO["os"] == "linux":
            self._ensure_docker_active(_do_build)
        else:
            _do_build()

    def _ensure_docker_active(self, on_ready):
        """Ensure Docker service is running on Linux before proceeding."""
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

    def clear_sensitive_data(self):
        """Wipe passwords (including sudo), usernames, and miinames from the UI, cache, and disk."""
        res = QMessageBox.warning(self, "Clear Data", "This will permanently wipe your saved credentials, identity info, and admin password. Proceed?", QMessageBox.Yes | QMessageBox.No)
        if res == QMessageBox.Yes:
            self.cached_password = None
            self.cemu_username.clear()
            self.cemu_password.clear()
            self.cemu_miiname.clear()
            self.server_sudo_pass.clear()
            self.server_remember_pass.setChecked(False)
            self.settings.clear()
            self.settings.sync()
            QMessageBox.information(self, "Data Wiped", "All sensitive data and administrator credentials have been permanently cleared.")

    def fix_docker_permissions(self):
        """Fix Docker socket permissions on Linux."""
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

    def install_buildx(self):
        """Install Docker Buildx plugin."""
        def _do_install():
            if not OS_INFO.get("pkg_mgr"):
                QMessageBox.warning(self, "Error", "No supported package manager found (pacman, apt, dnf).")
                return
            pw = self._ask_sudo_password()
            if not pw: return
            pkg = "docker-buildx-plugin" if OS_INFO["pkg_mgr"] == "apt" else "docker-buildx"
            cmd = f"sudo -S {OS_INFO['pkg_install'].replace('docker docker-compose', pkg)}"
            self._run_command(cmd, self.setup_log, stdin_data=pw)

        self._ensure_docker_active(_do_install)

    def patch_cemu_settings(self, url):
        p = OS_INFO.get("cemu_settings", "")
        if not p or not os.path.exists(p):
            QMessageBox.warning(self, "Not Found", f"Cemu settings.xml not found at:\n{p}\n\nMake sure Cemu has been run at least once.")
            return
        try:
            with open(p, "r") as f: c = f.read()
            # Replace existing proxy_server tag
            if re.search(r"<proxy_server>.*?</proxy_server>", c):
                c = re.sub(r"<proxy_server>.*?</proxy_server>", f"<proxy_server>{url}</proxy_server>", c)
            # If no proxy_server tag exists, add it before the closing </content> tag
            elif "</content>" in c:
                c = c.replace("</content>", f"    <proxy_server>{url}</proxy_server>\n</content>")
            else:
                QMessageBox.warning(self, "Error", "Could not find a valid Cemu settings structure.")
                return
            with open(p, "w") as f: f.write(c)
            QMessageBox.information(self, "Success", f"Patched Cemu to use:\n{url}")
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def generate_cemu_manual(self):
        username = self.cemu_username.text().strip()
        password = self.cemu_password.text().strip()
        miiname = self.cemu_miiname.text().strip() or "Player"
        cemu_path = self.cemu_dir_field.text().strip()

        if not username or not password:
            QMessageBox.warning(self, "Error", "Username and password are required.")
            return

        self.cemu_log.clear()
        self.cemu_log.append("Generating Cemu online files...\n")

        try:
            # OTP
            otp = bytearray(1024)
            otp[0x100:0x110] = bytearray(bytes.fromhex(WIIU_COMMON_KEY))
            otp[0x10:0x24] = bytearray(bytes.fromhex(WIIU_STARBUCK_ANCAST))
            otp[0x30:0x44] = bytearray(bytes.fromhex(ESPRESSO_ANCAST_KEY))
            otp[0xB0:0xB4] = bytearray(os.urandom(4))
            
            os.makedirs(cemu_path, exist_ok=True)
            with open(os.path.join(cemu_path, "otp.bin"), "wb") as f: f.write(otp)

            # SEEPROM
            seeprom = bytearray(os.urandom(512))
            serial = f"FW{random.randint(400000000, 799999999)}"
            seeprom[0x170:0x170+len(serial)] = bytearray(serial.encode('ascii'))
            mac = bytes([0x00, 0x19, 0xFD, random.randint(0,255), random.randint(0,255), random.randint(0,255)])
            seeprom[0x10:0x16] = bytearray(mac)
            with open(os.path.join(cemu_path, "seeprom.bin"), "wb") as f: f.write(seeprom)

            # account.dat
            pid = random.randint(1000000000, 2000000000)
            pid_bytes = pid.to_bytes(4, byteorder='little')
            pwd_hash = hashlib.sha256(pid_bytes + bytes([2, 101, 67, 70]) + password.encode('utf-8')).hexdigest()
            uuid_hex = binascii.hexlify(os.urandom(16)).decode('ascii')
            mii_u16 = miiname[:10].encode('utf-16be')
            mii_hex = binascii.hexlify(mii_u16[:20].ljust(22, b'\x00')).decode('ascii')

            lines = [
                "AccountInstance_20120705", "PersistentId=80000001",
                f"Uuid={uuid_hex}", f"MiiName={mii_hex}", f"AccountId={username}",
                f"PrincipalId={pid:08x}", f"AccountPasswordCache={pwd_hash}",
                "ServerAccountStatus=1", "IsCommitted=1"
            ]
            
            acct_dir = os.path.join(cemu_path, "mlc01", "usr", "save", "system", "act", "80000001")
            os.makedirs(acct_dir, exist_ok=True)
            with open(os.path.join(acct_dir, "account.dat"), "w") as f: f.write("\n".join(lines))

            self.cemu_log.append(f"Files generated for {username}!")
            self.cemu_log.append(f"Location: {cemu_path}")
            QMessageBox.information(self, "Success", f"Wii U files generated!")
        except Exception as e:
            self.cemu_log.append(f"Error: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def patch_citra(self, mode):
        p = OS_INFO.get("citra_config", "")
        if not p or not os.path.exists(p):
            QMessageBox.warning(self, "Not Found", "No Citra/Lime3DS config found.\n\nChecked:\n• ~/.config/citra-emu/\n• ~/.config/lime-3ds/\n• Flatpak paths\n\nMake sure your emulator has been run at least once.")
            return
        
        if mode == "official_restore":
            url = "https://api.pretendo.network"
        elif mode == "nintendo_restore":
            url = "https://api.nintendo.net"
        elif mode == "reset_default":
            url = ""
        else:
            url = self.patch_url_input.text() if mode == "custom" else "http://localhost:8070"
            
        try:
            with open(p, "r") as f: lines = f.readlines()
            if url:
                new = [f"web_api_url={url}\n" if l.startswith("web_api_url=") else l for l in lines]
                if not any(l.startswith("web_api_url=") for l in lines): new.append(f"\nweb_api_url={url}\n")
            else:
                new = [l for l in lines if not l.startswith("web_api_url=")]
            
            with open(p, "w") as f: f.writelines(new)
            if url:
                QMessageBox.information(self, "Success", f"Patched Citra with {url}")
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def fetch_pnid(self):
        if not HAS_REQUESTS:
            QMessageBox.warning(self, "Missing Dependency", "The 'requests' Python library is not installed.\nInstall it with: pip install requests")
            return
        user = self.pnid_input.text().strip()
        if not user: return
        try:
            r = requests.get(PNID_API + user)
            if r.status_code == 200:
                self._pnid_data = r.json()
                self.pnid_results.setText(f"Success: Found {user}")
                
                # Auto-fill identity fields (not password)
                self.cemu_username.setText(self._pnid_data.get("username", user))
                
                # Extract and set Mii Name if available
                pnid_name = self._pnid_data.get("name")
                if pnid_name:
                    self.cemu_miiname.setText(pnid_name)
                
                mii = self._pnid_data.get("mii", {}).get("data")
                if mii:
                    import base64
                    self._mii_data_hex = binascii.hexlify(base64.b64decode(mii)).decode()
            else: self.pnid_results.setText("PNID not found on the network.")
        except Exception as e:
            self.pnid_results.setText(f"Fetch failed: {str(e)}")

    def generate_console_bundle_zip(self):
        user = self.cemu_username.text()
        passw = self.cemu_password.text()
        miiname = self.cemu_miiname.text().strip() or "Player"
        path, _ = QFileDialog.getSaveFileName(self, "Save Console Bundle", f"Pretendo_Bundle_{user}.zip", "ZIP Files (*.zip)")
        if not path: return
        try:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
                # ─── Wii U Folder ───
                otp = bytearray(1024)
                # Fixed: Use explicit assignment to satisfy linter and avoid errors
                common_key = bytes.fromhex(WIIU_COMMON_KEY)
                for i, b in enumerate(common_key): otp[0x100 + i] = b
                
                ancast_key = bytes.fromhex(WIIU_STARBUCK_ANCAST)
                for i, b in enumerate(ancast_key): otp[0x10 + i] = b
                
                esp_key = bytes.fromhex(ESPRESSO_ANCAST_KEY)
                for i, b in enumerate(esp_key): otp[0x30 + i] = b
                
                rand4 = os.urandom(4)
                for i, b in enumerate(rand4): otp[0xB0 + i] = b
                z.writestr("Wii U/otp.bin", bytes(otp))

                seeprom = bytearray(os.urandom(512))
                serial = f"FW{random.randint(400000000, 799999999)}"
                serial_enc = serial.encode('ascii')
                for i, b in enumerate(serial_enc): 
                    if 0x170 + i < len(seeprom): seeprom[0x170 + i] = b
                    
                mac = bytes([0x00, 0x19, 0xFD, random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)])
                for i, b in enumerate(mac): seeprom[0x10 + i] = b
                z.writestr("Wii U/seeprom.bin", bytes(seeprom))

                pid = random.randint(1000000000, 2000000000)
                pid_bytes = pid.to_bytes(4, byteorder='little')
                pwd_hash = hashlib.sha256(pid_bytes + bytes([2, 101, 67, 70]) + passw.encode('utf-8')).hexdigest()
                uuid_hex = binascii.hexlify(os.urandom(16)).decode('ascii')
                mii_u16 = miiname[:10].encode('utf-16be')
                mii_hex = binascii.hexlify(mii_u16[:20].ljust(22, b'\x00')).decode('ascii')
                
                acct_lines = [
                    "AccountInstance_20120705", "PersistentId=80000001", f"Uuid={uuid_hex}",
                    f"MiiName={mii_hex}", f"AccountId={user}", f"PrincipalId={pid:08x}",
                    f"AccountPasswordCache={pwd_hash}", "ServerAccountStatus=1", "IsCommitted=1"
                ]
                z.writestr("Wii U/account.dat", "\n".join(acct_lines))

                # ─── 3DS Folder ───
                local_ip = self._get_local_ip()
                p_port = self.host_port.text().strip()
                z.writestr("3DS/local_server_url.txt", f"http://{local_ip}:{p_port}\n(Use this in Citra or Nimbus)")
                z.writestr("3DS/mii_data.bin", bytes.fromhex(getattr(self, '_mii_data_hex', '01000100' + '0'*184)))
                
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

            with open(path, "wb") as f: f.write(buf.getvalue())
            QMessageBox.information(self, "Success", f"Premium Bundle created!\nLocation: {path}")
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def closeEvent(self, event):
        self.save_settings()
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait(2000)
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = PretendoManager()
    win.show()
    sys.exit(app.exec())
