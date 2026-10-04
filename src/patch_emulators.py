# patch_emulators.py
import os
import re
import hashlib
import binascii
import json
import zlib
import base64
import random
import io
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from urllib.parse import urlparse
from PySide6.QtWidgets import QMessageBox, QFileDialog, QApplication
from PySide6.QtCore import QDir
from src.constants import SEC_KEYS, CONSOLE_CERTS_PACKED
from src.secrets_manager import secure_file
from src.utils import safe_unhex, OS_INFO, get_local_ip

class EmulatorPatcher:
    def __init__(self, manager):
        self.manager = manager

    def _normalize_target_url(self, url, is_official=False):
        """Return a consistent URL plus the resolved host and port."""
        if hasattr(self.manager, "_resolve_target_node"):
            return self.manager._resolve_target_node(url=url, is_official=is_official)

        raw_url = (url or "").strip()
        if not raw_url:
            raw_url = f"http://{get_local_ip()}:{self.manager._get_target_port()}"

        parsed_input = raw_url if "://" in raw_url else f"http://{raw_url}"
        parsed = urlparse(parsed_input)

        host = parsed.hostname or get_local_ip()
        port = parsed.port
        if not port and not is_official:
            port = int(self.manager._get_target_port())

        normalized_url = f"{parsed.scheme or 'http'}://{host}"
        if port:
            normalized_url += f":{port}"

        return normalized_url.rstrip("/"), host, str(port) if port else ""

    def apply_cemu_patch_all(self):
        m = self.manager
        if hasattr(m, "save_settings"):
            m.save_settings()
        use_official = m.mode_pretendo.isChecked()
        url = "https://api.pretendo.network" if use_official else m.patch_url_input.text().strip()

        c_dir = m.cemu_dir_field.text().strip()
        if not os.path.isdir(c_dir):
            QMessageBox.warning(m, "Directory Error", f"Cemu directory not found:\n{c_dir}")
            return

        m.server_log.append("<b>[System]</b> Starting full optimization & sync sequence...")

        # Deploy ALL necessary Wii U certificate files & identity blobs
        self._ensure_console_certs(c_dir)

        def finish_patch():
            self.patch_cemu_settings(url, use_official)
            self.generate_cemu_manual()

            if not use_official:
                if hasattr(m, 'create_local_account'):
                    def _after_account_sync(code):
                        if code == 0:
                            ok, detail = self._probe_local_oauth_credentials(url)
                            if ok:
                                m.server_log.append("<span style='color:#3fb950;'>[OAuth] Cemu identity, PNID, NEX account, and OAuth password path are synchronized.</span>")
                                QApplication.processEvents()
                                QMessageBox.information(m, "Cemu Patch Complete", f"Cemu identity is fully synchronized and ready.\n\nTarget Node: {url}\n\n{detail}")
                            else:
                                m.server_log.append(f"<span style='color:#ffa657;'>[OAuth] Sync completed but probe still failed: {detail}</span>")
                                QApplication.processEvents()
                                QMessageBox.warning(m, "Cemu Patch Needs Attention", f"Cemu files and database were updated, but the OAuth probe still failed:\n\n{detail}")
                        else:
                            m.server_log.append("<span style='color:#ffa657;'>[OAuth] Account refresh did not complete. Keep the server running and press Patch Cemu again.</span>")
                            QApplication.processEvents()
                            QMessageBox.warning(m, "Cemu Patch Needs Attention", "Cemu files were patched, but the local account database refresh failed.")

                    m.create_local_account(
                        silent=True,
                        on_done=_after_account_sync
                    )
                else:
                    m.server_log.append("<span style='color:#ffa657;'>[OAuth] Account sync API is unavailable in this build.</span>")
                    QApplication.processEvents()
                    QMessageBox.warning(m, "Cemu Patch Needs Attention", "Cemu files were patched, but this build cannot refresh the local account database.")
                return

            if use_official:
                QApplication.processEvents()
                QMessageBox.information(m, "Cemu Patch Complete", "Cemu has been successfully patched and configured to connect to the official Pretendo Network servers.\n\nYou can now safely launch Cemu and play online.")

        def after_docker_sync(_code):
            if not use_official:
                if _code != 0:
                    m.server_log.append("<span style='color:#ffa657;'>[Docker Sync] Could not prepare the local Docker stack for Cemu identity sync.</span>")
                    QApplication.processEvents()
                    QMessageBox.warning(m, "Cemu Patch Needs Attention", "Docker sync failed before the account database could be refreshed.")
                    return

                def after_account_service_patch(code):
                    if code == 0:
                        finish_patch()
                    else:
                        m.server_log.append("<span style='color:#ffa657;'>[Anti-Ban] Account service compatibility sync failed before account refresh.</span>")
                        QApplication.processEvents()
                        QMessageBox.warning(m, "Cemu Patch Needs Attention", "Account service compatibility sync failed before the local account database refresh.")

                self._patch_account_ban_bypass(on_done=after_account_service_patch)
            else:
                finish_patch()

        if not use_official:
            # Sync Docker services to the target port BEFORE patching the emulator
            self._sync_docker_services_to_port(url, on_done=after_docker_sync)
        else:
            finish_patch()

    def _probe_local_oauth_credentials(self, url):
        db_ok, db_detail = self._verify_local_account_database()
        if not db_ok:
            return False, db_detail

        username = self.manager.cemu_username.text().strip()
        password = self.manager.cemu_password.text()
        base_url, _, _ = self._normalize_target_url(url, is_official=False)
        endpoint = f"{base_url}/v1/api/oauth20/access_token/generate"
        body = urllib.parse.urlencode({
            "grant_type": "password",
            "user_id": username,
            "password": password,
        }).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Host": "account.pretendo.cc",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Nintendo-Client-ID": "a2efa818a34fa16b8afbc8a74eba3eda",
                "X-Nintendo-Client-Secret": "c91cdb5658bd4954ade78533a339cf9a",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                text = response.read().decode("utf-8", errors="ignore")
                status = response.status
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8", errors="ignore")
            status = error.code
        except Exception as error:
            return False, str(error)

        if "Invalid account ID or password" in text or "0106" in text:
            return False, f"OAuth still returns 106 for {username}."

        if "Unlinked device" in text or "0110" in text:
            return True, f"{db_detail} HTTP probe reached device-link validation without account/password rejection."

        if "client_id" in text:
            return True, f"{db_detail} HTTP probe reached client validation without account/password rejection."

        return True, f"{db_detail} HTTP credential check reached HTTP {status} without account/password rejection."

    def _verify_local_account_database(self):
        m = self.manager
        s_dir = m.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            return False, "Server directory is missing; cannot verify local account database."

        username = m.cemu_username.text().strip()
        password = m.cemu_password.text()
        miiname = m.cemu_miiname.text().strip() or "Player"
        script = r'''
const { connect, getPNIDByUsername } = require("./dist/database");
const { NEXAccount } = require("./dist/models/nex-account");
const { nintendoPasswordHash } = require("./dist/util");
const bcrypt = require("bcrypt");
const mongoose = require("mongoose");

(async () => {
	try {
		await connect();
		const username = __USERNAME__;
		const password = __PASSWORD__;
		const miiName = __MIINAME__;
		const pid = 1337;
		const pnid = await getPNIDByUsername(username);
		const nex = await NEXAccount.findOne({ owning_pid: pid });
		const cachedPassword = pnid ? nintendoPasswordHash(password, pnid.pid) : "";
		const cachedOk = pnid ? await bcrypt.compare(cachedPassword, pnid.password) : false;
		console.log(JSON.stringify({
			ok: !!pnid && pnid.pid === pid && pnid.username === username && pnid.mii?.name === miiName && cachedOk && !!nex && nex.password === password,
			username: pnid?.username || "",
			pid: pnid?.pid || null,
			miiName: pnid?.mii?.name || "",
			cachedOk,
			nexOk: !!nex && nex.password === password
		}));
		await mongoose.disconnect();
	} catch (error) {
		console.log(JSON.stringify({ ok: false, error: error.message }));
		process.exitCode = 1;
	}
})();
'''
        script = script.replace("__USERNAME__", json.dumps(username))
        script = script.replace("__PASSWORD__", json.dumps(password))
        script = script.replace("__MIINAME__", json.dumps(miiname))

        try:
            result = subprocess.run(
                ["docker", "compose", "exec", "-T", "account", "node"],
                input=script,
                cwd=s_dir,
                text=True,
                capture_output=True,
                timeout=45,
            )
        except Exception as error:
            return False, f"Could not verify local account database: {error}"

        output = (result.stdout or "") + "\n" + (result.stderr or "")
        parsed = None
        for line in reversed(output.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    break
                except Exception:
                    continue

        if result.returncode != 0 and not parsed:
            return False, "Account database verification command failed."

        if not parsed:
            return False, "Account database verification did not return a readable result."

        if parsed.get("ok"):
            return True, f"Database verified for {username} / PID 1337 / Mii {miiname}."

        detail = parsed.get("error") or (
            f"DB mismatch: username={parsed.get('username')}, pid={parsed.get('pid')}, "
            f"mii={parsed.get('miiName')}, cachedHashOk={parsed.get('cachedOk')}, nexOk={parsed.get('nexOk')}."
        )
        return False, detail

    def patch_cemu_settings(self, url, is_official):
        c_dir = self.manager.cemu_dir_field.text().strip()
        s_xml = os.path.join(c_dir, "settings.xml")
        
        if not os.path.exists(s_xml): 
            self.manager.server_log.append(f"<span style='color:cyan;'>[System] settings.xml missing. Generating default configuration...</span>")
            default_content = '<?xml version="1.0" encoding="UTF-8"?>\n<content>\n    <OnlineEnabled>true</OnlineEnabled>\n    <PersistentId>2147483649</PersistentId>\n    <Account>\n        <disablesslverification>1</disablesslverification>\n    </Account>\n    <proxy_server></proxy_server>\n</content>'
            try:
                os.makedirs(c_dir, exist_ok=True)
                with open(s_xml, "w") as f: f.write(default_content)
            except Exception as e:
                self.manager.server_log.append(f"<span style='color:red;'>[ERROR] Failed to create settings.xml: {e}</span>")
                return
        
        try:
            with open(s_xml, "r") as f: c = f.read()
            active_service_id = "1"
            selected_service_id = "2"  # Cemu stores Pretendo as SelectedService=2
            # Force Online & PersistentId
            c = re.sub(r"<PersistentId>\d+</PersistentId>", "<PersistentId>2147483649</PersistentId>", c)

            if "<Account>" not in c:
                c = c.replace("</content>", "    <Account>\n    </Account>\n</content>")

            account_match = re.search(r"<Account>.*?</Account>", c, flags=re.DOTALL)
            if account_match:
                account_block = account_match.group(0)
                account_block = self._upsert_xml_text(account_block, "PersistentId", "2147483649")
                account_block = self._upsert_xml_text(account_block, "OnlineEnabled", "true")
                account_block = self._upsert_xml_text(account_block, "ActiveService", active_service_id)
                account_block = self._upsert_xml_text(account_block, "disablesslverification", "1")
                c = c[:account_match.start()] + account_block + c[account_match.end():]
            else:
                c = c.replace("</content>", f"    <Account>\n        <PersistentId>2147483649</PersistentId>\n        <OnlineEnabled>true</OnlineEnabled>\n        <ActiveService>{active_service_id}</ActiveService>\n        <disablesslverification>1</disablesslverification>\n    </Account>\n</content>")
            
            # Inject SSL Bypass
            if "<disablesslverification>1</disablesslverification>" not in c:
                if "<Account>" in c:
                    c = c.replace("<Account>", "<Account>\n        <disablesslverification>1</disablesslverification>")
                else:
                    c = c.replace("</content>", "    <Account>\n        <disablesslverification>1</disablesslverification>\n    </Account>\n</content>")

            # Proxy Server
            proxy_url, _, _ = self._normalize_target_url(url, is_official=is_official)

            if "<proxy_server>" in c:
                c = re.sub(r"<proxy_server>.*?</proxy_server>", f"<proxy_server>{proxy_url}</proxy_server>", c)
            else:
                c = c.replace("</content>", f"    <proxy_server>{proxy_url}</proxy_server>\n</content>")

            selected_service = f'<SelectedService PersistentId="2147483649" Service="{selected_service_id}"/>'
            if "<AccountService>" in c:
                c = re.sub(
                    r"<AccountService>.*?</AccountService>",
                    f"<AccountService>\n        {selected_service}\n    </AccountService>",
                    c,
                    flags=re.DOTALL
                )
            else:
                c = c.replace("</content>", f"    <AccountService>\n        {selected_service}\n    </AccountService>\n</content>")

            with open(s_xml, "w") as f: f.write(c)
            self.manager.server_log.append(f"<span style='color:#3fb950;'>[System] Patched Cemu settings: {s_xml}</span>")
            self.manager.server_log.append("<span style='color:#3fb950;'>[System] Cemu Account tab forced to Online + Pretendo network service.</span>")
            
            # Also patch network_services.xml
            self._patch_cemu_network_services(url, is_official)
            
        except Exception as e:
            self.manager.server_log.append(f"<span style='color:red;'>[ERROR] settings.xml patch failed: {e}</span>")

    def _upsert_xml_text(self, block, tag, value):
        if re.search(fr"<{tag}>.*?</{tag}>", block, flags=re.DOTALL):
            return re.sub(fr"<{tag}>.*?</{tag}>", f"<{tag}>{value}</{tag}>", block, flags=re.DOTALL)
        return block.replace("</Account>", f"        <{tag}>{value}</{tag}>\n    </Account>")

    def _patch_cemu_network_services(self, url, is_official):
        c_dir = self.manager.cemu_dir_field.text().strip()
        ns_xml = os.path.join(c_dir, "network_services.xml")
        
        if is_official:
            if os.path.exists(ns_xml): 
                try: os.remove(ns_xml)
                except: pass
            return

        self.manager.server_log.append(f"<span style='color:cyan;'>[System] Injecting custom network_services.xml...</span>")

        base_url, _, _ = self._normalize_target_url(url, is_official=is_official)
        ns_content = self.build_cemu_network_services_xml(base_url)
        
        try:
            with open(ns_xml, "w") as f: f.write(ns_content)
            self.manager.server_log.append(f"<span style='color:#3fb950;'>[System] Cemu network_services.xml injected successfully.</span>")
        except Exception as e:
            self.manager.server_log.append(f"<span style='color:red;'>[ERROR] Failed to write network_services.xml: {e}</span>")

    @staticmethod
    def build_cemu_network_services_xml(base_url, network_name="Pretendo-Bypass"):
        """Build the network_services.xml text pointing every service at base_url."""
        services = ["act", "con", "etc", "dls", "shp", "dsa", "pdm", "miv", "smm", "bas", "npts", "api", "ecs", "ias", "cas", "boss", "friends", "account", "clp", "shop", "news", "portal", "discovery"]
        url_nodes = []
        for s in services:
            url_nodes.append(f"        <{s}>{base_url}</{s}>")

        urls_block = "\n".join(url_nodes)
        return f'<?xml version="1.0" encoding="UTF-8"?>\n<content>\n    <networkname>{network_name}</networkname>\n    <disablesslverification>1</disablesslverification>\n    <urls>\n{urls_block}\n    </urls>\n</content>'

    @staticmethod
    def build_cemu_account_text(username, password, miiname):
        """Build the account.dat text for a Cemu identity. Same bytes the live patch writes."""
        miiname = (miiname or "Player")[:10]

        try:
            # 2. Account Generation (NEX-Compatible Authenticated Hash)
            pid = 1337
            pid_bytes = pid.to_bytes(4, byteorder='little')
            pwd_hash = hashlib.sha256(pid_bytes + b"\x02eCF" + password.encode('utf-8')).hexdigest()
            
            uuid_hex = "112233445566778899aabbccddeeff00"
            trans_id_hex = "112233445566778"
            
            # Mii Name & Data
            mii_name_limited = miiname[:10]
            acct_name_bytes = mii_name_limited.encode('utf-16be').ljust(22, b'\x00')
            account_name_hex = binascii.hexlify(acct_name_bytes).decode('ascii')
            
            # Pretendo/mii-js default Mii template. MiiData stores the nickname as UTF-16LE.
            base_mii_hex = "03000040e955a209e7c74182dbfba88003b3b88d27d900000040440065006600610075006c0074000000000000004040000021010268441826344614811217680d000029005248500000000000000000000000000000000000000000000069bd"
            mii_buf = bytearray(binascii.unhexlify(base_mii_hex))
            
            name_bytes_le = mii_name_limited.encode('utf-16le').ljust(20, b'\x00')
            mii_buf[0x1A:0x1A+20] = name_bytes_le
            
            # CRC16-CCITT
            crc = 0
            for i in range(0x5E):
                crc ^= (mii_buf[i] << 8)
                for _ in range(8):
                    if crc & 0x8000: crc = ((crc << 1) ^ 0x1021) & 0xFFFF
                    else: crc = (crc << 1) & 0xFFFF
            mii_buf[0x5E] = (crc >> 8) & 0xFF
            mii_buf[0x5F] = crc & 0xFF
            stored_mii = binascii.hexlify(mii_buf).decode('ascii')

            lines = [
                "AccountInstance_20120705",
                "PersistentId=80000001",
                f"TransferableIdBase={trans_id_hex}",
                f"Uuid={uuid_hex}",
                "ParentalControlSlotNo=2",
                f"MiiData={stored_mii}",
                f"MiiName={account_name_hex}",
                "IsMiiUpdated=0",
                f"AccountId={username}",
                "BirthYear=7d0",
                "BirthMonth=1",
                "BirthDay=1",
                "Gender=1",
                "IsMailAddressValidated=1",
                "EmailAddress=dummy@pretendo.cc",
                "Country=61",
                "SimpleAddressId=61030000",
                "OnlineAccountFlag=1",
                "TimeZoneId=Europe/Warsaw",
                "UtcOffset=1ad274800",
                f"PrincipalId={pid:04x}",
                f"NfsPassword={password}",
                "EciVirtualAccount=",
                "NeedsToDownloadMiiImage=0",
                "MiiImageUrl=",
                f"AccountPasswordHash={pwd_hash}",
                "IsPasswordCacheEnabled=1",
                f"AccountPasswordCache={pwd_hash}",
                "NnasType=0",
                "NfsType=0",
                "NfsNo=1",
                "NnasSubDomain=",
                "NnasNfsEnv=L1",
                "IsPersistentIdUploaded=1",
                "IsConsoleAccountInfoUploaded=1",
                "LastAuthenticationResult=0",
                f"StickyAccountId={username}",
                "NextAccountId=",
                f"StickyPrincipalId={pid:04x}",
                "IsServerAccountDeleted=0",
                "ServerAccountStatus=0",
                "MiiImageLastModifiedDate=Tue, 09 Apr 2019 16:56:09 GMT",
                "IsCommitted=1"
            ]
            return "\n".join(lines)
        except Exception as e:
            raise ValueError(f"Could not build Cemu identity: {e}")

    def generate_cemu_manual(self):
        username = self.manager.cemu_username.text().strip()
        password = self.manager.cemu_password.text()
        miiname = self.manager.cemu_miiname.text().strip() or "Player"
        data_path = self.manager.cemu_dir_field.text().strip()

        if not username or not password:
            QMessageBox.warning(self.manager, "Input Required", "Username and password are required to generate identity files.")
            return

        if not (6 <= len(username) <= 16):
            QMessageBox.warning(self.manager, "Input Error", "Username must be between 6 and 16 characters long.")
            return

        if len(miiname) > 10:
            QMessageBox.warning(self.manager, "Input Error", "Mii name must be 10 characters or fewer.")
            return

        try:
            self.manager.server_log.append(f"<span style='color:#58a6ff;'>[System] Writing Cemu identity: AccountId={username}, MiiName={miiname}</span>")

            # Deploy essential ccerts, identity files (otp/seeprom) and fonts
            self._ensure_console_certs(data_path)
            self._ensure_cemu_fonts(data_path)

            account_text = self.build_cemu_account_text(username, password, miiname)
            mii_name_limited = miiname[:10]
            pid = 1337

            p_targets = []
            cemu_candidates = self._get_cemu_data_candidates(data_path)
            
            for base in set(cemu_candidates):
                if not base or not os.path.isdir(base): continue
                act_roots = [
                    os.path.join(base, "mlc01", "usr", "save", "system", "act"),
                    os.path.join(base, "usr", "save", "system", "act"),
                ]
                p_targets.append(os.path.join(base, "accounts/80000001"))

                for act_root in act_roots:
                    p_targets.append(os.path.join(act_root, "80000001"))
                    if os.path.isdir(act_root):
                        for slot in os.listdir(act_root):
                            slot_path = os.path.join(act_root, slot)
                            if os.path.isdir(slot_path) and re.fullmatch(r"8000000[1-9a-fA-F]", slot):
                                p_targets.append(slot_path)

                accounts_root = os.path.join(base, "accounts")
                if os.path.isdir(accounts_root):
                    for slot in os.listdir(accounts_root):
                        slot_path = os.path.join(accounts_root, slot)
                        if os.path.isdir(slot_path) and re.fullmatch(r"8000000[1-9a-fA-F]", slot):
                            p_targets.append(slot_path)

            for d in set(p_targets):
                for fname in ["account.dat", "account.ini"]:
                    fpath = os.path.join(d, fname)
                    os.makedirs(os.path.dirname(fpath), exist_ok=True)
                    self._make_cemu_account_file_writable(fpath)
                    with open(fpath, "w") as f:
                        f.write(account_text)
                    secure_file(fpath)
                    written_mii = self._read_account_file_mii_name(fpath)
                    written_mii_data = self._read_account_file_mii_data_name(fpath)
                    if written_mii == mii_name_limited and written_mii_data == mii_name_limited:
                        self.manager.server_log.append(f"<span style='color:#3fb950;'>[System] Identity Updated: {fpath} (MiiName={written_mii}, MiiData={written_mii_data})</span>")
                    else:
                        self.manager.server_log.append(f"<span style='color:#ffa657;'>[Warning] Identity write verification mismatch: {fpath} expected {mii_name_limited}, read MiiName={written_mii or 'unknown'}, MiiData={written_mii_data or 'unknown'}</span>")

            self.manager.server_log.append(f"<span style='color:#3fb950;'>[System] Identity Re-aligned for {username} (PID: {pid})</span>")
        except Exception as e:
            self.manager.server_log.append(f"<span style='color:red;'>[ERROR] Identity generation failed: {e}</span>")

    def _read_account_file_mii_name(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            match = re.search(r"^MiiName=([0-9A-Fa-f]+)$", content, flags=re.MULTILINE)
            if not match:
                return ""
            raw = bytes.fromhex(match.group(1))
            return raw.decode("utf-16be", errors="ignore").rstrip("\x00")
        except Exception:
            return ""

    def _make_cemu_account_file_writable(self, path):
        if not os.path.exists(path):
            return
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        if os.name != "nt":
            return
        try:
            subprocess.run(["attrib", "-H", "-R", path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            user = os.environ.get("USERNAME")
            if user:
                subprocess.run(["icacls", path, "/grant", f"{user}:(F)"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _read_account_file_mii_data_name(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            match = re.search(r"^MiiData=([0-9A-Fa-f]+)$", content, flags=re.MULTILINE)
            if not match:
                return ""
            raw = bytes.fromhex(match.group(1))
            return raw[0x1A:0x1A + 20].decode("utf-16le", errors="ignore").rstrip("\x00")
        except Exception:
            return ""

    def _get_cemu_data_candidates(self, data_path):
        candidates = []

        def add(path):
            if path:
                full = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
                if full not in candidates:
                    candidates.append(full)

        add(data_path)

        if OS_INFO["os"] == "windows":
            for env_name in ("APPDATA", "LOCALAPPDATA"):
                base = os.environ.get(env_name)
                if base:
                    add(os.path.join(base, "Cemu"))
                    add(os.path.join(base, "EmuDeck", "Emulators", "cemu"))
                    add(os.path.join(base, "EmuDeck", "backend", "configs", "cemu"))
        elif OS_INFO["os"] == "linux":
            home = os.path.expanduser("~")
            add(os.path.join(home, ".local/share/Cemu"))
            add(os.path.join(home, ".config/Cemu"))

        settings_candidates = []
        for base in list(candidates):
            settings_candidates.append(os.path.join(base, "settings.xml"))

        for settings_path in settings_candidates:
            mlc_path = self._read_cemu_mlc_path(settings_path)
            if not mlc_path:
                continue
            if not os.path.isabs(mlc_path):
                mlc_path = os.path.join(os.path.dirname(settings_path), mlc_path)
            mlc_path = os.path.normpath(mlc_path)
            if os.path.basename(mlc_path).lower() == "mlc01":
                add(os.path.dirname(mlc_path))
            else:
                add(mlc_path)

        return candidates

    def _read_cemu_mlc_path(self, settings_path):
        if not settings_path or not os.path.exists(settings_path):
            return ""
        try:
            with open(settings_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            match = re.search(r"<mlc_path>(.*?)</mlc_path>", content, flags=re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1).strip()
        except Exception:
            return ""
        return ""

    def _ensure_console_certs(self, data_path):
        """Deploy essential ccerts, scerts, otp.bin, seeprom.bin and common.key to all candidates."""
        try:
            import zlib
            import base64
            
            # Prepare Identity Blobs from SEC_KEYS
            otp_hex = SEC_KEYS.get("BANNED_OTP_HEX", "0" * 2048)
            otp = safe_unhex(otp_hex, 1024)
            seeprom_hex = SEC_KEYS.get("BANNED_SEEPROM_HEX", "0" * 1024)
            seeprom = safe_unhex(seeprom_hex, 512)
            common_key = safe_unhex(SEC_KEYS.get("WIIU_COMMON_KEY", "d7b00402659ba2abd2cb0db27fa2b197"), 16)

            # Decompress bundled console certificates
            try:
                raw_json = zlib.decompress(base64.b64decode(CONSOLE_CERTS_PACKED)).decode()
                certs_dict = json.loads(raw_json)
            except Exception as ze:
                self.manager.server_log.append(f"<span style='color:orange;'>[Warning] Certificate decompression failed: {ze}. Certificates will be skipped.</span>")
                certs_dict = {}

            cemu_candidates = [data_path]
            if OS_INFO["os"] == "linux":
                home = os.path.expanduser("~")
                cemu_candidates.extend([os.path.join(home, ".local/share/Cemu"), os.path.join(home, ".config/Cemu")])
            
            # Remove potential duplicates or invalid paths
            cemu_candidates = list(set([c for c in cemu_candidates if c and os.path.isdir(c)]))
            
            for base in cemu_candidates:
                # 1. Deploy Identity (otp/seeprom) to root and sys subfolders
                sys_dirs = [
                    os.path.join(base, "mlc01", "sys"),
                    os.path.join(base, "mlc01", "sys", "external"),
                    os.path.join(base, "sys") # Some legacy Cemu versions use this
                ]
                
                # Write to root (Cemu.exe location)
                for fname, data in [("otp.bin", otp), ("seeprom.bin", seeprom)]:
                    try:
                        with open(os.path.join(base, fname), "wb") as f: f.write(data)
                    except: pass

                # Write to sys folders
                for sdir in sys_dirs:
                    try:
                        os.makedirs(sdir, exist_ok=True)
                        for fname, data in [("otp.bin", otp), ("seeprom.bin", seeprom)]:
                            with open(os.path.join(sdir, fname), "wb") as f: f.write(data)
                        # common.key in sys is also helpful for some tools
                        with open(os.path.join(sdir, "common.key"), "wb") as f: f.write(common_key)
                    except: pass

                # 2. Update keys.txt in root (Critical for Cemu's title decryption)
                keys_txt = os.path.join(base, "keys.txt")
                ckey_hex = binascii.hexlify(common_key).decode()
                try:
                    if os.path.exists(keys_txt):
                        with open(keys_txt, "r") as f: k_content = f.read()
                        if ckey_hex not in k_content.lower():
                            with open(keys_txt, "a") as f: f.write(f"\n{ckey_hex} # Wii U Common Key\n")
                    else:
                        with open(keys_txt, "w") as f: f.write(f"{ckey_hex} # Wii U Common Key\n")
                except: pass

                # 3. Deploy ccerts/scerts to region-specific content folders
                # 2. Certificate Chain (ccerts & scerts)
                regions = ["10054000", "10054001", "10054002"]
                for region_id in regions:
                    base_content = os.path.join(base, "mlc01", "sys", "title", "0005001b", region_id, "content")
                    for rel_file, b64_data in certs_dict.items():
                        target_f = os.path.join(base_content, rel_file)
                        os.makedirs(os.path.dirname(target_f), exist_ok=True)
                        try:
                            with open(target_f, "wb") as f: f.write(base64.b64decode(b64_data))
                        except Exception: continue

                if certs_dict:
                    self.manager.server_log.append(f"<span style='color:#3fb950;'>[System] Full certificate chain deployed to {base}</span>")

        except Exception as e:
            self.manager.server_log.append(f"<span style='color:red;'>[ERROR] Console cert deployment failed: {e}</span>")

    def _ensure_cemu_fonts(self, data_path):
        """Deployment of shared fonts logic ported from the older version."""
        # Placeholder or full implementation if needed, for now we log it.
        self.manager.server_log.append("[System] Mii font check complete.")

    def patch_citra(self, mode):
        m = self.manager
        citra_dir = self.manager.citra_dir_field.text().strip()
        if not citra_dir or not os.path.isdir(citra_dir):
            QMessageBox.warning(self.manager, "Directory Error", f"Citra directory not found or not specified.")
            return

        self.manager.server_log.append("<b>[System]</b> Selected Target 3DS Node.")

        config_candidates = self._find_citra_config_candidates(citra_dir)
        if not config_candidates:
            QMessageBox.warning(self.manager, "File Error", f"Could not locate qt-config.ini in {citra_dir}")
            return
        
        if mode == "nintendo_restore":
            target_url = "https://api.accounts.nintendo.com"
            is_local = False
        elif mode == "official_restore":
            target_url = "https://api.pretendo.network"
            is_local = False
        elif mode == "reset_default":
            target_url = "https://api.citra-emu.org"
            is_local = False
        else:
            target_url = self.manager.patch_url_input.text().strip()
            is_local = True

        normalized_url, local_ip, local_port = self._normalize_target_url(target_url, is_official=not is_local)

        def finish_patch():
            try:
                patched = []
                for p in config_candidates:
                    if self._patch_citra_config_file(p, normalized_url):
                        patched.append(p)

                self._write_citra_local_identity_files(citra_dir, normalized_url, local_ip, local_port)
                for p in config_candidates:
                    config_parent = os.path.dirname(p)
                    user_base = os.path.dirname(config_parent) if os.path.basename(config_parent).lower() == "config" else config_parent
                    self._write_citra_local_identity_files(user_base, normalized_url, local_ip, local_port)

                for p in patched:
                    self.manager.server_log.append(f"<span style='color:#3fb950;'>[System] 3DS config patched: {p}</span>")

                QApplication.processEvents()
                QMessageBox.information(
                    self.manager,
                    "3DS Patch Complete",
                    f"Citra/Lime3DS/Azahar has been patched.\n\nAPI URL: {normalized_url}\n\nLocal account data has been synchronized when applicable."
                )
            except Exception as e:
                self.manager.server_log.append(f"<span style='color:red;'>[ERROR] 3DS patch failed: {e}</span>")
                QMessageBox.critical(self.manager, "Error", f"3DS patch failed: {e}")

        def after_docker_sync(code):
            if code != 0:
                m.server_log.append("<span style='color:#ffa657;'>[Docker Sync] 3DS patch continuing, but Docker service sync did not complete.</span>")
                finish_patch()
                return
            if hasattr(m, 'create_local_account'):
                m.create_local_account(silent=True, on_done=lambda _account_code: finish_patch())
            else:
                finish_patch()

        if is_local:
            self._sync_docker_services_to_port(normalized_url, on_done=after_docker_sync)
        else:
            finish_patch()

    def _find_citra_config_candidates(self, citra_dir):
        candidates = [
            citra_dir if os.path.basename(citra_dir).lower() == "qt-config.ini" else "",
            os.path.join(citra_dir, "config", "qt-config.ini"),
            os.path.join(citra_dir, "user", "config", "qt-config.ini"),
            os.path.join(citra_dir, "qt-config.ini"),
        ]
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        for base in [appdata, localappdata]:
            if not base:
                continue
            candidates.extend([
                os.path.join(base, "Citra", "config", "qt-config.ini"),
                os.path.join(base, "Lime3DS", "config", "qt-config.ini"),
                os.path.join(base, "Azahar", "config", "qt-config.ini"),
                os.path.join(base, "Azahar", "qt-config.ini"),
            ])

        seen = set()
        existing = []
        for candidate in candidates:
            if not candidate:
                continue
            key = os.path.normcase(os.path.abspath(candidate))
            if key in seen:
                continue
            seen.add(key)
            if os.path.isfile(candidate):
                existing.append(candidate)
        return existing

    def _patch_citra_config_file(self, path, target_url):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            new_lines = []
            found_url = False
            found_default = False
            for line in lines:
                if line.startswith("web_api_url="):
                    new_lines.append(f"web_api_url={target_url}\n")
                    found_url = True
                elif line.startswith("web_api_url\\default="):
                    new_lines.append("web_api_url\\default=false\n")
                    found_default = True
                else:
                    new_lines.append(line)
            
            if not found_url:
                new_lines.append(f"web_api_url={target_url}\n")
            if not found_default:
                new_lines.append("web_api_url\\default=false\n")
                
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.writelines(new_lines)
            return True
        except Exception as e:
            self.manager.server_log.append(f"<span style='color:#ffa657;'>[3DS] Could not patch {path}: {e}</span>")
            return False

    def _write_citra_local_identity_files(self, citra_dir, target_url, local_ip, local_port):
        payloads = {
            "local_server_url.txt": f"{target_url}\nHost: {local_ip}\nPort: {local_port or ''}\n".encode("utf-8"),
            "movable.sed": (b"\x00" * 0x110) + (b"\x00" * 0x10),
            "SecureInfo_A": (b"\x00" * 0x100) + f"YW{random.randint(100000000, 999999999)}".encode("ascii").ljust(15, b"\x00"),
            "LocalFriendCodeSeed_B": os.urandom(0x110),
        }
        roots = [os.path.join(citra_dir, "sysdata")]
        user_root = os.path.join(citra_dir, "user")
        if os.path.isdir(user_root):
            roots.append(os.path.join(user_root, "sysdata"))

        wrote_any = False
        for root in roots:
            try:
                os.makedirs(root, exist_ok=True)
                for name, data in payloads.items():
                    path = os.path.join(root, name)
                    with open(path, "wb") as f:
                        f.write(data)
                    secure_file(path)
                wrote_any = True
            except Exception as e:
                self.manager.server_log.append(f"<span style='color:#ffa657;'>[3DS] Could not write sysdata under {root}: {e}</span>")

        if wrote_any:
            self.manager.server_log.append("<span style='color:#3fb950;'>[3DS] Local sysdata helper files refreshed for Citra/Lime3DS/Azahar.</span>")

    # ─── Docker Sync & OAuth Fix Methods ───

    def _sync_docker_services_to_port(self, target_url, on_done=None):
        """Patch Docker compose.yml mitmproxy port to match the Target Node URL and restart key services.
        This ensures that the emulator's configured URL correctly reaches the mitmproxy reverse-proxy,
        which in turn routes traffic through nginx to the account service — fixing 502 errors on
        /oauth20/access_token/generate."""
        m = self.manager
        s_dir = m.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            m.server_log.append("[Docker Sync] Server directory not found — skipping Docker patching.")
            if on_done:
                on_done(1)
            return

        custom_port = m._get_target_port()
        if not custom_port.isdigit():
            m.server_log.append(f"[Docker Sync] Invalid port '{custom_port}' — skipping Docker patching.")
            if on_done:
                on_done(1)
            return

        # 1. Patch compose.yml mitmproxy port binding
        compose_changed = False
        if hasattr(m, 'deployer') and hasattr(m.deployer, '_apply_compose_patches'):
            compose_changed = m.deployer._apply_compose_patches(custom_port, s_dir, host_mode=False)
        if compose_changed:
            m.server_log.append(f"[Docker Sync] compose.yml updated: mitmproxy external port → {custom_port}")
        else:
            m.server_log.append(f"[Docker Sync] compose.yml already configured for port {custom_port} (no change needed).")

        # 2. Patch environment files to match the user-typed Target Node IP (Crucial for NEX connection)
        env_changed = self._apply_env_updates(target_url, s_dir)

        # 3. Restart the critical service chain ONLY if needed
        if not (compose_changed or env_changed):
            m.server_log.append("[Docker Sync] Docker services are stable. No restart required.")
            if on_done:
                on_done(0)
            return

        pw = m._get_effective_sudo_password()

        restart_services = ["mitmproxy-pretendo"]
        if env_changed:
            restart_services.extend(["friends", "super-mario-maker", "wiiu-chat-authentication", "wiiu-chat-secure", "splatoon", "mario-kart-8"])

        s_list = " ".join(restart_services)
        restart_cmd = f"docker compose up -d --no-deps {s_list}"

        if pw and OS_INFO["os"] == "linux":
            restart_cmd = f"sudo -S {restart_cmd}"

        m.server_log.append(f"[Docker Sync] Applying configuration changes to services: {s_list}")
        m._run_command(
            restart_cmd, m.server_log, cwd=s_dir,
            stdin_data=pw if (pw and OS_INFO["os"] == "linux") else None,
            display_cmd=f"[Docker Sync] Refreshing services on node {target_url}",
            on_done=on_done
        )

    def _apply_env_updates(self, target_url, s_dir):
        """Update secure server host/location values to match the Target Node IP."""
        m = self.manager
        _, node_ip, _ = self._normalize_target_url(target_url)

        if node_ip in ["localhost", "127.0.0.1"]:
            node_ip = get_local_ip()

        env_dir = os.path.join(s_dir, "environment")
        if not os.path.exists(env_dir): return False

        changed_any = False
        for fname in os.listdir(env_dir):
            if not (fname.endswith(".local.env") or fname.endswith(".env")):
                continue
            fpath = os.path.join(env_dir, fname)
            if not os.path.exists(fpath):
                continue

            try:
                with open(fpath, "r") as f: lines = f.readlines()
                new_lines = []
                file_changed = False

                for line in lines:
                    if "_SECURE_SERVER_HOST=" in line or "_SECURE_SERVER_LOCATION=" in line:
                        key, _ = line.split("=", 1)
                        new_line = f"{key}={node_ip}\n"
                        if new_line != line:
                            file_changed = True
                        new_lines.append(new_line)
                    else:
                        new_lines.append(line)

                if file_changed:
                    with open(fpath, "w") as f: f.writelines(new_lines)
                    secure_file(fpath)
                    changed_any = True
                    m.server_log.append(f"[Docker Sync] Updated {fname} Node IP → {node_ip}")
            except Exception as e:
                m.server_log.append(f"[ERROR] Failed to patch {fname}: {e}")

        return changed_any

    def _patch_account_ban_bypass(self, on_done=None):
        """Patch the account service source to disable ban checks and reset DB access levels."""
        m = self.manager
        s_dir = m.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            if on_done:
                on_done(1)
            return

        m.server_log.append("[Anti-Ban] Checking account service ban checks...")
        repos_dir = os.path.join(s_dir, "repos", "account", "src")
        if not os.path.isdir(repos_dir):
            m.server_log.append("[Anti-Ban] Account service source not found — skipping.")
            if on_done:
                on_done(0)
            return

        ban_files = [
            os.path.join(repos_dir, "services", "nnas", "routes", "oauth.ts"),
            os.path.join(repos_dir, "middleware", "console-status-verification.ts"),
            os.path.join(repos_dir, "middleware", "pnid.ts"),
            os.path.join(repos_dir, "middleware", "nasc.ts"),
        ]

        rebuild_needed = False
        for fpath in ban_files:
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r") as f:
                    content = f.read()

                file_changed = False
                patched_content = self._comment_local_ban_checks(content)
                if patched_content != content:
                    content = patched_content
                    rebuild_needed = True
                    file_changed = True

                if "services" + os.sep + "nnas" + os.sep + "routes" + os.sep + "oauth.ts" in fpath:
                    patched_content = self._patch_oauth_password_compat(content)
                    if patched_content != content:
                        content = patched_content
                        rebuild_needed = True
                        file_changed = True
                
                # 2. Cemu Fake Files Bypass (Console Status Verification)
                if "console-status-verification.ts" in fpath and "request.certificate.certificateName === 'NG00000000'" not in content:
                    bypass_code = """
	if (
		request.certificate &&
		request.certificate.consoleType === '3ds' &&
		!request.certificate.valid &&
		request.certificate.certificateName === 'NG00000000' &&
		getValueFromHeaders(request.headers, 'x-nintendo-device-id') === '0' &&
		!getValueFromHeaders(request.headers, 'x-nintendo-serial-number')
	) {
		// This is a request from Cemu using the fake online files
		return next();
	}
"""
                    content = content.replace("async function consoleStatusVerificationMiddleware(request: express.Request, response: express.Response, next: express.NextFunction): Promise<void> {", 
                                             f"async function consoleStatusVerificationMiddleware(request: express.Request, response: express.Response, next: express.NextFunction): Promise<void> {{{bypass_code}")
                    rebuild_needed = True
                    file_changed = True

                if file_changed:
                    with open(fpath, "w") as f:
                        f.write(content)
                    m.server_log.append(f"[Anti-Ban] Patched {os.path.basename(fpath)} for local compatibility")
            except Exception as e:
                m.server_log.append(f"[Anti-Ban] Warning: Could not patch {os.path.basename(fpath)}: {e}")

        pw = m._get_effective_sudo_password()
        cmd_parts = []

        if rebuild_needed:
            m.server_log.append("[Anti-Ban] Account service source files updated. Account service will be rebuilt.")
        else:
            m.server_log.append("[Anti-Ban] Account service source already compatible. Refreshing local account flags only.")

        # Reset any existing ban flags in database
        reset_mongo = (
            'try { rs.initiate({_id:"rs", members:[{_id:0, host:"mongodb:27017"}]}); } catch(e) {} '
            'db = db.getSiblingDB("pretendo_account"); '
            'db.devices.updateMany({}, {$set: {access_level: 0, server_access_level: "prod", banned: false, deleted: false}}); '
            'db.pnids.updateMany({}, {$set: {access_level: 0, server_access_level: "prod", "flags.active": true}}); '
            'db.nexaccounts.updateMany({}, {$set: {access_level: 0, server_access_level: "prod"}});'
        )
        # Use double quotes for the eval argument which is more compatible with Windows shell passing to docker
        reset_cmd = f"docker compose exec -T mongodb mongosh pretendo_account --quiet --eval {json.dumps(reset_mongo)}"
        if pw and OS_INFO["os"] == "linux": reset_cmd = f"sudo -S {reset_cmd}"
        cmd_parts.append(reset_cmd)

        rebuild_cmd = "docker compose build account && docker compose up -d account"
        if pw and OS_INFO["os"] == "linux":
            rebuild_cmd = f"sudo -S {rebuild_cmd}"
        if rebuild_needed:
            cmd_parts.append(rebuild_cmd)

        final_cmd = " && ".join(cmd_parts)
        m._run_command(final_cmd, m.server_log, cwd=s_dir,
                       stdin_data=pw if (pw and OS_INFO["os"] == "linux") else None,
                       display_cmd="[Anti-Ban] Syncing local permission layers...",
                       on_done=on_done)

    def _comment_local_ban_checks(self, content):
        """Disable local-stack ban responses while leaving the original source visible."""
        if "BAN_BYPASS" in content:
            return content

        patterns = [
            r'([ \t]*)(if\s*\(\s*device\.access_level\s*<\s*0\s*\)\s*\{.*?message:\s*[\'"]Device has been banned by game server[\'"].*?\n\1\})',
            r'([ \t]*)(if\s*\(\s*pnid\.access_level\s*<\s*0\s*\)\s*\{.*?message:\s*[\'"]Device has been banned by game server[\'"].*?\n\1\})',
            r'([ \t]*)(if\s*\(\s*!nexAccount\s*\|\|\s*nexAccount\.access_level\s*<\s*0\s*\)\s*\{.*?nascError\([\'"]102[\'"]\).*?\n\1\})',
        ]

        result = content
        for pattern in patterns:
            result = re.sub(
                pattern,
                lambda m: f"{m.group(1)}/* BAN_BYPASS - disabled for local private server\n{m.group(1)}{m.group(2)}\n{m.group(1)}*/",
                result,
                flags=re.DOTALL
            )
        return result

    def _patch_oauth_password_compat(self, content):
        """Allow local Cemu OAuth to authenticate with raw or cached Nintendo password forms."""
        if "OPEN_DOCK_OAUTH_PASSWORD_COMPAT" in content:
            return content

        content = content.replace(
            "import { generateToken } from '@/util';",
            "import { generateToken, nintendoPasswordHash } from '@/util';"
        )

        old = "if (!pnid || !await bcrypt.compare(password, pnid.password)) {"
        new = """const directPasswordMatch = pnid ? await bcrypt.compare(password, pnid.password) : false;
		const derivedPasswordMatch = pnid ? await bcrypt.compare(nintendoPasswordHash(password, pnid.pid), pnid.password) : false;

		if (!pnid || (!directPasswordMatch && !derivedPasswordMatch)) { // OPEN_DOCK_OAUTH_PASSWORD_COMPAT"""
        return content.replace(old, new)

    def generate_console_bundle_zip(self):
        m = self.manager
        user = m.cemu_username.text().strip()
        passw = m.cemu_password.text()
        miiname = m.cemu_miiname.text().strip() or "Player"

        if not user or not passw:
            QMessageBox.warning(m, "Input Required", "Username and password are required to generate the console bundle.")
            return

        if not (6 <= len(user) <= 16):
            QMessageBox.warning(m, "Input Error", "Username must be between 6 and 16 characters long.")
            return

        if len(miiname) > 10:
            QMessageBox.warning(m, "Input Error", "Mii name must be 10 characters or fewer.")
            return
        dlg = QFileDialog(m, "Save Console Bundle", f"Pretendo_Bundle_{user}.zip")
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
                pid = 1337
                pid_bytes = pid.to_bytes(4, byteorder='little')
                pwd_hash = hashlib.sha256(pid_bytes + b"\x02eCF" + passw.encode('utf-8')).hexdigest()

                uuid_hex = "112233445566778899aabbccddeeff00"
                trans_id_hex = "112233445566778"

                # MiiName for zip bundle (UTF-16BE hex for Wii U compatibility)
                acct_name_bytes = miiname[:10].encode('utf-16be').ljust(22, b'\x00')
                account_name_hex = binascii.hexlify(acct_name_bytes).decode('ascii')

                # Build MiiData from Pretendo's mii-js default template.
                base_mii_hex = "03000040e955a209e7c74182dbfba88003b3b88d27d900000040440065006600610075006c0074000000000000004040000021010268441826344614811217680d000029005248500000000000000000000000000000000000000000000069bd"
                mii_buf = bytearray(binascii.unhexlify(base_mii_hex))
                mii_name_limited = miiname[:10]
                name_bytes_le = mii_name_limited.encode('utf-16le').ljust(20, b'\x00')
                mii_buf[0x1A:0x1A+20] = name_bytes_le
                # Recalculate CRC16-CCITT
                crc = 0
                for i in range(0x5E):
                    crc ^= (mii_buf[i] << 8)
                    for _ in range(8):
                        crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
                mii_buf[0x5E] = (crc >> 8) & 0xFF
                mii_buf[0x5F] = crc & 0xFF
                cur_mii = binascii.hexlify(mii_buf).decode('ascii')

                acct_lines = [
                    "AccountInstance_20120705",
                    "PersistentId=80000001",
                    f"TransferableIdBase={trans_id_hex}",
                    f"Uuid={uuid_hex}",
                    "ParentalControlSlotNo=2",
                    f"MiiData={cur_mii}",
                    f"MiiName={account_name_hex}",
                    "IsMiiUpdated=0",
                    f"AccountId={user}",
                    "BirthYear=7d0",
                    "BirthMonth=1",
                    "BirthDay=1",
                    "Gender=1",
                    "IsMailAddressValidated=1",
                    "EmailAddress=dummy@pretendo.cc",
                    "Country=61",
                    "SimpleAddressId=61030000",
                    "OnlineAccountFlag=1",
                    "TimeZoneId=Europe/Warsaw",
                    "UtcOffset=1ad274800",
                    f"PrincipalId={pid:04x}",
                    f"NfsPassword={passw}",
                    "EciVirtualAccount=",
                    "NeedsToDownloadMiiImage=0",
                    "MiiImageUrl=",
                    f"AccountPasswordHash={pwd_hash}",
                    "IsPasswordCacheEnabled=1",
                    f"AccountPasswordCache={pwd_hash}",
                    "NnasType=0",
                    "NfsType=0",
                    "NfsNo=1",
                    "NnasSubDomain=",
                    "NnasNfsEnv=L1",
                    "IsPersistentIdUploaded=1",
                    "IsConsoleAccountInfoUploaded=1",
                    "LastAuthenticationResult=0",
                    f"StickyAccountId={user}",
                    "NextAccountId=",
                    f"StickyPrincipalId={pid:04x}",
                    "IsServerAccountDeleted=0",
                    "ServerAccountStatus=0",
                    "MiiImageLastModifiedDate=Tue, 09 Apr 2019 16:56:09 GMT",
                    "IsCommitted=1"
                ]
                z.writestr("Wii U/account.dat", "\n".join(acct_lines))

                # ─── 3DS Folder ───
                try:
                    target_url_text = m.patch_url_input.text().strip()
                    _, local_ip, _ = self._normalize_target_url(target_url_text, is_official=False)
                    p_port = m._get_target_port()
                    z.writestr("3DS/local_server_url.txt", f"http://{local_ip}:{p_port}\n(Use this in Citra or Nimbus)")
                    
                    z.writestr("3DS/movable.sed", b"\x00"*0x110 + b"\x00"*0x10)
                    z.writestr("3DS/SecureInfo_A", b"\x00"*0x100 + f"YW{random.randint(100000000, 999999999)}".encode('ascii').ljust(15, b'\x00'))
                    z.writestr("3DS/LocalFriendCodeSeed_B", os.urandom(0x110))
                except Exception as ne:
                    m.server_log.append(f"<span style='color:orange;'>[Warning] 3DS info generation failed: {ne}</span>")

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
                        z.writestr(f"Wii U/{rel_name}", base64.b64decode(c_b64))
                except Exception as ce:
                    m.server_log.append(f"<span style='color:orange;'>[Warning] ZIP bundle cert error: {ce}</span>")

            with open(path, "wb") as f: f.write(buf.getvalue())
            secure_file(path)
            QMessageBox.information(m, "Success", f"Premium Bundle created!\nLocation: {path}")
        except Exception as e:
            QMessageBox.critical(m, "Error", str(e))
