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
        
        # Check sub-repos: super-smash-bros-wiiu, pokken-tournament, mario-kart-8, super-mario-3d-world-secure
        for name, url in [
            ("super-smash-bros-wiiu", "https://github.com/PretendoNetwork/super-smash-bros-wiiu"),
            ("pokken-tournament", "https://github.com/PretendoNetwork/pokken-tournament"),
            ("mario-kart-8", "https://github.com/PretendoNetwork/mario-kart-8"),
            ("super-mario-3d-world-secure", "https://github.com/PretendoNetwork/super-mario-3d-world-secure")
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

    def _checkout_super_mario_maker_upstream(self, s_dir):
        """Keep Super Mario Maker on current upstream instead of the pinned submodule revision."""
        smm_dir = os.path.join(s_dir, "repos", "super-mario-maker")
        if not os.path.isdir(smm_dir):
            return

        try:
            flags = 0x08000000 if OS_INFO["os"] == "windows" else 0
            for args in (
                ["git", "-C", smm_dir, "fetch", "origin", "--prune"],
                ["git", "-C", smm_dir, "reset", "--hard", "origin/master"],
                ["git", "-C", smm_dir, "clean", "-fdx"],
            ):
                subprocess.run(
                    args,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    creationflags=flags,
                )
            self.manager.setup_log.append("[OK] Super Mario Maker source updated to current upstream.")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not update Super Mario Maker to upstream: {e}")

    def _patch_super_mario_maker_current_upstream_datastore(self, s_dir):
        """Patch current upstream SMM's common DataStore helper for local MinIO keys."""
        smm_dir = os.path.join(s_dir, "repos", "super-mario-maker")
        if not os.path.isdir(smm_dir):
            return

        go_mod_path = os.path.join(smm_dir, "go.mod")
        try:
            with open(go_mod_path, "r", encoding="utf-8") as f:
                go_mod = f.read()
            if "github.com/PretendoNetwork/nex-protocols-common-go/v2" not in go_mod:
                return

            third_party = os.path.join(smm_dir, "third_party", "nex-protocols-common-go")
            if not os.path.isdir(third_party):
                flags = 0x08000000 if OS_INFO["os"] == "windows" else 0
                subprocess.run(
                    [
                        "docker", "run", "--rm",
                        "-v", f"{smm_dir}:/src",
                        "-w", "/src",
                        "golang:1.23-alpine3.20",
                        "sh", "-lc",
                        "/usr/local/go/bin/go mod download >/dev/null && "
                        "rm -rf third_party/nex-protocols-common-go && "
                        "mkdir -p third_party && "
                        "cp -a /go/pkg/mod/github.com/!pretendo!network/nex-protocols-common-go/v2@v2.2.2 third_party/nex-protocols-common-go",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    creationflags=flags,
                )

            for root, _, files in os.walk(third_party):
                for file_name in files:
                    try:
                        os.chmod(os.path.join(root, file_name), 0o666)
                    except Exception:
                        pass

            replacements = {
                os.path.join(third_party, "datastore", "prepare_get_object.go"): [
                    ('import (\n\t"fmt"\n\t"time"', 'import (\n\t"fmt"\n\t"strings"\n\t"time"'),
                    (
                        'key := fmt.Sprintf("%s/%d.bin", commonProtocol.s3DataKeyBase, objectInfo.DataID)',
                        'key := fmt.Sprintf("%d.bin", objectInfo.DataID)\n\tif strings.TrimSpace(commonProtocol.s3DataKeyBase) != "" {\n\t\tkey = fmt.Sprintf("%s/%d.bin", commonProtocol.s3DataKeyBase, objectInfo.DataID)\n\t}',
                    ),
                    ("pReqGetInfo.DataID = param.DataID", "pReqGetInfo.DataID = objectInfo.DataID"),
                    (
                        'if err != nil {\n\t\tcommon_globals.Logger.Error(err.Error())\n\t\treturn nil, nex.NewError(nex.ResultCodes.DataStore.OperationNotAllowed, "change_error")\n\t}',
                        'if err != nil {\n\t\tcommon_globals.Logger.Error(err.Error())\n\t\treturn nil, nex.NewError(nex.ResultCodes.DataStore.OperationNotAllowed, "change_error")\n\t}\n\turl.RawQuery = ""',
                    ),
                ],
                os.path.join(third_party, "datastore", "complete_post_object.go"): [
                    ('import (\n\t"fmt"', 'import (\n\t"fmt"\n\t"strings"'),
                    (
                        'key := fmt.Sprintf("%s/%d.bin", commonProtocol.s3DataKeyBase, param.DataID)',
                        'key := fmt.Sprintf("%d.bin", param.DataID)\n\tif strings.TrimSpace(commonProtocol.s3DataKeyBase) != "" {\n\t\tkey = fmt.Sprintf("%s/%d.bin", commonProtocol.s3DataKeyBase, param.DataID)\n\t}',
                    ),
                ],
                os.path.join(third_party, "datastore", "complete_post_objects.go"): [
                    ('import (\n\t"fmt"', 'import (\n\t"fmt"\n\t"strings"'),
                    (
                        'key := fmt.Sprintf("%s/%d.bin", commonProtocol.s3DataKeyBase, dataID)',
                        'key := fmt.Sprintf("%d.bin", dataID)\n\t\tif strings.TrimSpace(commonProtocol.s3DataKeyBase) != "" {\n\t\t\tkey = fmt.Sprintf("%s/%d.bin", commonProtocol.s3DataKeyBase, dataID)\n\t\t}',
                    ),
                ],
            }

            for path, pairs in replacements.items():
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                for old, new in pairs:
                    content = content.replace(old, new)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)

            replace_line = "replace github.com/PretendoNetwork/nex-protocols-common-go/v2 => ./third_party/nex-protocols-common-go"
            if replace_line not in go_mod:
                with open(go_mod_path, "a", encoding="utf-8") as f:
                    f.write("\n" + replace_line + "\n")

            dockerfile_path = os.path.join(smm_dir, "Dockerfile")
            with open(dockerfile_path, "r", encoding="utf-8") as f:
                dockerfile = f.read()
            if "COPY third_party ./third_party" not in dockerfile:
                dockerfile = dockerfile.replace("WORKDIR ${app_dir}\n\nRUN --mount", "WORKDIR ${app_dir}\n\nCOPY third_party ./third_party\n\nRUN --mount", 1)
                with open(dockerfile_path, "w", encoding="utf-8") as f:
                    f.write(dockerfile)

            self.manager.setup_log.append("[OK] Patched current upstream Super Mario Maker DataStore helper for local MinIO.")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not patch current upstream Super Mario Maker DataStore helper: {e}")

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
            self._checkout_super_mario_maker_upstream(s_dir)
            self._patch_super_mario_maker_current_upstream_datastore(s_dir)
            self._inject_missing_services(s_dir)
            self._generate_env_files(s_dir, local_ip)
            self._apply_compose_patches(custom_port, s_dir, host_mode=host_mode)
            self._ensure_smm_metadata(s_dir)
            self._fix_go_build_compatibility(s_dir)
            self._patch_super_mario_maker_datastore_retry(s_dir)
            self._patch_friends(s_dir)
            self._patch_mario_kart_8(s_dir)
            self._patch_splatoon_schedules(s_dir)
            self._generate_juxtaposition_boot_config(s_dir)
            self._patch_mitmproxy_addon(s_dir)
            self._patch_nginx_timeouts(s_dir)
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
            "PN_ACT_CONFIG_ALLOW_LOCAL_BANNED_DEVICES=true",
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
            "PN_SMM_CONFIG_S3_ENDPOINT=npdi.cdn.pretendo.cc",
            "PN_SMM_CONFIG_S3_ACCESS_KEY=minio_pretendo",
            "PN_SMM_CONFIG_S3_BUCKET=super-mario-maker",
            "PN_SMM_CONFIG_S3_KEY_BASE=",
            "PN_SMM_CONFIG_S3_SECURE=false",
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

        # 13b. Super Mario 3D World
        env_files["super-mario-3d-world-secure.local.env"] = [
            "PN_SM3DW_AUTHENTICATION_SERVER_PORT=60200",
            f"PN_SM3DW_SECURE_SERVER_HOST={server_ip}",
            "PN_SM3DW_SECURE_SERVER_PORT=60201",
            "PN_SM3DW_ACCOUNT_GRPC_HOST=account",
            "PN_SM3DW_ACCOUNT_GRPC_PORT=5000",
            f"PN_SM3DW_ACCOUNT_GRPC_API_KEY={account_grpc_key}",
            f"PN_SM3DW_POSTGRES_URI=postgres://postgres_pretendo:{postgres_pass}@postgres/super_mario_3d_world?sslmode=disable",
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

        smm_base_env = os.path.join(env_dir, "super-mario-maker.env")
        if os.path.exists(smm_base_env):
            with open(smm_base_env, "r", encoding="utf-8") as f:
                smm_base_content = f.read()
            smm_base_content = smm_base_content.replace(
                "PN_SMM_CONFIG_S3_ENDPOINT=minio.pretendo.cc",
                "PN_SMM_CONFIG_S3_ENDPOINT=npdi.cdn.pretendo.cc",
            )
            _write_managed_file(smm_base_env, smm_base_content)
            self.manager.setup_log.append("  [ENV] Forced Super Mario Maker CDN endpoint to npdi.cdn.pretendo.cc")
        
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

        # Fallback: Create a minimal empty-list metadata payload. A zero-byte
        # object passes S3 stat checks but SMM can fail Course World before it
        # even issues the HTTP GET.
        try:
            with open(dest_path, "wb") as f:
                f.write(b"\x00\x00\x00\x00")
            self.manager.setup_log.append("[OK] SMM metadata placeholder created (4 bytes).")
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

            if rname == "account":
                try:
                    account_patches = [
                        os.path.join(r_path, "src", "middleware", "console-status-verification.ts"),
                        os.path.join(r_path, "src", "middleware", "pnid.ts"),
                        os.path.join(r_path, "src", "services", "nnas", "routes", "oauth.ts"),
                    ]
                    for account_path in account_patches:
                        if not os.path.isfile(account_path):
                            continue
                        with open(account_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        if "if (device.access_level < 0) {" in content and "PN_ACT_CONFIG_ALLOW_LOCAL_BANNED_DEVICES" not in content:
                            content = content.replace(
                                "if (device.access_level < 0) {",
                                "if (device.access_level < 0 && process.env.PN_ACT_CONFIG_ALLOW_LOCAL_BANNED_DEVICES === 'true') {\n\t\tdevice.access_level = 0;\n\t\tdevice.server_access_level = 'prod';\n\t\tawait device.save();\n\t}\n\n\tif (device.access_level < 0) {",
                                1,
                            )
                        if "if (pnid.access_level < 0) {" in content and "PN_ACT_CONFIG_ALLOW_LOCAL_BANNED_DEVICES" not in content:
                            content = content.replace(
                                "if (pnid.access_level < 0) {",
                                "if (pnid.access_level < 0 && process.env.PN_ACT_CONFIG_ALLOW_LOCAL_BANNED_DEVICES === 'true') {\n\t\tpnid.access_level = 0;\n\t\tpnid.server_access_level = 'prod';\n\t\tawait pnid.save();\n\t}\n\n\tif (pnid.access_level < 0) {",
                                1,
                            )
                        with open(account_path, "w", encoding="utf-8") as f:
                            f.write(content)
                    self.manager.setup_log.append("  [PATCH] Account local ban bypass enabled for self-host testing.")
                except Exception as e:
                    self.manager.setup_log.append(f"  [WARN] Account local ban bypass patch failed: {e}")
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

    def _patch_super_mario_maker_datastore_retry(self, s_dir):
        """Make SMM CompletePostObject idempotent so retry ACK races do not become 106-1204."""
        smm_dir = os.path.join(s_dir, "repos", "super-mario-maker")
        if not os.path.isdir(smm_dir):
            return

        go_mod_path = os.path.join(smm_dir, "go.mod")
        try:
            with open(go_mod_path, "r", encoding="utf-8") as f:
                go_mod_probe = f.read()
            if "github.com/PretendoNetwork/nex-go/v2" in go_mod_probe:
                self.manager.setup_log.append("[OK] Current upstream Super Mario Maker detected; skipping legacy SMM v1 deep patches.")
                return
        except Exception:
            pass

        secure_go_path = os.path.join(smm_dir, "nex", "secure.go")
        try:
            with open(secure_go_path, "r", encoding="utf-8") as f:
                secure_go = f.read()
            line = "\tglobals.SecureServer.SetDataStoreProtocolVersion(nex.NewPatchedNEXVersion(3, 4, 0, \"AMAJ\"))\n"
            if line not in secure_go:
                secure_go = secure_go.replace(
                    "\tglobals.SecureServer.SetDataStoreProtocolVersion(nex.NewNEXVersion(3, 4, 0))\n",
                    "",
                    1,
                )
                secure_go = secure_go.replace(
                    "\tglobals.SecureServer.SetDefaultNEXVersion(nex.NewPatchedNEXVersion(3, 8, 3, \"AMAJ\"))\n",
                    "\tglobals.SecureServer.SetDefaultNEXVersion(nex.NewPatchedNEXVersion(3, 8, 3, \"AMAJ\"))\n" + line,
                    1,
                )
                with open(secure_go_path, "w", encoding="utf-8") as f:
                    f.write(secure_go)
                self.manager.setup_log.append("[OK] Patched Super Mario Maker datastore protocol version for Course World.")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not patch SMM datastore protocol version: {e}")

        third_party = os.path.join(smm_dir, "third_party", "nex-protocols-common-go")
        try:
            if os.path.isdir(third_party):
                shutil.rmtree(third_party, ignore_errors=True)
            cmd = [
                "docker", "run", "--rm",
                "-v", f"{smm_dir}:/src",
                "-w", "/src",
                "golang:1.22-alpine",
                "sh", "-lc",
                "rm -rf third_party/nex-protocols-common-go && "
                "/usr/local/go/bin/go mod download >/dev/null && "
                "mkdir -p third_party && "
                "cp -a /go/pkg/mod/github.com/!pretendo!network/nex-protocols-common-go@v1.0.30 third_party/nex-protocols-common-go && "
                "chmod -R u+w third_party/nex-protocols-common-go",
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not stage SMM datastore retry patch module: {e}")
            return

        go_mod_path = os.path.join(smm_dir, "go.mod")
        try:
            with open(go_mod_path, "r", encoding="utf-8") as f:
                go_mod = f.read()
            replace_line = "replace github.com/PretendoNetwork/nex-protocols-common-go => ./third_party/nex-protocols-common-go"
            if replace_line not in go_mod:
                go_mod = go_mod.rstrip() + "\n\n" + replace_line + "\n"
                with open(go_mod_path, "w", encoding="utf-8") as f:
                    f.write(go_mod)
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not patch SMM go.mod for datastore retry fix: {e}")

        dockerfile_path = os.path.join(smm_dir, "Dockerfile")
        try:
            with open(dockerfile_path, "r", encoding="utf-8") as f:
                dockerfile = f.read()
            bind_line = "\t--mount=type=bind,source=third_party/nex-protocols-common-go,target=third_party/nex-protocols-common-go \\\n"
            if "source=third_party/nex-protocols-common-go" not in dockerfile:
                dockerfile = dockerfile.replace(
                    "\t--mount=type=bind,source=go.mod,target=go.mod \\\n",
                    "\t--mount=type=bind,source=go.mod,target=go.mod \\\n" + bind_line,
                    1,
                )
                with open(dockerfile_path, "w", encoding="utf-8") as f:
                    f.write(dockerfile)
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not patch SMM Dockerfile for datastore retry fix: {e}")

        complete_path = os.path.join(third_party, "datastore", "complete_post_object.go")
        try:
            with open(complete_path, "r", encoding="utf-8") as f:
                content = f.read()
            old = """	// * If GetObjectInfoByDataID returns data then that means
	// * the object has already been marked as uploaded. So do
	// * nothing
	objectInfo, _ := commonDataStoreProtocol.getObjectInfoByDataIDHandler(param.DataID)
	if objectInfo != nil {
		return nex.Errors.DataStore.PermissionDenied
	}
"""
            new = """	// * If GetObjectInfoByDataID returns data then that means
	// * the object has already been marked as uploaded. Treat this
	// * as a successful idempotent retry instead of failing Cemu
	// * during flaky upload-complete acknowledgement windows.
	objectInfo, _ := commonDataStoreProtocol.getObjectInfoByDataIDHandler(param.DataID)
	if objectInfo != nil {
		rmcResponse := nex.NewRMCResponse(datastore.ProtocolID, callID)
		rmcResponse.SetSuccess(datastore.MethodCompletePostObject, nil)

		rmcResponseBytes := rmcResponse.Bytes()

		var responsePacket nex.PacketInterface

		if commonDataStoreProtocol.server.PRUDPVersion() == 0 {
			responsePacket, _ = nex.NewPacketV0(client, nil)
			responsePacket.SetVersion(0)
		} else {
			responsePacket, _ = nex.NewPacketV1(client, nil)
			responsePacket.SetVersion(1)
		}

		responsePacket.SetSource(packet.Destination())
		responsePacket.SetDestination(packet.Source())
		responsePacket.SetType(nex.DataPacket)
		responsePacket.SetPayload(rmcResponseBytes)

		responsePacket.AddFlag(nex.FlagNeedsAck)
		responsePacket.AddFlag(nex.FlagReliable)

		commonDataStoreProtocol.server.Send(responsePacket)

		return 0
	}
"""
            changed = False
            if old in content:
                content = content.replace(old, new, 1)
                changed = True
            old_zero_size_check = """	if param.IsSuccess {
		objectSizeS3, err := commonDataStoreProtocol.S3ObjectSize(bucket, key)
		if err != nil {
			common_globals.Logger.Error(err.Error())
			return nex.Errors.DataStore.NotFound
		}

		objectSizeDB, errCode := commonDataStoreProtocol.getObjectSizeByDataIDHandler(param.DataID)
		if errCode != 0 {
			return errCode
		}

		if objectSizeS3 != uint64(objectSizeDB) {
			common_globals.Logger.Errorf("Object with DataID %d did not upload correctly! Mismatched sizes", param.DataID)
			// TODO - Is this a good error?
			return nex.Errors.DataStore.Unknown
		}
"""
            new_zero_size_check = """	if param.IsSuccess {
		objectSizeDB, errCode := commonDataStoreProtocol.getObjectSizeByDataIDHandler(param.DataID)
		if errCode != 0 {
			return errCode
		}

		if objectSizeDB > 0 {
			objectSizeS3, err := commonDataStoreProtocol.S3ObjectSize(bucket, key)
			if err != nil {
				common_globals.Logger.Error(err.Error())
				return nex.Errors.DataStore.NotFound
			}

			if objectSizeS3 != uint64(objectSizeDB) {
				common_globals.Logger.Errorf("Object with DataID %d did not upload correctly! Mismatched sizes", param.DataID)
				// TODO - Is this a good error?
				return nex.Errors.DataStore.Unknown
			}
		}
"""
            if old_zero_size_check in content:
                content = content.replace(old_zero_size_check, new_zero_size_check, 1)
                changed = True
            early_zero_size_marker = """	// * Only allow an objects owner to make this request
	ownerPID, errCode := commonDataStoreProtocol.getObjectOwnerByDataIDHandler(param.DataID)
"""
            early_zero_size_patch = """	if param.IsSuccess {
		objectSizeDB, errCode := commonDataStoreProtocol.getObjectSizeByDataIDHandler(param.DataID)
		if errCode != 0 {
			return errCode
		}

		if objectSizeDB == 0 {
			errCode = commonDataStoreProtocol.updateObjectUploadCompletedByDataIDHandler(param.DataID, true)
			if errCode != 0 {
				return errCode
			}

			rmcResponse := nex.NewRMCResponse(datastore.ProtocolID, callID)
			rmcResponse.SetSuccess(datastore.MethodCompletePostObject, nil)

			rmcResponseBytes := rmcResponse.Bytes()

			var responsePacket nex.PacketInterface

			if commonDataStoreProtocol.server.PRUDPVersion() == 0 {
				responsePacket, _ = nex.NewPacketV0(client, nil)
				responsePacket.SetVersion(0)
			} else {
				responsePacket, _ = nex.NewPacketV1(client, nil)
				responsePacket.SetVersion(1)
			}

			responsePacket.SetSource(packet.Destination())
			responsePacket.SetDestination(packet.Source())
			responsePacket.SetType(nex.DataPacket)
			responsePacket.SetPayload(rmcResponseBytes)

			responsePacket.AddFlag(nex.FlagNeedsAck)
			responsePacket.AddFlag(nex.FlagReliable)

			commonDataStoreProtocol.server.Send(responsePacket)

			return 0
		}
	}

	// * Only allow an objects owner to make this request
	ownerPID, errCode := commonDataStoreProtocol.getObjectOwnerByDataIDHandler(param.DataID)
"""
            if early_zero_size_marker in content and "if objectSizeDB == 0" not in content:
                content = content.replace(early_zero_size_marker, early_zero_size_patch, 1)
                changed = True
            old_failed_upload = """	} else {
		errCode := commonDataStoreProtocol.deleteObjectByDataIDHandler(param.DataID)
		if errCode != 0 {
			return errCode
		}
	}
"""
            new_failed_upload = """	} else {
		objectSizeDB, errCode := commonDataStoreProtocol.getObjectSizeByDataIDHandler(param.DataID)
		if errCode != 0 {
			return errCode
		}

		if objectSizeDB == 0 {
			errCode = commonDataStoreProtocol.updateObjectUploadCompletedByDataIDHandler(param.DataID, true)
			if errCode != 0 {
				return errCode
			}
		} else {
			errCode = commonDataStoreProtocol.deleteObjectByDataIDHandler(param.DataID)
			if errCode != 0 {
				return errCode
			}
		}
	}
"""
            if old_failed_upload in content:
                content = content.replace(old_failed_upload, new_failed_upload, 1)
                changed = True
            if '"context"' not in content:
                content = content.replace('import (\n', 'import (\n\t"context"\n', 1)
                changed = True
            if '"strings"' not in content:
                content = content.replace('\t"fmt"\n', '\t"fmt"\n\t"strings"\n', 1)
                changed = True
            if '"github.com/minio/minio-go/v7"' not in content:
                content = content.replace(
                    '\tdatastore_types "github.com/PretendoNetwork/nex-protocols-go/datastore/types"\n',
                    '\tdatastore_types "github.com/PretendoNetwork/nex-protocols-go/datastore/types"\n\t"github.com/minio/minio-go/v7"\n',
                    1,
                )
                changed = True
            if "ensureZeroByteDataStoreObject(param.DataID)" not in content:
                content = content.replace(
                    """		if objectSizeDB == 0 {
			errCode = commonDataStoreProtocol.updateObjectUploadCompletedByDataIDHandler(param.DataID, true)
""",
                    """		if objectSizeDB == 0 {
			errCode = ensureZeroByteDataStoreObject(param.DataID)
			if errCode != 0 {
				return errCode
			}

			errCode = commonDataStoreProtocol.updateObjectUploadCompletedByDataIDHandler(param.DataID, true)
""",
                )
                changed = True
            content2 = content.replace(
                'fmt.Sprintf("%s/%d.bin", commonDataStoreProtocol.s3DataKeyBase, param.DataID)',
                "dataStoreObjectKey(param.DataID)",
            ).replace(
                'fmt.Sprintf("%s/%d.bin", commonDataStoreProtocol.s3DataKeyBase, dataID)',
                "dataStoreObjectKey(dataID)",
            )
            if content2 != content:
                content = content2
                changed = True
            if "func ensureZeroByteDataStoreObject(dataID uint64) uint32" not in content:
                content = content.rstrip() + """

func ensureZeroByteDataStoreObject(dataID uint64) uint32 {
	bucket := commonDataStoreProtocol.s3Bucket
	key := dataStoreObjectKey(dataID)

	_, err := commonDataStoreProtocol.minIOClient.PutObject(context.TODO(), bucket, key, strings.NewReader(""), 0, minio.PutObjectOptions{ContentType: "application/octet-stream"})
	if err != nil {
		common_globals.Logger.Error(err.Error())
		return nex.Errors.DataStore.Unknown
	}

	common_globals.Logger.Infof("SMM DataStore: ensured zero-byte object %s/%s", bucket, key)
	return 0
}
"""
                changed = True
            if "func dataStoreObjectKey(dataID uint64) string" not in content:
                content = content.rstrip() + """

func dataStoreObjectKey(dataID uint64) string {
	base := strings.Trim(commonDataStoreProtocol.s3DataKeyBase, "/")
	if base == "" {
		return fmt.Sprintf("%d.bin", dataID)
	}

	return fmt.Sprintf("%s/%d.bin", base, dataID)
}
"""
                changed = True
            if changed:
                with open(complete_path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.manager.setup_log.append("[OK] Patched Super Mario Maker datastore upload-complete retries.")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not patch SMM datastore retry behavior: {e}")

        prepare_get_path = os.path.join(third_party, "datastore", "prepare_get_object.go")
        try:
            with open(prepare_get_path, "r", encoding="utf-8") as f:
                content = f.read()
            changed = False
            content2 = content.replace(
                'fmt.Sprintf("%s/%d.bin", commonDataStoreProtocol.s3DataKeyBase, param.DataID)',
                "dataStoreObjectKey(param.DataID)",
            )
            if content2 != content:
                content = content2.replace('\t"fmt"\n', "")
                changed = True
            guard = """	if objectInfo.Size == 0 && commonDataStoreProtocol.minIOClient != nil {
		errCode = ensureZeroByteDataStoreObject(param.DataID)
		if errCode != 0 {
			return errCode
		}
	}

"""
            marker = "\turl, err := commonDataStoreProtocol.S3Presigner.GetObject(bucket, key, time.Minute*15)\n"
            if "ensureZeroByteDataStoreObject(param.DataID)" not in content and marker in content:
                content = content.replace(marker, guard + marker, 1)
                changed = True
            old_signed_get = """	url, err := commonDataStoreProtocol.S3Presigner.GetObject(bucket, key, time.Minute*15)
	if err != nil {
		common_globals.Logger.Error(err.Error())
		return nex.Errors.DataStore.OperationNotAllowed
	}
	common_globals.Logger.Infof("SMM PrepareGetObject: dataID=%d bucket=%s key=%s url=%s", param.DataID, bucket, key, url.String())
"""
            new_routed_get = """	displayURL := ""
	if bucket == "super-mario-maker" {
		displayURL = "http://npts.app.pretendo.cc/" + bucket + "/" + key
	} else {
		url, err := commonDataStoreProtocol.S3Presigner.GetObject(bucket, key, time.Minute*15)
		if err != nil {
			common_globals.Logger.Error(err.Error())
			return nex.Errors.DataStore.OperationNotAllowed
		}
		displayURL = url.String()
	}
	common_globals.Logger.Infof("SMM PrepareGetObject: dataID=%d bucket=%s key=%s url=%s", param.DataID, bucket, key, displayURL)
"""
            if old_signed_get in content:
                content = content.replace(old_signed_get, new_routed_get, 1)
                content = content.replace("pReqGetInfo.URL = url.String()", "pReqGetInfo.URL = displayURL", 1)
                changed = True
            if "pReqGetInfo := datastore_types.NewDataStoreReqGetInfoV1()" in content:
                content = content.replace(
                    "pReqGetInfo := datastore_types.NewDataStoreReqGetInfoV1()",
                    "pReqGetInfo := datastore_types.NewDataStoreReqGetInfo()",
                    1,
                )
                changed = True
            if "pReqGetInfo := datastore_types.NewDataStoreReqGetInfo()" in content and "\n\tpReqGetInfo.DataID = param.DataID\n" not in content:
                content = content.replace(
                    "\tpReqGetInfo.RootCACert = commonDataStoreProtocol.rootCACert\n",
                    "\tpReqGetInfo.RootCACert = commonDataStoreProtocol.rootCACert\n\tpReqGetInfo.DataID = param.DataID\n",
                    1,
                )
                changed = True
            if changed:
                with open(prepare_get_path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.manager.setup_log.append("[OK] Patched Super Mario Maker zero-byte datastore download placeholders.")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not patch SMM datastore download placeholders: {e}")

        object_infos_path = os.path.join(smm_dir, "nex", "datastore", "super-mario-maker", "get_object_infos.go")
        try:
            with open(object_infos_path, "r", encoding="utf-8") as f:
                content = f.read()
            changed = False
            old_infos_url = """		URL, err := globals.Presigner.GetObject(bucket, key, time.Minute*15)
		if err != nil {
			globals.Logger.Error(err.Error())
			return nex.Errors.DataStore.OperationNotAllowed
		}
"""
            new_infos_url = """		displayURL := ""
		if bucket == "super-mario-maker" {
			displayURL = "http://npts.app.pretendo.cc/" + bucket + "/" + key
		} else {
			URL, err := globals.Presigner.GetObject(bucket, key, time.Minute*15)
			if err != nil {
				globals.Logger.Error(err.Error())
				return nex.Errors.DataStore.OperationNotAllowed
			}
			displayURL = URL.String()
		}
"""
            if old_infos_url in content:
                content = content.replace(old_infos_url, new_infos_url, 1)
                content = content.replace("info.GetInfo.URL = URL.String()", "info.GetInfo.URL = displayURL", 1)
                changed = True
            if 'datastore_super_mario_maker_types "github.com/PretendoNetwork/nex-protocols-go/datastore/super-mario-maker/types"\n' in content:
                content = content.replace(
                    '\tdatastore_super_mario_maker_types "github.com/PretendoNetwork/nex-protocols-go/datastore/super-mario-maker/types"\n',
                    "",
                    1,
                )
                changed = True
            if "type fileServerObjectInfoV1 struct" not in content:
                content = content.replace(
                    "\tclient := packet.Sender()\n\n\tpInfos := make([]*datastore_super_mario_maker_types.DataStoreFileServerObjectInfo, 0)\n",
                    """	client := packet.Sender()

	type fileServerObjectInfoV1 struct {
		dataID  uint64
		getInfo *datastore_types.DataStoreReqGetInfoV1
	}

	pInfos := make([]fileServerObjectInfoV1, 0)
""",
                    1,
                )
                content = content.replace(
                    """		info := datastore_super_mario_maker_types.NewDataStoreFileServerObjectInfo()
		info.DataID = objectInfo.DataID
		info.GetInfo = datastore_types.NewDataStoreReqGetInfo()
		info.GetInfo.URL = displayURL
		info.GetInfo.RequestHeaders = []*datastore_types.DataStoreKeyValue{}
		info.GetInfo.Size = objectInfo.Size
		info.GetInfo.RootCACert = []byte{}
		info.GetInfo.DataID = objectInfo.DataID

		pInfos = append(pInfos, info)
""",
                    """		globals.Logger.Infof("SMM GetObjectInfos: dataID=%d size=%d url=%s", objectInfo.DataID, objectInfo.Size, displayURL)

		info := datastore_types.NewDataStoreReqGetInfoV1()
		info.URL = displayURL
		info.RequestHeaders = []*datastore_types.DataStoreKeyValue{}
		info.Size = objectInfo.Size
		info.RootCACert = []byte{}

		pInfos = append(pInfos, fileServerObjectInfoV1{
			dataID:  objectInfo.DataID,
			getInfo: info,
		})
""",
                    1,
                )
                content = content.replace(
                    "\trmcResponseStream.WriteListStructure(pInfos)\n",
                    """	rmcResponseStream.WriteUInt32LE(uint32(len(pInfos)))
	for _, info := range pInfos {
		rmcResponseStream.WriteUInt64LE(info.dataID)
		rmcResponseStream.WriteStructure(info.getInfo)
	}
""",
                    1,
                )
                changed = True
            if changed:
                with open(object_infos_path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.manager.setup_log.append("[OK] Patched Super Mario Maker object info download URLs.")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not patch SMM object info download URLs: {e}")

        for cfg_name, marker in (
            ("get_application_config.go", "SMM GetApplicationConfig: applicationID=%d values=%d"),
            ("get_application_config_string.go", "SMM GetApplicationConfigString: applicationID=%d values=%d"),
        ):
            cfg_path = os.path.join(smm_dir, "nex", "datastore", "super-mario-maker", cfg_name)
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    content = f.read()
                if cfg_name == "get_application_config.go":
                    content = content.replace("var MAX_COURSE_UPLOADS uint32 = 100", "var MAX_COURSE_UPLOADS uint32 = 10")
                    endian_replacements = {
                        "0x01000000, 0x32000000, 0x96000000, 0x2c010000, 0xf4010000,": "0x00000001, 0x00000032, 0x00000096, 0x0000012c, 0x000001f4,",
                        "0x20030000, 0x14050000, 0xd0070000, 0xb80b0000, 0x88130000,": "0x00000320, 0x00000514, 0x000007d0, 0x00000bb8, 0x00001388,",
                        "MAX_COURSE_UPLOADS, 0x14000000, 0x1e000000, 0x28000000, 0x32000000,": "MAX_COURSE_UPLOADS, 0x00000014, 0x0000001e, 0x00000028, 0x00000032,",
                        "0x3c000000, 0x46000000, 0x50000000, 0x5a000000, 0x64000000,": "0x0000003c, 0x00000046, 0x00000050, 0x0000005a, 0x00000064,",
                        "0x23000000, 0x4b000000, 0x23000000, 0x4b000000, 0x32000000,": "0x00000023, 0x0000004b, 0x00000023, 0x0000004b, 0x00000032,",
                        "0x00000000, 0x03000000, 0x03000000, 0x64000000, 0x06000000,": "0x00000000, 0x00000003, 0x00000003, 0x00000064, 0x00000006,",
                        "0x01000000, 0x60000000, 0x05000000, 0x60000000, 0x00000000,": "0x00000001, 0x00000060, 0x00000005, 0x00000060, 0x00000000,",
                        "0xe4070000, 0x01000000, 0x01000000, 0x0c000000, 0x00000000,": "0x000007e4, 0x00000001, 0x00000001, 0x0000000c, 0x00000000,",
                        "0x02000000, // * 2": "2, // * 2",
                        "0x70cc8269, // * 1770179696": "1770179696, // * 1770179696",
                        "0x50cc8269, // * 1770179664": "1770179664, // * 1770179664",
                        "0x38cc8269, // * 1770179640": "1770179640, // * 1770179640",
                        "0xdbd08269, // * 1770180827": "1770180827, // * 1770180827",
                        "0xa9d08269, // * 1770180777": "1770180777, // * 1770180777",
                        "0x89d08269, // * 1770180745": "1770180745, // * 1770180745",
                        "0x59c48269, // * 1770177625": "1770177625, // * 1770177625",
                        "0x36c48269, // * 1770177590": "1770177590, // * 1770177590",
                        "0xdf070000, 0x0c000000, 0x16000000, 0x05000000, 0x00000000": "0x000007df, 0x0000000c, 0x00000016, 0x00000005, 0x00000000",
                    }
                    for old_value, new_value in endian_replacements.items():
                        content = content.replace(old_value, new_value)
                if marker not in content:
                    if cfg_name == "get_application_config.go":
                        content = content.replace(
                            "\trmcResponseStream := nex.NewStreamOut(globals.SecureServer)\n",
                            '\tglobals.Logger.Infof("SMM GetApplicationConfig: applicationID=%d values=%d", applicationID, len(config))\n\n\trmcResponseStream := nex.NewStreamOut(globals.SecureServer)\n',
                            1,
                        )
                    else:
                        content = content.replace(
                            "\trmcResponseStream := nex.NewStreamOut(globals.SecureServer)\n",
                            '\tglobals.Logger.Infof("SMM GetApplicationConfigString: applicationID=%d values=%d", applicationID, len(config))\n\n\trmcResponseStream := nex.NewStreamOut(globals.SecureServer)\n',
                            1,
                        )
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        f.write(content)
            except Exception:
                pass

        config_string_path = os.path.join(smm_dir, "nex", "datastore", "super-mario-maker", "get_application_config_string.go")
        try:
            self._write_file(config_string_path, """package nex_datastore_super_mario_maker

import (
	"fmt"

	nex "github.com/PretendoNetwork/nex-go"
	datastore_super_mario_maker "github.com/PretendoNetwork/nex-protocols-go/datastore/super-mario-maker"
	"github.com/PretendoNetwork/super-mario-maker-secure/globals"
)

func GetApplicationConfigString(err error, packet nex.PacketInterface, callID uint32, applicationID uint32) uint32 {
	if err != nil {
		globals.Logger.Error(err.Error())
		return nex.Errors.DataStore.Unknown
	}

	client := packet.Sender()

	config := make([]string, 0)

	switch applicationID {
	case 128:
		config = getApplicationConfigString_WordBlacklist1()
	case 129:
		config = getApplicationConfigString_WordBlacklist2()
	case 130:
		config = getApplicationConfigString_WordBlacklist3()
	default:
		fmt.Printf("[Warning] DataStoreSMMProtocol::GetApplicationConfigString Unsupported applicationID: %v\\n", applicationID)
	}

	globals.Logger.Infof("SMM GetApplicationConfigString: applicationID=%d values=%d", applicationID, len(config))

	rmcResponseStream := nex.NewStreamOut(globals.SecureServer)

	rmcResponseStream.WriteListString(config)

	rmcResponseBody := rmcResponseStream.Bytes()

	rmcResponse := nex.NewRMCResponse(datastore_super_mario_maker.ProtocolID, callID)
	rmcResponse.SetSuccess(datastore_super_mario_maker.MethodGetApplicationConfigString, rmcResponseBody)

	rmcResponseBytes := rmcResponse.Bytes()

	responsePacket, _ := nex.NewPacketV1(client, nil)

	responsePacket.SetVersion(1)
	responsePacket.SetSource(0xA1)
	responsePacket.SetDestination(0xAF)
	responsePacket.SetType(nex.DataPacket)
	responsePacket.SetPayload(rmcResponseBytes)

	responsePacket.AddFlag(nex.FlagNeedsAck)
	responsePacket.AddFlag(nex.FlagReliable)

	globals.SecureServer.Send(responsePacket)

	return 0
}

func getApplicationConfigString_WordBlacklist1() []string {
	return []string{}
}

func getApplicationConfigString_WordBlacklist2() []string {
	return []string{}
}

func getApplicationConfigString_WordBlacklist3() []string {
	return []string{}
}
""")
        except Exception:
            pass

        buffer_queue_path = os.path.join(smm_dir, "nex", "datastore", "super-mario-maker", "get_buffer_queue.go")
        try:
            with open(buffer_queue_path, "r", encoding="utf-8") as f:
                content = f.read()
            content = content.replace("rmcResponseStream.WriteListQBuffer(pBufferQueue)", "rmcResponseStream.WriteListBuffer(pBufferQueue)")
            with open(buffer_queue_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass

        for path, expr in (
            (os.path.join(third_party, "datastore", "prepare_post_object.go"), "dataID"),
            (os.path.join(third_party, "datastore", "complete_post_objects.go"), "dataID"),
        ):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                content2 = content.replace(
                    f'fmt.Sprintf("%s/%d.bin", commonDataStoreProtocol.s3DataKeyBase, {expr})',
                    f"dataStoreObjectKey({expr})",
                )
                if content2 != content:
                    content2 = content2.replace('\t"fmt"\n', "")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content2)
                    self.manager.setup_log.append("[OK] Patched Super Mario Maker datastore S3 object key normalization.")
            except Exception as e:
                self.manager.setup_log.append(f"[WARN] Could not patch SMM datastore S3 object key normalization: {e}")

        update_completed_path = os.path.join(smm_dir, "database", "datastore", "update_object_upload_completed_by_data_id.go")
        try:
            with open(update_completed_path, "r", encoding="utf-8") as f:
                content = f.read()
            old = "SELECT update_password FROM datastore.objects WHERE data_id=$1 AND deleted=FALSE"
            new = "SELECT under_review FROM datastore.objects WHERE data_id=$1 AND deleted=FALSE"
            if old in content:
                content = content.replace(old, new, 1)
                with open(update_completed_path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.manager.setup_log.append("[OK] Patched Super Mario Maker upload completion flag update.")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not patch SMM upload completion flag update: {e}")

        init_postgres_path = os.path.join(smm_dir, "database", "init_postgres.go")
        try:
            with open(init_postgres_path, "r", encoding="utf-8") as f:
                content = f.read()
            changed = False
            if "ensureOpenDockStarterCourseExists()" not in content:
                content = content.replace(
                    "\tensureEventCourseMetaDataFileExists()\n}",
                    "\tensureEventCourseMetaDataFileExists()\n\tensureOpenDockStarterCourseExists()\n}",
                    1,
                )
                content = content.rstrip() + r'''

func ensureOpenDockStarterCourseExists() {
	const courseDataID uint64 = 940100

	bucket := os.Getenv("PN_SMM_CONFIG_S3_BUCKET")
	key := "940100.bin"

	_, err := globals.S3ObjectSize(bucket, key)
	if err != nil {
		_, err = globals.MinIOClient.PutObject(context.TODO(), bucket, key, strings.NewReader(""), 0, minio.PutObjectOptions{ContentType: "application/octet-stream"})
		if err != nil {
			globals.Logger.Warningf("Could not seed starter SMM course object in S3: %s", err.Error())
			return
		}
	}

	now := time.Now()
	_, err = Postgres.Exec(`INSERT INTO datastore.objects (
		data_id, upload_completed, deleted, under_review, owner, size, name, data_type,
		meta_binary, permission, permission_recipients, delete_permission, delete_permission_recipients,
		flag, period, refer_data_id, tags, persistence_slot_id, extra_data, access_password,
		update_password, creation_date, update_date
	) VALUES (
		$1, TRUE, FALSE, FALSE, $2, 4, $3, 3, $4, 0, $5, 3, $6,
		0, 90, 0, $7, 0, $8, 0, 0, $9, $9
	) ON CONFLICT (data_id) DO UPDATE SET
		upload_completed=TRUE, deleted=FALSE, under_review=FALSE, owner=$2, size=4,
		name=$3, data_type=3, permission=0, delete_permission=3, flag=0, period=90,
		update_date=$9`,
		courseDataID, 1337, "Starter Course", []byte{}, pq.Array([]uint32{}),
		pq.Array([]uint32{}), pq.Array([]string{"opendock"}), pq.Array([]string{}), now)
	if err != nil {
		globals.Logger.Warningf("Could not seed starter SMM course object in Postgres: %s", err.Error())
		return
	}

	for _, applicationID := range []uint32{0, 300000000, 300002400} {
		_, err = Postgres.Exec(`INSERT INTO datastore.object_custom_rankings (data_id, application_id, value)
			VALUES ($1, $2, 0)
			ON CONFLICT (data_id, application_id) DO UPDATE SET value=0`, courseDataID, applicationID)
		if err != nil {
			globals.Logger.Warningf("Could not seed starter SMM ranking: %s", err.Error())
			return
		}
	}

	_, err = Postgres.Exec(`INSERT INTO datastore.object_ratings (
		data_id, slot, flag, internal_flag, lock_type, initial_value, range_min,
		range_max, period_hour, period_duration, total_value, count
	) VALUES ($1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
	ON CONFLICT (data_id, slot) DO UPDATE SET total_value=0, count=0, initial_value=0`, courseDataID)
	if err != nil {
		globals.Logger.Warningf("Could not seed starter SMM rating: %s", err.Error())
		return
	}

	_, err = Postgres.Exec(`INSERT INTO datastore.course_records (
		data_id, slot, first_pid, best_pid, best_score, creation_date, update_date
	) VALUES ($1, 0, 1337, 1337, 0, $2, $2)
	ON CONFLICT (data_id, slot) DO UPDATE SET first_pid=1337, best_pid=1337, best_score=0, update_date=$2`, courseDataID, now)
	if err != nil {
		globals.Logger.Warningf("Could not seed starter SMM course record: %s", err.Error())
		return
	}

	_, err = Postgres.Exec(`INSERT INTO datastore.buffer_queues (
		data_id, slot, creation_date, buffer
	)
	SELECT data_id, 0, $1, $2
	FROM datastore.objects
	WHERE data_type=1 AND deleted=FALSE
	ON CONFLICT (data_id, slot, buffer) DO UPDATE SET creation_date=$1`, now, []byte{0x44, 0x58, 0x0e, 0x00, 0x00, 0x00, 0x00, 0x00})
	if err != nil {
		globals.Logger.Warningf("Could not seed starter SMM maker buffer queue: %s", err.Error())
		return
	}

	globals.Logger.Success("Open Dock starter SMM Course World seed is ready")
}
'''
                changed = True
            if changed:
                with open(init_postgres_path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.manager.setup_log.append("[OK] Patched Super Mario Maker starter Course World seed.")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not patch SMM starter Course World seed: {e}")

        attach_complete_path = os.path.join(smm_dir, "nex", "datastore", "super-mario-maker", "complete_attach_file.go")
        try:
            with open(attach_complete_path, "r", encoding="utf-8") as f:
                content = f.read()

            changed = False
            if '"context"' not in content:
                content = content.replace('import (\n', 'import (\n\t"context"\n', 1)
                changed = True
            if '"strings"' not in content:
                content = content.replace('\t"os"\n', '\t"os"\n\t"strings"\n', 1)
                changed = True
            if '"github.com/minio/minio-go/v7"' not in content:
                content = content.replace(
                    '\t"github.com/PretendoNetwork/super-mario-maker-secure/globals"\n',
                    '\t"github.com/PretendoNetwork/super-mario-maker-secure/globals"\n\t"github.com/minio/minio-go/v7"\n',
                    1,
                )
                changed = True

            old_failed_attach = """	// TODO - What is param.IsSuccess? Is this correct?
	if !param.IsSuccess {
		return nex.Errors.DataStore.InvalidArgument
	}
"""
            new_failed_attach = """	if !param.IsSuccess {
		errCode := datastore_db.DeleteObjectByDataID(param.DataID)
		if errCode != 0 {
			return errCode
		}

		rmcResponse := nex.NewRMCResponse(datastore_super_mario_maker.ProtocolID, callID)
		rmcResponse.SetSuccess(datastore_super_mario_maker.MethodCompleteAttachFile, []byte{})

		responsePacket, _ := nex.NewPacketV1(client, nil)
		responsePacket.SetVersion(1)
		responsePacket.SetSource(0xA1)
		responsePacket.SetDestination(0xAF)
		responsePacket.SetType(nex.DataPacket)
		responsePacket.SetPayload(rmcResponse.Bytes())
		responsePacket.AddFlag(nex.FlagNeedsAck)
		responsePacket.AddFlag(nex.FlagReliable)

		globals.SecureServer.Send(responsePacket)

		return 0
	}
"""
            if old_failed_attach in content:
                content = content.replace(old_failed_attach, new_failed_attach, 1)
                changed = True

            old_s3_size_check = """	objectSizeS3, err := globals.S3ObjectSize(bucket, key)
	if err != nil {
		globals.Logger.Error(err.Error())
		return nex.Errors.DataStore.NotFound
	}

	objectSizeDB, errCode := datastore_db.GetObjectSizeDataID(param.DataID)
	if errCode != 0 {
		return errCode
	}

	if objectSizeS3 != uint64(objectSizeDB) {
		// TODO - Is this a good error?
		return nex.Errors.DataStore.Unknown
	}
"""
            new_s3_size_check = """	objectSizeDB, errCode := datastore_db.GetObjectSizeDataID(param.DataID)
	if errCode != 0 {
		return errCode
	}

	if objectSizeDB == 0 {
		_, err := globals.MinIOClient.PutObject(context.TODO(), bucket, key, strings.NewReader(""), 0, minio.PutObjectOptions{ContentType: "image/jpeg"})
		if err != nil {
			globals.Logger.Error(err.Error())
			return nex.Errors.DataStore.Unknown
		}
	} else {
		objectSizeS3, err := globals.S3ObjectSize(bucket, key)
		if err != nil {
			globals.Logger.Error(err.Error())
			return nex.Errors.DataStore.NotFound
		}

		if objectSizeS3 != uint64(objectSizeDB) {
			// TODO - Is this a good error?
			return nex.Errors.DataStore.Unknown
		}
	}
"""
            if old_s3_size_check in content:
                content = content.replace(old_s3_size_check, new_s3_size_check, 1)
                changed = True

            if changed:
                with open(attach_complete_path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.manager.setup_log.append("[OK] Patched Super Mario Maker attach-file upload completion.")
        except Exception as e:
            self.manager.setup_log.append(f"[WARN] Could not patch SMM attach-file upload completion: {e}")

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
                                           "wiiu-chat-authentication", "wiiu-chat-secure", "pokken-tournament",
                                           "super-mario-3d-world-secure"]:
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
                    if current_section == "depends_on" and line.startswith("      - ") and current_service in ["account", "friends", "super-mario-maker", "mario-kart-8", "pikmin-3", "splatoon", "super-smash-bros-wiiu", "super-mario-3d-world-secure", "boss", "mongo-express", "miiverse-api", "juxtaposition-ui", "website"]:
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

    def _patch_nginx_timeouts(self, s_dir):
        """Keep Cemu's long-lived account/friends HTTP sessions from idling out."""
        timeout_block = """keepalive_timeout 3600s;
    keepalive_requests 10000;
    client_body_timeout 3600s;
    client_header_timeout 3600s;
    send_timeout 3600s;
    proxy_connect_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_read_timeout 3600s;"""

        for rel_path in ("config/nginx.conf", "config/nginx-sssl.conf"):
            path = os.path.join(s_dir, rel_path)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()

                old_blocks = [
                    "keepalive_timeout 65;",
                    timeout_block,
                ]
                for old in old_blocks:
                    if old in content:
                        content = content.replace(old, timeout_block, 1)
                        break
                else:
                    content = content.replace("sendfile on;", "sendfile on;\n\n    " + timeout_block, 1)

                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                self.manager.setup_log.append(f"[WARN] Failed to patch {rel_path} timeouts: {e}")

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

        # Friends PRUDP sessions are shared by MK8 and can be quiet long enough
        # for the stock nex-go/v2 heartbeat to falsely clean them up.
        friends_timeout_patches = [
            (
                os.path.join(friends_dir, "nex", "authentication.go"),
                "globals.AuthenticationServer.AccessKey = \"ridfebb9\"",
                "globals.AuthenticationServer.AccessKey = \"ridfebb9\"\n\tglobals.AuthenticationEndpoint.DefaultStreamSettings.MaxSilenceTime = 600000\n\tglobals.AuthenticationEndpoint.DefaultStreamSettings.KeepAliveTimeout = 600000",
            ),
            (
                os.path.join(friends_dir, "nex", "secure.go"),
                "globals.SecureServer.AccessKey = \"ridfebb9\"",
                "globals.SecureServer.AccessKey = \"ridfebb9\"\n\tglobals.SecureEndpoint.DefaultStreamSettings.MaxSilenceTime = 600000\n\tglobals.SecureEndpoint.DefaultStreamSettings.KeepAliveTimeout = 600000",
            ),
        ]
        for timeout_path, timeout_old, timeout_new in friends_timeout_patches:
            if os.path.isfile(timeout_path):
                try:
                    with open(timeout_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    content = content.replace("DefaultStreamSettings.MaxSilenceTime = 120000", "DefaultStreamSettings.MaxSilenceTime = 600000")
                    content = content.replace("DefaultStreamSettings.KeepAliveTimeout = 120000", "DefaultStreamSettings.KeepAliveTimeout = 600000")
                    if "DefaultStreamSettings.MaxSilenceTime" not in content:
                        content = content.replace(timeout_old, timeout_new)
                        with open(timeout_path, "w", encoding="utf-8") as f:
                            f.write(content)
                    else:
                        with open(timeout_path, "w", encoding="utf-8") as f:
                            f.write(content)
                except Exception:
                    pass

        auth_protocol_path = os.path.join(friends_dir, "nex", "register_common_authentication_server_protocols.go")
        if os.path.isfile(auth_protocol_path):
            try:
                with open(auth_protocol_path, "r", encoding="utf-8") as f:
                    content = f.read()
                content = content.replace(
                    "commonTicketGrantingProtocol.SecureServerAccount = globals.SecureEndpoint.ServerAccount",
                    "commonTicketGrantingProtocol.SecureServerAccount = globals.SecureServerAccount",
                )
                with open(auth_protocol_path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception:
                pass

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
                if 'nexServer.SetPingTimeout(120)' not in c:
                    c = c.replace('nexServer.SetAccessKey("25dbf96a")', 'nexServer.SetAccessKey("25dbf96a")\n\tnexServer.SetPingTimeout(120)')
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
            if 'nexServer.SetPingTimeout(120)' not in c:
                c = c.replace('nexServer.SetAccessKey(config.AccessKey)', 'nexServer.SetAccessKey(config.AccessKey)\n\tnexServer.SetPingTimeout(120)')
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
    location = /p01/tasksheet/1//preport {
        default_type application/xml;
        return 200 '<?xml version="1.0" encoding="UTF-8"?><TaskSheet><TitleId>000500001018dc00</TitleId><TaskId>preport</TaskId><ServiceStatus>open</ServiceStatus><Files></Files></TaskSheet>';
    }
    location = /p01/tasksheet/1/preport {
        default_type application/xml;
        return 200 '<?xml version="1.0" encoding="UTF-8"?><TaskSheet><TitleId>000500001018dc00</TitleId><TaskId>preport</TaskId><ServiceStatus>open</ServiceStatus><Files></Files></TaskSheet>';
    }
    location ~ ^/p01/tasksheet/1/[^/]+/CHARA$ {
        default_type application/xml;
        return 200 '<?xml version="1.0" encoding="UTF-8"?><TaskSheet><TitleId>000500001018dc00</TitleId><TaskId>CHARA</TaskId><ServiceStatus>open</ServiceStatus><Files></Files></TaskSheet>';
    }
    location ^~ /super-mario-maker/ {
        proxy_pass http://minio:9000;
        proxy_set_header Host $host;
    }
    location / {
        resolver 8.8.8.8 ipv6=off;
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

            if "super-mario-3d-world-secure:" not in content:
                self.manager.setup_log.append("[System] Injecting super-mario-3d-world-secure into compose.yml...")
                injections.append("""  super-mario-3d-world-secure:
    build: ./repos/super-mario-3d-world-secure
    depends_on:
      - account
      - postgres
    restart: unless-stopped
    ports:
      - 60200:60200/udp
      - 60201:60201/udp
    networks:
      internal:
    dns: 172.20.0.200
    env_file:
      - ./environment/super-mario-3d-world-secure.env
      - ./environment/super-mario-3d-world-secure.local.env""")
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
        services = "pokken-tournament mario-kart-8 super-mario-3d-world-secure boss super-smash-bros-wiiu website friends miiverse-api juxtaposition-ui wiiu-chat-authentication wiiu-chat-secure super-mario-maker splatoon minecraft-wiiu pikmin-3"
        cmd = f"docker compose build {services}"
        pw = self.manager.cached_password
        if not pw and OS_INFO["os"] == "linux":
            pw = self.manager._ask_sudo_password()
        
        if OS_INFO["os"] == "linux" and pw:
            cmd = f"sudo -S {cmd}"
        
        def _on_build_done(c):
            if c == 0:
                if not getattr(self.manager, "_suppress_deploy_complete_popup", False):
                    from PySide6.QtWidgets import QMessageBox
                    QMessageBox.information(self.manager, "Deployment Complete", "The Full Stack Deployment has finished successfully! Your Pretendo environment is fully built.\n\nYou are now ready to Start the Server.")
            else:
                self.manager.setup_log.append(f"[ERROR] Build process failed with status {c}. Verify Docker status.")
            if hasattr(self.manager, "_on_deploy_complete"):
                self.manager._on_deploy_complete(c)

        self.manager._run_command(cmd, self.manager.setup_log, cwd=s_dir, stdin_data=pw, 
                                  on_done=_on_build_done, lock_ui=True)
