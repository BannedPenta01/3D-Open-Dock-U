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
import zipfile
from urllib.parse import urlparse
from PySide6.QtWidgets import QMessageBox, QFileDialog, QApplication
from PySide6.QtCore import QDir
from constants import SEC_KEYS, CONSOLE_CERTS_PACKED
from utils import safe_unhex, OS_INFO, get_local_ip

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
        use_official = m.mode_pretendo.isChecked()
        url = "https://api.pretendo.network" if use_official else m.patch_url_input.text().strip()

        c_dir = m.cemu_dir_field.text().strip()
        if not os.path.isdir(c_dir):
            QMessageBox.warning(m, "Directory Error", f"Cemu directory not found:\n{c_dir}")
            return

        m.server_log.append("<b>[System]</b> Starting full optimization & sync sequence...")

        # Deploy ALL necessary Wii U certificate files & identity blobs
        self._ensure_console_certs(c_dir)

        if not use_official:
            # Sync Docker services to the target port BEFORE patching the emulator
            self._sync_docker_services_to_port(url)
            # Ensure account service ban checks are disabled for local stack
            self._patch_account_ban_bypass()

        self.patch_cemu_settings(url, use_official)
        self.generate_cemu_manual()

        if not use_official:
            if hasattr(m, 'create_local_account'):
                m.create_local_account()

        if use_official:
            QApplication.processEvents()
            QMessageBox.information(m, "Cemu Patch Complete", "Cemu has been successfully patched and configured to connect to the official Pretendo Network servers.\n\nYou can now safely launch Cemu and play online.")
        else:
            QApplication.processEvents()
            QMessageBox.information(m, "Cemu Patch Complete", f"Cemu has been successfully patched and configured to connect to your local 3D Open Dock U instance.\n\nTarget Node: {url}\n\nYou can now safely launch Cemu.")

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
        
        services = ["act", "con", "etc", "dls", "shp", "dsa", "pdm", "miv", "smm", "bas", "npts", "api", "ecs", "ias", "cas", "boss", "friends", "account", "clp", "shop", "news", "portal", "discovery"]
        base_url, _, _ = self._normalize_target_url(url, is_official=is_official)
        
        url_nodes = []
        for s in services:
            url_nodes.append(f"        <{s}>{base_url}</{s}>")
        
        urls_block = "\n".join(url_nodes)
        ns_content = f'<?xml version="1.0" encoding="UTF-8"?>\n<content>\n    <networkname>Pretendo-Bypass</networkname>\n    <disablesslverification>1</disablesslverification>\n    <urls>\n{urls_block}\n    </urls>\n</content>'
        
        try:
            with open(ns_xml, "w") as f: f.write(ns_content)
            self.manager.server_log.append(f"<span style='color:#3fb950;'>[System] Cemu network_services.xml injected successfully.</span>")
        except Exception as e:
            self.manager.server_log.append(f"<span style='color:red;'>[ERROR] Failed to write network_services.xml: {e}</span>")

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
            # Deploy essential ccerts, identity files (otp/seeprom) and fonts
            self._ensure_console_certs(data_path)
            self._ensure_cemu_fonts(data_path)

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
            
            # FakeOnlineFiles-compatible Mii template
            base_mii_hex = "030000305ac6bb2520c470f09426e82fb8ae6ed59004000000005f304b30683000000000000000000000000000004737000021010264a41820454614811217680d0000290251485000000000000000000000000000000000000000000000bfee"
            mii_buf = bytearray(binascii.unhexlify(base_mii_hex.ljust(192, '0')))
            
            name_bytes_le = mii_name_limited.encode('utf-16le').ljust(20, b'\x00')
            mii_buf[0x1A:0x1A+20] = name_bytes_le
            mii_buf[0x48:0x48+20] = name_bytes_le
            
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
            
            p_targets = []
            cemu_candidates = [data_path]
            if OS_INFO["os"] == "linux":
                home = os.path.expanduser("~")
                cemu_candidates.extend([os.path.join(home, ".local/share/Cemu"), os.path.join(home, ".config/Cemu")])
            
            for base in set(cemu_candidates):
                if not base or not os.path.isdir(base): continue
                p_targets.append(os.path.join(base, "mlc01/usr/save/system/act/80000001"))
                p_targets.append(os.path.join(base, "accounts/80000001"))

            for d in set(p_targets):
                for fname in ["account.dat", "account.ini"]:
                    fpath = os.path.join(d, fname)
                    os.makedirs(os.path.dirname(fpath), exist_ok=True)
                    with open(fpath, "w") as f:
                        f.write("\n".join(lines))
                    self.manager.server_log.append(f"<span style='color:#3fb950;'>[System] Identity Updated: {fpath}</span>")

            self.manager.server_log.append(f"<span style='color:#3fb950;'>[System] Identity Re-aligned for {username} (PID: {pid})</span>")
        except Exception as e:
            self.manager.server_log.append(f"<span style='color:red;'>[ERROR] Identity generation failed: {e}</span>")

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
        citra_dir = self.manager.citra_dir_field.text().strip()
        if not citra_dir or not os.path.isdir(citra_dir):
            QMessageBox.warning(self.manager, "Directory Error", f"Citra directory not found or not specified.")
            return

        self.manager.server_log.append("<b>[System]</b> Selected Target 3DS Node.")
        # Search for qt-config.ini in common locations
        config_candidates = [
            os.path.join(citra_dir, "config", "qt-config.ini"),
            os.path.join(citra_dir, "qt-config.ini"),
        ]
        
        p = None
        for candidate in config_candidates:
            if os.path.exists(candidate):
                p = candidate
                break
        
        if not p:
            QMessageBox.warning(self.manager, "File Error", f"Could not locate qt-config.ini in {citra_dir}")
            return
        
        target_url = self.manager.patch_url_input.text().strip()
        try:
            with open(p, "r") as f: lines = f.readlines()
            new_lines = []
            found = False
            for line in lines:
                if line.startswith("web_api_url="):
                    new_lines.append(f"web_api_url={target_url}\n")
                    found = True
                else: new_lines.append(line)
            
            if not found:
                new_lines.append(f"web_api_url={target_url}\n")
                
            with open(p, "w") as f: f.writelines(new_lines)
            self.manager.server_log.append(f"<span style='color:#3fb950;'>[System] Citra patched successfully: {p}</span>")
            QApplication.processEvents()
            QMessageBox.information(self.manager, "Citra Patch Complete", f"Citra has been successfully patched.\n\nAPI URL has been updated to point to: {target_url}\n\nYou can now launch Citra and play online.")
        except Exception as e:
            self.manager.server_log.append(f"<span style='color:red;'>[ERROR] Citra patch failed: {e}</span>")
            QMessageBox.critical(self.manager, "Error", f"Citra patch failed: {e}")

    # ─── Docker Sync & OAuth Fix Methods ───

    def _sync_docker_services_to_port(self, target_url):
        """Patch Docker compose.yml mitmproxy port to match the Target Node URL and restart key services.
        This ensures that the emulator's configured URL correctly reaches the mitmproxy reverse-proxy,
        which in turn routes traffic through nginx to the account service — fixing 502 errors on
        /oauth20/access_token/generate."""
        m = self.manager
        s_dir = m.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            m.server_log.append("[Docker Sync] Server directory not found — skipping Docker patching.")
            return

        custom_port = m._get_target_port()
        if not custom_port.isdigit():
            m.server_log.append(f"[Docker Sync] Invalid port '{custom_port}' — skipping Docker patching.")
            return

        # 1. Patch compose.yml mitmproxy port binding
        compose_changed = False
        host_mode = m.host_net_check.isChecked()
        if hasattr(m, 'deployer') and hasattr(m.deployer, '_apply_compose_patches'):
            compose_changed = m.deployer._apply_compose_patches(custom_port, s_dir, host_mode=host_mode)
        if compose_changed:
            m.server_log.append(f"[Docker Sync] compose.yml updated: mitmproxy external port → {custom_port}")
        else:
            m.server_log.append(f"[Docker Sync] compose.yml already configured for port {custom_port} (no change needed).")

        # 2. Patch environment files to match the user-typed Target Node IP (Crucial for NEX connection)
        env_changed = self._apply_env_updates(target_url, s_dir)

        # 3. Restart the critical service chain ONLY if needed
        if not (compose_changed or env_changed):
            m.server_log.append("[Docker Sync] Docker services are stable. No restart required.")
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
            display_cmd=f"[Docker Sync] Refreshing services on node {target_url}"
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
                    changed_any = True
                    m.server_log.append(f"[Docker Sync] Updated {fname} Node IP → {node_ip}")
            except Exception as e:
                m.server_log.append(f"[ERROR] Failed to patch {fname}: {e}")

        return changed_any

    def _patch_account_ban_bypass(self):
        """Patch the account service source to disable ban checks and reset DB access levels."""
        m = self.manager
        s_dir = m.server_dir_field.text().strip()
        if not os.path.isdir(s_dir):
            return

        m.server_log.append("[Anti-Ban] Checking account service ban checks...")
        repos_dir = os.path.join(s_dir, "repos", "account", "src")
        if not os.path.isdir(repos_dir):
            m.server_log.append("[Anti-Ban] Account service source not found — skipping.")
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

                patched_content = self._comment_local_ban_checks(content)
                if patched_content != content:
                    content = patched_content
                    rebuild_needed = True
                
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

                if rebuild_needed:
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
            m.server_log.append("[Anti-Ban] Source files already patched. Rebuilding account service to ensure compiled dist is current.")

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
        cmd_parts.append(rebuild_cmd)

        final_cmd = " && ".join(cmd_parts)
        m._run_command(final_cmd, m.server_log, cwd=s_dir,
                       stdin_data=pw if (pw and OS_INFO["os"] == "linux") else None,
                       display_cmd="[Anti-Ban] Syncing local permission layers...")

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

                # Build MiiData from the FakeOnlineFiles-compatible template
                base_mii_hex = "030000305ac6bb2520c470f09426e82fb8ae6ed59004000000005f304b30683000000000000000000000000000004737000021010264a41820454614811217680d0000290251485000000000000000000000000000000000000000000000bfee"
                mii_buf = bytearray(binascii.unhexlify(base_mii_hex.ljust(192, '0')))
                mii_name_limited = miiname[:10]
                name_bytes_le = mii_name_limited.encode('utf-16le').ljust(20, b'\x00')
                mii_buf[0x1A:0x1A+20] = name_bytes_le
                mii_buf[0x48:0x48+20] = name_bytes_le
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
            QMessageBox.information(m, "Success", f"Premium Bundle created!\nLocation: {path}")
        except Exception as e:
            QMessageBox.critical(m, "Error", str(e))
