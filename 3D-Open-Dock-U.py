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
import shlex

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
APP_VERSION = "1.0.2"
PRETENDO_REPO = "https://github.com/MatthewL246/pretendo-docker.git"

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
        
        # Dual-path support for Cemu 2.0+ Linux
        if os.path.isdir(config_cemu):
            info["cemu_dir"] = config_cemu # Configuration
            info["cemu_data"] = local_cemu if os.path.isdir(local_cemu) else config_cemu # MLC/Keys
        else:
            info["cemu_dir"] = local_cemu
            info["cemu_data"] = local_cemu
        
        info["cemu_settings"] = os.path.join(info["cemu_dir"], "settings.xml")
        
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
        self.bypassing_close_prompt = False

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

        self.statusBar().setStyleSheet(f"background: {BG_CARD}; color: {TEXT_SECONDARY}; padding: 4px;")
        self.statusBar().showMessage(f"{APP_NAME} v{APP_VERSION} - Ready")
        self.load_settings()
        self.last_connectivity_state = (self._get_local_ip() != "127.0.0.1")
        
        # Start Heartbeat Timer (Checks status & Connectivity every 5s)
        self.heartbeat = QTimer()
        self.heartbeat.timeout.connect(self._on_status_tick)
        self.heartbeat.start(5000)
        self._on_status_tick()

    def load_settings(self):
        """Load user configurations from persistent storage."""
        self.cemu_username.setText(str(self.settings.value("username", "")))
        self.cemu_password.setText(str(self.settings.value("password", "")))
        self.cemu_miiname.setText(str(self.settings.value("miiname", "")))
        self.host_port.setText(str(self.settings.value("host_port", "8070")))
        self.cached_password = self.settings.value("sudo_cache", None)
        
        # Load the new password field if remembered
        if self.cached_password:
            self.server_sudo_pass.blockSignals(True)
            self.server_remember_pass.blockSignals(True)
            self.server_remember_pass.setChecked(True)
            self.server_sudo_pass.setText(str(self.cached_password))
            self.server_sudo_pass.blockSignals(False)
            self.server_remember_pass.blockSignals(False)
            
        self.refresh_vault_list()

    def save_settings(self):
        """Save current identity fields to persistent storage."""
        self.settings.setValue("username", self.cemu_username.text())
        self.settings.setValue("password", self.cemu_password.text())
        self.settings.setValue("miiname", self.cemu_miiname.text())
        self.settings.setValue("host_port", self.host_port.text())
        
        if self.server_remember_pass.isChecked() and self.server_sudo_pass.text().strip():
             self.settings.setValue("sudo_cache", self.server_sudo_pass.text().strip())
             self.cached_password = self.server_sudo_pass.text().strip()
        else:
             self.settings.remove("sudo_cache")
             self.cached_password = None

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

        self.server_toggle_btn = QPushButton("START SERVER")
        self.server_toggle_btn.setObjectName("startBtn")
        self.server_toggle_btn.setMinimumHeight(50)
        self.server_toggle_btn.clicked.connect(self.toggle_server)
        glay.addWidget(self.server_toggle_btn)
        
        btn_hl = QHBoxLayout()
        self.stream_log_btn = QPushButton("Stream Node Logs", clicked=self.stream_docker_logs)
        self.stream_log_btn.setStyleSheet(f"color: {CYAN_LIGHT};")
        btn_hl.addWidget(self.stream_log_btn)
        self.clear_server_log_btn = QPushButton("Clear Logs", clicked=lambda: self.server_log.clear())
        btn_hl.addWidget(self.clear_server_log_btn)
        self.check_btn = QPushButton("Health Check", clicked=self.run_setup_check)
        btn_hl.addWidget(self.check_btn)
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

        row_sys = QHBoxLayout()
        row_sys.addWidget(QPushButton("Fix Perms", clicked=self.fix_docker_permissions))
        row_sys.addWidget(QPushButton("Clear Logs", clicked=lambda: self.setup_log.clear()))
        dlay.addLayout(row_sys)

        rv.addWidget(dep)
        
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
        
        cred = QGroupBox("Console Identity Parameters")
        crgl = QVBoxLayout(cred)
        form = QFormLayout()
        self.cemu_username = QLineEdit()
        self.cemu_username.textChanged.connect(self.save_settings)
        form.addRow("Username:", self.cemu_username)
        self.cemu_password = QLineEdit()
        self.cemu_password.setEchoMode(QLineEdit.Password)
        self.cemu_password.textChanged.connect(self.save_settings)
        form.addRow("Password:", self.cemu_password)
        self.cemu_miiname = QLineEdit()
        self.cemu_miiname.textChanged.connect(self.save_settings)
        form.addRow("Mii Name:", self.cemu_miiname)
        self.cemu_dir_field = QLineEdit(CEMU_DIR)
        form.addRow("Cemu Dir:", self.cemu_dir_field)
        crgl.addLayout(form)
        
        p_row = QHBoxLayout()
        self.create_account_btn = QPushButton("Register Database Account", clicked=self.create_local_account)
        self.create_account_btn.setStyleSheet(f"background: {CYAN_DARK}; color: white; padding: 10px; font-weight: bold; border-radius: 8px;")
        p_row.addWidget(self.create_account_btn)
        
        self.bundle_btn = QPushButton("Gen Console Zip", objectName="patchBtn", clicked=self.generate_console_bundle_zip)
        self.bundle_btn.setStyleSheet(f"padding: 10px; font-weight: bold; border-radius: 8px;")
        p_row.addWidget(self.bundle_btn)
        crgl.addLayout(p_row)
        

        self.cemu_log = QTextEdit(objectName="logBox")
        self.cemu_log.setReadOnly(True)
        self.cemu_log.setMaximumHeight(80)
        crgl.addWidget(self.cemu_log)
        
        lv.addWidget(cred)
        
        net = QGroupBox("Universal Network Router")
        nlay = QVBoxLayout(net)
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Target Node:"))
        current_ip = self._get_local_ip()
        self.patch_url_input = QLineEdit(f"http://{current_ip}:8070")
        url_row.addWidget(self.patch_url_input)
        nlay.addLayout(url_row)
        
        pat_row = QHBoxLayout()
        pat_row.addWidget(QPushButton("Patch Wii U (Cemu)", objectName="patchBtn", clicked=lambda: self.patch_cemu_settings(self.patch_url_input.text())))
        pat_row.addWidget(QPushButton("Patch 3DS (Citra)", objectName="patchBtn", clicked=lambda: self.patch_citra("custom")))
        nlay.addLayout(pat_row)
        
        lv.addWidget(net)
        h_layout.addWidget(left, 1)

        # RIGHT PANE: Vault & Restore
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0,0,0,0)
        
        vault = QGroupBox("Offline Credentials Vault")
        vlay = QVBoxLayout(vault)
        vlay.addWidget(QLabel("Swap accounts dynamically without manual file management."))
        self.profile_list = QListWidget()
        self.profile_list.setMinimumHeight(150)
        vlay.addWidget(self.profile_list)
        
        vbtn = QHBoxLayout()
        self.save_curr_btn = QPushButton("Save Active Profile", clicked=self.save_to_vault)
        self.apply_sel_btn = QPushButton("Deploy Saved Loadout", clicked=self.apply_from_vault)
        vbtn.addWidget(self.save_curr_btn)
        vbtn.addWidget(self.apply_sel_btn)
        vlay.addLayout(vbtn)
        
        vbtn2 = QHBoxLayout()
        self.open_vault_btn = QPushButton("Browse Vault...", clicked=self.open_vault_folder)
        self.delete_prof_btn = QPushButton("Erase Setup", clicked=self.delete_profile)
        vbtn2.addWidget(self.open_vault_btn)
        vbtn2.addWidget(self.delete_prof_btn)
        vlay.addLayout(vbtn2)
        
        rv.addWidget(vault)
        
        port_group = QGroupBox("Port Control & Network Adjustments")
        play = QFormLayout(port_group)
        self.host_port = QLineEdit("8070")
        self.apply_port_btn = QPushButton("Sync Custom Port", clicked=self.apply_port_tuning)
        play.addRow("Mitmproxy Binding:", self.host_port)
        play.addRow(self.apply_port_btn)
        rv.addWidget(port_group)
        
        n_row = QHBoxLayout()
        nintendo_btn = QPushButton("Restore Nintendo Services", clicked=self.restore_nintendo_official)
        nintendo_btn.setStyleSheet(f"background: {RED_DARK}; color: white; padding: 8px;")
        pretendo_btn = QPushButton("Restore Pretendo Mainnet", clicked=self.restore_pretendo_official)
        pretendo_btn.setStyleSheet(f"background: {CYAN_DARK}; color: white; padding: 8px;")
        n_row.addWidget(nintendo_btn)
        n_row.addWidget(pretendo_btn)
        rv.addLayout(n_row)
        
        rv.addWidget(QPushButton("Emergency Factory Defaults", clicked=self.reset_to_defaults, styleSheet="background: #8b4513; color: white; padding: 8px;"))

        h_layout.addWidget(right, 1)
        layout.addLayout(h_layout)

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
                <li><b>"IOSU_CRYPTO / RSA Failures":</b> Use the <i>Inject System Keys</i> tool in the Setup tab to fix decryption errors.</li>
                <li><b>"account.dat not found":</b> Make sure you have run the emulator at least once so it creates the file structure.</li>
            </ul>
        """)
        layout.addWidget(guide)
        
        reset_row = QHBoxLayout()
        reset_row.addStretch()
        self.guide_reset_btn = QPushButton("Reset Emulator Settings to Defaults", clicked=self.show_reset_dialog)
        self.guide_reset_btn.setStyleSheet(f"color: {RED_LIGHT}; border-color: {RED_DARK}; padding: 8px 16px;")
        reset_row.addWidget(self.guide_reset_btn)
        layout.addLayout(reset_row)
        
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

    def _apply_compose_patches(self, port, s_dir):
        """Robust YAML patching for mitmproxy port and MongoDB version."""
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
                        
                    if "image: mongo:latest" in line:
                        line = line.replace("mongo:latest", "mongo:4.4")
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
        if self._apply_compose_patches(port, s_dir):
             self.save_settings()
             QMessageBox.information(self, "Success", f"Port updated to {port}.\nRestart server to apply.")
        else:
             QMessageBox.warning(self, "Error", "Could not find or patch compose file. Is the stack downloaded?")

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
                res = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
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
            self.check_result.setText("✔ Audit Passed: System is fully operational.")
            self.check_result.setStyleSheet("color: #3fb950; font-weight: bold;")
        else:
            report = ""
            if missing: report += "❌ MISSING:\n• " + "\n• ".join(missing)
            if warnings: report += ("\n\n" if report else "") + "⚠ WARNINGS:\n• " + "\n• ".join(warnings)
            self.check_result.setText(report)
            self.check_result.setStyleSheet(f"color: {RED_PRIMARY if missing else '#d29922'}; font-weight: bold;")

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

    def inject_cemu_keys(self):
        """Inject real Wii U system keys (OTP/SEEPROM) into the Cemu folder to fix log.txt errors."""
        cemu_dir = self.cemu_dir_field.text().strip()
        if not os.path.isdir(cemu_dir):
            QMessageBox.warning(self, "Error", "Cemu folder not found. Set it in the Patching tab.")
            return

        otp_path = os.path.join(cemu_dir, "otp.bin")
        seeprom_path = os.path.join(cemu_dir, "seeprom.bin")

        try:
            # 1. Generate/Patch OTP (1024 bytes)
            otp = bytearray(1024)
            if os.path.exists(otp_path):
                with open(otp_path, "rb") as f:
                    data = f.read(1024)
                    for i, b in enumerate(data): otp[i] = b
            
            # Inject Keys
            # Common Key at 0x100
            comm = bytes.fromhex(WIIU_COMMON_KEY)
            for i, b in enumerate(comm): otp[0x100 + i] = b
            
            # Starbuck Ancast at 0x10
            star = bytes.fromhex(WIIU_STARBUCK_ANCAST)
            for i, b in enumerate(star): otp[0x10 + i] = b
            
            # Espresso Ancast at 0x30
            espr = bytes.fromhex(ESPRESSO_ANCAST_KEY)
            for i, b in enumerate(espr): otp[0x30 + i] = b
            
            # Randomness for uniqueness
            rand4 = os.urandom(4)
            for i, b in enumerate(rand4): otp[0xB0 + i] = b

            with open(otp_path, "wb") as f: f.write(otp)

            # 2. Generate/Patch SEEPROM (512 bytes)
            seep = bytearray(os.urandom(512))
            serial = f"FW{random.randint(400000000, 799999999)}"
            serial_bytes = serial.encode('ascii')
            for i, b in enumerate(serial_bytes):
                if 0x170 + i < 512: seep[0x170 + i] = b
            
            mac = bytes([0x00, 0x19, 0xFD, random.randint(0,255), random.randint(0,255), random.randint(0,255)])
            for i, b in enumerate(mac):
                if 0x10 + i < 0x16: seep[0x10 + i] = b
                
            with open(seeprom_path, "wb") as f: f.write(seep)
            
            QMessageBox.information(self, "Success", "Wii U System Keys injected!\nDecryption errors in log.txt should now be resolved.")
            self.setup_log.append(f"[System] Fully patched {otp_path} and {seeprom_path} with master keys.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Key Injection failed: {e}")


    def _on_status_tick(self):
        """Periodic check for system status and dynamic network sync."""
        self._check_docker_status()
        current_ip = self._get_local_ip()
        is_connected = (current_ip != "127.0.0.1")
        
        # 1. Update IP Label Dynamically
        if hasattr(self, 'ip_info'):
            self.ip_info.setText(f"Local Network IP: {current_ip}")
        
        # 2. Automated Safeguard
        if not is_connected and self.last_connectivity_state:
            if self.server_running or self.docker_service_running:
                msg = "\n[ALARM] Connection Terminated! Triggering Secure Shutdown Protocol...\n"
                self.server_log.append(msg)
                self.setup_log.append(msg)
                
                if self.server_running: self.stop_server()
                if self.docker_service_running: self.toggle_docker_service()
                
                self.statusBar().showMessage("NETWORK LOSS DETECTED - Safe Mode Active", 15000)
        
        self.last_connectivity_state = is_connected

    def show_reset_dialog(self):
        """Helper to show the reset options from different tabs."""
        self.reset_to_defaults()

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

    def _run_command(self, cmd, log_widget, cwd=None, on_done=None, stdin_data=None, display_cmd=None):
        if self.worker and self.worker.isRunning(): return
        
        # Technical stylized command output
        clean_cmd = display_cmd if display_cmd else (cmd.replace(stdin_data, "********") if stdin_data else cmd)
        log_widget.append(f"<b>[EXEC]</b> <span style='color:{CYAN_PRIMARY};'>guest@pretendo-manager:</span> <span style='color:white;'>{clean_cmd}</span>")
        
        if stdin_data:
            log_widget.append(f"<i style='color:{TEXT_SECONDARY};'>[SYSTEM] Elevating privileges for secure task...</i>")
            
        self.worker = CommandWorker(cmd, cwd, stdin_data)
        self.worker.output.connect(log_widget.append)
        
        def handle_done(code):
            status_clr = "#3fb950" if code == 0 else RED_LIGHT
            status_txt = "SUCCESS" if code == 0 else f"FAILED ({code})"
            log_widget.append(f"<b>[DONE]</b> Process exited with status: <b style='color:{status_clr};'>{status_txt}</b>\n")
            if on_done: on_done(code)
            
        self.worker.finished.connect(handle_done)
        self.worker.start()

    def emergency_exit(self):
        """Total system shutdown: Stops server, disables Docker, and quits."""
        msg = "ATOMIC SHUTDOWN INITIATED\n\nThis will stop the Pretendo server, disable the Docker service, and quit the app.\n\nProceed?"
        if QMessageBox.critical(self, "Emergency Shutdown", msg, QMessageBox.Yes | QMessageBox.No) == QMessageBox.No:
            return

        self.bypassing_close_prompt = True
        self.statusBar().showMessage("SYSTEM SHUTDOWN IN PROGRESS...")
        
        # We need to run these commands and then close. 
        # Since the app will close, we'll try to trigger the shutdown logic.
        self.stop_server()
        
        # We want to wait for server to stop or at least trigger it.
        # Then disable service
        if OS_INFO["os"] == "linux" and self.docker_service_running:
            self.toggle_docker_service() 
            
        # We'll use a single-shot timer to allow the commands to at least start being sent
        # before we force the app to close.
        QTimer.singleShot(2000, QApplication.quit)

    def toggle_server(self):
        if self.server_running: self.stop_server()
        else: self.start_server()

    def start_server(self):
        # 1. Connectivity Gate
        if self._get_local_ip() == "127.0.0.1":
            QMessageBox.warning(self, "No Connection", "An internet connection is required to start the Pretendo server.\n\nPlease check your network and try again.")
            return

        s_dir = self.server_dir_field.text().strip()
        custom_port = self.host_port.text().strip()
        if not custom_port.isdigit():
            QMessageBox.warning(self, "Security Verification", "Warning: Port must be numeric to assign network bindings safely.")
            return

        if not os.path.isdir(s_dir):
            QMessageBox.warning(self, "Error", "Server directory not found! Download the stack first.")
            return

        self._apply_compose_patches(custom_port, s_dir)

        # 2. Credential Aggregation
        pw = None
        if hasattr(self, 'server_sudo_pass') and self.server_sudo_pass.text().strip():
            pw = self.server_sudo_pass.text().strip()
        elif self.cached_password:
            pw = self.cached_password

        # 3. Docker Service Check & Auto-Start
        if OS_INFO["os"] == "linux" and not self.docker_service_running:
            if not pw:
                pw = self._ask_sudo_password()
                if not pw: return # User cancelled

        ports = f"80 443 21 53 8080 {custom_port} 9231"
        fuser_cmd = "; ".join([f"fuser -k -n tcp {p}" for p in ports.split()])
        
        # Helper to finalize and kickstart mongo replica
        def _on_start(code):
            if OS_INFO["os"] == "linux" and s_dir:
                # Initialize the replica set if the container name matches, with retries since Mongo takes time
                 cmd = "for i in {1..15}; do docker exec pretendo-network-mongodb-1 mongo --eval 'rs.initiate()' && break; sleep 2; done;"
                 
                 # Initialize Postgres databases by executing the script if necessary
                 cmd += " for i in {1..15}; do docker exec pretendo-network-postgres-1 sh -c 'chmod +x /docker-entrypoint-initdb.d/postgres-init.sh && /docker-entrypoint-initdb.d/postgres-init.sh' && break; sleep 2; done;"
                 
                 # Go applications fail their connections if DBs are not ready, and don't exit. Force restart them after boot.
                 cmd += " sleep 10; docker compose restart friends splatoon super-mario-maker pikmin-3 wiiu-chat-authentication wiiu-chat-secure minecraft-wiiu miiverse-api juxtaposition-ui boss || true"
                 subprocess.Popen(cmd, shell=True, cwd=s_dir)
            self._check_docker_status()

        if OS_INFO["os"] == "linux":
            if pw:
                self.server_log.append("[System] Starting background services and clearing ports...")
                # Start docker services + clear ports + docker compose
                cmd = f"sudo -S bash -c 'systemctl start docker.socket docker.service; {fuser_cmd} || true'; docker compose up -d"
                self._run_command(cmd, self.server_log, s_dir, stdin_data=pw, on_done=_on_start, display_cmd="[Elevated] Clean Ports & Start Framework")
            else:
                self.server_log.append("[System] Starting server (Best-Effort Mode)...")
                cmd = f"{fuser_cmd} || true; docker compose up -d"
                self._run_command(cmd, self.server_log, s_dir, on_done=_on_start, display_cmd="[Standard] Clean Ports & Start Framework")
        else:
            self._run_command("docker compose up -d", self.server_log, s_dir, on_done=_on_start, display_cmd="docker compose up -d")

    def stop_server(self):
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir): return
        
        custom_port = self.host_port.text().strip()
        if not custom_port.isdigit(): return
        
        ports = f"80 443 21 53 8080 {custom_port} 9231"
        # Optional fuser command for deep cleaning
        fuser_cmd = "; ".join([f"fuser -k -n tcp {p}" for p in ports.split()])
        
        if OS_INFO["os"] == "linux":
            # Attempt to get password: Server Tab Slot > Cache
            pw = None
            if hasattr(self, 'server_sudo_pass') and self.server_sudo_pass.text().strip():
                pw = self.server_sudo_pass.text().strip()
            elif self.cached_password:
                pw = self.cached_password

            if pw:
                self.server_log.append("[System] Stopping server and force-releasing ports (Secure-Fast-Track)...")
                cmd = f"docker compose down; sudo -S bash -c 'systemctl stop docker.socket docker.service; {fuser_cmd} || true'"
                self._run_command(cmd, self.server_log, s_dir, stdin_data=pw, on_done=lambda c: self._check_docker_status(), display_cmd="[Elevated] Stop Framework & Clean Ports")
            else:
                self.server_log.append("[System] Stopping server containers and clearing ports (Best-Effort)...")
                # Try to kill what we can as current user, then docker down
                cmd = f"{fuser_cmd} || true; docker compose down"
                self._run_command(cmd, self.server_log, s_dir, on_done=lambda c: self._check_docker_status(), display_cmd="[Standard] Stop Framework & Clean Ports")
        else:
            self._run_command("docker compose down", self.server_log, s_dir, on_done=lambda c: self._check_docker_status(), display_cmd="docker compose down")

    def stream_docker_logs(self):
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir): return
        
        self.server_log.append("[System] Connecting to deep Docker event stream...")
        self.server_log.append("--------------------------------------------------")
        cmd = "docker compose logs -f --tail=20"
        
        if hasattr(self, 'log_worker') and getattr(self.log_worker, 'isRunning')():
            self.log_worker.terminate()
            self.server_log.append("[System] Restarting log watcher...")
            
        self.log_worker = CommandWorker(cmd, cwd=s_dir)
        self.log_worker.output.connect(self.server_log.append)
        self.log_worker.start()

    def toggle_docker_service(self):
        # Attempt to get password: Server Tab Slot > Cache
        pw = None
        if hasattr(self, 'server_sudo_pass') and self.server_sudo_pass.text().strip():
            pw = self.server_sudo_pass.text().strip()
        elif self.cached_password:
            pw = self.cached_password

        # Aggressive port cleaning logic
        custom_port = self.host_port.text().strip()
        if not custom_port.isdigit(): return
        ports = f"80 443 21 53 8080 {custom_port} 9231"
        fuser_cmd = "; ".join([f"fuser -k -n tcp {p}" for p in ports.split()])

        if self.docker_service_running:
            # DISABLING: Make it popup-free and clear ports
            if pw:
                self.setup_log.append("[System] Disabling Docker service and clearing ports (Fast-Track)...")
                cmd = f"sudo -S bash -c 'systemctl stop docker.socket docker.service; {fuser_cmd} || true'"
                self._run_command(cmd, self.setup_log, stdin_data=pw, on_done=lambda c: self._check_docker_status())
            else:
                self.setup_log.append("[System] Clearing ports and attempting service stop (Silent-Best-Effort)...")
                # Try killing ports as user, then stop service (service stop requires sudo but might work if NOPASSWD)
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
        if not os.path.isdir(s_dir):
            reply = QMessageBox.question(self, "Proceed", f"Directory '{s_dir}' not found. Clone repository here?", QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.setup_log.append("[System] Cloning repository...")
                self._run_command(f"git clone --recurse-submodules {PRETENDO_REPO} {shlex.quote(s_dir)}", self.setup_log, on_done=lambda c: self._continue_installation(c))
        else:
            self._continue_installation(0)
            
    def _continue_installation(self, code):
        if code != 0: return
        self.setup_log.append("\n[System] Stack downloaded. Initiating Deep Config...")
        self.run_pretendo_setup()

    def run_pretendo_setup(self):
        """Run the official Pretendo setup script in non-interactive mode."""
        s_dir = self.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            return

        custom_port = self.host_port.text().strip()
        if not custom_port.isdigit(): return
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
            if not pw: return

        self.setup_log.append("[System] Wiping port conflicts and removing old containers...")
        fuser_cmd = "; ".join([f"fuser -k -n tcp {p}" for p in ports_to_kill.split()])
        
        cmd = f"docker compose down --remove-orphans; sudo -S bash -c '{fuser_cmd} || true' && ./setup.sh --force --server-ip {local_ip}"
        
        self.setup_log.append(f"[System] Starting comprehensive setup with IP: {local_ip}...")
        self._run_command(cmd, self.setup_log, cwd=s_dir, stdin_data=pw, 
                          on_done=lambda c: self._post_setup_build(c))

    def _post_setup_build(self, code):
        if code != 0: return
        self.setup_log.append("\n[System] Deep Config Complete. Orchestrating container build process...")
        t = self.server_dir_field.text().strip()
        if OS_INFO["os"] == "linux":
            self._ensure_docker_active(lambda: self._run_command("docker compose build", self.setup_log, cwd=t, on_done=lambda c: QMessageBox.information(self, "Success", "Full Stack Deployment Finished! Ready to boot.")))
        else:
             self._run_command("docker compose build", self.setup_log, cwd=t, on_done=lambda c: QMessageBox.information(self, "Success", "Full Stack Deployment Finished! Ready to boot."))

    def _ensure_docker_active(self, on_ready):
        """Ensure Docker service is running on Linux before proceeding."""
        try:
            res = subprocess.run(["systemctl", "is-active", "docker"], capture_output=True, text=True)
            if res.stdout.strip() == "active":
                on_ready()
                return
        except: pass
        
        pw = None
        if OS_INFO["os"] == "linux":
            pw = self._ask_sudo_password()
            if not pw: return
            
        self.setup_log.append("[System] Resetting and starting Docker services...")
        self._run_command("sudo -S bash -c 'systemctl reset-failed docker; systemctl start docker.socket docker.service'", self.setup_log, stdin_data=pw, on_done=lambda c: on_ready() if c == 0 else None)

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

    def create_local_account(self):
        """Execute a node.js script inside the container to inject an account."""
        username = self.cemu_username.text().strip()
        password = self.cemu_password.text()
        miiname = self.cemu_miiname.text().strip() or "Player"
        
        if not username or not password or not username.isalnum():
            QMessageBox.warning(self, "Input Error", "Please provide a valid alphanumeric Username and a Password.")
            return
            
        if not self.server_running:
            QMessageBox.warning(self, "Network Error", "The Pretendo Server must be RUNNING (ONLINE) to create an account in the database.")
            return

        js_script = f"""
