# utils.py
import os
import platform
import subprocess
import socket
import json
import base64
import shutil
import binascii
import time

def _is_windows():
    return platform.system().lower() == "windows"

_CACHED_RESULTS = {}

def _get_cached_or_run(key, func, ttl=30):
    """Generic cache helper for slow subprocess checks."""
    now = time.time()
    if key in _CACHED_RESULTS:
        result, expiry = _CACHED_RESULTS[key]
        if now < expiry:
            return result
    
    res = func()
    _CACHED_RESULTS[key] = (res, now + ttl)
    return res

def _wsl_installed():
    """Check if WSL2 is available on this Windows machine."""
    if not _is_windows():
        return False
    
    def _run():
        try:
            res = subprocess.run(
                ["wsl", "--status"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000
            )
            return res.returncode == 0
        except Exception:
            return False
            
    return _get_cached_or_run("wsl_installed", _run, ttl=300)

def _wsl_distro_installed():
    """Check if at least one WSL distro is installed."""
    if not _is_windows():
        return False

    def _run():
        try:
            res = subprocess.run(
                ["wsl", "-l", "-q"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000
            )
            distros = [d.strip().replace('\x00', '') for d in res.stdout.strip().splitlines() if d.strip().replace('\x00', '')]
            return len(distros) > 0
        except Exception:
            return False
            
    return _get_cached_or_run("wsl_distro_installed", _run, ttl=300)

def _get_default_wsl_distro():
    """Return the name of the default WSL distro, or None."""
    if not _is_windows():
        return None

    def _run():
        try:
            res = subprocess.run(
                ["wsl", "-l", "-v"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000
            )
            for line in res.stdout.replace('\x00', '').splitlines():
                line = line.strip()
                if line.startswith("*"):
                    parts = line[1:].split()
                    if parts:
                        return parts[0]
        except Exception:
            pass
        return None
        
    return _get_cached_or_run("default_wsl_distro", _run, ttl=300)

def _docker_desktop_running():
    """Check if Docker Desktop is running on Windows."""
    if not _is_windows():
        return False
        
    def _run():
        try:
            res = subprocess.run(
                ["powershell", "-Command", "Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000
            )
            return bool(res.stdout.strip())
        except Exception:
            return False
            
    return _get_cached_or_run("docker_desktop_running", _run, ttl=15)

def _docker_available():
    """Check if the docker CLI is available and responsive."""
    def _run():
        try:
            res = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000 if _is_windows() else 0
            )
            return res.returncode == 0
        except Exception:
            return False
            
    return _get_cached_or_run("docker_available", _run, ttl=10)

def _get_running_pretendo_containers(server_dir=None):
    """Check whether the selected Pretendo compose stack has running containers."""
    def _run():
        try:
            if server_dir and os.path.isdir(server_dir):
                has_compose = any(
                    os.path.isfile(os.path.join(server_dir, f))
                    for f in ("compose.yml", "docker-compose.yml", "compose.yaml")
                )
                if has_compose:
                    res = subprocess.run(
                        ["docker", "compose", "ps", "--status", "running", "-q"],
                        cwd=server_dir,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        creationflags=0x08000000 if _is_windows() else 0
                    )
                    return res.returncode == 0 and bool(res.stdout.strip())

            res = subprocess.run(
                ["docker", "ps", "--filter", "name=pretendo", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=0x08000000 if _is_windows() else 0
            )
            return bool(res.stdout.strip())
        except Exception:
            return False
            
    cache_key = f"pretendo_containers_running:{os.path.abspath(server_dir) if server_dir else 'global'}"
    return _get_cached_or_run(cache_key, _run, ttl=2)

def _docker_service_running_win():
    """Check if any Docker-related Windows services are running."""
    if not _is_windows():
        return False
        
    def _run():
        try:
            # Check for common Docker services: 'docker' (Engine) and 'com.docker.service' (Desktop)
            cmd = "Get-Service -Name docker, com.docker.service -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Running' }"
            res = subprocess.run(
                ["powershell", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000
            )
            return bool(res.stdout.strip())
        except Exception:
            return False
            
    return _get_cached_or_run("docker_service_running_win", _run, ttl=15)

def _docker_installed_win():
    """Check if any Docker-related Windows services are installed (even if stopped)."""
    if not _is_windows():
        return False
        
    def _run():
        try:
            # Check for common Docker services: 'docker' (Engine) and 'com.docker.service' (Desktop)
            cmd = "Get-Service -Name docker, com.docker.service -ErrorAction SilentlyContinue"
            res = subprocess.run(
                ["powershell", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000
            )
            return bool(res.stdout.strip())
        except Exception:
            return False
            
    return _get_cached_or_run("docker_installed_win", _run, ttl=300)

def _is_docker_service_active():
    """Check if the docker service is active (Linux/Windows)."""
    if _is_windows():
        return _docker_desktop_running() or _docker_available() or _docker_service_running_win()
        
    def _run():
        try:
            svc = subprocess.run(["systemctl", "is-active", "docker"], capture_output=True, text=True, timeout=3)
            return svc.stdout.strip() == "active"
        except Exception:
            return False
            
    return _get_cached_or_run("docker_service_active", _run, ttl=5)

def _start_docker_desktop():
    """Attempt to start Docker Desktop on Windows."""
    if not _is_windows():
        return False
    try:
        dd_paths = [
            os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Docker", "Docker", "Docker Desktop.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Docker", "Docker Desktop.exe"),
        ]
        for dd in dd_paths:
            if os.path.isfile(dd):
                subprocess.Popen([dd], creationflags=0x00000008)
                return True
        subprocess.Popen(["cmd", "/c", "start", "", "Docker Desktop"], creationflags=0x00000008)
        return True
    except Exception:
        return False

def detect_os_info():
    """Detect OS, package manager, and default emulator paths."""
    system = platform.system().lower()
    info = {"os": system, "pkg_mgr": None, "pkg_install": "",
            "cemu_dir": "", "cemu_data": "", "cemu_settings": "", "citra_config": "", "server_dir": "", "distro": "",
            "has_wsl": False, "has_wsl_distro": False, "wsl_distro": None,
            "has_docker_desktop": False, "docker_available": False}

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
        
        config_cemu = os.path.join(home, ".config/Cemu")
        local_cemu = os.path.join(home, ".local/share/Cemu")
        if os.path.isdir(config_cemu):
            info["cemu_dir"] = config_cemu
            info["cemu_data"] = local_cemu if os.path.isdir(local_cemu) else config_cemu
        else:
            info["cemu_dir"] = local_cemu
            info["cemu_data"] = local_cemu

        info["cemu_settings"] = os.path.join(info["cemu_dir"], "settings.xml") if info["cemu_dir"] else ""

        citra_paths = [
            os.path.join(home, ".config/citra-emu/config/qt-config.ini"),
            os.path.join(home, ".var/app/org.citra_emu.citra/config/citra-emu/config/qt-config.ini"),
            os.path.join(home, ".config/lime-3ds/config/qt-config.ini"),
            os.path.join(home, ".config/azahar-emu/qt-config.ini"),
            os.path.join(home, ".config/EmuDeck/backend/configs/citra-emu/qt-config.ini"),
            os.path.join(home, ".config/EmuDeck/backend/configs/azahar/qt-config.ini"),
        ]
        for p in citra_paths:
            if os.path.exists(p):
                info["citra_config"] = p
                break
    elif system == "windows":
        userprofile = os.environ.get("USERPROFILE", "C:\\Users\\User")
        info["server_dir"] = os.path.join(userprofile, "pretendo-docker")
        info["distro"] = "Windows"
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")

        cemu_candidates = []
        if appdata:
            cemu_candidates.append(os.path.join(appdata, "Cemu"))
        if localappdata:
            cemu_candidates.append(os.path.join(localappdata, "Cemu"))
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
        info["cemu_data"] = found_cemu
        info["cemu_settings"] = os.path.join(found_cemu, "settings.xml") if found_cemu else ""

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
        
        # Slow checks are now cached and should be called on-demand
        # We initialize with safe defaults to ensure instantaneous boot
        info["has_wsl"] = False
        info["has_wsl_distro"] = False
        info["wsl_distro"] = None
        info["has_docker_desktop"] = False
        info["docker_available"] = False
            
    return info

OS_INFO = detect_os_info()
DEFAULT_SERVER_DIR = OS_INFO["server_dir"]
CEMU_DIR = OS_INFO["cemu_dir"]

def _obs(text):
    if not text: return ""
    return base64.b64encode(str(text).encode()).decode()

def _deobs(text):
    if not text: return ""
    try:
        return base64.b64decode(str(text).encode()).decode()
    except:
        return text

def get_local_ip():
    """Robustly find the primary LAN IPv4 address without necessarily requiring internet access."""
    # Method 1: Connect to a non-routable address to let the OS pick the primary interface
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        pass

    # Method 2: Fallback to a common internet address (needs connectivity)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        pass
    
    # Method 3: Use socket.gethostname() (less reliable on some Linux setups)
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127."):
                return ip
    except Exception:
        pass
        
    return "127.0.0.1"

_LAST_INET_CHECK = 0.0
_LAST_INET_RESULT = False

def has_internet_connectivity(timeout=0.25, refresh=5.0):
    """Lightweight cached probe; only rechecks every `refresh` seconds."""
    global _LAST_INET_CHECK, _LAST_INET_RESULT
    now = time.time()
    if now - _LAST_INET_CHECK < refresh:
        return _LAST_INET_RESULT
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.connect(("1.1.1.1", 53))
        sock.close()
        _LAST_INET_RESULT = True
    except OSError:
        _LAST_INET_RESULT = False
    _LAST_INET_CHECK = now
    return _LAST_INET_RESULT

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

def _grep_env_file(filepath, key):
    """Search an env file for a specific key and return its value."""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r") as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.strip().split("=", 1)[1]
    except:
        pass
    return None
