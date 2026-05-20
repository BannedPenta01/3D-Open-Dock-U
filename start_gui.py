#!/usr/bin/env python3
# start_gui.py
import os
import sys
import platform
import shutil
import socket
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QLineEdit, QTextEdit, QFormLayout,
    QGroupBox, QMessageBox, QFileDialog, QProgressBar, QFrame,
    QScrollArea, QSizePolicy, QSpacerItem, QInputDialog, QDialog,
    QCheckBox, QDialogButtonBox, QScroller, QScrollerProperties,
    QListWidget, QListWidgetItem, QRadioButton, QButtonGroup
)
from PySide6.QtCore import Qt, QThread, Signal, QSize, QSettings, QTimer, QDir, QLockFile, QStandardPaths
from PySide6.QtGui import QColor, QPixmap, QIcon

from constants import (
    APP_NAME, APP_VERSION, STYLESHEET, RED_PRIMARY, CYAN_PRIMARY, 
    CYAN_DARK, CYAN_LIGHT, TEXT_SECONDARY, RED_DARK, RED_LIGHT, 
    BG_DARK, BG_CARD, GREEN_MONEY, ORANGE_ACCOUNT
)
from utils import OS_INFO, DEFAULT_SERVER_DIR, CEMU_DIR, _obs, _deobs, get_local_ip, has_internet_connectivity

# Import mixins
from mixins.server_mixin import ManagerServerMixin
from mixins.vault_mixin import ManagerVaultMixin
from mixins.utils_mixin import ManagerUtilsMixin

# Import new modules
from deploy import Deployer
from patch_emulators import EmulatorPatcher

def make_scrollable(widget):
    scroll = QScrollArea()
    scroll.setWidget(widget)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setFrameShape(QFrame.NoFrame)
    scroller = QScroller.scroller(scroll.viewport())
    scroller.grabGesture(scroll.viewport(), QScroller.LeftMouseButtonGesture)
    return scroll

