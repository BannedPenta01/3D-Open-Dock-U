#!/usr/bin/env python3
"""3D Open Dock U - Simple Mode.
One calm window, three steps. For non-technical users.

  1. Your player name + Cemu folder
  2. Point at any custom Wii U / 3DS server IP, export a folder with
     two copy-and-paste packs: one for Cemu, one for 3DS emulators
  3. Patch Cemu and play

Running the server on this PC stays available under step 2 as an option.
Nothing was removed, only spaced out and put in plain language.
"""
import os
import re
import html
import sys
import subprocess

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QTextEdit, QGroupBox, QMessageBox,
    QFileDialog, QCheckBox, QRadioButton, QScrollArea, QFrame, QSizePolicy,
    QProgressBar,
)
from PySide6.QtCore import Qt, QSettings, QTimer, QStandardPaths, QLockFile

from src.constants import APP_NAME, APP_VERSION, STYLESHEET, CYAN_LIGHT, TEXT_SECONDARY
from src.utils import OS_INFO, DEFAULT_SERVER_DIR, CEMU_DIR, get_local_ip
from src.utils import _docker_available, _start_docker_desktop, _CACHED_RESULTS
from src.secrets_manager import SecretStore
from src.deploy import Deployer
from src.patch_emulators import EmulatorPatcher
from src.mixins.server_mixin import ManagerServerMixin
from src.mixins.vault_mixin import ManagerVaultMixin
from src.mixins.utils_mixin import ManagerUtilsMixin

# Extra breathing room on top of the shared dark theme.
ROOMY_QSS = """
QLineEdit {
    padding: 12px 14px;
    font-size: 15px;
    border-radius: 8px;
}
QTextEdit {
    font-size: 13px;
}
QGroupBox {
    font-size: 16px;
    padding: 18px;
    padding-top: 40px;
    margin-top: 18px;
}
QPushButton {
    font-size: 14px;
    padding: 12px;
    border-radius: 8px;
}
"""


# Containers started for a game night. Websites, other games and admin
# tools stay off so weak PCs survive. The 3 supported games always start.
LITE_SERVICES = ("coredns-internal nginx mitmproxy-pretendo mongodb postgres minio redis "
                 "account friends splatoon super-smash-bros-wiiu pokken-tournament")


