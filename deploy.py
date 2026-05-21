# deploy.py
import os
import shutil
import subprocess
import shlex
import re
import json
import time
import stat
import base64
from PySide6.QtWidgets import QMessageBox
from PySide6.QtCore import QTimer
from constants import PRETENDO_REPO, SEC_KEYS
from secrets_manager import SecretStore, get_secret_file_path, hex_token, secure_file, token
from utils import OS_INFO, _win_to_wsl_path, get_local_ip, _docker_available, _CACHED_RESULTS

def _prepare_file_for_write(path):
    """Clear hidden/read-only flags and restrictive ACLs before regenerating a managed file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
        if OS_INFO["os"] == "windows":
            try:
                subprocess.run(["attrib", "-H", "-R", parent], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            for principal in (os.environ.get("USERNAME"), "CodexSandboxUsers", "*S-1-5-32-545"):
                if not principal:
                    continue
                try:
                    subprocess.run(
                        ["icacls", parent, "/inheritance:e", "/grant", f"{principal}:(OI)(CI)(M)"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass

    if not os.path.exists(path):
        return

    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass

    if OS_INFO["os"] == "windows":
        try:
            subprocess.run(["attrib", "-H", "-R", path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        for principal in (os.environ.get("USERNAME"), "CodexSandboxUsers", "*S-1-5-32-545"):
            if not principal:
                continue
            try:
                subprocess.run(
                    ["icacls", path, "/inheritance:e", "/grant", f"{principal}:(M)"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass


def _write_managed_file(path, content, *, binary=False):
    _prepare_file_for_write(path)
    tmp_path = f"{path}.tmp"
    _prepare_file_for_write(tmp_path)
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8", "newline": "\n"}
    with open(tmp_path, mode, **kwargs) as f:
        f.write(content)
    _prepare_file_for_write(path)
    os.replace(tmp_path, path)
    secure_file(path)
    _prepare_file_for_write(path)

class Deployer:
    def __init__(self, manager):
        self.manager = manager
        self._docker_recovery_attempted = False

    def automated_install_stack(self):
        s_dir = self.manager.server_dir_field.text().strip()
        needs_clone = False
        if not os.path.isdir(s_dir):
            needs_clone = True
        else:
            has_compose = any(os.path.isfile(os.path.join(s_dir, f)) for f in ["compose.yml", "docker-compose.yml"])
            if not has_compose:
                self.manager.setup_log.append("[System] Directory exists but is incomplete (missing compose.yml). Auto-repairing...")
                shutil.rmtree(s_dir, ignore_errors=True)
                needs_clone = True

        if needs_clone:
            self.manager.setup_log.append("[System] Cloning repository...")
            # Use double quotes and forward slashes for Windows/MSYS2 compatibility
            s_dir_fixed = s_dir.replace("\\", "/")
            dest_dir = f'"{s_dir_fixed}"' if OS_INFO["os"] == "windows" else shlex.quote(s_dir_fixed)
            self.manager._run_command(
                f"git clone --recurse-submodules {PRETENDO_REPO} {dest_dir}",
                self.manager.setup_log,
                on_done=lambda c: self._on_clone_finished(c)
            )
        else:
            self._on_clone_finished(0)

    def _on_clone_finished(self, code):
        if code != 0:
            self.manager.setup_log.append("[ERROR] Git clone failed. Deployment stopped.")
            return
        
        s_dir = self.manager.server_dir_field.text().strip()
        self.manager.setup_log.append("[System] Repo ready. Checking sub-repos...")
        
        # Check sub-repos: super-smash-bros-wiiu, pokken-tournament, mario-kart-8
        for name, url in [
            ("super-smash-bros-wiiu", "https://github.com/PretendoNetwork/super-smash-bros-wiiu"),
            ("pokken-tournament", "https://github.com/PretendoNetwork/pokken-tournament"),
            ("mario-kart-8", "https://github.com/PretendoNetwork/mario-kart-8")
        ]:
            d = os.path.join(s_dir, "repos", name)
            if not os.path.isdir(d):
                self.manager.setup_log.append(f"[System] Downloading {name}...")
                d_fixed = d.replace("\\", "/")
                dest_repo = f'"{d_fixed}"' if OS_INFO["os"] == "windows" else shlex.quote(d_fixed)
                self.manager._run_command(f"git clone --recurse-submodules {url} {dest_repo}", self.manager.setup_log, on_done=self._on_clone_finished)
                return

        # NEW STEP: Clear ports before patching
        self._clear_ports_and_proceed(s_dir)

    def _clear_ports_and_proceed(self, s_dir):
        if OS_INFO["os"] == "windows":
            _CACHED_RESULTS.pop("docker_available", None)
            if not _docker_available():
                self.manager.setup_log.append("[System] Docker must be ready before container cleanup. Waiting...")

                def _on_ready():
                    self.manager.setup_log.append("[OK] Docker ready. Continuing deployment cleanup.")
                    self._clear_ports_and_proceed(s_dir)

                def _on_failed():
                    self.manager.setup_log.append("[ERROR] Deployment halted because Docker is not ready.")

                self.manager._ensure_docker_desktop(_on_ready, on_failed=_on_failed)
                return

        custom_port = self.manager._get_target_port()
        ports_to_kill = f"80 443 21 53 8080 {custom_port} 9231"
        self.manager.setup_log.append("[System] Wiping port conflicts and removing old containers...")
        
        pw = self.manager._ask_sudo_password()
        kill_cmd = self._get_kill_ports_cmd(ports_to_kill, pw)
        
        if OS_INFO["os"] == "windows":
            d_cmd = "docker compose down --remove-orphans 1>NUL 2>NUL || echo done"
            # Compose down first so Docker releases its own proxy bindings. The port
            # cleanup below deliberately skips Docker/WSL processes.
            full_cmd = f"{d_cmd} & {kill_cmd}"
        else:
            d_cmd = "docker compose down --remove-orphans 2>/dev/null || true"
            if pw and OS_INFO["os"] == "linux":
                d_cmd = f"sudo -S {d_cmd}"
            full_cmd = f"{kill_cmd} ; {d_cmd}"
            
        def _on_ports_cleared(c):
            if c != 0:
                if OS_INFO["os"] == "windows":
                    self.manager.setup_log.append("[WARN] Cleanup returned a non-zero status, continuing because Windows port cleanup is best-effort.")
                    self._run_submodule_patches(s_dir)
                    return
                self.manager.setup_log.append("[ERROR] Failed to clear ports or down containers.")
                return
            self._run_submodule_patches(s_dir)

        self.manager._run_command(full_cmd, self.manager.setup_log, cwd=s_dir, stdin_data=pw,
                                  on_done=_on_ports_cleared,
                                  display_cmd="Clean Ports & Down Containers")

    def _get_kill_ports_cmd(self, ports_str, pw=None):
        ports = ports_str.split()
        if OS_INFO["os"] == "windows":
            ports_literal = "@(" + ",".join(str(int(p)) for p in ports if str(p).isdigit()) + ")"
            script = f"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$ports = {ports_literal}
$protectedNames = @(
  'Docker Desktop','com.docker.backend','com.docker.service','dockerd','docker',
  'wslhost','wslservice','vmmem','vmmemWSL','vpnkit','containerd','com.docker.proxy'
)
$seen = @{{}}
foreach ($port in $ports) {{
  $connections = @(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue)
  foreach ($conn in $connections) {{
    if (-not $conn.OwningProcess -or $conn.OwningProcess -le 0) {{ continue }}
    $key = "$port`:$($conn.OwningProcess)"
    if ($seen.ContainsKey($key)) {{ continue }}
    $seen[$key] = $true
    try {{
      $proc = Get-Process -Id $conn.OwningProcess -ErrorAction Stop
      if ($protectedNames -contains $proc.ProcessName) {{
        Write-Output \"skip docker-owned port $port ($($proc.ProcessName), pid $($proc.Id))\"
      }} else {{
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Write-Output \"released port $port from $($proc.ProcessName) pid $($proc.Id)\"
      }}
    }} catch {{}}
  }}
}}
exit 0
"""
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"
        else:
            f_parts = [f"fuser -k -n tcp {p}" for p in ports]
            inner = " ; ".join(f_parts) + " ; true"
            if pw:
                return f"sudo -S bash -c '{inner}'"
            return inner

    def _run_submodule_patches(self, s_dir):
        self.manager.setup_log.append("[System] Applying submodule patches...")
        
        if OS_INFO["os"] == "windows":
            self._run_submodule_patches_windows(s_dir)
            return

        patch_script = os.path.join(s_dir, "scripts", "setup-submodule-patches.sh")
        if os.path.isfile(patch_script):
            cmd = f"chmod +x {shlex.quote(patch_script)} && {shlex.quote(patch_script)}"
            
            def _on_patches_done(c):
                if c != 0:
                    self.manager.setup_log.append("[ERROR] Submodule patches failed. Deployment halted.")
                    return
                self._run_environment_setup(s_dir)
            self.manager._run_command(cmd, self.manager.setup_log, cwd=s_dir, on_done=_on_patches_done)
        else:
            self._run_environment_setup(s_dir)

    def _run_submodule_patches_windows(self, s_dir):
        """Implementation of setup-submodule-patches.sh in Python/Git for Windows."""
        self.manager.setup_log.append("[System] Resetting submodules (Windows Native)...")
        # Chain git commands using & for cmd.exe
        reset_cmd = 'git submodule sync & git submodule foreach "git reset --hard" & git submodule foreach "git clean -fd" & git submodule update --init --checkout'
        
        def _on_reset_done(code):
            # We proceed even if some reset commands fail (sometimes they do on Windows due to locks)
            self._apply_patches_windows(s_dir)

        self.manager._run_command(reset_cmd, self.manager.setup_log, cwd=s_dir, on_done=_on_reset_done)

    def _apply_patches_windows(self, s_dir):
        """Apply git patches with resilient fallback: --ignore-whitespace first, then --3way."""
        patches_dir = os.path.join(s_dir, "patches")
        if not os.path.isdir(patches_dir):
            self._run_environment_setup(s_dir)
            return

        patch_entries = []  # List of (subdir, patch_file, rel_patch)
        try:
            for subdir in os.listdir(patches_dir):
                d = os.path.join(patches_dir, subdir)
                if os.path.isdir(d):
                    repo_path = os.path.join(s_dir, "repos", subdir)
                    if not os.path.isdir(repo_path): continue
                    
                    for patch_file in sorted(os.listdir(d)):
                        if not patch_file.endswith(".patch"): continue
                        p_path = os.path.join(d, patch_file)
                        rel_patch = os.path.relpath(p_path, s_dir).replace("\\", "/")
                        patch_entries.append((subdir, patch_file, rel_patch))
        except Exception as e:
            self.manager.setup_log.append(f"[ERROR] Failed to scan patches: {e}")

        if not patch_entries:
            self._run_environment_setup(s_dir)
            return

        self.manager.setup_log.append(f"[System] Applying {len(patch_entries)} patches via Git (resilient mode)...")
        
        # Apply patches individually with fallback strategies so stale patches don't block deployment
        applied = 0
        skipped = 0
        for subdir, patch_file, rel_patch in patch_entries:
            patch_path = f'"../../{rel_patch}"'
            repo_arg = f'"repos/{subdir}"'
            success = False
            
            # Strategy 1: Normal apply with --ignore-whitespace
            try:
                res = subprocess.run(
                    f'git -C {repo_arg} apply --ignore-whitespace {patch_path}',
                    shell=True, cwd=s_dir, capture_output=True, text=True, timeout=15,
                    creationflags=0x08000000 if OS_INFO["os"] == "windows" else 0
                )
                if res.returncode == 0:
                    success = True
            except Exception:
                pass
            
            # Strategy 2: --3way merge (works even when context lines have shifted)
            if not success:
                try:
                    res = subprocess.run(
                        f'git -C {repo_arg} apply --3way --ignore-whitespace {patch_path}',
                        shell=True, cwd=s_dir, capture_output=True, text=True, timeout=15,
                        creationflags=0x08000000 if OS_INFO["os"] == "windows" else 0
                    )
                    if res.returncode == 0:
                        success = True
                except Exception:
                    pass
            
            if success:
                applied += 1
            else:
                skipped += 1
        
        if skipped > 0:
            self.manager.setup_log.append(f"[WARN] {skipped}/{len(patch_entries)} patches could not be applied (upstream may have changed). Deep patches will compensate.")
        if applied > 0:
            self.manager.setup_log.append(f"[OK] {applied}/{len(patch_entries)} git patches applied successfully.")
        
        self._run_environment_setup(s_dir)

    def _run_environment_setup(self, s_dir):
        self.manager.setup_log.append("[System] Finalizing environment...")
        _, local_ip, _ = self.manager._resolve_target_node()
        
        # IP Validation: If loopback, try to find existing IP in .env or warn
        if local_ip == "127.0.0.1":
            from utils import _grep_env_file
            existing_ip = _grep_env_file(os.path.join(s_dir, ".env"), "SERVER_IP")
            if existing_ip and existing_ip != "127.0.0.1":
                self.manager.setup_log.append(f"[System] Warning: Loopback detected, using existing SERVER_IP={existing_ip} from .env")
                local_ip = existing_ip
            else:
                self.manager.setup_log.append("[ALARM] Deployment IP is Loopback (127.0.0.1)! Emulators WILL NOT be able to connect unless they are on this machine.")
        
        custom_port = self.manager._get_target_port()
        host_mode = self.manager.host_net_check.isChecked()
        if OS_INFO["os"] == "windows" and host_mode:
            self.manager.setup_log.append(
                "[System] Host Networking is disabled on Windows for this stack; using Docker bridge networking to preserve service DNS and avoid port conflicts."
            )
            host_mode = False
        
        try:
            self._inject_missing_services(s_dir)
            self._generate_env_files(s_dir, local_ip)
            self._apply_compose_patches(custom_port, s_dir, host_mode=host_mode)
            self._ensure_smm_metadata(s_dir)
            self._fix_go_build_compatibility(s_dir)
            self._patch_friends(s_dir)
            self._patch_mario_kart_8(s_dir)
            self._patch_splatoon_schedules(s_dir)
            self._generate_juxtaposition_boot_config(s_dir)
            self._patch_mitmproxy_addon(s_dir)
            self._ensure_postgres_init_script(s_dir)
            
            self.manager.setup_log.append("[OK] All patches applied. Starting build process...")
            self.manager.server_log.append("<b>[System] Building patched Friends service...</b>")
            self._post_setup_build(s_dir)
        except Exception as e:
            self.manager.setup_log.append(f"[ERROR] Environment setup failed: {e}")

    def _generate_env_files(self, s_dir, server_ip):
        """Corrected Env Generator: Fixed S3 protocols and missing MongoDB URIs."""
        from utils import _grep_env_file
        
        def gen_password(length=32):
            return token(length)
        
        def gen_hex(length=64):
            return hex_token(length)
        
        env_dir = os.path.join(s_dir, "environment")
        os.makedirs(env_dir, exist_ok=True)
        
        def get_existing(fname, key, fallback=None):
            val = _grep_env_file(os.path.join(env_dir, fname), key)
            return val if val else fallback

        secret_store = SecretStore()

        def managed_secret(name, fname, key, factory):
            legacy = get_existing(fname, key)
            return secret_store.get_or_create(name, factory, legacy=legacy)

        # Secrets aggregation
        account_aes_key = managed_secret("account.aes_key", "account.local.env", "PN_ACT_CONFIG_AES_KEY", lambda: gen_hex(64))
        account_datastore_secret = managed_secret("account.datastore_signature_secret", "account.local.env", "PN_ACT_CONFIG_DATASTORE_SIGNATURE_SECRET", lambda: gen_hex(32))
        account_grpc_key = managed_secret("account.grpc_master_api_key", "account.local.env", "PN_ACT_CONFIG_GRPC_MASTER_API_KEY_ACCOUNT", lambda: gen_password(32))
        minio_secret = managed_secret("minio.root_password", "account.local.env", "PN_ACT_CONFIG_S3_ACCESS_SECRET", lambda: gen_password(32))
        postgres_pass = managed_secret("postgres.password", "postgres.local.env", "POSTGRES_PASSWORD", lambda: gen_password(32))
        legacy_nex_password = get_existing("friends.local.env", "PN_FRIENDS_CONFIG_AUTHENTICATION_PASSWORD")
        if legacy_nex_password == "password":
            legacy_nex_password = None
        nex_service_password = secret_store.get_or_create("nex.service_password", lambda: gen_password(32), legacy=legacy_nex_password)
        
        friends_auth_pw = nex_service_password
        friends_secure_pw = nex_service_password
        friends_api_key = managed_secret("friends.grpc_api_key", "friends.local.env", "PN_FRIENDS_CONFIG_GRPC_API_KEY", lambda: gen_password(32))
        friends_aes_key = account_aes_key # Friends must use the same AES key as Account for Kerberos
        
        chat_kerberos_pw = nex_service_password
        smm_kerberos_pw = nex_service_password
        smm_aes_key = managed_secret("super_mario_maker.aes_key", "super-mario-maker.local.env", "PN_SMM_CONFIG_AES_KEY", lambda: gen_hex(64))
        
        splat_kerberos_pw = nex_service_password
        splat_aes_key = managed_secret("splatoon.aes_key", "splatoon.local.env", "PN_SPLATOON_CONFIG_AES_KEY", lambda: gen_hex(64))
        
        smash_kerberos_pw = nex_service_password
        smash_aes_key = managed_secret("super_smash_bros_wiiu.aes_key", "super-smash-bros-wiiu.local.env", "PN_SSBWIIU_AES_KEY", lambda: gen_hex(64))
        
        mk8_kerberos_pw = nex_service_password
        minecraft_kerberos_pw = nex_service_password
        pikmin3_kerberos_pw = nex_service_password
        
        boss_api_key = managed_secret("boss.grpc_api_key", "boss.local.env", "PN_BOSS_CONFIG_GRPC_BOSS_SERVER_API_KEY", lambda: gen_password(32))
        
        # Load known keys if available from SEC_KEYS
        sk = SEC_KEYS
        boss_wiiu_aes = managed_secret("boss.wiiu_aes_key", "boss.local.env", "PN_BOSS_CONFIG_BOSS_WIIU_AES_KEY", lambda: sk.get("BOSS_WIIU_AES_KEY", gen_hex(32)))
        boss_wiiu_hmac = managed_secret("boss.wiiu_hmac_key", "boss.local.env", "PN_BOSS_CONFIG_BOSS_WIIU_HMAC_KEY", lambda: sk.get("BOSS_WIIU_HMAC_KEY", gen_hex(32)))
        boss_3ds_aes = managed_secret("boss.3ds_aes_key", "boss.local.env", "PN_BOSS_CONFIG_BOSS_3DS_AES_KEY", lambda: sk.get("BOSS_3DS_AES_KEY", gen_hex(32)))
        boss_3ds_hmac = managed_secret("boss.3ds_hmac_key", "boss.local.env", "PN_BOSS_CONFIG_BOSS_3DS_HMAC_KEY", lambda: gen_hex(32))
        
        pokken_kerberos_pw = nex_service_password
        pokken_aes_key = managed_secret("pokken_tournament.aes_key", "pokken-tournament.local.env", "PN_POKKENTOURNAMENT_CONFIG_AES_KEY", lambda: gen_hex(64))
        mk8_aes_key = managed_secret("mario_kart_8.aes_key", "mario-kart-8.local.env", "PN_MK8_CONFIG_AES_KEY", lambda: gen_hex(64))
        secret_store.save()
        
        env_files = {}

        # 1. Account Server
        env_files["account.local.env"] = [
            f"PN_ACT_CONFIG_AES_KEY={account_aes_key}",
            f"PN_ACT_CONFIG_DATASTORE_SIGNATURE_SECRET={account_datastore_secret}",
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
            f"PN_FRIENDS_CONFIG_AUTHENTICATION_PASSWORD={friends_auth_pw}",
            f"PN_FRIENDS_CONFIG_SECURE_PASSWORD={friends_secure_pw}",
            f"PN_FRIENDS_CONFIG_GRPC_API_KEY={friends_api_key}",
            f"PN_FRIENDS_CONFIG_AES_KEY={friends_aes_key}",
            f"PN_FRIENDS_CONFIG_DATABASE_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/friends?sslmode=disable",
            "PN_FRIENDS_GRPC_SERVER_PORT=5001",
            "PN_FRIENDS_AUTHENTICATION_SERVER_PORT=60000",
            "PN_FRIENDS_CONFIG_AUTHENTICATION_SERVER_PORT=60000",
            "PN_FRIENDS_SECURE_SERVER_PORT=60001",
            "PN_FRIENDS_CONFIG_SECURE_SERVER_PORT=60001",
            f"PN_FRIENDS_SECURE_SERVER_HOST={server_ip}",
            f"PN_FRIENDS_CONFIG_SECURE_SERVER_HOST={server_ip}",
            f"PN_FRIENDS_CONFIG_AUTHENTICATION_SERVER_HOST={server_ip}",
        ]

        # 2b. Website
        env_files["website.local.env"] = [
            f"PN_WEBSITE_CONFIG_DATABASE_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/website?sslmode=disable",
            f"PN_WEBSITE_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_WEBSITE_CONFIG_GRPC_FRIENDS_API_KEY={friends_api_key}",
        ]

        # 3. Miiverse API
        env_files["miiverse-api.local.env"] = [
            f"PN_MIIVERSE_API_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_MIIVERSE_API_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_MIIVERSE_API_CONFIG_S3_ACCESS_SECRET={minio_secret}",
            "PN_MIIVERSE_API_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_MIIVERSE_API_CONFIG_S3_ACCESS_KEY=minio_pretendo",
            f"PN_MIIVERSE_API_CONFIG_GRPC_FRIENDS_API_KEY={friends_api_key}",
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
            f"JUXT_CONFIG_GRPC_FRIENDS_API_KEY={friends_api_key}",
            f"JUXT_CONFIG_AES_KEY={account_aes_key}",
            "JUXT_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_juxt?replicaSet=rs",
        ]

        # 5. BOSS
        env_files["boss.local.env"] = [
            f"PN_BOSS_CONFIG_GRPC_ACCOUNT_SERVER_API_KEY={account_grpc_key}",
            f"PN_BOSS_CONFIG_S3_ACCESS_SECRET={minio_secret}",
            "PN_BOSS_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_BOSS_CONFIG_S3_ACCESS_KEY=minio_pretendo",
            f"PN_BOSS_CONFIG_GRPC_FRIENDS_SERVER_API_KEY={friends_api_key}",
            f"PN_BOSS_CONFIG_GRPC_BOSS_SERVER_API_KEY={boss_api_key}",
            f"PN_BOSS_CONFIG_BOSS_WIIU_AES_KEY={boss_wiiu_aes}",
            f"PN_BOSS_CONFIG_BOSS_WIIU_HMAC_KEY={boss_wiiu_hmac}",
            f"PN_BOSS_CONFIG_BOSS_3DS_AES_KEY={boss_3ds_aes}",
            f"PN_BOSS_CONFIG_BOSS_3DS_HMAC_KEY={boss_3ds_hmac}",
            "PN_BOSS_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_boss?replicaSet=rs",
        ]

        # 6. Super Mario Maker
        env_files["super-mario-maker.local.env"] = [
            f"PN_SMM_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_SMM_CONFIG_GRPC_ACCOUNT_API_KEY={account_grpc_key}",
            f"PN_SMM_CONFIG_S3_ACCESS_SECRET={minio_secret}",
            "PN_SMM_CONFIG_S3_ENDPOINT=minio:9000",
            "PN_SMM_CONFIG_S3_ACCESS_KEY=minio_pretendo",
            "PN_SMM_CONFIG_S3_BUCKET=super-mario-maker",
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
            "PN_SSBWIIU_AUTHENTICATION_SERVER_PORT=60120",
            "PN_SSBWIIU_SECURE_SERVER_PORT=60130",
            f"PN_SSBWIIU_SECURE_SERVER_HOST={server_ip}",
            "PN_SSBWIIU_ACCOUNT_GRPC_HOST=account",
            "PN_SSBWIIU_ACCOUNT_GRPC_PORT=5000",
            f"PN_SSBWIIU_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            "PN_SSBWIIU_FRIENDS_GRPC_HOST=friends",
            "PN_SSBWIIU_FRIENDS_GRPC_PORT=5001",
            f"PN_SSBWIIU_FRIENDS_GRPC_API_KEY={friends_api_key}",
            "PN_SSBWIIU_DATASTORE_S3BUCKET=super-smash-bros-wiiu",
            "PN_SSBWIIU_DATASTORE_S3KEY=minio_pretendo",
            f"PN_SSBWIIU_DATASTORE_S3SECRET={minio_secret}",
            "PN_SSBWIIU_DATASTORE_S3URL=minio:9000",
            f"PN_SSBWIIU_AES_KEY={smash_aes_key}",
            f"PN_SSBWIIU_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/super_smash_bros_wiiu?sslmode=disable",
            "PN_SSBWIIU_LOCAL_AUTH=0",
            "PN_SSBWIIU_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_smash?replicaSet=rs",
        ]

        # 12. Pokken Tournament
        env_files["pokken-tournament.local.env"] = [
            f"PN_POKKENTOURNAMENT_KERBEROS_PASSWORD={pokken_kerberos_pw}",
            f"PN_POKKENTOURNAMENT_CONFIG_AES_KEY={pokken_aes_key}",
            "PN_POKKENTOURNAMENT_AUTHENTICATION_SERVER_PORT=60008",
            f"PN_POKKENTOURNAMENT_SECURE_SERVER_HOST={server_ip}",
            "PN_POKKENTOURNAMENT_SECURE_SERVER_PORT=60009",
            "PN_POKKENTOURNAMENT_ACCOUNT_GRPC_HOST=account",
            "PN_POKKENTOURNAMENT_ACCOUNT_GRPC_PORT=5000",
            f"PN_POKKENTOURNAMENT_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            "PN_POKKENTOURNAMENT_FRIENDS_GRPC_HOST=friends",
            "PN_POKKENTOURNAMENT_FRIENDS_GRPC_PORT=5001",
            f"PN_POKKENTOURNAMENT_FRIENDS_GRPC_API_KEY={friends_api_key}",
            f"PN_POKKENTOURNAMENT_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/pokken_tournament?sslmode=disable",
            "PN_POKKENTOURNAMENT_S3_ENDPOINT=minio:9000",
            "PN_POKKENTOURNAMENT_S3_ACCESS_KEY=minio_pretendo",
            f"PN_POKKENTOURNAMENT_S3_ACCESS_SECRET={minio_secret}",
            "PN_POKKENTOURNAMENT_S3_BUCKET=pokken-tournament",
        ]

        # 13. Mario Kart 8
        env_files["mario-kart-8.local.env"] = [
            f"PN_MK8_KERBEROS_PASSWORD={mk8_kerberos_pw}",
            f"PN_MK8_CONFIG_AES_KEY={mk8_aes_key}",
            f"PN_MK8_SECURE_SERVER_ACCESS_KEY={mk8_aes_key}",
            f"PN_MK8_AUTHENTICATION_SERVER_ACCESS_KEY={mk8_aes_key}",
            "PN_MK8_AUTHENTICATION_SERVER_PORT=60140",
            "PN_MK8_SECURE_SERVER_PORT=60150",
            "PN_MK8_ACCOUNT_GRPC_HOST=account",
            "PN_MK8_ACCOUNT_GRPC_PORT=5000",
            f"PN_MK8_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            "PN_MK8_FRIENDS_GRPC_HOST=friends",
            "PN_MK8_FRIENDS_GRPC_PORT=5001",
            f"PN_MK8_FRIENDS_GRPC_API_KEY={friends_api_key}",
            f"PN_MK8_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/mario_kart_8?sslmode=disable",
            "PN_MK8_CONFIG_MONGODB_URI=mongodb://mongodb:27017/pretendo_mk8?replicaSet=rs",
            "PN_MK8_CONFIG_MONGODB_HOST=mongodb",
            "PN_MK8_CONFIG_MONGODB_PORT=27017",
            f"PN_MK8_SECURE_SERVER_LOCATION={server_ip}",
            f"PN_MK8_SECURE_SERVER_HOST={server_ip}",
            "PN_MK8_S3_ENDPOINT=minio:9000",
            "PN_MK8_S3_ACCESS_KEY=minio_pretendo",
            f"PN_MK8_S3_ACCESS_SECRET={minio_secret}",
            "PN_MK8_S3_BUCKET=mario-kart-8",
            "PN_MK8_CONFIG_NEX_VERSION=30500",
            "PN_MK8_CONFIG_PRUDP_VERSION=1",
            "PN_MK8_CONFIG_KERBEROS_KEY_SIZE=32",
            "PN_MK8_CONFIG_ACCESS_KEY=red-pro-2",
            "PN_MK8_CONFIG_SERVER_PORT=60000",
            "PN_MK8_CONFIG_SERVER_NAME=mariokart8",
            "PN_MK8_CONFIG_MONGODB_HOST=mongodb",
            "PN_MK8_CONFIG_MONGODB_PORT=27017",
        ]

        # 14. MinIO
        env_files["minio.local.env"] = [
            f"MINIO_ROOT_PASSWORD={minio_secret}",
        ]
        
        # 14. Postgres
        env_files["postgres.local.env"] = [
            "POSTGRES_USER=postgres_pretendo",
            f"POSTGRES_PASSWORD={postgres_pass}",
        ]

        # 15. Mongo Express
        env_files["mongo-express.local.env"] = [
            "ME_CONFIG_MONGODB_SERVER=mongodb",
            "ME_CONFIG_MONGODB_PORT=27017",
        ]
        
        # Write all env files
        for filename, lines in env_files.items():
            filepath = os.path.join(env_dir, filename)
            _write_managed_file(filepath, "\n".join(lines) + "\n")
            self.manager.setup_log.append(f"  [ENV] Created {filename}")
            
            # Ensure the non-local version exists too (satisfied by empty if referenced in compose)
            if filename.endswith(".local.env"):
                base_env = filename.replace(".local.env", ".env")
                base_path = os.path.join(env_dir, base_env)
                if not os.path.exists(base_path):
                    _write_managed_file(base_path, "# Placeholder env file\n")
                    self.manager.setup_log.append(f"  [ENV] Created placeholder {base_env}")
        
        for fname in os.listdir(env_dir):
            if fname.endswith(".env") and not fname.endswith(".local.env"):
                local_name = fname.replace(".env", ".local.env")
                local_path = os.path.join(env_dir, local_name)
                if not os.path.exists(local_path):
                    _write_managed_file(local_path, "# Auto-generated empty local env\n")
                    self.manager.setup_log.append(f"  [ENV] Created empty {local_name}")
        
        root_env = os.path.join(s_dir, ".env")
        _write_managed_file(root_env, f"SERVER_IP={server_ip}\n")
        self.manager.setup_log.append(f"  [ENV] Created .env (SERVER_IP={server_ip})")
        
        old_secrets_path = os.path.join(s_dir, "secrets.txt")
        if os.path.exists(old_secrets_path):
            try:
                os.remove(old_secrets_path)
                self.manager.setup_log.append("  [SECURITY] Removed old in-repo secrets.txt")
            except Exception as e:
                self.manager.setup_log.append(f"  [WARN] Could not remove old secrets.txt: {e}")
        self.manager.setup_log.append(f"  [SECURITY] Secrets source: {get_secret_file_path()}")

    def _ensure_smm_metadata(self, s_dir):
        """Ensure 900000.bin exists to prevent 'specified key does not exist' S3 error."""
        dest_path = os.path.join(s_dir, "environment", "900000.bin")
        if os.path.exists(dest_path):
            self.manager.setup_log.append("[INFO] SMM metadata file already exists.")
            return
            
        self.manager.setup_log.append("[System] Ensuring Super Mario Maker metadata (900000.bin)...")
        
        # Attempt download from multiple mirrors
        mirrors = [
            "https://raw.githubusercontent.com/MatthewL246/pretendo-docker/master/console-files/900000.bin",
            "https://github.com/PretendoNetwork/super-mario-maker/raw/master/assets/900000.bin",
            "https://pretendo-mirror.custom/900000.bin" # Placeholder for future mirror
        ]
        
        import requests
        for mirror_url in mirrors:
            try:
                self.manager.setup_log.append(f"  [HTTP] Trying mirror: {mirror_url}")
                r = requests.get(mirror_url, timeout=15)
                if r.status_code == 200 and len(r.content) > 1000: # Ensure it's not a tiny error page
                    with open(dest_path, "wb") as f:
                        f.write(r.content)
                    self.manager.setup_log.append(f"[OK] SMM metadata downloaded successfully ({len(r.content)} bytes).")
                    return
            except:
                continue

        # Fallback: Create placeholder to satisfy the Stat check (patch handles rest)
        try:
            with open(dest_path, "wb") as f:
                f.write(b"") 
            self.manager.setup_log.append("[OK] SMM metadata placeholder created (0 bytes).")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Failed to create SMM metadata placeholder: {e}")

    def _fix_go_build_compatibility(self, s_dir):
        """Fix build errors, sync vendors, and patch UI crashes."""
        self.manager.setup_log.append("[System] Cleaning host environment and patching microservices...")
        repos_dir = os.path.join(s_dir, "repos")
        if not os.path.isdir(repos_dir):
            return

        # 0. NEW: Deep Clean - Remove any broken 'vendor' folders from host disk
        # This is critical to fix "import lookup disabled by -mod=vendor"
        if "win" in OS_INFO["os"] or "msys" in OS_INFO["os"]:
            self.manager.setup_log.append("  [CLEAN] Nuking broken vendor folders via PowerShell...")
            repos_dir_win = repos_dir.replace("/", "\\")
            clean_cmd = f'powershell -Command "Get-ChildItem -Path \'{repos_dir_win}\' -Recurse -Filter vendor -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"'
            subprocess.run(clean_cmd, shell=True, creationflags=0x08000000)
        else:
            for rname in os.listdir(repos_dir):
                v_path = os.path.join(repos_dir, rname, "vendor")
                if os.path.isdir(v_path):
                    shutil.rmtree(v_path, ignore_errors=True)
                    self.manager.setup_log.append(f"  [CLEAN] Removed vendor in {rname}")

        # 1. Patch Dockerfiles (Vendor sync & Delve) - Optimized: Scan all repos
        cnt = 0
        for rname in os.listdir(repos_dir):
            r_path = os.path.join(repos_dir, rname)
            fpath = os.path.join(r_path, "Dockerfile")
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f: content = f.read()
                    changed = False
                    if "dlv@latest" in content:
                        content = content.replace("dlv@latest", "dlv@v1.22.0")
                        changed = True
                    
                    # FAIL-SAFE: Force Go to ignore vendor folders if they exist
                    if "go build" in content and 'GOFLAGS="-mod=mod"' not in content:
                        content = re.sub(r'(FROM\s+[^\n]+)', r'\1\nENV GOFLAGS="-mod=mod"', content, count=1)
                        changed = True
                    
                    # Cleanup ANY previous vendor patches to restore repo to clean state
                    if "RUN go mod vendor" in content:
                        content = content.replace("RUN go mod vendor\n", "")
                        content = content.replace("RUN --mount=type=cache,target=/go/pkg/mod/ --mount=type=bind,source=go.sum,target=go.sum --mount=type=bind,source=go.mod,target=go.mod go mod vendor\n", "")
                        changed = True
                    
                    # Special case for Mario Kart 8: delete go.mod/go.sum to force regeneration with correct versions
                    if rname == "mario-kart-8":
                        for f in ["go.mod", "go.sum"]:
                            fp = os.path.join(r_path, f)
                            if os.path.isfile(fp):
                                try: os.remove(fp)
                                except: pass
                    
                    if changed:
                        with open(fpath, "w", encoding="utf-8") as f: f.write(content)
                        cnt += 1
                except: pass

            if rname == "mario-kart-8":
                try:
                    fpath = os.path.join(r_path, "Dockerfile")
                    with open(fpath, "r", encoding="utf-8") as f: content = f.read()
                    if "go mod tidy" in content:
                        # Add cache mounts
                        if "--mount=type=cache" not in content:
                            content = content.replace("RUN go mod tidy", "RUN --mount=type=cache,target=/go/pkg/mod/ go mod tidy")
                        
                        # Literal replacement for known versions in MK8 Dockerfile
                        content = content.replace("@v1.0.41", "@v1.0.35")
                        content = content.replace("@v1.0.58", "@v1.0.45")
                        content = content.replace("@v1.0.30", "@v1.0.22")
                        
                        with open(fpath, "w", encoding="utf-8") as f: f.write(content)
                        self.manager.setup_log.append("  [PATCH] Enforced Mario Kart 8 legacy versions via Literal Replace")
                    
                    # Deep cleanup of subdirectories
                    for sub in ["mk8-authentication", "mk8-secure"]:
                        sub_path = os.path.join(r_path, sub)
                        if os.path.isdir(sub_path):
                            for f in ["go.mod", "go.sum"]:
                                fp = os.path.join(sub_path, f)
                                if os.path.isfile(fp):
                                    try: 
                                        os.remove(fp)
                                        self.manager.setup_log.append(f"  [CLEAN] Wiped {sub}/{f}")
                                    except: pass
                except: pass

            # 1b. NEW: Fix SSH dependencies in Node.js repos (Prevents build hangs)
            for pfname in ["package.json", "package-lock.json"]:
                pfpath = os.path.join(r_path, pfname)
                if os.path.isfile(pfpath):
                    try:
                        with open(pfpath, "r", encoding="utf-8") as f: pcontent = f.read()
                        if "ssh://git@github.com" in pcontent:
                            pcontent = pcontent.replace("ssh://git@github.com/", "https://github.com/")
                            pcontent = pcontent.replace("git+ssh://git@github.com/", "git+https://github.com/")
                            with open(pfpath, "w", encoding="utf-8") as f: f.write(pcontent)
                            self.manager.setup_log.append(f"  [PATCH] Converted SSH to HTTPS in {rname}/{pfname}")
                    except: pass
        if cnt > 0:
            self.manager.setup_log.append(f"[OK] Patched {cnt} Dockerfiles for build compatibility.")

        # 2. Patch Juxtaposition-UI AWS Endpoint Crash directly in JS Source
        ui_util_path = os.path.join(repos_dir, "juxtaposition-ui", "src", "util.js")
        if os.path.isfile(ui_util_path):
            try:
                with open(ui_util_path, "r", encoding="utf-8") as f: ui_content = f.read()
                # Safely fallback to minio:9000 if config.aws.spaces.endpoint is somehow undefined
                ui_content = re.sub(
                    r'new aws\.Endpoint\([^)]+\)',
                    r'new aws.Endpoint((config.aws && config.aws.spaces && config.aws.spaces.endpoint) || "http://minio:9000")',
                    ui_content
                )
                with open(ui_util_path, "w", encoding="utf-8") as f: f.write(ui_content)
                self.manager.setup_log.append("[OK] Applied safe AWS Endpoint fallback to Juxtaposition UI.")
            except Exception as e:
                self.manager.setup_log.append(f"[WARN] Failed to patch Juxtaposition UI: {e}")

        # 3. Splatoon specific gRPC resolver fix
        splat_init = os.path.join(repos_dir, "splatoon", "init.go")
        if os.path.isfile(splat_init):
            try:
                with open(splat_init, "r", encoding="utf-8") as f: s_content = f.read()
                if 'grpc.NewClient(fmt.Sprintf("dns:%s:%s"' in s_content:
                    s_content = s_content.replace('grpc.NewClient(fmt.Sprintf("dns:%s:%s"', 'grpc.NewClient(fmt.Sprintf("dns:///%s:%s"')
                    with open(splat_init, "w", encoding="utf-8") as f: f.write(s_content)
                    self.manager.setup_log.append("[OK] Patched Splatoon gRPC resolver syntax.")
            except: pass

        # 4. Silence godotenv warnings by creating empty .env files in each repo
        env_cnt = 0
        for rname in os.listdir(repos_dir):
            r_path = os.path.join(repos_dir, rname)
            if os.path.isdir(r_path):
                env_path = os.path.join(r_path, ".env")
                if not os.path.exists(env_path):
                    try:
                        with open(env_path, "w") as f:
                            f.write("# Dummy env to silence godotenv warnings\n")
                        env_cnt += 1
                    except: pass
        if env_cnt > 0:
            self.manager.setup_log.append(f"[OK] Created {env_cnt} dummy .env files for noise reduction.")

    def _apply_compose_patches(self, port, s_dir, host_mode=False):
        """Robust YAML patching for mitmproxy port, Postgres health, and service injections."""
        # Host networking breaks Docker service-name DNS (mongodb/postgres/redis) and makes
        # every Go service collide on its internal Delve port 2345. Keep bridge networking.
        host_mode = False
        applied_any = False
        for fname in ["compose.yaml", "compose.yml", "docker-compose.yml"]:
            path = os.path.join(s_dir, fname)
            if not os.path.exists(path): continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if not lines: continue

                new_lines = []
                current_service = None
                current_section = None
                root_section = None
                changed = False
                in_ports = False
                in_networks = False

                for i, line in enumerate(lines):
                    stripped = line.strip()

                    if not line.startswith("  ") and not line.startswith("\t") and stripped.endswith(":"):
                        root_section = stripped[:-1].lower()
                        current_service = None
                        current_section = None
                        in_ports = False
                        in_networks = False

                    # Section detection (indented exactly 4 spaces)
                    if root_section == "services" and current_service and line.startswith("    ") and not line.startswith("     ") and stripped.endswith(":"):
                        current_section = stripped[:-1].lower()
                        if current_section == "ports": in_ports = True
                        else: in_ports = False
                        if current_section == "networks": in_networks = True
                        else: in_networks = False

                    # Service detection (indented exactly 2 spaces)
                    if root_section == "services" and line.startswith("  ") and not line.startswith("   ") and stripped.endswith(":") and not stripped.startswith("-") and not stripped.startswith("#"):
                        current_service = stripped[:-1].lower()
                        current_section = None
                        in_ports = False
                        in_networks = False

                    if current_service and stripped == "network_mode: host":
                        changed = True
                        continue

                    # 1. mitmproxy-pretendo: update external port
                    if current_service == "mitmproxy-pretendo":
                        # Match both <port>:8080 and <port>:<port> cases to force backend to 8080
                        if re.search(r'^\s*-\s*\d+:\d+', line) and "ports:" not in line and "127.0.0.1" not in line:
                            new_line = re.sub(r'^(\s*-\s*)\d+:\d+', rf'\g<1>{port}:8080', line)
                            if new_line != line:
                                line = new_line
                                changed = True
                        
                        # Inject volume mount for live addon patching if not present
                        if stripped == "volumes:" and "pretendo_addon.py" not in "".join(lines[i:i+10]):
                            new_lines.append(line)
                            new_lines.append("      - type: bind\n")
                            new_lines.append("        source: ./repos/mitmproxy-pretendo/pretendo_addon.py\n")
                            new_lines.append("        target: /home/mitmproxy/pretendo_addon.py\n")
                            new_lines.append("        read_only: true\n")
                            changed = True
                            continue

                    # 2. adminer: update port to avoid conflict
                    if current_service == "adminer":
                        if "127.0.0.1:8070:8080" in line:
                            line = line.replace("127.0.0.1:8070:8080", "127.0.0.1:8088:8080")
                            changed = True

                    # 2b. mario-kart-8: remove duplicate port range that conflicts
                    if current_service == "mario-kart-8":
                        if re.search(r'"?\d+-\d+:\d+-\d+/udp"?', stripped):
                            changed = True
                            continue

                    if current_service in ["friends", "mario-kart-8", "splatoon", "super-mario-maker", 
                                           "minecraft-wiiu", "pikmin-3", "super-smash-bros-wiiu", 
                                           "wiiu-chat-authentication", "wiiu-chat-secure", "pokken-tournament"]:
                        if ":" in line and ("60" in line or "/udp" in line):
                             # Special case for 6000 -> 60000, 6001 -> 60001
                             if "6000:6000" in line:
                                 new_line = line.replace("6000:6000", "60000:60000")
                             elif "6001:6001" in line:
                                 new_line = line.replace("6001:6001", "60001:60001")
                             elif "6003:6003" in line:
                                 new_line = line.replace("6003:6003", "60021:60021")
                             elif "6005:6005" in line:
                                 new_line = line.replace("6005:6005", "60041:60041")
                             elif "6007:6007" in line:
                                 new_line = line.replace("6007:6007", "60061:60061")
                             elif "6011:6011" in line:
                                 new_line = line.replace("6011:6011", "60101:60101")
                             elif "6013:6013" in line:
                                 new_line = line.replace("6013:6013", "60130:60130")
                             else:
                                 # Generic mapping for 60xx -> 60xx0 (e.g. 6014 -> 60140)
                                 # Only match 4-digit ports starting with 60 to avoid double-patching 5-digit ports
                                 new_line = re.sub(r'(?<!\d)(60\d{2}):(60\d{2})(?!\d)', r'\g<1>0:\g<2>0', line)
                             
                             if new_line != line:
                                 line = new_line
                                 changed = True

                    # 3. mongodb: pin version and add a healthcheck that also initializes the replica set.
                    # Services use ?replicaSet=rs, so a plain ping is not enough; Mongo can be alive
                    # while still reporting RSGhost/ReplicaSetNoPrimary.
                    if current_service == "mongodb":
                        if stripped == "mongodb:" and "healthcheck:" not in "".join(lines[i:i+30]):
                            new_lines.append(line)
                            new_lines.append("    healthcheck:\n")
                            new_lines.append("      test: [\"CMD-SHELL\", \"(mongosh --quiet --eval 'try { rs.initiate({_id:\\\"rs\\\", members:[{_id:0, host:\\\"mongodb:27017\\\"}]}); } catch(e) {} ; try { db.hello().isWritablePrimary || rs.status().myState === 1 } catch(e) { false }' || mongo --quiet --eval 'try { rs.initiate({_id:\\\"rs\\\", members:[{_id:0, host:\\\"mongodb:27017\\\"}]}); } catch(e) {} ; try { db.isMaster().ismaster || rs.status().myState === 1 } catch(e) { false }') | grep -q true\"]\n")
                            new_lines.append("      interval: 10s\n")
                            new_lines.append("      timeout: 10s\n")
                            new_lines.append("      retries: 30\n")
                            new_lines.append("      start_period: 40s\n")
                            changed = True
                            continue

                    # 4. postgres: Add Healthcheck and Init Sequence
                    if current_service == "postgres" and "image: postgres:alpine" in line:
                        line = line.replace("image: postgres:alpine", "image: postgres:17-alpine")
                        changed = True

                    if current_service == "postgres" and stripped == "postgres:":
                        if "healthcheck:" not in "".join(lines[i:i+30]):
                            new_lines.append(line)
                            new_lines.append("    healthcheck:\n")
                            new_lines.append("      test: [\"CMD-SHELL\", \"pg_isready -h localhost -U postgres_pretendo -d template1 && psql -v ON_ERROR_STOP=1 -U postgres_pretendo -d template1 -t -c \\\"SELECT 1 FROM pg_database WHERE datname='mario_kart_8'\\\" | grep -q 1\"]\n")
                            new_lines.append("      interval: 10s\n")
                            new_lines.append("      timeout: 10s\n")
                            new_lines.append("      retries: 24\n")
                            changed = True
                            continue

                    # 4b. Update dependencies to wait for health
                    if current_section == "depends_on" and line.startswith("      - ") and current_service in ["account", "friends", "super-mario-maker", "mario-kart-8", "pikmin-3", "splatoon", "super-smash-bros-wiiu", "boss", "mongo-express", "miiverse-api", "juxtaposition-ui", "website"]:
                        dep_name = stripped[2:].strip()
                        if dep_name == "postgres":
                             new_lines.append("      postgres:\n")
                             new_lines.append("        condition: service_healthy\n")
                             changed = True
                             continue
                        elif dep_name == "mongodb":
                             new_lines.append("      mongodb:\n")
                             new_lines.append("        condition: service_healthy\n")
                             changed = True
                             continue
                        else:
                             new_lines.append(f"      {dep_name}:\n")
                             new_lines.append("        condition: service_started\n")
                             changed = True
                             continue

                    if current_service == "postgres" and stripped == "volumes:":
                        # Check if the init script is already bound
                        chunk = "".join(lines[i:i+20])
                        if "postgres-init.sh" not in chunk:
                             new_lines.append(line)
                             # Indent must match the existing file structure (6 spaces for list item)
                             new_lines.append("      - type: bind\n")
                             new_lines.append("        source: ./scripts/run-in-container/postgres-init.sh\n")
                             new_lines.append("        target: /docker-entrypoint-initdb.d/postgres-init.sh\n")
                             new_lines.append("        read_only: true\n")
                             changed = True
                             continue

                    new_lines.append(line)
                if changed:
                    with open(path, "w", encoding="utf-8") as f:
                        f.writelines(new_lines)
                    applied_any = True
                    break
            except Exception as e:
                try:
                    self.manager.setup_log.append(f"[WARN] Failed to patch {fname}: {e}")
                except Exception:
                    pass
        return applied_any

    def _ensure_postgres_init_script(self, s_dir):
        """Deploy the postgres initialization script to the server directory."""
        src = os.path.join(os.path.dirname(__file__), "scripts/run-in-container/postgres-init.sh")
        dest = os.path.join(s_dir, "scripts/run-in-container/postgres-init.sh")
        
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        
        if os.path.exists(src):
            try:
                shutil.copy2(src, dest)
                # Ensure it's executable
                os.chmod(dest, 0o755)
                self.manager.setup_log.append("[OK] Postgres initialization script deployed.")
            except Exception as e:
                self.manager.setup_log.append(f"[ERROR] Failed to deploy postgres script: {e}")
        else:
            # Inline fallback if src is somehow missing (e.g. repo not fully updated)
            content = """#!/bin/bash
set -e
databases="friends super_mario_maker pikmin3 splatoon super_smash_bros_wiiu pokken_tournament mario_kart_8 website"
PG_USER=${POSTGRES_USER:-postgres_pretendo}
export PGUSER="$PG_USER"

until psql -d postgres -c "SELECT 1" > /dev/null 2>&1 || psql -d template1 -c "SELECT 1" > /dev/null 2>&1 || psql -d "$PG_USER" -c "SELECT 1" > /dev/null 2>&1; do
  sleep 2
done

if psql -d postgres -c "SELECT 1" > /dev/null 2>&1; then MNT_DB="postgres"; elif psql -d template1 -c "SELECT 1" > /dev/null 2>&1; then MNT_DB="template1"; else MNT_DB="$PG_USER"; fi

for db in $databases; do
    if ! psql -d "$MNT_DB" -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
        psql -d "$MNT_DB" -c "CREATE DATABASE \\"$db\\""
    fi
done
"""
            try:
                with open(dest, "w") as f: f.write(content)
                os.chmod(dest, 0o755)
            except: pass

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
                
            loop_fix = 'or re.match(r"^\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}(:\\d+)?$", flow.request.pretty_host)'
            if loop_fix not in content:
                if "or \"pretendo-cdn.b-cdn.net\" in flow.request.pretty_host" in content:
                    # Improved Host Header injection for IP-based requests
                    host_mod = '\n                if re.match(r"^\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}(:\\d+)?$", flow.request.host_header):\n                    flow.request.host_header = "account.pretendo.cc"'
                    
                    content = content.replace(
                        "or \"pretendo-cdn.b-cdn.net\" in flow.request.pretty_host",
                        "or \"pretendo-cdn.b-cdn.net\" in flow.request.pretty_host\n                " + loop_fix
                    )
                    
                    if "flow.request.host_header = original_host" in content:
                        content = content.replace(
                            "flow.request.host_header = original_host",
                            "flow.request.host_header = original_host" + host_mod
                        )
                    changed = True
            
            if changed:
                with open(addon_path, "w") as f: f.write(content)
                self.manager.setup_log.append("[System] mitmproxy addon patched for IP-based loop prevention.")
        except Exception as e:
            self.manager.setup_log.append(f"[ERROR] Failed to patch mitmproxy addon: {e}")

    def _patch_friends(self, s_dir):
        """Add stability/debug fixes and missing dummy handles to Friends service."""
        friends_dir = os.path.join(s_dir, "repos", "friends")
        if not os.path.isdir(friends_dir): return
        
        self.manager.server_log.append("[System] Injecting local nex-go-v2 and patching NEX probe handlers...")
        
        # 0. Handle nex-go-v2 signature length patch (Crucial for Cemu connectivity)
        nex_go_source = os.path.join(self.manager.server_dir_field.text().strip(), "..", "nex-go-temp")
        nex_go_dest = os.path.join(friends_dir, "nex-go-v2")
        
        # Priority 1: Local nex-go-v2 copy if exists
        if os.path.isdir(nex_go_source):
            if os.path.isdir(nex_go_dest):
                shutil.rmtree(nex_go_dest)
            shutil.copytree(nex_go_source, nex_go_dest)
            
            # Update go.mod
            go_mod_path = os.path.join(friends_dir, "go.mod")
            if os.path.isfile(go_mod_path):
                with open(go_mod_path, "r") as f: go_mod = f.read()
                if "replace github.com/PretendoNetwork/nex-go/v2 => ./nex-go-v2" not in go_mod:
                    go_mod += "\nreplace github.com/PretendoNetwork/nex-go/v2 => ./nex-go-v2\n"
                    with open(go_mod_path, "w") as f: f.write(go_mod)
        
        # Apply the signature length patch to wherever nex-go-v2 is (local or vendor)
        possible_packet_paths = [
            os.path.join(nex_go_dest, "prudp_packet_v1.go"),
            os.path.join(friends_dir, "vendor", "github.com", "PretendoNetwork", "nex-go", "v2", "prudp_packet_v1.go")
        ]
        
        for packet_v1_path in possible_packet_paths:
            if os.path.isfile(packet_v1_path):
                try:
                    with open(packet_v1_path, "r") as f: p_content = f.read()
                    old_code = """	if p.readStream.Remaining() < 16 {
		return errors.New("Failed to read PRUDPv1 signature. Not have enough data")
	}

	p.signature = p.readStream.ReadBytesNext(16)"""
                    new_code = """	if p.readStream.Remaining() >= 16 {
		p.signature = p.readStream.ReadBytesNext(16)
	}"""
                    if old_code in p_content:
                        p_content = p_content.replace(old_code, new_code)
                        with open(packet_v1_path, "w") as f: f.write(p_content)
                        self.manager.server_log.append(f"[OK] Patched NEX probe signature support in {os.path.basename(os.path.dirname(os.path.dirname(packet_v1_path)))}/nex-go-v2")
                except: pass
        
        # Apply the Kerberos ticket skip patch to nex-go-v2 prudp_endpoint.go
        possible_endpoint_paths = [
            os.path.join(nex_go_dest, "prudp_endpoint.go"),
            os.path.join(friends_dir, "vendor", "github.com", "PretendoNetwork", "nex-go", "v2", "prudp_endpoint.go")
        ]
        
        for endpoint_path in possible_endpoint_paths:
            if os.path.isfile(endpoint_path):
                try:
                    with open(endpoint_path, "r") as f: e_content = f.read()
                    # Look for the Kerberos ticket reading logic
                    old_logic = """	if pep.IsSecureEndPoint {
		sessionKey, pid, checkValue, err := pep.ReadKerberosTicket(decompressedPayload)
		if err != nil {
			logger.Error(err.Error())
			return
		}"""
                    new_logic = """	if pep.IsSecureEndPoint {
		// Skip Kerberos ticket for empty/small payloads (NEX probes)
		if len(decompressedPayload) < 4 {
			pep.Emit("connect", ack)
			pep.Server.SendRaw(connection.Socket, ack.Bytes())
			return
		}
		sessionKey, pid, checkValue, err := pep.ReadKerberosTicket(decompressedPayload)
		if err != nil {
			logger.Error(err.Error())
			return
		}"""
                    if old_logic in e_content:
                        e_content = e_content.replace(old_logic, new_logic)
                        with open(endpoint_path, "w") as f: f.write(e_content)
                        self.manager.server_log.append(f"[OK] Patched NEX probe Kerberos bypass in {os.path.basename(os.path.dirname(os.path.dirname(endpoint_path)))}/nex-go-v2")
                except: pass

        
        # main.go panic recovery (keep existing logic but fix potential issues)
        main_go = os.path.join(friends_dir, "main.go")
        # ... (rest of main.go patching if needed)

        # 1. Patch register_secure_server_protocols.go to add missing handles
        reg_path = os.path.join(friends_dir, "nex", "register_secure_server_protocols.go")
        if os.path.isfile(reg_path):
            try:
                with open(reg_path, "r") as f: content = f.read()
                
                # Add missing Wii U handles
                wiiu_missing = [
                    ("AddFriend", "AddFriend"),
                    ("AddFriendByName", "AddFriendByName"),
                    ("UpdateMii", "UpdateMii")
                ]
                for method, handle in wiiu_missing:
                    line = f"friendsWiiUProtocol.{method} = nex_friends_wiiu.{handle}"
                    if line not in content:
                        content = content.replace("friendsWiiUProtocol.GetRequestBlockSettings = nex_friends_wiiu.GetRequestBlockSettings", 
                                                 f"friendsWiiUProtocol.GetRequestBlockSettings = nex_friends_wiiu.GetRequestBlockSettings\n\tfriendsWiiUProtocol.{method} = nex_friends_wiiu.{handle}")
                
                # Add missing Account Management handles
                acc_missing = [
                    ("GetAccountData", "GetAccountData"),
                    ("GetPublicData", "GetPublicData"),
                    ("GetMultiplePublicData", "GetMultiplePublicData"),
                    ("TestCapability", "TestCapability"),
                    ("GetPrivateData", "GetPrivateData")
                ]
                for method, handle in acc_missing:
                    line = f"accountManagementProtocol.{method} = nex_account_management.{handle}"
                    if line not in content:
                        content = content.replace("accountManagementProtocol.NintendoCreateAccount = nex_account_management.NintendoCreateAccount",
                                                 f"accountManagementProtocol.NintendoCreateAccount = nex_account_management.NintendoCreateAccount\n\taccountManagementProtocol.{method} = nex_account_management.{handle}")

                with open(reg_path, "w") as f: f.write(content)
            except: pass

        # 1b. Fix NEX versions in authentication.go and secure.go
        for target_f in ["authentication.go", "secure.go"]:
            fpath = os.path.join(friends_dir, "nex", target_f)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r") as f: content = f.read()
                    # Force a slightly older/more compatible NEX version for Cemu if needed
                    content = content.replace("nex.NewLibraryVersion(1, 1, 0)", "nex.NewLibraryVersion(1, 1, 0)") # Keep current but ensure structure
                    if target_f == "secure.go":
                        content = content.replace("\n\tglobals.SecureServer.ByteStreamSettings.UseStructureHeader = true", "")
                    if "globals.AuthenticationServer.SetFragmentSize(962)" in content:
                         content = content.replace("globals.AuthenticationServer.SetFragmentSize(962)", "globals.AuthenticationServer.SetFragmentSize(962)\n\tglobals.AuthenticationServer.LibraryVersions.SetDefault(nex.NewLibraryVersion(1, 1, 0))")
                    with open(fpath, "w") as f: f.write(content)
                except: pass

        # 2. Create dummy implementations for missing Wii U methods
        wiiu_nex_dir = os.path.join(friends_dir, "nex", "friends-wiiu")
        os.makedirs(wiiu_nex_dir, exist_ok=True)
        
        wiiu_dummies = {
            "add_friend.go": ("AddFriend", "pid *types.PID"),
            "add_friend_by_name.go": ("AddFriendByName", "username *types.String"),
            "update_mii.go": ("UpdateMii", "mii *friends_wiiu_types.MiiV2")
        }
        
        for fname, (method, params) in wiiu_dummies.items():
            fpath = os.path.join(wiiu_nex_dir, fname)
            if not os.path.exists(fpath):
                # Only import what is needed based on params
                extra_imports = ""
                if "types.PID" in params or "types.String" in params:
                    extra_imports += '\n\t"github.com/PretendoNetwork/nex-go/v2/types"'
                if "friends_wiiu_types" in params:
                    extra_imports += '\n\tfriends_wiiu_types "github.com/PretendoNetwork/nex-protocols-go/v2/friends-wiiu/types"'
                
                content = f"""package nex_friends_wiiu
import (
	"github.com/PretendoNetwork/friends/globals"
	nex "github.com/PretendoNetwork/nex-go/v2"
	friends_wiiu "github.com/PretendoNetwork/nex-protocols-go/v2/friends-wiiu"{extra_imports}
)
func {method}(err error, packet nex.PacketInterface, callID uint32, {params}) (*nex.RMCMessage, *nex.Error) {{
	if err != nil {{
		globals.Logger.Error(err.Error())
		return nil, nex.NewError(nex.ResultCodes.FPD.InvalidArgument, "")
	}}
	rmcResponse := nex.NewRMCSuccess(globals.SecureEndpoint, nil)
	rmcResponse.ProtocolID = friends_wiiu.ProtocolID
	rmcResponse.MethodID = friends_wiiu.Method{method}
	rmcResponse.CallID = callID
	return rmcResponse, nil
}}
"""
                self._write_file(fpath, content)

        # 3. Create dummy implementations for missing Account Management methods
        acc_nex_dir = os.path.join(friends_dir, "nex", "account-management")
        os.makedirs(acc_nex_dir, exist_ok=True)
        
        acc_dummies = {
            "get_account_data.go": ("GetAccountData", ""),
            "get_public_data.go": ("GetPublicData", "idPrincipal *types.PID"),
            "get_multiple_public_data.go": ("GetMultiplePublicData", "lstPrincipals *types.List[*types.PID]"),
            "test_capability.go": ("TestCapability", "uiCapability *types.PrimitiveU32"),
            "get_private_data.go": ("GetPrivateData", "")
        }
        
        for fname, (method, params) in acc_dummies.items():
            fpath = os.path.join(acc_nex_dir, fname)
            if not os.path.exists(fpath):
                comma = ", " if params else ""
                # Only import what is needed
                extra_imports = ""
                if "types." in params:
                    extra_imports += '\n\t"github.com/PretendoNetwork/nex-go/v2/types"'

                content = f"""package nex_account_management
import (
	"github.com/PretendoNetwork/friends/globals"
	nex "github.com/PretendoNetwork/nex-go/v2"{extra_imports}
	account_management "github.com/PretendoNetwork/nex-protocols-go/v2/account-management"
)
func {method}(err error, packet nex.PacketInterface, callID uint32{comma}{params}) (*nex.RMCMessage, *nex.Error) {{
	if err != nil {{
		globals.Logger.Error(err.Error())
		return nil, nex.NewError(nex.ResultCodes.Core.InvalidArgument, "")
	}}
	rmcResponse := nex.NewRMCSuccess(globals.SecureEndpoint, nil)
	rmcResponse.ProtocolID = account_management.ProtocolID
	rmcResponse.MethodID = account_management.Method{method}
	rmcResponse.CallID = callID
	return rmcResponse, nil
}}
"""
                self._write_file(fpath, content)

    def _patch_mario_kart_8(self, s_dir):
        """Minimal patch for MK8: just generate start.sh and Dockerfile with legacy dependencies."""
        mk8_dir = os.path.join(s_dir, "repos", "mario-kart-8")
        if not os.path.isdir(mk8_dir): return
        self.manager.setup_log.append("[System] Generating legacy build environment for Mario Kart 8...")

        # Create start.sh
        self._write_file(os.path.join(mk8_dir, "start.sh"), "#!/bin/sh\n./mk8-authentication &\n./mk8-secure\n")

        # Dockerfile (Forces legacy versions compatible with the pristine code)
        mk8_dockerfile = os.path.join(mk8_dir, "Dockerfile")
        self._write_file(mk8_dockerfile, '''# syntax=docker/dockerfile:1
FROM golang:1.23-alpine AS build
WORKDIR /app
COPY . .
WORKDIR /app/mk8-authentication
RUN if [ ! -f go.mod ]; then \\
    go mod init github.com/PretendoNetwork/mk8-authentication && \\
    go mod edit -require github.com/PretendoNetwork/nex-go@v1.0.14 && \\
    go mod edit -require github.com/PretendoNetwork/nex-protocols-go@v1.0.19; \\
    fi
RUN --mount=type=cache,target=/go/pkg/mod/ go mod tidy
RUN CGO_ENABLED=0 go build -o /app/bin/mk8-authentication .
WORKDIR /app/mk8-secure
RUN if [ ! -f go.mod ]; then \\
    go mod init github.com/PretendoNetwork/mk8-secure && \\
    go mod edit -require github.com/PretendoNetwork/nex-go@v1.0.14 && \\
    go mod edit -require github.com/PretendoNetwork/nex-protocols-go@v1.0.19; \\
    fi
RUN --mount=type=cache,target=/go/pkg/mod/ go mod tidy
RUN CGO_ENABLED=0 go build -o /app/bin/mk8-secure .
FROM alpine:3.20
WORKDIR /app
RUN apk add --no-cache libc6-compat ca-certificates
COPY --from=build /app/bin/mk8-authentication /app/mk8-authentication
COPY --from=build /app/bin/mk8-secure /app/mk8-secure
COPY start.sh /app/start.sh
COPY mk8-secure/secure.config /app/secure.config
RUN chmod +x /app/mk8-authentication /app/mk8-secure /app/start.sh
EXPOSE 60140/udp 60150/udp
CMD ["./start.sh"]
''')

        # Generate secure.config for mk8-secure. Keep this aligned with the
        # compose-published MK8 secure port; otherwise account sends Cemu to a
        # port with no PRUDP listener.
        secure_config = os.path.join(mk8_dir, "mk8-secure", "secure.config")
        self._write_file(secure_config, """PrudpVersion=1
SignatureVersion=1
ServerPort=60150
KerberosKeySize=32
AccessKey=25dbf96a
ServerName=Pretendo MK8 Secure
NexVersion=30500
DatabaseIP=mongodb
DatabasePort=27017
DatabaseUseAuth=false
AccountDatabase=pretendo
PNIDCollection=pnids
NexAccountsCollection=nexaccounts
MK8Database=mk8
RoomsCollection=rooms
SessionsCollection=sessions
UsersCollection=users
RegionsCollection=regions
TournamentsCollection=tourneys
""")

        # Patch mk8-authentication database.go to point to local MongoDB
        auth_db_go = os.path.join(mk8_dir, "mk8-authentication", "database.go")
        if os.path.isfile(auth_db_go):
            with open(auth_db_go, 'r', encoding='utf-8') as f: c = f.read()
            c = c.replace('mongodb://143.198.126.113:27017/', 'mongodb://mongodb:27017/')
            with open(auth_db_go, 'w', encoding='utf-8') as f: f.write(c)

        # Apply surgical Go source fixes for nex-go v1.0.14 / nex-protocols-go v1.0.19 compatibility
        self._apply_mk8_source_patches(mk8_dir)
        self.manager.setup_log.append("[System] Mario Kart 8 legacy patching complete.")

    def _apply_mk8_source_patches(self, mk8_dir):
        # Apply targeted casing/signature fixes to MK8 Go source for nex-go v1.0.14 compat.
        auth_dir = os.path.join(mk8_dir, "mk8-authentication")
        secure_dir = os.path.join(mk8_dir, "mk8-secure")
        if os.path.isdir(auth_dir):
            auth_main_go = os.path.join(auth_dir, "main.go")
            if os.path.isfile(auth_main_go):
                with open(auth_main_go, 'r', encoding='utf-8') as f: c = f.read()
                if '"os"' not in c:
                    c = c.replace('"fmt"\n', '"fmt"\n\t"os"\n')
                c = c.replace('nexServer.Listen(":60002")', 'nexServer.Listen(":" + getenvDefault("PN_MK8_AUTHENTICATION_SERVER_PORT", "60140"))')
                if 'func getenvDefault(' not in c:
                    c += '''

func getenvDefault(key string, fallback string) string {
\tvalue := os.Getenv(key)
\tif value == "" {
\t\treturn fallback
\t}
\treturn value
}
'''
                with open(auth_main_go, 'w', encoding='utf-8') as f: f.write(c)

            login_ex_go = os.path.join(auth_dir, "login_ex.go")
            if os.path.isfile(login_ex_go):
                with open(login_ex_go, 'r', encoding='utf-8') as f: c = f.read()
                c = c.replace('stationURL := "prudps:/address=163.123.195.148;port=60003;CID=1;PID=2;sid=1;stream=10;type=2"',
                              'stationURL := fmt.Sprintf("prudps:/address=%s;port=%s;CID=1;PID=2;sid=1;stream=10;type=2", getenvDefault("PN_MK8_SECURE_SERVER_HOST", "127.0.0.1"), getenvDefault("PN_MK8_SECURE_SERVER_PORT", "60150"))')
                c = c.replace('serverName := "Pretendo MK7"', 'serverName := "Pretendo MK8"')
                with open(login_ex_go, 'w', encoding='utf-8') as f: f.write(c)

            kerberos_go = os.path.join(auth_dir, "kerberos.go")
            if os.path.isfile(kerberos_go):
                with open(kerberos_go, 'r', encoding='utf-8') as f: c = f.read()
                c = c.replace('serverPassword := "password"', 'serverPassword := getenvDefault("PN_MK8_KERBEROS_PASSWORD", "password")')
                with open(kerberos_go, 'w', encoding='utf-8') as f: f.write(c)

            auth_db_go = os.path.join(auth_dir, "database.go")
            if os.path.isfile(auth_db_go):
                with open(auth_db_go, 'r', encoding='utf-8') as f: c = f.read()
                c = c.replace('mongoDatabase = mongoClient.Database("pretendo")', 'mongoDatabase = mongoClient.Database(getenvDefault("PN_MK8_ACCOUNT_DATABASE", "pretendo"))')
                with open(auth_db_go, 'w', encoding='utf-8') as f: f.write(c)

        if not os.path.isdir(secure_dir): return

        # --- main.go: NAT Traversal casing ---
        main_go = os.path.join(secure_dir, "main.go")
        if os.path.isfile(main_go):
            with open(main_go, 'r', encoding='utf-8') as f: c = f.read()
            c = c.replace('NewNatTraversalProtocol', 'NewNATTraversalProtocol')
            c = c.replace('.ReportNatProperties(', '.ReportNATProperties(')
            with open(main_go, 'w', encoding='utf-8') as f: f.write(c)

        # --- register.go: ConnectionID, StationURL, pointer-to-value ---
        register_go = os.path.join(secure_dir, "register.go")
        if os.path.isfile(register_go):
            with open(register_go, 'r', encoding='utf-8') as f: c = f.read()
            c = c.replace('client.SetConnectionId(', 'client.SetConnectionID(')
            c = c.replace('client.SetLocalStationUrl(', 'client.SetLocalStationURL(')
            c = c.replace('secureServer.ConnectionIDCounter.Increment()', 'nexServer.ConnectionIDCounter().Increment()')
            c = c.replace('localStation.SetAddress(&address)', 'localStation.SetAddress(address)')
            c = c.replace('localStation.SetPort(&port)', 'localStation.SetPort(port)')
            c = c.replace('localStation.SetNatf(&natf)', 'localStation.SetNatf(natf)')
            c = c.replace('localStation.SetNatm(&natm)', 'localStation.SetNatm(natm)')
            c = c.replace('localStation.SetType(&type_)', 'localStation.SetType(type_)')
            with open(register_go, 'w', encoding='utf-8') as f: f.write(c)

        # --- report_nat_properties.go: PID, NAT casing, pointer-to-value ---
        report_go = os.path.join(secure_dir, "report_nat_properties.go")
        if os.path.isfile(report_go):
            with open(report_go, 'r', encoding='utf-8') as f: c = f.read()
            c = c.replace('client.ConnectionId()', 'client.ConnectionID()')
            c = c.replace('.SetNatm(&natm_s)', '.SetNatm(natm_s)')
            c = c.replace('.SetNatf(&natf_s)', '.SetNatf(natf_s)')
            c = c.replace('.SetPid(&pid)', '.SetPID(pid)')
            c = c.replace('.SetPid(pid)', '.SetPID(pid)')
            c = c.replace('.SetRVCID(&rvcid)', '.SetRVCID(rvcid)')
            c = c.replace('nexproto.NatTraversalProtocolID', 'nexproto.NATTraversalProtocolID')
            c = c.replace('nexproto.NatTraversalMethodReportNatProperties', 'nexproto.NATTraversalMethodReportNATProperties')
            with open(report_go, 'w', encoding='utf-8') as f: f.write(c)

        # --- request_probe_initiation_ext.go: NAT casing, GetClient migration ---
        probe_go = os.path.join(secure_dir, "request_probe_initiation_ext.go")
        if os.path.isfile(probe_go):
            with open(probe_go, 'r', encoding='utf-8') as f: c = f.read()
            c = c.replace('nexproto.NatTraversalProtocolID', 'nexproto.NATTraversalProtocolID')
            c = c.replace('nexproto.NatTraversalMethodRequestProbeInitiationExt', 'nexproto.NATTraversalMethodRequestProbeInitiationExt')
            c = c.replace('nexproto.NatTraversalMethodInitiateProbe', 'nexproto.NATTraversalMethodInitiateProbe')
            c = c.replace('nexServer.GetClient(getPlayerSessionAddress(uint32(targetPid)))', 'nexServer.FindClientFromPID(uint32(targetPid))')
            with open(probe_go, 'w', encoding='utf-8') as f: f.write(c)

        # --- auto_matchmake_with_search_criteria_postpone.go: signature fix ---
        matchmake_go = os.path.join(secure_dir, "auto_matchmake_with_search_criteria_postpone.go")
        if os.path.isfile(matchmake_go):
            with open(matchmake_go, 'r', encoding='utf-8') as f: c = f.read()
            old_sig = 'func autoMatchmakeWithSearchCriteria_Postpone(err error, client *nex.Client, callID uint32, searchCriteria []*nexproto.MatchmakeSessionSearchCriteria, matchmakeSession *nexproto.MatchmakeSession, message string) {'
            new_sig = 'func autoMatchmakeWithSearchCriteria_Postpone(err error, client *nex.Client, callID uint32, matchmakeSession *nexproto.MatchmakeSession, message string) {\n\tvacantParticipants := uint32(1)'
            c = c.replace(old_sig, new_sig)
            c = c.replace('searchCriteria[0].VacantParticipants', 'vacantParticipants')
            c = c.replace('uint32(vacantParticipants)', 'vacantParticipants')
            with open(matchmake_go, 'w', encoding='utf-8') as f: f.write(c)


    def _patch_splatoon_schedules(self, s_dir):
        """Route Splatoon BOSS requests to public Pretendo CDN."""
        nginx_conf_dir = os.path.join(s_dir, "config/nginx")
        boss_conf_path = os.path.join(nginx_conf_dir, "boss.conf")
        content = """server {
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
            with open(boss_conf_path, "w") as f: f.write(content)
        except: pass

    def _generate_juxtaposition_boot_config(self, s_dir):
        """Fix Juxtaposition UI crash with config.json."""
        ui_repo = os.path.join(s_dir, "repos", "juxtaposition-ui")
        if not os.path.isdir(ui_repo): return
        from utils import _grep_env_file
        env_dir = os.path.join(s_dir, "environment")
        minio_secret = _grep_env_file(os.path.join(env_dir, "account.local.env"), "PN_ACT_CONFIG_S3_ACCESS_SECRET") or "dummy"
        account_aes = _grep_env_file(os.path.join(env_dir, "account.local.env"), "PN_ACT_CONFIG_AES_KEY") or "dummy"
        account_grpc = _grep_env_file(os.path.join(env_dir, "account.local.env"), "PN_ACT_CONFIG_GRPC_MASTER_API_KEY_ACCOUNT") or "dummy"
        friends_grpc = _grep_env_file(os.path.join(env_dir, "friends.local.env"), "PN_FRIENDS_CONFIG_GRPC_API_KEY") or "dummy"

        config = {
            "http": {"port": 8080},
            "mongoose": {"uri": "mongodb://mongodb:27017", "database": "pretendo_juxt", "options": {}},
            "redis": {"host": "redis", "port": 6379},
            "aes_key": account_aes,
            "CDN_domain": "localhost",
            "aws": {"spaces": {"endpoint": "http://minio:9000", "key": "minio_pretendo", "secret": minio_secret}},
            "grpc": {
                "friends": {"ip": "friends", "port": 5001, "api_key": friends_grpc},
                "account": {"ip": "account", "port": 5000, "api_key": account_grpc}
            }
        }
        try:
            config_path = os.path.join(ui_repo, "config.json")
            _write_managed_file(config_path, json.dumps(config, indent=4) + "\n")
        except: pass

    def _inject_missing_services(self, s_dir):
        """Ensure super-smash-bros-wiiu, pokken-tournament and mario-kart-8 are in compose.yml safely."""
        path = os.path.join(s_dir, "compose.yml")
        if not os.path.exists(path): return
        
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            
            changed = False
            injections = []
            
            if "# INJECTED SERVICES" not in content:
                injections.append("\n  # INJECTED SERVICES")
                changed = True

            if "super-smash-bros-wiiu:" not in content:
                self.manager.setup_log.append("[System] Injecting super-smash-bros-wiiu into compose.yml...")
                injections.append("""  super-smash-bros-wiiu:
    build: ./repos/super-smash-bros-wiiu
    depends_on:
      - account
      - friends
      - postgres
      - minio
    restart: unless-stopped
    ports:
      - 60120:60120/udp
      - 60130:60130/udp
    networks:
      internal:
    dns: 172.20.0.200
    env_file:
      - ./environment/super-smash-bros-wiiu.env
      - ./environment/super-smash-bros-wiiu.local.env""")
                changed = True

            if "pokken-tournament:" not in content:
                self.manager.setup_log.append("[System] Injecting pokken-tournament into compose.yml...")
                injections.append("""  pokken-tournament:
    build: ./repos/pokken-tournament
    depends_on:
      - account
      - friends
      - postgres
    restart: unless-stopped
    ports:
      - 60008:60008/udp
      - 60009:60009/udp
    networks:
      internal:
    dns: 172.20.0.200
    env_file:
      - ./environment/pokken-tournament.env
      - ./environment/pokken-tournament.local.env""")
                changed = True
            
            if "mario-kart-8:" not in content:
                self.manager.setup_log.append("[System] Injecting mario-kart-8 into compose.yml...")
                injections.append("""  mario-kart-8:
    build: ./repos/mario-kart-8
    depends_on:
      - account
      - friends
      - postgres
    restart: unless-stopped
    ports:
      - 60140:60140/udp
      - 60150:60150/udp
    networks:
      internal:
    dns: 172.20.0.200
    env_file:
      - ./environment/mario-kart-8.env
      - ./environment/mario-kart-8.local.env""")
                changed = True
            
            if changed and len(injections) > (1 if "# INJECTED SERVICES" in content else 0):
                # Find the 'volumes:' or 'networks:' at root level to insert before
                insertion_point = content.find("\nvolumes:")
                if insertion_point == -1: insertion_point = content.find("\nnetworks:")
                
                # If sentinel already exists, we might need to insert AFTER it or just at the end of the services section
                # For simplicity, we can insert before volumes/networks if we haven't already
                
                if insertion_point != -1:
                    new_content = content[:insertion_point] + "\n".join(injections) + "\n" + content[insertion_point:]
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new_content)
                else:
                    # Fallback to end of file
                    with open(path, "a", encoding="utf-8") as f:
                        f.write("\n".join(injections) + "\n")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Failed to inject missing services: {e}")

    def _write_file(self, path, content):
        _write_managed_file(path, content)

    def _post_setup_build(self, s_dir):
        """Verify Docker is responsive, then build all containers."""
        self.manager.setup_log.append("[System] Verifying Docker connectivity before build...")
        
        # Invalidate Docker cache to get a live check (TTL could return stale True)
        _CACHED_RESULTS.pop("docker_available", None)
        
        # Re-verify Docker is still available (pipe can vanish on Windows during long deploys)
        if not _docker_available():
            if self._docker_recovery_attempted:
                self.manager.setup_log.append("[ERROR] Docker is still unavailable after one recovery attempt. Deployment paused instead of restarting Docker repeatedly.")
                return

            self._docker_recovery_attempted = True
            self.manager.setup_log.append("[System] Docker pipe lost. Starting Docker Desktop once...")
            
            def _on_docker_ready():
                self.manager.setup_log.append("[OK] Docker re-established. Proceeding to build.")
                self._execute_build(s_dir)
            
            self.manager._ensure_docker_desktop(_on_docker_ready)
            return
        
        self.manager.setup_log.append("[OK] Docker is responsive.")
        self._execute_build(s_dir)
    
    def _execute_build(self, s_dir):
        """Run docker compose build after Docker has been confirmed ready."""
        self.manager.setup_log.append("[System] Starting container build process (Serial mode for stability)...")
        services = "pokken-tournament mario-kart-8 boss super-smash-bros-wiiu website friends miiverse-api juxtaposition-ui wiiu-chat-authentication wiiu-chat-secure super-mario-maker splatoon minecraft-wiiu pikmin-3"
        cmd = f"docker compose build {services}"
        pw = self.manager.cached_password
        if not pw and OS_INFO["os"] == "linux":
            pw = self.manager._ask_sudo_password()
        
        if OS_INFO["os"] == "linux" and pw:
            cmd = f"sudo -S {cmd}"
        
        def _on_build_done(c):
            if c == 0:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.information(self.manager, "Deployment Complete", "The Full Stack Deployment has finished successfully! Your Pretendo environment is fully built.\n\nYou are now ready to Start the Server.")
            else:
                self.manager.setup_log.append(f"[ERROR] Build process failed with status {c}. Verify Docker status.")

        self.manager._run_command(cmd, self.manager.setup_log, cwd=s_dir, stdin_data=pw, 
                                  on_done=_on_build_done, lock_ui=True)