class PretendoManager(QMainWindow, ManagerServerMixin, ManagerVaultMixin, ManagerUtilsMixin):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1350, 850)
        
        self.deployer = Deployer(self)
        self.patcher = EmulatorPatcher(self)
        
        self.worker = None
        self.log_worker = None
        self.cached_password = None
        self.server_running = False
        self.docker_service_running = False
        self.docker_transitioning = False
        self.server_dir = DEFAULT_SERVER_DIR
        self.settings = QSettings(APP_NAME, "Config")
        self.bypassing_close_prompt = False
        self.command_lock_count = 0
        self.last_popup_time = 0
        self.seen_errors = {}
        self.session_start_time = None
        self.last_connectivity_state = has_internet_connectivity()
        self._cemu_log_offset = 0
        self._citra_log_offset = 0
        self._last_resolved_ip = None
        self._cemu_log_path_cache = None
        self._citra_log_path_cache = None
        self._cemu_timeout_count = 0
        self._cemu_last_timeout_alert = 0
        self._prev_active_emu_ips = set()
        self._last_compose_port = None
        self._last_env_ip = None
        self._last_connection_check = 0
        
        self._init_ui()
        self.load_settings()
        
        self.heartbeat = QTimer()
        self.heartbeat.timeout.connect(self._on_status_tick)
        self.heartbeat.start(5000)
        QTimer.singleShot(100, self._on_status_tick)

    def _init_ui(self):
        central = QWidget(objectName="centralWidget")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.setAlignment(Qt.AlignTop) # Set alignment to top to remove blank space
        
        # Header / Logo
        self.logo_label = QLabel()
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
        if os.path.exists(logo_path):
            pix = QPixmap(logo_path)
            # Scale to reasonable header height
            # Scale specifically to the header height (120) so the whole logo fits exactly
            scaled = pix.scaledToHeight(120, Qt.SmoothTransformation)
            self.logo_label.setPixmap(scaled)
        else:
            self.logo_label.setText(f'<span style="color:{RED_PRIMARY};">3D</span> <span style="color:white;">Open Dock</span> <span style="color:{CYAN_PRIMARY};">U</span>')
            self.logo_label.setStyleSheet("font-size: 22px; font-weight: bold;")
        
        self.logo_label.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self.logo_label.setScaledContents(False)
        self.logo_label.setFixedHeight(120)
        self.logo_label.setStyleSheet("margin: 0px; padding: 0px; border: none; background: transparent;")
        root.addWidget(self.logo_label)
        
        sep = QFrame(objectName="separator")
        sep.setFrameShape(QFrame.HLine)
        root.addWidget(sep)

        self.tabs = QTabWidget()
        self.tabs.addTab(make_scrollable(self._build_dashboard_tab()), "Server & Deployment")
        self.tabs.addTab(make_scrollable(self._build_emulator_tab()), "Identities & Emulators")
        self.tabs.addTab(make_scrollable(self._build_guide_tab()), "Help & Guide")
        
        self.tabs.tabBar().setExpanding(True)
        root.addWidget(self.tabs)

        # Footer
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(10, 3, 10, 3)
        
        self.atomic_btn = QPushButton("ATOMIC SHUTDOWN & EXIT")
        self.atomic_btn.setObjectName("emergencyBtn")
        # Removed fixed size for better scaling
        self.atomic_btn.setMinimumHeight(24)
        self.atomic_btn.clicked.connect(self.emergency_exit)
        footer_layout.addWidget(self.atomic_btn)

        self.footer_text = QLabel(f'Made by BannedPenta AKA Jan Michael | Powered by Gemini 3 Flash & Google Antigravity')
        self.footer_text.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 10px;")
        self.footer_text.setAlignment(Qt.AlignCenter)
        footer_layout.addWidget(self.footer_text, 1)
        
        self.reset_data_btn = QPushButton("Reset Sensitive Data")
        self.reset_data_btn.setStyleSheet(f"font-size: 9px; color: {RED_LIGHT}; border: none; background: transparent;")
        self.reset_data_btn.clicked.connect(self.clear_sensitive_data)
        footer_layout.addWidget(self.reset_data_btn)
        
        root.addLayout(footer_layout)
        
        self.setStyleSheet(STYLESHEET)

    def _build_dashboard_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        h_layout = QHBoxLayout()

        # LEFT PANE
        left = QWidget()
        lv = QVBoxLayout(left)
        group = QGroupBox("Network Status & Node")
        glay = QVBoxLayout(group)
        self.status_label = QLabel("OFFLINE")
        self.status_label.setStyleSheet(f"color: {RED_PRIMARY}; font-size: 20px; font-weight: bold;")
        self.status_label.setAlignment(Qt.AlignCenter)
        glay.addWidget(self.status_label)
        self.ip_info = QLabel(f"Local Network IP: {get_local_ip()}")
        self.ip_info.setStyleSheet(f"color: {CYAN_LIGHT}; font-size: 14px;")
        self.ip_info.setAlignment(Qt.AlignCenter)
        glay.addWidget(self.ip_info)
        self.detected_game_label = QLabel("🎮 Cemu: None Detected")
        self.detected_game_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px; font-style: italic;")
        self.detected_game_label.setAlignment(Qt.AlignCenter)
        glay.addWidget(self.detected_game_label)
        self.server_toggle_btn = QPushButton("START SERVER")
        self.server_toggle_btn.setObjectName("startBtn")
        self.server_toggle_btn.setMinimumHeight(50)
        self.server_toggle_btn.clicked.connect(self.toggle_server)
        glay.addWidget(self.server_toggle_btn)
        btn_hl = QHBoxLayout()
        btn_hl.addWidget(QPushButton("Clear Logs", clicked=lambda: self.server_log.clear()))
        btn_hl.addWidget(QPushButton("Fix Server IPs", clicked=self.refresh_database_ips))
        glay.addLayout(btn_hl)
        lv.addWidget(group)
        self.server_log = QTextEdit(objectName="logBox")
        self.server_log.setReadOnly(True)
        lv.addWidget(self.server_log)
        h_layout.addWidget(left, 1)

        # RIGHT PANE
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(5, 5, 5, 5)
        rv.setSpacing(5)
        dep = QGroupBox("Infrastructure Deployment")
        dlay = QVBoxLayout(dep)
        self.service_toggle_btn = QPushButton("Enable Docker Service", clicked=self.toggle_docker_service)
        dlay.addWidget(self.service_toggle_btn)
        self.host_net_check = QCheckBox("Host Networking (Bypass Bridge/NAT)")
        self.host_net_check.setToolTip("Recommended for mobile hotspots or strict NAT issues. Binds containers directly to host interfaces.")
        self.host_net_check.stateChanged.connect(self.save_settings)
        dlay.addWidget(self.host_net_check)
        
        row_dir = QHBoxLayout()
        row_dir.addWidget(QLabel("Stack Dir:"))
        self.server_dir_field = QLineEdit(DEFAULT_SERVER_DIR)
        row_dir.addWidget(self.server_dir_field)
        dlay.addLayout(row_dir)
        self.deploy_btn = QPushButton("Deploy Server Stack (Automated Setup)")
        self.deploy_btn.setObjectName("deployBtn")
        self.deploy_btn.setMinimumHeight(60)
        self.deploy_btn.clicked.connect(self.deployer.automated_install_stack)
        dlay.addWidget(self.deploy_btn)
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
            row_sys = QHBoxLayout()
            row_sys.addWidget(QPushButton("Fix Perms", clicked=self.fix_docker_permissions))
            row_sys.addWidget(QPushButton("Reset Per-Session Sudo", clicked=lambda: self.server_sudo_pass.clear()))
            dlay.addLayout(row_sys)
        rv.addWidget(dep)
        splat = QGroupBox("Splatoon Fixes")
        splat.setObjectName("splatBox")
        slay = QVBoxLayout(splat)
        slay.setContentsMargins(2, 2, 2, 2)
        slay.setSpacing(0)
        self.patch_rotations_btn = QPushButton("Patch Splatoon Rotations", clicked=self.apply_splatoon_rotation_patch)
        self.patch_rotations_btn.setObjectName("patchRotationsBtn")
        self.patch_rotations_btn.setMaximumHeight(30)
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

        # LEFT PANE
        left = QWidget()
        lv = QVBoxLayout(left)
        cred = QGroupBox("Console Identity Parameters")
        crgl = QFormLayout(cred)
        self.cemu_username = QLineEdit()
        self.cemu_username.setMaxLength(16)
        self.cemu_password = QLineEdit()
        self.cemu_password.setEchoMode(QLineEdit.Password)
        self.cemu_miiname = QLineEdit()
        self.cemu_miiname.setMaxLength(10)
        self.cemu_dir_field = QLineEdit(CEMU_DIR)
        self.citra_dir_field = QLineEdit()
        crgl.addRow("Username:", self.cemu_username)
        crgl.addRow("Password:", self.cemu_password)
        crgl.addRow("Mii Name:", self.cemu_miiname)
        row_c = QHBoxLayout()
        row_c.addWidget(self.cemu_dir_field)
        btn_c = QPushButton("📁")
        btn_c.clicked.connect(lambda: self._pick_dir(self.cemu_dir_field, "Cemu Dir"))
        row_c.addWidget(btn_c)
        crgl.addRow("Cemu Dir:", row_c)
        row_cit = QHBoxLayout()
        row_cit.addWidget(self.citra_dir_field)
        btn_cit = QPushButton("📁")
        btn_cit.clicked.connect(lambda: self._pick_dir(self.citra_dir_field, "Citra Dir"))
        row_cit.addWidget(btn_cit)
        crgl.addRow("Citra Dir:", row_cit)
        lv.addWidget(cred)
        sec_warn = QLabel("⚠️ WARNING: NEVER use your real passwords for email or primary accounts. Always create filler or unique passwords for use with emulators.")
        sec_warn.setStyleSheet(f"color: {RED_LIGHT}; font-weight: bold; font-size: 10px;")
        sec_warn.setWordWrap(True)
        lv.addWidget(sec_warn)
        self.bundle_btn = QPushButton("Generate Credentials Bundle")
        self.bundle_btn.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffffff, stop:1 #e6e6e6); color: #111111; font-weight: bold;")
        self.bundle_btn.setMinimumHeight(40)
        self.bundle_btn.clicked.connect(self.patcher.generate_console_bundle_zip)
        lv.addWidget(self.bundle_btn)
        net = QGroupBox("Universal Network Router")
        nlay = QVBoxLayout(net)
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Target Node:"))
        self.patch_url_input = QLineEdit(f"http://{get_local_ip()}:8070")
        url_row.addWidget(self.patch_url_input)
        nlay.addLayout(url_row)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Connection Mode:"))
        self.mode_local = QRadioButton("Private (Local Stack)")
        self.mode_local.setChecked(True)
        self.mode_pretendo = QRadioButton("Public (Pretendo Network)")
        mode_row.addWidget(self.mode_local)
        mode_row.addWidget(self.mode_pretendo)
        nlay.addLayout(mode_row)
        pat_row = QHBoxLayout()
        btn_pc = QPushButton("Patch & Connect (Cemu)", objectName="patchBtn")
        btn_pc.setCursor(Qt.PointingHandCursor)
        btn_pc.clicked.connect(self.patcher.apply_cemu_patch_all)
        btn_pct = QPushButton("Patch & Connect (Citra)", objectName="patchBtn")
        btn_pct.setCursor(Qt.PointingHandCursor)
        btn_pct.clicked.connect(lambda: self.patcher.patch_citra("custom"))
        pat_row.addWidget(btn_pc)
        pat_row.addWidget(btn_pct)
        nlay.addLayout(pat_row)
        lv.addWidget(net)
        h_layout.addWidget(left, 1)

        # RIGHT PANE
        right = QWidget()
        rv = QVBoxLayout(right)
        vault = QGroupBox("Offline Credentials Vault")
        vlay = QVBoxLayout(vault)
        vlay.addWidget(QLabel("Swap accounts dynamically without manual file management."))
        self.profile_list = QListWidget()
        self.profile_list.setMinimumHeight(50)
        vlay.addWidget(self.profile_list)
        vbtn = QHBoxLayout()
        vbtn.addWidget(QPushButton("Save Active Profile", clicked=self.save_to_vault))
        vbtn.addWidget(QPushButton("Deploy Saved Loadout", clicked=self.apply_from_vault))
        vlay.addLayout(vbtn)
        vbtn2 = QHBoxLayout()
        vbtn2.addWidget(QPushButton("Browse Vault...", clicked=self.open_vault_folder))
        vbtn2.addWidget(QPushButton("Erase Setup", clicked=self.delete_profile))
        vlay.addLayout(vbtn2)
        rv.addWidget(vault)
        res_row = QHBoxLayout()
        btn_rn = QPushButton("Restore Nintendo Services", objectName="restoreOfficialBtn", clicked=self.restore_nintendo_official)
        btn_rp = QPushButton("Restore Pretendo Mainnet", objectName="restorePretendoBtn", clicked=self.restore_pretendo_official)
        res_row.addWidget(btn_rn)
        res_row.addWidget(btn_rp)
        rv.addLayout(res_row)
        rv.addWidget(QPushButton("Emergency Factory Defaults", objectName="factoryResetBtn", clicked=self.reset_to_defaults))
        h_layout.addWidget(right, 1)
        layout.addLayout(h_layout)
        return w

    def _build_guide_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        guide = QTextEdit()
        guide.setReadOnly(True)
        guide.setHtml(f"""
            <h1 style='color:{RED_LIGHT};'>Simple Setup Guide</h1>
            <p>Follow these steps to get online quickly:</p>
            <ol>
                <li><b>Step 1:</b> Click <b>Deploy Server Stack</b> on the first tab.</li>
                <li><b>Step 2:</b> Wait until the <b>Deploy Server Stack</b> button does its job.</li>
                <li><b>Step 3:</b> Click <b>Start Server</b>.</li>
                <li><b>Step 4:</b> Put a username, password (<b>NOT</b> your daily one), and a Mii name.</li>
                <li><b>Step 5:</b> Locate your Cemu or any Citra fork folder and choose the root of these folders.</li>
                <li><b>Step 6:</b> Go to the second tab and click either <b>Patch Cemu</b> or <b>Patch Citra</b>.</li>
                <li><b>Step 7 (For Cemu):</b> Go to Options, General Settings, press the Account tab and toggle <b>Pretendo</b>. You should also see your Mii name on the active account.</li>
                <li><b>Step 8:</b> Enjoy! And don't ever forget to share the <b>Target Node</b> to whoever you want to play against.</li>
            </ol>
        """)
        layout.addWidget(guide)
        return w

    def load_settings(self):
        # Block signals to prevent setText() from triggering save_settings() prematurely
        self.cemu_username.blockSignals(True)
        self.cemu_password.blockSignals(True)
        self.cemu_miiname.blockSignals(True)
        if hasattr(self, 'server_sudo_pass'): self.server_sudo_pass.blockSignals(True)

        self.cemu_username.setText(str(self.settings.value("username", "")))
        self.cemu_password.setText(_deobs(self.settings.value("password", "")))
        self.cemu_miiname.setText(str(self.settings.value("miiname", "")))
        self.server_dir_field.setText(str(self.settings.value("server_dir", DEFAULT_SERVER_DIR)))
        self.cemu_dir_field.setText(str(self.settings.value("cemu_dir", CEMU_DIR)))
        
        saved_sudo = self.settings.value("sudo_cache", None)
        self.cached_password = _deobs(saved_sudo) if saved_sudo else None
        if self.cached_password and hasattr(self, 'server_sudo_pass'):
            self.server_sudo_pass.setText(str(self.cached_password))

        self.refresh_vault_list()
        
        self.host_net_check.setChecked(self.settings.value("host_net", "false") == "true")
        self.cemu_username.blockSignals(False)
        self.cemu_password.blockSignals(False)
        self.cemu_miiname.blockSignals(False)
        if hasattr(self, 'server_sudo_pass'): self.server_sudo_pass.blockSignals(False)

    def save_settings(self):
        self.settings.setValue("username", self.cemu_username.text())
        self.settings.setValue("password", _obs(self.cemu_password.text()))
        self.settings.setValue("miiname", self.cemu_miiname.text())
        self.settings.setValue("server_dir", self.server_dir_field.text())
        self.settings.setValue("cemu_dir", self.cemu_dir_field.text())
        self.settings.setValue("host_net", "true" if self.host_net_check.isChecked() else "false")
        
        if hasattr(self, 'server_sudo_pass'):
            sudo_pw = self.server_sudo_pass.text()
            if sudo_pw:
                self.settings.setValue("sudo_cache", _obs(sudo_pw))
                self.cached_password = sudo_pw
        self.settings.sync()

    def clear_sensitive_data(self):
        res = QMessageBox.warning(self, "Clear Data", "Permanently wipe credentials and admin password?", QMessageBox.Yes | QMessageBox.No)
        if res == QMessageBox.Yes:
            self.cached_password = None
            self.cemu_username.clear()
            self.cemu_password.clear()
            self.cemu_miiname.clear()
            if hasattr(self, "server_sudo_pass"): self.server_sudo_pass.clear()
            for key in self.settings.allKeys(): self.settings.remove(key)
            self.settings.sync()
            QMessageBox.information(self, "Data Wiped", "All sensitive data has been permanently cleared.")

    def closeEvent(self, event):
        if self.server_running and not self.bypassing_close_prompt:
            reply = QMessageBox.question(self, "Server Running", 
                                       "The server stack is still running. Would you like to STOP it before exiting?",
                                       QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if reply == QMessageBox.Yes:
                # Synchronous shutdown
                event.ignore()
                self.bypassing_close_prompt = True
                self.statusBar().showMessage("Full Shutdown in progress — stopping containers...")
                QApplication.processEvents()
                self._force_shutdown_sync(show_progress=True)
                self.save_settings()
                os._exit(0)
            elif reply == QMessageBox.No:
                self.save_settings()
                event.accept()
            else:
                event.ignore()
        else:
            self.save_settings()
            event.accept()

    def _on_status_tick(self):
        self._check_docker_status()
        is_connected = has_internet_connectivity()
        current_ip = get_local_ip()

        if hasattr(self, 'ip_info'):
            self.ip_info.setText(f"Local Network IP: {current_ip}")

        if not is_connected and self.last_connectivity_state:
            msg = "\n[ALARM] Internet Connection Lost!\n"
            if current_ip == "127.0.0.1":
                msg += "[WARN] Network IP reverted to Loopback (127.0.0.1). Emulators will disconnect.\n"
            else:
                msg += f"[INFO] Local LAN IP ({current_ip}) is still active. Private server remains reachable.\n"
            msg += "[INFO] Secure Shutdown Protocol is READY but not triggered automatically to avoid accidental downtime.\n"
            self.server_log.append(f"<span style='color:{RED_LIGHT}; font-weight:bold;'>{msg}</span>")
            self.setup_log.append(f"<span style='color:{RED_LIGHT};'>{msg}</span>")
            self.statusBar().showMessage("NETWORK LOSS DETECTED - Verify Connectivity", 15000)

        self.last_connectivity_state = is_connected
        self._detect_current_game()
        self._detect_emulator_connections()

    def _get_target_port(self):
        text = self.patch_url_input.text().strip()
        import re
        match = re.search(r":(\d+)(?:/|$)", text)
        return match.group(1) if match else "8070"

    def _resolve_target_node(self, url=None, is_official=False):
        raw_url = (url if url is not None else ("https://api.pretendo.network" if is_official else self.patch_url_input.text())).strip()
        if not raw_url:
            raw_url = f"http://{get_local_ip()}:{self._get_target_port()}"

        parsed_input = raw_url if "://" in raw_url else f"http://{raw_url}"
        parsed = urlparse(parsed_input)

        host = parsed.hostname or get_local_ip()
        port = parsed.port
        if not port and not is_official:
            port = int(self._get_target_port())

        scheme = parsed.scheme or ("https" if is_official else "http")
        normalized_url = f"{scheme}://{host}"
        if port:
            normalized_url += f":{port}"

        return normalized_url.rstrip("/"), host, str(port) if port else ""

    def _get_target_host(self, url=None, is_official=False):
        _, host, _ = self._resolve_target_node(url=url, is_official=is_official)
        return host

    def _pick_dir(self, field, title):
        d = QFileDialog.getExistingDirectory(self, title, field.text())
        if d: 
            field.setText(d)
            self.save_settings()



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
            warning.setStyleSheet(STYLESHEET)
        except: pass

        warning.exec()
        sys.exit(1)

    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    win = PretendoManager()
    win.show()

    # Pass the lock_file reference to the window so it persists for the lifetime of the app
    win._instance_lock = lock_file

    sys.exit(app.exec())