def find_cemu_automatic():
    """Best-effort Cemu location search."""
    if CEMU_DIR and os.path.isdir(CEMU_DIR):
        return CEMU_DIR
    if OS_INFO["os"] == "windows":
        for base in (os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")):
            if base:
                p = os.path.join(base, "Cemu")
                if os.path.isdir(p):
                    return p
        for drive in ("C:", "D:", "E:"):
            for sub in ("Cemu", "Games\\Cemu", "Emulation\\Cemu"):
                p = os.path.join(drive, os.sep, sub)
                if os.path.isdir(p):
                    return p
    else:
        home = os.path.expanduser("~")
        for p in (os.path.join(home, ".local/share/Cemu"), os.path.join(home, ".config/Cemu")):
            if os.path.isdir(p):
                return p
    return ""


class SimpleDock(QMainWindow, ManagerServerMixin, ManagerVaultMixin, ManagerUtilsMixin):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION} - Simple Mode")
        self.setMinimumSize(720, 860)
        self.deployer = Deployer(self)
        self.patcher = EmulatorPatcher(self)
        self.worker = None
        self.cached_password = None
        self.server_running = False
        self.docker_service_running = False
        self.settings = QSettings(APP_NAME, "SimpleConfig")
        # fields the backend expects (hidden dummies where needed)
        self.server_dir = DEFAULT_SERVER_DIR
        self._suppress_deploy_complete_popup = False
        # Start-button state machine: only one server action runs at a time.
        self._server_busy = False
        self._docker_wait_tries = 0
        self._docker_wait_action = None
        self._docker_wait_active = False

        self._build_ui()
        self._load()
        self.setStyleSheet(STYLESHEET + ROOMY_QSS)

        self.heartbeat = QTimer(self)
        self.heartbeat.timeout.connect(self._tick)
        self.heartbeat.start(5000)
        QTimer.singleShot(300, self._tick)

    # ---------- UI helpers ----------
    def _caption(self, text):
        label = QLabel(text)
        label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 13px;")
        label.setWordWrap(True)
        return label

    def _roomy_field(self, placeholder=""):
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        field.setMinimumHeight(46)
        field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return field

    def _field_block(self, layout, caption, field):
        """Caption stacked above a tall full-width field. Airy, not cramped."""
        layout.addWidget(self._caption(caption))
        layout.addWidget(field)
        layout.addSpacing(10)

    def _step_card(self, number, title, hint):
        card = QGroupBox(f"Step {number}  -  {title}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(8)
        sub = QLabel(hint)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 13px;")
        lay.addWidget(sub)
        lay.addSpacing(8)
        return card, lay

    def _big_button(self, text, minimum_height=56):
        btn = QPushButton(text)
        btn.setMinimumHeight(minimum_height)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn.setCursor(Qt.PointingHandCursor)
        return btn

    # ---------- UI ----------
    def _build_ui(self):
        # Hidden stand-ins the shared backends read (never shown).
        self.mode_local = QRadioButton("local"); self.mode_local.setChecked(True)
        self.mode_local.setVisible(False)
        self.mode_pretendo = QRadioButton("pretendo"); self.mode_pretendo.setVisible(False)
        self.host_net_check = QCheckBox("host"); self.host_net_check.setVisible(False)
        self.citra_dir_field = QLineEdit(); self.citra_dir_field.setVisible(False)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setCentralWidget(scroll)

        central = QWidget(objectName="centralWidget")
        scroll.setWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(36, 30, 36, 30)
        root.setSpacing(22)

        title = QLabel("3D Open Dock U")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 26px; font-weight: bold;")
        root.addWidget(title)

        subtitle = QLabel("Play Wii U online on your own server. Three steps, big buttons.")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 14px;")
        root.addWidget(subtitle)

        # Status banner
        banner = QFrame()
        banner.setFrameShape(QFrame.StyledPanel)
        banner_lay = QVBoxLayout(banner)
        banner_lay.setContentsMargins(20, 16, 20, 16)
        banner_lay.setSpacing(4)
        self.status_label = QLabel("Checking...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("font-size: 19px; font-weight: bold;")
        banner_lay.addWidget(self.status_label)
        self.ip_info = QLabel("")
        self.ip_info.setAlignment(Qt.AlignCenter)
        self.ip_info.setWordWrap(True)
        self.ip_info.setStyleSheet(f"color: {CYAN_LIGHT}; font-size: 14px;")
        banner_lay.addWidget(self.ip_info)
        root.addWidget(banner)

        # ---- Step 1 ----
        card1, l1 = self._step_card(1, "Your player name",
            "Make up a new username and password just for Cemu. "
            "Never reuse a real password here.")
        self.cemu_username = self._roomy_field("Username - 6 to 16 letters or numbers")
        self.cemu_username.setMaxLength(16)
        self._field_block(l1, "Username", self.cemu_username)

        self.cemu_password = self._roomy_field("Password - invent a new one")
        self.cemu_password.setEchoMode(QLineEdit.Password)
        self._field_block(l1, "Password", self.cemu_password)

        self.show_password = QCheckBox("Show password")
        self.show_password.toggled.connect(
            lambda on: self.cemu_password.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        l1.addWidget(self.show_password)
        l1.addSpacing(10)

        self.cemu_miiname = self._roomy_field("Mii nickname - up to 10 characters")
        self.cemu_miiname.setMaxLength(10)
        self._field_block(l1, "Mii nickname", self.cemu_miiname)

        l1.addWidget(self._caption("Where is Cemu installed?"))
        self.cemu_dir_field = self._roomy_field("Cemu folder - press Find automatically")
        l1.addWidget(self.cemu_dir_field)
        l1.addSpacing(6)
        finder_row = QHBoxLayout()
        finder_row.setSpacing(10)
        find_btn = self._big_button("Find automatically", 48)
        find_btn.clicked.connect(self._auto_find_cemu)
        browse_btn = self._big_button("Browse...", 48)
        browse_btn.clicked.connect(self._browse_cemu)
        finder_row.addWidget(find_btn)
        finder_row.addWidget(browse_btn)
        l1.addLayout(finder_row)
        for w in (self.cemu_username, self.cemu_password, self.cemu_miiname, self.cemu_dir_field):
            w.textChanged.connect(self.save_settings)
        root.addWidget(card1)

        # ---- Step 2 ----
        card2, l2 = self._step_card(2, "Custom server",
            "Type any Wii U / 3DS server address, then export two copy-and-paste packs: "
            "one for Cemu, one for 3DS emulators.")
        self.custom_host = self._roomy_field("Server address - for example 192.168.1.50")
        self._field_block(l2, "Server address - IP or name", self.custom_host)
        self.custom_port = self._roomy_field("8070")
        self.custom_port.setMaxLength(5)
        self._field_block(l2, "Port", self.custom_port)
        self.custom_host.textChanged.connect(self.save_settings)
        self.custom_port.textChanged.connect(self.save_settings)
        self.export_btn = self._big_button("Export connection folder")
        self.export_btn.setObjectName("deployBtn")
        self.export_btn.clicked.connect(self._on_export)
        l2.addWidget(self.export_btn)
        l2.addSpacing(6)
        self.export_result = self._caption("")
        self.export_result.setWordWrap(True)
        l2.addWidget(self.export_result)
        self.open_export_btn = self._big_button("Open the exported folder", 48)
        self.open_export_btn.clicked.connect(self._open_export_folder)
        self.open_export_btn.setVisible(False)
        l2.addWidget(self.open_export_btn)
        self._last_export_folder = ""

        # Self-hosting stays available, tucked away so step 2 stays simple.
        # One button: downloads the server stack on first use, then starts it.
        self.selfhost_toggle = QCheckBox("I want to run the server on this PC instead")
        l2.addWidget(self.selfhost_toggle)
        self.selfhost_box = QWidget()
        sl = QVBoxLayout(self.selfhost_box)
        sl.setContentsMargins(0, 6, 0, 0)
        sl.setSpacing(8)
        # Kept off-screen: the backend needs the field, the user does not.
        self.server_dir_field = QLineEdit(DEFAULT_SERVER_DIR)
        self.server_dir_field.setVisible(False)
        sl.addWidget(self._caption("Optional: run the server on this PC so friends can join you. "
                                   "The first start downloads everything and takes a while."))
        self.server_btn = self._big_button("Start Server")
        self.server_btn.setObjectName("startBtn")
        self.server_btn.clicked.connect(self.start_or_download_server)
        sl.addWidget(self.server_btn)
        # Rectangular progress bar: swaps in while the download runs.
        self.server_progress = QProgressBar()
        self.server_progress.setRange(0, 0)
        self.server_progress.setFormat("Downloading server files... leave this window open")
        self.server_progress.setMinimumHeight(56)
        self.server_progress.setVisible(False)
        sl.addWidget(self.server_progress)
        # Bold red failure readout + a way back.
        self.server_failure = QLabel()
        self.server_failure.setWordWrap(True)
        self.server_failure.setStyleSheet("font-size: 14px;")
        self.server_failure.setVisible(False)
        sl.addWidget(self.server_failure)
        self.server_retry = self._big_button("Try again", 44)
        self.server_retry.clicked.connect(self._reset_server_button)
        self.server_retry.setVisible(False)
        sl.addWidget(self.server_retry)
        self.selfhost_box.setVisible(False)
        self.selfhost_toggle.toggled.connect(self.selfhost_box.setVisible)
        l2.addWidget(self.selfhost_box)
        root.addWidget(card2)

        # ---- Step 3 ----
        card3, l3 = self._step_card(3, "Play",
            "Playing on your own PC needs nothing here. To join a friend, "
            "paste their address first. Exporting in step 2 fills it in.")
        self.patch_url_input = self._roomy_field("http://... your PC, or a friend's PC")
        self._field_block(l3, "Play on", self.patch_url_input)
        self.patch_url_input.textChanged.connect(self.save_settings)
        self.patch_btn = self._big_button("Patch Cemu and Play")
        self.patch_btn.setObjectName("patchBtn")
        self.patch_btn.clicked.connect(self._on_patch_play)
        l3.addWidget(self.patch_btn)
        l3.addSpacing(6)
        self.launch_btn = self._big_button("Just open Cemu without patching", 48)
        self.launch_btn.clicked.connect(self._on_launch)
        l3.addWidget(self.launch_btn)
        root.addWidget(card3)

        # ---- Messages ----
        messages = QGroupBox("Messages")
        ml = QVBoxLayout(messages)
        ml.setContentsMargins(24, 20, 24, 20)
        ml.setSpacing(8)
        ml.addWidget(self._caption("If anything looks red, copy the text and ask for help."))
        self.log = QTextEdit(objectName="logBox")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(190)
        self.log.setPlaceholderText("Messages appear here...")
        ml.addWidget(self.log)
        # backends write here
        self.server_log = self.log
        self.setup_log = self.log
        root.addWidget(messages)

        foot = QLabel("Tip: close this window when you are done playing. "
                      "Your player name and folders are remembered.")
        foot.setWordWrap(True)
        foot.setAlignment(Qt.AlignCenter)
        foot.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        root.addWidget(foot)
        root.addStretch(1)

    # ---------- export ----------
    def _default_export_parent(self):
        desk = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
        if desk and os.path.isdir(desk):
            return desk
        return os.path.expanduser("~")

    def _clean_host_and_port(self):
        host = self.custom_host.text().strip()
        if "://" in host:
            from urllib.parse import urlparse
            parsed = urlparse(host)
            if parsed.hostname:
                host = parsed.hostname
            if parsed.port and not self.custom_port.text().strip():
                self.custom_port.setText(str(parsed.port))
        host = host.strip().strip("/").split("/")[0].strip()
        port = self.custom_port.text().strip() or "8070"
        return host, port

    def _on_export(self):
        if not self._check_identity(show_popup=True):
            return
        host, port = self._clean_host_and_port()
        if not host:
            QMessageBox.warning(self, "Server address missing",
                                "Type the server IP or name first.")
            return
        if not port.isdigit():
            QMessageBox.warning(self, "Port problem",
                                "The port must be a number, for example 8070.")
            return
        base_url = f"http://{host}:{port}"
        self.save_settings()
        parent = QFileDialog.getExistingDirectory(
            self, "Where should the export folder be created?",
            self._default_export_parent())
        if not parent:
            return
        try:
            folder = self._build_export_folder(parent, host, port, base_url)
        except Exception as e:
            QMessageBox.warning(self, "Export failed",
                                f"Could not write the export folder:\n\n{e}")
            return
        self._last_export_folder = folder
        self.export_result.setText(f"Exported to:\n{folder}")
        self.open_export_btn.setVisible(True)
        self.patch_url_input.setText(base_url)
        self.save_settings()
        self.log.append(f"<b>[Export]</b> Connection folder ready: {folder}")
        QMessageBox.information(
            self, "Export complete",
            "The connection folder is ready.\n\n"
            "Paste 1: copy everything inside Paste-Into-Cemu into the Cemu folder.\n"
            "Paste 2: copy the one line from web_api_url.txt into the 3DS emulator config.\n\n"
            "Full steps are in README.txt inside the folder.")

    def _build_export_folder(self, parent, host, port, base_url):
        safe = re.sub(r"[^A-Za-z0-9]+", "-", host).strip("-") or "server"
        folder = os.path.join(parent, f"DockU-Connection-{safe}-{port}")
        cemu_pack = os.path.join(folder, "Paste-Into-Cemu")
        ds_pack = os.path.join(folder, "Paste-Into-3DS-Emulator")
        os.makedirs(cemu_pack, exist_ok=True)
        os.makedirs(ds_pack, exist_ok=True)

        username = self.cemu_username.text().strip()
        password = self.cemu_password.text()
        miiname = self.cemu_miiname.text().strip() or "Player"

        # ---- Paste 1: Cemu merge-copy tree ----
        ns_xml = EmulatorPatcher.build_cemu_network_services_xml(
            base_url, network_name="Custom-Server")
        with open(os.path.join(cemu_pack, "network_services.xml"),
                  "w", encoding="utf-8", newline="\n") as f:
            f.write(ns_xml)
        account_text = EmulatorPatcher.build_cemu_account_text(username, password, miiname)
        act_dir = os.path.join(cemu_pack, "mlc01", "usr", "save", "system", "act", "80000001")
        os.makedirs(act_dir, exist_ok=True)
        with open(os.path.join(act_dir, "account.dat"),
                  "w", encoding="utf-8", newline="\n") as f:
            f.write(account_text)
        # Console files (otp / seeprom / keys / certificates), same as the live patch.
        self.patcher._ensure_console_certs(cemu_pack)
        with open(os.path.join(cemu_pack, "cemu-settings-note.txt"),
                  "w", encoding="utf-8", newline="\n") as f:
            f.write(
                "Only needed if Cemu does not go online after Paste 1.\n"
                "\n"
                "1. Close Cemu completely.\n"
                "2. Open settings.xml in your Cemu folder with Notepad.\n"
                "3. Add this line just before </content> at the end:\n"
                "\n"
                f"    <proxy_server>{base_url}</proxy_server>\n"
                "\n"
                "4. Save and start Cemu again.\n")

        # ---- Paste 2: one line for 3DS emulators ----
        with open(os.path.join(ds_pack, "web_api_url.txt"),
                  "w", encoding="utf-8", newline="\n") as f:
            f.write(base_url + "\n")
        with open(os.path.join(ds_pack, "README-3DS.txt"),
                  "w", encoding="utf-8", newline="\n") as f:
            f.write(
                "Paste 2 - 3DS emulators (Citra / Lime3DS / Azahar)\n"
                "\n"
                "1. Open web_api_url.txt next to this file and copy the one line inside it.\n"
                "2. Open your emulator's qt-config.ini:\n"
                "     Citra:    %APPDATA%\\Citra\\config\\qt-config.ini\n"
                "     Lime3DS:  %APPDATA%\\Lime3DS\\config\\qt-config.ini\n"
                "     Azahar:   %APPDATA%\\Azahar\\config\\qt-config.ini\n"
                "3. Find the line starting with  web_api_url=  and paste over the whole line.\n"
                "4. Save and restart the emulator.\n")

        with open(os.path.join(folder, "README.txt"),
                  "w", encoding="utf-8", newline="\n") as f:
            f.write(
                f"Dock U connection pack for {base_url}\n"
                f"Player: {username} / Mii: {miiname}\n"
                "\n"
                "PASTE 1 - Cemu (Wii U):\n"
                "1. Open your Cemu folder, the one with Cemu.exe in it.\n"
                "2. Copy EVERYTHING inside the Paste-Into-Cemu folder into it.\n"
                "   Say yes to merge and overwrite.\n"
                f"3. The account {username} is already included.\n"
                "4. Start Cemu. Under Options, General settings, Account, the account\n"
                "   should be there and online.\n"
                "5. If Cemu still does not go online, do the one extra line\n"
                "   in Paste-Into-Cemu\\cemu-settings-note.txt.\n"
                "\n"
                "PASTE 2 - 3DS emulators (Citra / Lime3DS / Azahar):\n"
                "1. Open Paste-Into-3DS-Emulator\\web_api_url.txt, copy the one line.\n"
                "2. Paste it over the web_api_url= line in your emulator's qt-config.ini.\n"
                "   Exact config paths are in Paste-Into-3DS-Emulator\\README-3DS.txt.\n"
                "3. Save and restart the emulator.\n"
                "\n"
                f"Note: the account {username} must exist on the custom server with\n"
                "the same password. If the server owner has not created it for you yet,\n"
                "ask them first, or online login will fail.\n")
        return folder

    def _open_export_folder(self):
        folder = getattr(self, "_last_export_folder", "")
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "No export yet",
                                "Press Export connection folder first.")
            return
        try:
            if os.name == "nt":
                os.startfile(folder)
            else:
                QMessageBox.information(self, "Export folder", folder)
        except Exception as e:
            QMessageBox.warning(self, "Open folder failed", str(e))

    def _set_server_busy(self, busy, message=""):
        """Swap the Start button for the progress bar (and back)."""
        self._server_busy = busy
        self.server_btn.setVisible(not busy)
        self.server_failure.setVisible(False)
        self.server_retry.setVisible(False)
        if message:
            self.server_progress.setFormat(message)
        self.server_progress.setVisible(busy)

    def start_or_download_server(self):
        """One button: download the server stack on first use, then start it."""
        if self._server_busy:
            self.log.append("[Server] Please wait, the previous step is still working...")
            return
        if getattr(self, "server_running", False):
            self.stop_server()
            return
        s_dir = self.server_dir_field.text().strip().strip('"') or DEFAULT_SERVER_DIR
        self.server_dir_field.setText(s_dir)
        self.save_settings()
        has_compose = os.path.isdir(s_dir) and any(
            os.path.isfile(os.path.join(s_dir, f))
            for f in ("compose.yml", "docker-compose.yml", "compose.yaml"))
        env_ready = os.path.isfile(os.path.join(s_dir, "environment", "account.local.env"))
        if not (has_compose and env_ready):
            self._ensure_docker_then(self._begin_download)
            return
        self._ensure_docker_then(self._begin_start)

    def _ensure_docker_then(self, action):
        """Run action once Docker answers. Never blocks the window."""
        _CACHED_RESULTS.pop("docker_available", None)
        if _docker_available():
            action()
            return
        self._set_server_busy(True, "Starting Docker Desktop... please wait")
        self.log.append("[Docker] Docker is not running yet. Starting Docker Desktop, "
                        "this can take a minute...")
        try:
            _start_docker_desktop()
        except Exception:
            pass
        self._docker_wait_tries = 0
        self._docker_wait_action = action
        self._docker_wait_active = True
        QTimer.singleShot(5000, self._wait_for_docker)

    def _wait_for_docker(self):
        if not self._docker_wait_active:
            return
        _CACHED_RESULTS.pop("docker_available", None)
        if _docker_available():
            self._docker_wait_active = False
            action, self._docker_wait_action = self._docker_wait_action, None
            self.log.append("[Docker] Docker is ready.")
            if action:
                action()
            return
        self._docker_wait_tries += 1
        if self._docker_wait_tries >= 24:
            self._docker_wait_active = False
            self._docker_wait_action = None
            self._set_server_busy(False)
            self._show_server_failure([
                "Docker Desktop did not start after 2 minutes.",
                "Open Docker Desktop by hand, wait until it says Running, "
                "then press Start Server again.",
            ])
            return
        QTimer.singleShot(5000, self._wait_for_docker)

    def _begin_download(self):
        self._set_server_busy(True, "Downloading server files... leave this window open")
        self.log.append("<b>[Server]</b> No server files yet. "
                        "Downloading them from GitHub first...")
        self._auto_start_after_deploy = True
        self._suppress_deploy_complete_popup = True
        self.deployer.automated_install_stack()

    def _begin_start(self):
        self._set_server_busy(True, "Starting server... please wait")
        self.log.append("<b>[Server]</b> Starting the server (lite set for weak PCs)...")
        self.start_server(services=LITE_SERVICES, on_done=self._after_start_attempt)

    def _after_start_attempt(self, code):
        if code != 0:
            self._set_server_busy(False)
            self._show_server_failure()
            return
        # `up -d` returns 0 even if a container exits right away, so confirm.
        QTimer.singleShot(8000, self._verify_server_up)

    def _verify_server_up(self):
        self._check_docker_status()
        if getattr(self, "server_running", False):
            self._set_server_busy(False)
            self.log.append("<b>[Server]</b> Server is up. Friends can connect now.")
            return
        self._set_server_busy(False)
        self._show_server_failure()

    # ---------- actions ----------
    def _on_deploy_complete(self, code):
        self._suppress_deploy_complete_popup = False
        auto = getattr(self, "_auto_start_after_deploy", False)
        self._auto_start_after_deploy = False
        if code != 0:
            self._set_server_busy(False)
            self._show_server_failure()
            return
        self.server_btn.setVisible(True)
        if auto:
            self.log.append("<b>[Setup]</b> Download done. Starting the server...")
            self._begin_start()
        else:
            self._set_server_busy(False)
            self.log.append("<b>[Setup]</b> Done! The server files are ready.")
            QMessageBox.information(self, "Setup Complete",
                "The server files are ready.")

    def _important_error_lines(self):
        """Pull the telling lines out of the message log for the FAILURE readout."""
        try:
            text = self.log.toPlainText()
        except Exception:
            return []
        keywords = ("error", "fail", "exception", "traceback", "denied", "refused",
                    "timed out", "timeout", "not found", "invalid", "cannot", "could not",
                    "eaddrinuse", "econnrefused", "unauthorized", "forbidden", "no such")
        found = []
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("[Done]") or s.startswith("[Run]"):
                continue
            if any(k in s.lower() for k in keywords):
                if not found or found[-1] != s:
                    found.append(s[-160:])
        if not found:
            plain = [l.strip() for l in text.splitlines() if l.strip()]
            return plain[-3:]
        return found[-6:]

    def _show_server_failure(self, lines=None):
        lines = lines if lines is not None else self._important_error_lines()
        reason = lines[0] if lines else "the download did not finish."
        rest = "<br>".join(html.escape(l) for l in lines[1:6])
        body = f"<b style='color:#ff7b72; font-size:15px;'>FAILURE - {html.escape(reason)}</b>"
        if rest:
            body += f"<br><span style='color:#e8e8f0;'>{rest}</span>"
        self.server_btn.setVisible(False)
        self.server_failure.setText(body)
        self.server_failure.setVisible(True)
        self.server_retry.setVisible(True)
        self.log.append("<b>[Server]</b> Download failed. See the red message above.")

    def _reset_server_button(self):
        self._docker_wait_active = False
        self._docker_wait_action = None
        self._server_busy = False
        self.server_failure.setVisible(False)
        self.server_retry.setVisible(False)
        self.server_progress.setVisible(False)
        self.server_btn.setVisible(True)

    def _on_patch_play(self):
        if not self._check_identity(show_popup=True):
            return
        if not os.path.isdir(self.cemu_dir_field.text().strip()):
            QMessageBox.warning(self, "Cemu Missing",
                "I can't find your Cemu folder.\n\nPress 'Find automatically' or 'Browse...' first.")
            return
        self.patcher.apply_cemu_patch_all()

    def _on_launch(self):
        path = self.cemu_dir_field.text().strip()
        exe = None
        if os.path.isfile(path) and os.path.basename(path).lower() == "cemu.exe":
            exe = path
        elif os.path.isdir(path):
            for root, _, files in os.walk(path):
                if "cemu.exe" in [f.lower() for f in files]:
                    for f in files:
                        if f.lower() == "cemu.exe":
                            exe = os.path.join(root, f)
                            break
                if exe:
                    break
        if not exe:
            QMessageBox.warning(self, "Cemu Missing", "Could not find cemu.exe in that folder.")
            return
        subprocess.Popen([exe], cwd=os.path.dirname(exe))

    def _check_identity(self, show_popup=False):
        u = self.cemu_username.text().strip()
        p = self.cemu_password.text()
        if not (6 <= len(u) <= 16):
            if show_popup:
                QMessageBox.warning(self, "Check username", "Username must be 6-16 characters.")
            return False
        if not p:
            if show_popup:
                QMessageBox.warning(self, "Check password", "Please invent a password (not your real one).")
            return False
        return True

    def _auto_find_cemu(self):
        found = find_cemu_automatic()
        if found:
            self.cemu_dir_field.setText(found)
            self.log.append(f"[Cemu] Found: {found}")
        else:
            self.log.append("[Cemu] Not found automatically - please Browse...")
            self._browse_cemu()

    def _browse_cemu(self):
        d = QFileDialog.getExistingDirectory(self, "Where is Cemu installed?",
                                             self.cemu_dir_field.text() or os.path.expanduser("~"))
        if d:
            self.cemu_dir_field.setText(d)
            self.save_settings()

    # ---------- plumbing expected by backends ----------
    def _get_target_port(self):
        import re
        m = re.search(r":(\d+)(?:/|$)", self.patch_url_input.text().strip())
        return m.group(1) if m else "8070"

    def _resolve_target_node(self, url=None, is_official=False):
        from urllib.parse import urlparse
        raw = (url if url is not None else self.patch_url_input.text()).strip() \
            or f"http://{get_local_ip()}:{self._get_target_port()}"
        if is_official:
            raw = "https://api.pretendo.network"
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        host = parsed.hostname or get_local_ip()
        port = parsed.port or (None if is_official else int(self._get_target_port()))
        scheme = parsed.scheme or ("https" if is_official else "http")
        out = f"{scheme}://{host}" + (f":{port}" if port else "")
        return out.rstrip("/"), host, str(port) if port else ""

    def refresh_vault_list(self):
        pass

    def _tick(self):
        self._check_docker_status()
        ip = get_local_ip()
        self.ip_info.setText(f"Your address on this network:  http://{ip}:8070")
        running = bool(getattr(self, "server_running", False))
        self.status_label.setText("Server is RUNNING" if running else "Server is STOPPED")
        self.status_label.setStyleSheet("color: #3fb950; font-size: 19px; font-weight: bold;"
                                        if running else "color: #CC0000; font-size: 19px; font-weight: bold;")
        self.server_btn.setText("Stop Server" if running else "Start Server")
        self.server_btn.setObjectName("stopBtn" if running else "startBtn")
        self.server_btn.style().unpolish(self.server_btn)
        self.server_btn.style().polish(self.server_btn)

    def _load(self):
        store = SecretStore()
        self.cemu_username.setText(str(self.settings.value("username", "")))
        self.cemu_password.setText(store.get("ui.cemu_password", ""))
        self.cemu_miiname.setText(str(self.settings.value("miiname", "")))
        self.cemu_dir_field.setText(str(self.settings.value("cemu_dir", "") or self.cemu_dir_field.text()))
        self.server_dir_field.setText(str(self.settings.value("server_dir", "") or DEFAULT_SERVER_DIR))
        self.custom_host.setText(str(self.settings.value("custom_host", "") or get_local_ip()))
        self.custom_port.setText(str(self.settings.value("custom_port", "") or "8070"))
        self.patch_url_input.setText(str(self.settings.value("target_url", "") or f"http://{get_local_ip()}:8070"))
        self.cached_password = store.get("ui.sudo_cache")

    def save_settings(self):
        try:
            store = SecretStore()
            self.settings.setValue("username", self.cemu_username.text())
            store.set("ui.cemu_password", self.cemu_password.text(), save=False)
            self.settings.setValue("miiname", self.cemu_miiname.text())
            self.settings.setValue("cemu_dir", self.cemu_dir_field.text())
            self.settings.setValue("server_dir", self.server_dir_field.text())
            self.settings.setValue("custom_host", self.custom_host.text())
            self.settings.setValue("custom_port", self.custom_port.text())
            self.settings.setValue("target_url", self.patch_url_input.text())
            store.save()
            self.settings.sync()
        except Exception:
            pass

    def closeEvent(self, event):
        try:
            self.save_settings()
        except Exception:
            pass
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    lock_path = os.path.join(QStandardPaths.writableLocation(QStandardPaths.TempLocation),
                             "3d_open_dock_u_simple.lock")
    lock = QLockFile(lock_path)
    if not lock.tryLock(100):
        QMessageBox.warning(None, "Already Running", "3D Open Dock U is already open.")
        sys.exit(1)
    app.setStyle("Fusion")
    win = SimpleDock()
    win._instance_lock = lock
    win.show()
    sys.exit(app.exec())
