#!/usr/bin/env python3
# start_gui.py
import os
import sys
import platform
import shutil
import socket
import json
import re
import subprocess
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QLineEdit, QTextEdit, QFormLayout,
    QGroupBox, QMessageBox, QFileDialog, QProgressBar, QFrame,
    QScrollArea, QSizePolicy, QSpacerItem, QInputDialog, QDialog,
    QCheckBox, QDialogButtonBox, QScroller, QScrollerProperties,
    QListWidget, QListWidgetItem, QRadioButton, QButtonGroup, QColorDialog
)
from PySide6.QtCore import Qt, QThread, Signal, QSize, QSettings, QTimer, QDir, QLockFile, QStandardPaths
from PySide6.QtGui import QColor, QPixmap, QIcon

from src.constants import (
    APP_NAME, APP_VERSION, STYLESHEET, RED_PRIMARY, CYAN_PRIMARY, 
    CYAN_DARK, CYAN_LIGHT, TEXT_SECONDARY, RED_DARK, RED_LIGHT, 
    BG_DARK, BG_CARD, GREEN_MONEY, ORANGE_ACCOUNT, LOGO_PATH, HOST_JSON_PATH
)
from src.utils import OS_INFO, DEFAULT_SERVER_DIR, CEMU_DIR, _obs, _deobs, get_local_ip, has_internet_connectivity
from src.secrets_manager import SecretStore

# Import mixins
from src.mixins.server_mixin import ManagerServerMixin
from src.mixins.vault_mixin import ManagerVaultMixin
from src.mixins.utils_mixin import ManagerUtilsMixin