const {{ connect }} = require("./dist/database");
const {{ PNID }} = require("./dist/models/pnid");
const {{ nintendoPasswordHash }} = require("./dist/util");
const crypto = require("crypto");

(async () => {{
    try {{
        await connect();
        const username = "{username}";
        const pass = "{password}";
        const miiName = "{miiname}";
        const email = username + "@pretendo.local";

        let user = await PNID.findOne({{ usernameLower: username.toLowerCase() }});
        if (user) {{
            console.log("[Notice] " + username + " is already registered.");
            process.exit(0);
        }}

        const pid = Math.floor(Math.random() * 1000000000) + 1000000000;
        const hashedPw = await nintendoPasswordHash(pass, pid);
        const miiDataHex = "010001000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000";

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
            flags: {{ active: true, is_admin: true, is_dev: true }},
            access_level: 2
        }});

        await user.save();
        console.log("[Success] User " + username + " injected with Admin privileges! PID: " + pid);
        process.exit(0);
    }} catch(e) {{
        console.error(e);
        process.exit(1);
    }}
}})();
"""
        s_dir = self.server_dir_field.text().strip()
        cmd = f"docker exec -i pretendo-network-account-1 node -e '{js_script}'"
        
        self.cemu_log.append("[System] Injecting Account into Local Service Layer...")
        self._run_command(cmd, self.cemu_log, cwd=s_dir, on_done=lambda c: QMessageBox.information(self, "Registration", f"Account '{username}' Registration task completed!") if c == 0 else None)


    def patch_cemu_settings(self, url):
        # Dynamically resolve localhost if needed
        if "localhost" in url or "127.0.0.1" in url:
            real_ip = self._get_local_ip()
            if real_ip != "127.0.0.1":
                url = url.replace("localhost", real_ip).replace("127.0.0.1", real_ip)
                self.statusBar().showMessage(f"Redirected localhost -> {real_ip} for AppImage compatibility.", 3000)

        p = OS_INFO.get("cemu_settings", "")
        if not p or not os.path.exists(p):
            QMessageBox.warning(self, "Not Found", f"Cemu settings.xml not found at:\n{p}")
            return
        try:
            with open(p, "r") as f: c = f.read()
            
            # 1. Force Online & Global SSL Bypass
            if "<OnlineEnabled>true</OnlineEnabled>" not in c:
                if "<OnlineEnabled>false</OnlineEnabled>" in c:
                    c = c.replace("<OnlineEnabled>false</OnlineEnabled>", "<OnlineEnabled>true</OnlineEnabled>")
                elif "</Account>" in c:
                    c = c.replace("</Account>", "    <OnlineEnabled>true</OnlineEnabled>\n    </Account>")
            
            # 1b. INJECT SSL BYPASS into Account block for latest Cemu compatibility
            if "<Account>" in c and "<disablesslverification>1</disablesslverification>" not in c:
                c = c.replace("<Account>", "<Account>\n        <disablesslverification>1</disablesslverification>")
            
            # Ensure disablesslverification is in settings.xml root too
            if "<disablesslverification>1</disablesslverification>" not in c:
                if "<disablesslverification>0</disablesslverification>" in c:
                    c = c.replace("<disablesslverification>0</disablesslverification>", "<disablesslverification>1</disablesslverification>")
                elif "</content>" in c:
                    c = c.replace("</content>", "    <disablesslverification>1</disablesslverification>\n</content>")
            
            # 2. Kill Legacy Cert Pointers
            c = re.sub(r"<account_cert_path>.*?</account_cert_path>", "<account_cert_path></account_cert_path>", c)

            # 3. Patch Proxy URL
            if "<proxy_server>" in c:
                c = re.sub(r"<proxy_server>.*?</proxy_server>", f"<proxy_server>{url}</proxy_server>", c)
            elif "</content>" in c:
                c = c.replace("</content>", f"    <proxy_server>{url}</proxy_server>\n</content>")
            
            with open(p, "w") as f: f.write(c)
            
            # 4. Multi-Path network_services.xml injection (Full Service Redirect)
            data_dir = OS_INFO.get("cemu_data", OS_INFO.get("cemu_dir"))
            services = {
                "act": "https://account.nintendo.net",
                "con": "https://con.nintendo.net",
                "etc": "https://etc.nintendo.net",
                "dls": "https://dls.nintendo.net",
                "shp": "https://shp.nintendo.net",
                "dsa": "https://dsa.nintendo.net",
                "pdm": "https://pdm.nintendo.net",
                "miv": "https://api.olv.nintendo.net",
                "smm": "https://supermariomaker.nintendo.net",
                "bas": "https://bayonetta2.nintendo.net"
            }
            # Mitmproxy identifies hosts via normal domains, not via raw IP:PORT !
            url_nodes = "\n".join([f"        <{s}>{v}</{s}>" for s, v in services.items()])
            ns_content = f'<?xml version="1.0" encoding="UTF-8"?>\n<content>\n    <networkname>Pretendo-Bypass</networkname>\n    <disablesslverification>1</disablesslverification>\n    <urls>\n{url_nodes}\n    </urls>\n</content>'
            
            for target_dir in set([data_dir, os.path.dirname(p)]):
                ns_xml = os.path.join(target_dir, "network_services.xml")
                with open(ns_xml, "w") as f:
                    f.write(ns_content)

            self.statusBar().showMessage("Cemu Bypass Infrastructure Reinforced!", 5000)
            QMessageBox.information(self, "Success", f"Wii U Connection Patched!\n\nAll services (act, con, etc.) redirected to {url}\nSSL Verification Disabled.")
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def generate_cemu_manual(self):
        username = self.cemu_username.text().strip()
        password = self.cemu_password.text().strip()
        miiname = self.cemu_miiname.text().strip() or "Player"
        data_path = OS_INFO.get("cemu_data", self.cemu_dir_field.text().strip())

        if not username or not password:
            QMessageBox.warning(self, "Error", "Username and password required.")
            return

        try:
            # 1. Identity Blobs (Multi-write to root and sys)
            otp = bytearray(1024)
            comm = bytes.fromhex(WIIU_COMMON_KEY)
            for i, b in enumerate(comm): otp[0x100 + i] = b
            star = bytes.fromhex(WIIU_STARBUCK_ANCAST)
            for i, b in enumerate(star): otp[0x10 + i] = b
            espr = bytes.fromhex(ESPRESSO_ANCAST_KEY)
            for i, b in enumerate(espr): otp[0x30 + i] = b
            
            seeprom = bytearray(os.urandom(512))
            serial = f"FW{random.randint(400000000, 799999999)}".encode('ascii')
            for i, b in enumerate(serial): seeprom[0x170 + i] = b
            
            # Destination Sweep (Keys must be in both root and sys for different Cemu versions)
            targets = [data_path, os.path.join(data_path, "mlc01", "sys")]
            for t in targets:
                os.makedirs(t, exist_ok=True)
                with open(os.path.join(t, "otp.bin"), "wb") as f: f.write(otp)
                with open(os.path.join(t, "seeprom.bin"), "wb") as f: f.write(seeprom)

            # 2. Account Generation (NEX-Compatible Authenticated Hash)
            pid = getattr(self, '_pnid_pid', None)
            if not pid:
                pid = 123456789
            pid_bytes = pid.to_bytes(4, byteorder='little')
            
            pwd_hash = hashlib.sha256(pid_bytes + b"\x02eCF" + password.encode('utf-8')).hexdigest()
            uuid_bytes = os.urandom(16)
            uuid_hex = binascii.hexlify(uuid_bytes).decode('ascii')
            trans_id = (0x2000004 << 32) | (uuid_bytes[12] << 24) | (uuid_bytes[13] << 16) | (uuid_bytes[14] << 8) | uuid_bytes[15]
            
            # Persistent Mii Hex (MiiName & Data sync)
            stored_mii = getattr(self, '_mii_data_hex', None)
            if stored_mii and len(stored_mii) >= 40:
                mii_hex = stored_mii[:40] # take the name chunk or use it directly
            else:
                mii_u16 = miiname[:10].encode('utf-16be')
                mii_hex = binascii.hexlify(mii_u16.ljust(20, b'\x00')).decode('ascii')

            lines = [
                "AccountInstance_20120705",
                "PersistentId=80000001",
                f"TransferableIdBase={trans_id:x}",
                f"Uuid={uuid_hex}",
                "ParentalControlSlotNo=0",
                f"MiiData={getattr(self, '_mii_data_hex', '01000110' + '0' * 184)}", # Keep full MiiData if available
                f"MiiName={mii_hex}",
                "IsMiiUpdated=1",
                f"AccountId={username}",
                "BirthYear=2003", "BirthMonth=1", "BirthDay=1", "Gender=0",
                "IsMailAddressValidated=1",
                "EmailAddress=none@pretendo.network",
                "Country=49", "SimpleAddressId=49010000",
                "TimeZoneId=America/New_York",
                "UtcOffset=ffffffff9ac22000",
                f"PrincipalId={pid:08x}",
                "IsPasswordCacheEnabled=1",
                f"AccountPasswordCache={pwd_hash}",
                "NnasType=0", "NfsType=0", "NfsNo=1", "NnasNfsEnv=L1",
                "IsPersistentIdUploaded=1",
                "IsConsoleAccountInfoUploaded=1",
                "LastAuthenticationResult=0",
                f"StickyAccountId={username}",
                f"StickyPrincipalId={pid:08x}",
                "IsServerAccountDeleted=0",
                "IsCommitted=1"
            ]
            
            acct_dir = os.path.join(data_path, "mlc01", "usr", "save", "system", "act", "80000001")
            os.makedirs(acct_dir, exist_ok=True)
            with open(os.path.join(acct_dir, "account.dat"), "w", encoding="utf-8") as f: f.write("\n".join(lines))

            self.statusBar().showMessage(f"Deep Identity Fix Applied for {username}", 5000)
            QMessageBox.information(self, "Success", f"Wii U Identity Files Realigned!\n\nAuthentication Hash: OK\nPrincipalId: {pid:08x}")
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def patch_citra(self, mode):
        p = OS_INFO.get("citra_config", "")
        if not p or not os.path.exists(p):
            QMessageBox.warning(self, "Not Found", "Citra/Lime/Azahar configuration not found.")
            return

        target_url = "https://account.pretendo.cc" if mode == "pretendo" else self.patch_url_input.text()
        if mode == "nintendo": target_url = "https://account.nintendo.net"

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

            QMessageBox.information(self, "Success", f"Patched Citra to use:\n{target_url}\n\nIdentity bypass files checked.")
        except Exception as e: QMessageBox.critical(self, "Error", str(e))




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

                pid = getattr(self, '_pnid_pid', None)
                if not pid:
                    pid = random.randint(1000000000, 2000000000)
                pid_bytes = pid.to_bytes(4, byteorder='little')
                pwd_hash = hashlib.sha256(pid_bytes + b"\x02eCF" + passw.encode('utf-8')).hexdigest()
                uuid_bytes = os.urandom(16)
                uuid_hex = binascii.hexlify(uuid_bytes).decode('ascii')
                trans_id = (0x2000004 << 32) | (uuid_bytes[12] << 24) | (uuid_bytes[13] << 16) | (uuid_bytes[14] << 8) | uuid_bytes[15]
                
                mii_data_hex_val = getattr(self, '_mii_data_hex', '01000110' + '0' * 184)
                mii_name_utf16 = miiname[:10].encode('utf-16be')
                mii_name_hex = binascii.hexlify(mii_name_utf16[:20].ljust(22, b'\x00')).decode('ascii')

                acct_lines = [
                    "AccountInstance_20120705",
                    "PersistentId=80000001",
                    f"TransferableIdBase={trans_id:x}",
                    f"Uuid={uuid_hex}",
                    "ParentalControlSlotNo=0",
                    f"MiiData={mii_data_hex_val}",
                    f"MiiName={mii_name_hex}",
                    "IsMiiUpdated=1",
                    f"AccountId={user}",
                    "BirthYear=2003", "BirthMonth=1", "BirthDay=1", "Gender=0",
                    "IsMailAddressValidated=1",
                    "EmailAddress=none@pretendo.network",
                    "Country=49", "SimpleAddressId=49010000",
                    "TimeZoneId=America/New_York",
                    "UtcOffset=ffffffff9ac22000",
                    f"PrincipalId={pid:08x}",
                    "IsPasswordCacheEnabled=1",
                    f"AccountPasswordCache={pwd_hash}",
                    "NnasType=0", "NfsType=0", "NfsNo=1", "NnasNfsEnv=L1",
                    "IsPersistentIdUploaded=1",
                    "IsConsoleAccountInfoUploaded=1",
                    "LastAuthenticationResult=0",
                    f"StickyAccountId={user}",
                    f"StickyPrincipalId={pid:08x}",
                    "IsServerAccountDeleted=0",
                    "IsCommitted=1"
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
        """Handle application exit with persistence options."""
        if self.bypassing_close_prompt:
            self.save_settings()
            if self.worker and self.worker.isRunning():
                self.worker.terminate()
                self.worker.wait(1000)
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
            # Atomic shutdown
            self.stop_server()
            if OS_INFO["os"] == "linux" and self.docker_service_running:
                self.toggle_docker_service()
            # No persistent wait needed here, the commands are queued
            
        self.save_settings()
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait(1000)
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = PretendoManager()
    win.show()
    sys.exit(app.exec())