# Import new modules
from src.deploy import Deployer
from src.patch_emulators import EmulatorPatcher

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
        self.server_profiles = []
        
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
        logo_path = LOGO_PATH
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
        self.tabs.addTab(make_scrollable(self._build_quick_start_tab()), "Quick Start")
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

    def _build_quick_start_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("Quick Start")
        title.setStyleSheet(f"color: {CYAN_LIGHT}; font-size: 24px; font-weight: bold;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Run your local stack on the left, or pick a saved server on the right.")
        subtitle.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 13px;")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        main = QHBoxLayout()
        main.setSpacing(14)

        left_half = QWidget()
        left_lay = QVBoxLayout(left_half)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(10)

        self.quick_local_address = QLabel(f"Local server address: http://{get_local_ip()}:8070")
        self.quick_local_address.setStyleSheet(f"color: {GREEN_MONEY}; font-size: 14px; font-weight: bold;")
        self.quick_local_address.setAlignment(Qt.AlignCenter)
        left_lay.addWidget(self.quick_local_address)

        local = QGroupBox("Play on My Local Server")
        local_lay = QVBoxLayout(local)
        local_text = QLabel("Use this when the server runs on this PC.")
        local_text.setWordWrap(True)
        local_text.setStyleSheet(f"color: {TEXT_SECONDARY};")
        local_lay.addWidget(local_text)
        self.quick_prepare_btn = QPushButton("Prepare Server")
        self.quick_prepare_btn.setObjectName("startBtn")
        self.quick_prepare_btn.setMinimumHeight(52)
        self.quick_prepare_btn.clicked.connect(self.quick_prepare_or_stop_server)
        local_lay.addWidget(self.quick_prepare_btn)
        self.quick_server_toggle_btn = QPushButton("Quick Start Server")
        self.quick_server_toggle_btn.setObjectName("startBtn")
        self.quick_server_toggle_btn.setMinimumHeight(42)
        self.quick_server_toggle_btn.clicked.connect(self.quick_toggle_server)
        local_lay.addWidget(self.quick_server_toggle_btn)
        local_play_row = QHBoxLayout()
        play_cemu_btn = QPushButton("Play Cemu")
        play_cemu_btn.setStyleSheet("background: #007c89; color: white; border-color: #00AEDE;")
        play_cemu_btn.clicked.connect(lambda: self.quick_play_emulator("cemu"))
        play_citra_btn = QPushButton("Play Citra")
        play_citra_btn.setStyleSheet("background: #5c1111; color: white; border-color: #8B0000;")
        play_citra_btn.clicked.connect(lambda: self.quick_play_emulator("citra"))
        local_play_row.addWidget(play_cemu_btn)
        local_play_row.addWidget(play_citra_btn)
        local_lay.addLayout(local_play_row)
        left_lay.addWidget(local)

        account = QGroupBox("Console Identity")
        account_lay = QFormLayout(account)
        self.quick_cemu_username = QLineEdit()
        self.quick_cemu_username.setMaxLength(16)
        self.quick_cemu_password = QLineEdit()
        self.quick_cemu_password.setEchoMode(QLineEdit.Password)
        self.quick_cemu_miiname = QLineEdit()
        self.quick_cemu_miiname.setMaxLength(10)
        self.quick_cemu_username.textChanged.connect(self._sync_quick_identity_to_advanced)
        self.quick_cemu_password.textChanged.connect(self._sync_quick_identity_to_advanced)
        self.quick_cemu_miiname.textChanged.connect(self._sync_quick_identity_to_advanced)
        account_lay.addRow("Username:", self.quick_cemu_username)
        account_lay.addRow("Password:", self.quick_cemu_password)
        account_lay.addRow("Mii Name:", self.quick_cemu_miiname)
        identity_patch_row = QHBoxLayout()
        identity_cemu_btn = QPushButton("Patch Cemu")
        identity_cemu_btn.setStyleSheet("background: #007c89; color: white; border-color: #00AEDE;")
        identity_cemu_btn.clicked.connect(self.quick_patch_cemu)
        identity_citra_btn = QPushButton("Patch Citra")
        identity_citra_btn.setStyleSheet("background: #5c1111; color: white; border-color: #8B0000;")
        identity_citra_btn.clicked.connect(self.quick_patch_citra)
        identity_patch_row.addWidget(identity_cemu_btn)
        identity_patch_row.addWidget(identity_citra_btn)
        account_lay.addRow(identity_patch_row)
        left_lay.addWidget(account)

        join = QGroupBox("Join Someone Else's Server")
        join_lay = QVBoxLayout(join)
        join_text = QLabel("Paste a server address here if it is not in your saved server list.")
        join_text.setWordWrap(True)
        join_text.setStyleSheet(f"color: {TEXT_SECONDARY};")
        join_lay.addWidget(join_text)
        self.quick_join_url = QLineEdit()
        self.quick_join_url.setPlaceholderText("Example: http://123.45.67.89:8070")
        self.quick_join_url.editingFinished.connect(self.quick_join_server)
        join_lay.addWidget(self.quick_join_url)
        join_hint = QLabel("The target node updates automatically when you leave this field.")
        join_hint.setWordWrap(True)
        join_hint.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 11px;")
        join_lay.addWidget(join_hint)
        quick_patch_row = QHBoxLayout()
        quick_cemu_btn = QPushButton("Patch Cemu")
        quick_cemu_btn.setStyleSheet("background: #007c89; color: white; border-color: #00AEDE;")
        quick_cemu_btn.clicked.connect(self.quick_patch_cemu)
        quick_citra_btn = QPushButton("Patch Citra")
        quick_citra_btn.setStyleSheet("background: #5c1111; color: white; border-color: #8B0000;")
        quick_citra_btn.clicked.connect(self.quick_patch_citra)
        quick_patch_row.addWidget(quick_cemu_btn)
        quick_patch_row.addWidget(quick_citra_btn)
        join_lay.addLayout(quick_patch_row)
        left_lay.addWidget(join)
        left_lay.addStretch(1)
        main.addWidget(left_half, 1)

        directory = QGroupBox("Saved Servers")
        directory_lay = QVBoxLayout(directory)
        server_help = QLabel("Choose a server profile, then patch Cemu or Citra. Game labels are shown first so the list stays scannable.")
        server_help.setWordWrap(True)
        server_help.setStyleSheet(f"color: {TEXT_SECONDARY};")
        directory_lay.addWidget(server_help)

        self.server_profile_list = QListWidget()
        self.server_profile_list.setMinimumHeight(260)
        self.server_profile_list.itemDoubleClicked.connect(lambda _item: self.quick_use_selected_server())
        directory_lay.addWidget(self.server_profile_list, 1)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QPushButton("Add", clicked=self.quick_add_server_profile))
        profile_row.addWidget(QPushButton("Edit", clicked=self.quick_edit_server_profile))
        profile_row.addWidget(QPushButton("Remove", clicked=self.quick_remove_server_profile))
        directory_lay.addLayout(profile_row)

        file_row = QHBoxLayout()
        file_row.addWidget(QPushButton("Open host.json", clicked=self.open_host_json))
        file_row.addWidget(QPushButton("Reload List", clicked=self.reload_host_json))
        directory_lay.addLayout(file_row)

        patch_row = QHBoxLayout()
        use_btn = QPushButton("Use Selected")
        use_btn.setObjectName("patchBtn")
        use_btn.clicked.connect(self.quick_use_selected_server)
        patch_row.addWidget(use_btn)
        selected_cemu_btn = QPushButton("Patch Cemu")
        selected_cemu_btn.setStyleSheet("background: #007c89; color: white; border-color: #00AEDE;")
        selected_cemu_btn.clicked.connect(lambda: self.quick_patch_selected_server("cemu"))
        selected_citra_btn = QPushButton("Patch Citra")
        selected_citra_btn.setStyleSheet("background: #5c1111; color: white; border-color: #8B0000;")
        selected_citra_btn.clicked.connect(lambda: self.quick_patch_selected_server("citra"))
        patch_row.addWidget(selected_cemu_btn)
        patch_row.addWidget(selected_citra_btn)
        directory_lay.addLayout(patch_row)

        main.addWidget(directory, 1)
        layout.addLayout(main, 1)
        return w

    def _quick_local_url(self):
        return f"http://{get_local_ip()}:8070"

    def _quick_set_target(self, url, message=None):
        normalized, _, _ = self._resolve_target_node(url=url, is_official=False)
        if hasattr(self, "patch_url_input"):
            self.patch_url_input.setText(normalized)
        if hasattr(self, "mode_local"):
            self.mode_local.setChecked(True)
        if hasattr(self, "mode_pretendo"):
            self.mode_pretendo.setChecked(False)
        if hasattr(self, "quick_join_url"):
            self.quick_join_url.setText(normalized)
        if message:
            self.statusBar().showMessage(message, 8000)
        self.save_settings()
        return normalized

    def _sync_quick_identity_to_advanced(self):
        if not all(hasattr(self, name) for name in ("cemu_username", "cemu_password", "cemu_miiname", "quick_cemu_username", "quick_cemu_password", "quick_cemu_miiname")):
            return
        self.cemu_username.blockSignals(True)
        self.cemu_password.blockSignals(True)
        self.cemu_miiname.blockSignals(True)
        self.cemu_username.setText(self.quick_cemu_username.text())
        self.cemu_password.setText(self.quick_cemu_password.text())
        self.cemu_miiname.setText(self.quick_cemu_miiname.text())
        self.cemu_username.blockSignals(False)
        self.cemu_password.blockSignals(False)
        self.cemu_miiname.blockSignals(False)
        self.save_settings()

    def _sync_advanced_identity_to_quick(self):
        if not all(hasattr(self, name) for name in ("cemu_username", "cemu_password", "cemu_miiname", "quick_cemu_username", "quick_cemu_password", "quick_cemu_miiname")):
            return
        self.quick_cemu_username.blockSignals(True)
        self.quick_cemu_password.blockSignals(True)
        self.quick_cemu_miiname.blockSignals(True)
        self.quick_cemu_username.setText(self.cemu_username.text())
        self.quick_cemu_password.setText(self.cemu_password.text())
        self.quick_cemu_miiname.setText(self.cemu_miiname.text())
        self.quick_cemu_username.blockSignals(False)
        self.quick_cemu_password.blockSignals(False)
        self.quick_cemu_miiname.blockSignals(False)

    def _normalize_color(self, value, fallback):
        color = str(value or "").strip()
        if re.match(r"^#[0-9a-fA-F]{6}$", color):
            return color
        return fallback

    def _pick_color_for_field(self, field, fallback):
        current = QColor(self._normalize_color(field.text(), fallback))
        color = QColorDialog.getColor(current, self, "Choose Color")
        if color.isValid():
            field.setText(color.name())

    def _default_server_profiles(self):
        local_url = self._quick_local_url()
        return [
            {"game": "Splatoon", "name": "My Local Server", "url": local_url, "game_color": CYAN_LIGHT, "name_color": GREEN_MONEY},
            {"game": "Mario Kart 8", "name": "My Local Server", "url": local_url, "game_color": "#ff4d4d", "name_color": GREEN_MONEY},
            {"game": "Smash Wii U", "name": "My Local Server", "url": local_url, "game_color": "#d7b7ff", "name_color": GREEN_MONEY},
        ]

    def _host_json_path(self):
        return HOST_JSON_PATH

    def _coerce_server_profiles(self, payload):
        profiles = payload.get("servers", payload) if isinstance(payload, dict) else payload
        if not isinstance(profiles, list):
            return []

        clean = []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            game = str(profile.get("game", "")).strip()
            name = str(profile.get("name", "")).strip()
            url = str(profile.get("url", "")).strip()
            game_color = self._normalize_color(profile.get("game_color", CYAN_LIGHT), CYAN_LIGHT)
            name_color = self._normalize_color(profile.get("name_color", GREEN_MONEY), GREEN_MONEY)
            if game and name and url:
                clean.append({"game": game, "name": name, "url": url, "game_color": game_color, "name_color": name_color})
        return clean

    def _load_server_profiles(self):
        path = self._host_json_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    clean = self._coerce_server_profiles(json.load(f))
                if clean:
                    self.server_profiles = clean
                    return
            except Exception as error:
                QMessageBox.warning(self, "host.json Error", f"Could not read host.json:\n\n{error}")

        legacy = []
        raw = self.settings.value("server_profiles", "")
        if raw:
            try:
                legacy = self._coerce_server_profiles(json.loads(str(raw)))
            except Exception:
                legacy = []

        self.server_profiles = legacy or self._default_server_profiles()
        self._save_server_profiles()

    def _save_server_profiles(self):
        payload = {
            "servers": self.server_profiles
        }
        with open(self._host_json_path(), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")

    def open_host_json(self):
        path = self._host_json_path()
        if not os.path.exists(path):
            self._save_server_profiles()
        try:
            if OS_INFO["os"] == "windows":
                os.startfile(path)
            else:
                QMessageBox.information(self, "host.json", f"Edit this file:\n\n{path}")
        except Exception as error:
            QMessageBox.warning(self, "Open host.json Failed", str(error))

    def reload_host_json(self):
        self._load_server_profiles()
        self._refresh_server_profile_list()
        self.statusBar().showMessage("Reloaded host.json", 5000)

    def _refresh_server_profile_list(self):
        if not hasattr(self, "server_profile_list"):
            return
        self.server_profile_list.clear()
        for profile in self.server_profiles:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, profile)
            item.setSizeHint(QSize(100, 58))
            self.server_profile_list.addItem(item)
            game_color = self._normalize_color(profile.get("game_color", CYAN_LIGHT), CYAN_LIGHT)
            name_color = self._normalize_color(profile.get("name_color", GREEN_MONEY), GREEN_MONEY)

            label = QLabel(
                f"<b style='color:{game_color};'>{profile['game']}</b> &nbsp; <span style='color:{name_color};'>{profile['name']}</span><br>"
                f"<span style='color:{TEXT_SECONDARY}; font-size:10px;'>{profile['url']}</span>"
            )
            label.setStyleSheet("padding: 6px;")
            label.setWordWrap(True)
            self.server_profile_list.setItemWidget(item, label)

        if self.server_profiles:
            self.server_profile_list.setCurrentRow(0)

    def _selected_server_profile(self):
        if not hasattr(self, "server_profile_list"):
            return None
        item = self.server_profile_list.currentItem()
        if not item:
            return None
        return item.data(Qt.UserRole)

    def _server_profile_dialog(self, profile=None):
        profile = profile or {"game": "", "name": "", "url": "", "game_color": CYAN_LIGHT, "name_color": GREEN_MONEY}
        dialog = QDialog(self)
        dialog.setWindowTitle("Server Profile")
        form = QFormLayout(dialog)

        game_field = QLineEdit(str(profile.get("game", "")))
        game_field.setPlaceholderText("Splatoon")
        name_field = QLineEdit(str(profile.get("name", "")))
        name_field.setPlaceholderText("Friend's Server")
        url_field = QLineEdit(str(profile.get("url", "")))
        url_field.setPlaceholderText("http://123.45.67.89:8070")
        game_color_field = QLineEdit(self._normalize_color(profile.get("game_color", CYAN_LIGHT), CYAN_LIGHT))
        name_color_field = QLineEdit(self._normalize_color(profile.get("name_color", GREEN_MONEY), GREEN_MONEY))

        game_color_row = QHBoxLayout()
        game_color_row.addWidget(game_color_field)
        game_color_row.addWidget(QPushButton("Pick", clicked=lambda: self._pick_color_for_field(game_color_field, CYAN_LIGHT)))
        name_color_row = QHBoxLayout()
        name_color_row.addWidget(name_color_field)
        name_color_row.addWidget(QPushButton("Pick", clicked=lambda: self._pick_color_for_field(name_color_field, GREEN_MONEY)))

        form.addRow("Game:", game_field)
        form.addRow("Server name:", name_field)
        form.addRow("Address:", url_field)
        form.addRow("Game color:", game_color_row)
        form.addRow("Name color:", name_color_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.Accepted:
            return None

        game = game_field.text().strip()
        name = name_field.text().strip()
        url = url_field.text().strip()
        game_color = self._normalize_color(game_color_field.text(), CYAN_LIGHT)
        name_color = self._normalize_color(name_color_field.text(), GREEN_MONEY)
        if not game or not name or not url:
            QMessageBox.warning(self, "Missing Server Info", "Game, server name, and address are all required.")
            return None
        return {"game": game, "name": name, "url": url, "game_color": game_color, "name_color": name_color}

    def quick_add_server_profile(self):
        profile = self._server_profile_dialog({"game": "Splatoon", "name": "New Server", "url": self._quick_local_url(), "game_color": CYAN_LIGHT, "name_color": GREEN_MONEY})
        if not profile:
            return
        self.server_profiles.append(profile)
        self._save_server_profiles()
        self._refresh_server_profile_list()

    def quick_edit_server_profile(self):
        profile = self._selected_server_profile()
        if not profile:
            QMessageBox.warning(self, "No Server Selected", "Choose a server profile to edit first.")
            return
        row = self.server_profile_list.currentRow()
        updated = self._server_profile_dialog(profile)
        if not updated:
            return
        self.server_profiles[row] = updated
        self._save_server_profiles()
        self._refresh_server_profile_list()
        self.server_profile_list.setCurrentRow(row)

    def quick_remove_server_profile(self):
        profile = self._selected_server_profile()
        if not profile:
            QMessageBox.warning(self, "No Server Selected", "Choose a server profile to remove first.")
            return
        row = self.server_profile_list.currentRow()
        if QMessageBox.question(self, "Remove Server", f"Remove {profile['name']}?", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        del self.server_profiles[row]
        self._save_server_profiles()
        self._refresh_server_profile_list()

    def quick_use_selected_server(self):
        profile = self._selected_server_profile()
        if not profile:
            QMessageBox.warning(self, "No Server Selected", "Choose a server profile first.")
            return None
        url = self._quick_set_target(profile["url"], f"{profile['game']} server selected.")
        if hasattr(self, "server_log"):
            self.server_log.append(f"<b>[Quick Start]</b> Selected {profile['game']} server: {profile['name']} ({url})")
        return url

    def quick_patch_selected_server(self, emulator):
        if not self.quick_use_selected_server():
            return
        if not self._ensure_emulator_executable(emulator):
            return
        if emulator == "citra":
            self.patcher.patch_citra("custom")
        else:
            self.patcher.apply_cemu_patch_all()

    def quick_play_local(self):
        url = self._quick_set_target(self._quick_local_url(), "Local server selected.")
        if hasattr(self, "server_log"):
            self.server_log.append(f"<b>[Quick Start]</b> Local server target selected: {url}")
        self.tabs.setCurrentIndex(2)

    def _quick_style_button(self, button, object_name):
        if not button:
            return
        button.setObjectName(object_name)
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()

    def _refresh_quick_start_state(self):
        if not hasattr(self, "quick_prepare_btn") or not hasattr(self, "quick_server_toggle_btn"):
            return

        running = bool(self.server_running)
        if running:
            self.quick_prepare_btn.setText("Stop Server")
            self._quick_style_button(self.quick_prepare_btn, "stopBtn")
            self.quick_server_toggle_btn.setText("Quick Stop Server")
            self._quick_style_button(self.quick_server_toggle_btn, "stopBtn")
        else:
            self.quick_prepare_btn.setText("Prepare Server")
            self._quick_style_button(self.quick_prepare_btn, "startBtn")
            self.quick_server_toggle_btn.setText("Quick Start Server")
            self._quick_style_button(self.quick_server_toggle_btn, "startBtn")

    def quick_prepare_or_stop_server(self):
        self._refresh_quick_start_state()
        if self.server_running:
            self.stop_server()
            return
        self._quick_prepare_after_deploy = True
        self._suppress_deploy_complete_popup = True
        self.quick_deploy_local()

    def _on_deploy_complete(self, code):
        self._suppress_deploy_complete_popup = False
        if not getattr(self, "_quick_prepare_after_deploy", False):
            return
        self._quick_prepare_after_deploy = False
        if code != 0:
            QMessageBox.warning(self, "Prepare Server Failed", "Deployment did not finish successfully, so the server was not started.")
            return
        self._quick_set_target(self._quick_local_url(), "Starting local server after deployment.")
        self._quick_prepare_server_flow = True
        self.start_server()

    def quick_toggle_server(self):
        self._quick_set_target(self._quick_local_url(), "Local server selected.")
        self.toggle_server()
        self._refresh_quick_start_state()

    def quick_deploy_local(self):
        url = self._quick_set_target(self._quick_local_url(), "Preparing local server deployment.")
        if hasattr(self, "setup_log"):
            self.setup_log.append(f"<b>[Quick Start]</b> Deploying local server stack for {url}")
        self.tabs.setCurrentIndex(1)
        self.deployer.automated_install_stack()

    def quick_host_for_friends(self):
        url = self._quick_set_target(self._quick_local_url(), "Hosting mode prepared.")
        if hasattr(self, "setup_log"):
            self.setup_log.append(f"<b>[Quick Start]</b> Hosting address prepared: {url}")
        QMessageBox.information(
            self,
            "Hosting Prepared",
            f"Your local server address is:\n\n{url}\n\nUse the Server tab to deploy/start the stack. Friends on your same network can try this address first."
        )
        self.tabs.setCurrentIndex(1)

    def quick_copy_local_address(self):
        url = self._quick_local_url()
        QApplication.clipboard().setText(url)
        self.statusBar().showMessage(f"Copied {url}", 6000)

    def quick_join_server(self):
        raw = self.quick_join_url.text().strip() if hasattr(self, "quick_join_url") else ""
        if not raw:
            return None
        url = self._quick_set_target(raw, "Join server selected.")
        if hasattr(self, "server_log"):
            self.server_log.append(f"<b>[Quick Start]</b> Join target selected: {url}")
        return url

    def quick_patch_cemu(self):
        if hasattr(self, "quick_join_url") and self.quick_join_url.text().strip():
            self.quick_join_server()
        elif hasattr(self, "patch_url_input") and not self.patch_url_input.text().strip():
            self._quick_set_target(self._quick_local_url())
        if not self._ensure_emulator_executable("cemu"):
            return
        self.patcher.apply_cemu_patch_all()

    def quick_patch_citra(self):
        if hasattr(self, "quick_join_url") and self.quick_join_url.text().strip():
            self.quick_join_server()
        elif hasattr(self, "patch_url_input") and not self.patch_url_input.text().strip():
            self._quick_set_target(self._quick_local_url())
        if not self._ensure_emulator_executable("citra"):
            return
        self.patcher.patch_citra("custom")

    def _find_emulator_executable(self, path, names):
        candidate = os.path.expanduser(str(path or "").strip().strip('"'))
        if not candidate:
            return None
        if os.path.isfile(candidate) and os.path.basename(candidate).lower() in names:
            return candidate
        if os.path.isdir(candidate):
            for name in names:
                exe = os.path.join(candidate, name)
                if os.path.isfile(exe):
                    return exe
            for root, _dirs, files in os.walk(candidate):
                for file_name in files:
                    if file_name.lower() in names:
                        return os.path.join(root, file_name)
        return None

    def _ensure_emulator_executable(self, emulator):
        if emulator == "citra":
            field = self.citra_dir_field
            names = {"citra-qt.exe", "lime3ds.exe", "azahar.exe", "mandarine.exe", "citra.exe"}
            title = "Choose Citra, Lime3DS, Azahar, or another 3DS fork executable"
        else:
            field = self.cemu_dir_field
            names = {"cemu.exe"}
            title = "Choose Cemu.exe"

        exe = self._find_emulator_executable(field.text(), names)
        if exe:
            field.setText(os.path.dirname(exe))
            self.save_settings()
            return True

        selected, _ = QFileDialog.getOpenFileName(
            self,
            title,
            field.text() if field.text().strip() else os.path.expanduser("~"),
            "Emulator executables (*.exe);;All files (*.*)"
        )
        if not selected:
            return False
        if os.path.basename(selected).lower() not in names:
            QMessageBox.warning(self, "Wrong Emulator File", f"That does not look like a supported {emulator.upper()} executable.")
            return False
        field.setText(os.path.dirname(selected))
        self.save_settings()
        return True

    def _emulator_executable_for_launch(self, emulator):
        if emulator == "citra":
            field = self.citra_dir_field
            names = {"citra-qt.exe", "lime3ds.exe", "azahar.exe", "mandarine.exe", "citra.exe"}
        else:
            field = self.cemu_dir_field
            names = {"cemu.exe"}
        return self._find_emulator_executable(field.text(), names)

    def quick_play_emulator(self, emulator):
        if not self._ensure_emulator_executable(emulator):
            return
        exe = self._emulator_executable_for_launch(emulator)
        if not exe:
            QMessageBox.warning(self, "Emulator Missing", f"Could not find the {emulator.upper()} executable after selecting its folder.")
            return
        try:
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
            self.statusBar().showMessage(f"Launched {os.path.basename(exe)}", 6000)
        except Exception as error:
            QMessageBox.warning(self, "Launch Failed", f"Could not start {exe}:\n\n{error}")

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
        self.cemu_username.textChanged.connect(self._sync_advanced_identity_to_quick)
        self.cemu_password.textChanged.connect(self._sync_advanced_identity_to_quick)
        self.cemu_miiname.textChanged.connect(self._sync_advanced_identity_to_quick)
        self.cemu_username.textChanged.connect(self.save_settings)
        self.cemu_password.textChanged.connect(self.save_settings)
        self.cemu_miiname.textChanged.connect(self.save_settings)
        self.cemu_dir_field.textChanged.connect(self.save_settings)
        self.citra_dir_field.textChanged.connect(self.save_settings)
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
        self.patch_url_input.textChanged.connect(self.save_settings)
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
                <li><b>Step 1:</b> Use the <b>Quick Start</b> tab to choose Local, Host, or Join.</li>
                <li><b>Step 2:</b> Wait until the <b>Deploy Server Stack</b> button does its job.</li>
                <li><b>Step 3:</b> Click <b>Start Server</b>.</li>
                <li><b>Step 4:</b> Put a username, password (<b>NOT</b> your daily one), and a Mii name.</li>
                <li><b>Step 5:</b> Locate your Cemu or any Citra fork folder and choose the root of these folders.</li>
                <li><b>Step 6:</b> Go to the identity tab and click either <b>Patch Cemu</b> or <b>Patch Citra</b>.</li>
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
        self.cemu_dir_field.blockSignals(True)
        self.patch_url_input.blockSignals(True)
        self.citra_dir_field.blockSignals(True)
        if hasattr(self, "quick_cemu_username"):
            self.quick_cemu_username.blockSignals(True)
            self.quick_cemu_password.blockSignals(True)
            self.quick_cemu_miiname.blockSignals(True)
        if hasattr(self, 'server_sudo_pass'): self.server_sudo_pass.blockSignals(True)

        store = SecretStore()
        password_ref = self.settings.value("password_ref", "ui.cemu_password")
        saved_password = store.get(str(password_ref), "")
        legacy_password = self.settings.value("password", "")
        if not saved_password and legacy_password:
            saved_password = _deobs(legacy_password)
            store.set(str(password_ref), saved_password, save=True)
            self.settings.setValue("password_ref", str(password_ref))
            self.settings.remove("password")

        self.cemu_username.setText(str(self.settings.value("username", "")))
        self.cemu_password.setText(saved_password)
        self.cemu_miiname.setText(str(self.settings.value("miiname", "")))
        if hasattr(self, "quick_cemu_username"):
            self.quick_cemu_username.setText(self.cemu_username.text())
            self.quick_cemu_password.setText(self.cemu_password.text())
            self.quick_cemu_miiname.setText(self.cemu_miiname.text())
        self.server_dir_field.setText(str(self.settings.value("server_dir", DEFAULT_SERVER_DIR)))
        self.cemu_dir_field.setText(str(self.settings.value("cemu_dir", CEMU_DIR)))
        target_url = str(self.settings.value("target_url", f"http://{get_local_ip()}:8070"))
        self.patch_url_input.setText(target_url)
        self.citra_dir_field.setText(str(self.settings.value("citra_dir", "")))
        if hasattr(self, "quick_join_url"):
            self.quick_join_url.setText(target_url)
        self._load_server_profiles()
        self._refresh_server_profile_list()
        
        sudo_ref = self.settings.value("sudo_cache_ref", "ui.sudo_cache")
        self.cached_password = store.get(str(sudo_ref), None)
        saved_sudo = self.settings.value("sudo_cache", None)
        if not self.cached_password and saved_sudo:
            self.cached_password = _deobs(saved_sudo)
            store.set(str(sudo_ref), self.cached_password, save=True)
            self.settings.setValue("sudo_cache_ref", str(sudo_ref))
            self.settings.remove("sudo_cache")
        if self.cached_password and hasattr(self, 'server_sudo_pass'):
            self.server_sudo_pass.setText(str(self.cached_password))

        self.refresh_vault_list()
        
        self.host_net_check.setChecked(self.settings.value("host_net", "false") == "true")
        self.cemu_username.blockSignals(False)
        self.cemu_password.blockSignals(False)
        self.cemu_miiname.blockSignals(False)
        self.cemu_dir_field.blockSignals(False)
        self.patch_url_input.blockSignals(False)
        self.citra_dir_field.blockSignals(False)
        if hasattr(self, "quick_cemu_username"):
            self.quick_cemu_username.blockSignals(False)
            self.quick_cemu_password.blockSignals(False)
            self.quick_cemu_miiname.blockSignals(False)
        if hasattr(self, 'server_sudo_pass'): self.server_sudo_pass.blockSignals(False)

    def save_settings(self):
        store = SecretStore()
        self.settings.setValue("username", self.cemu_username.text())
        store.set("ui.cemu_password", self.cemu_password.text(), save=False)
        self.settings.setValue("password_ref", "ui.cemu_password")
        self.settings.remove("password")
        self.settings.setValue("miiname", self.cemu_miiname.text())
        self.settings.setValue("server_dir", self.server_dir_field.text())
        self.settings.setValue("cemu_dir", self.cemu_dir_field.text())
        self.settings.setValue("citra_dir", self.citra_dir_field.text())
        self.settings.setValue("target_url", self.patch_url_input.text())
        self.settings.setValue("host_net", "true" if self.host_net_check.isChecked() else "false")
        
        if hasattr(self, 'server_sudo_pass'):
            sudo_pw = self.server_sudo_pass.text()
            if sudo_pw:
                store.set("ui.sudo_cache", sudo_pw, save=False)
                self.settings.setValue("sudo_cache_ref", "ui.sudo_cache")
                self.settings.remove("sudo_cache")
                self.cached_password = sudo_pw
        store.save()
        self.settings.sync()

    def clear_sensitive_data(self):
        res = QMessageBox.warning(self, "Clear Data", "Permanently wipe credentials and admin password?", QMessageBox.Yes | QMessageBox.No)
        if res == QMessageBox.Yes:
            store = SecretStore()
            for key in ("ui.cemu_password", "ui.sudo_cache"):
                store.delete(key, save=False)
            store.save()
            self.cached_password = None
            self.cemu_username.clear()
            self.cemu_password.clear()
            self.cemu_miiname.clear()
            if hasattr(self, "quick_cemu_username"):
                self.quick_cemu_username.clear()
                self.quick_cemu_password.clear()
                self.quick_cemu_miiname.clear()
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
        if hasattr(self, 'quick_local_address'):
            self.quick_local_address.setText(f"Your current local server address: http://{current_ip}:8070")
        self._refresh_quick_start_state()

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
