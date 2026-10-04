# workers.py
import subprocess
import os
import re
from PySide6.QtCore import QThread, Signal
from src.utils import OS_INFO

class CommandWorker(QThread):
    output = Signal(str)
    finished = Signal(int)
    
    def __init__(self, cmd, cwd=None, stdin_data=None):
        super().__init__()
        self.cmd = cmd
        self.cwd = cwd
        self.stdin_data = stdin_data
        self.process = None
        self.is_running = True

    def run(self):
        try:
            env = os.environ.copy()
            env.pop("LD_PRELOAD", None)
            if "TERM" not in env:
                env["TERM"] = "xterm-256color"

            popen_kwargs = {
                "shell": True,
                "cwd": self.cwd,
                "env": env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.PIPE if self.stdin_data is not None else None,
                "bufsize": 1,
                "universal_newlines": True
            }
            if OS_INFO["os"] == "windows":
                popen_kwargs["creationflags"] = 0x08000000 
                
            self.process = subprocess.Popen(self.cmd, **popen_kwargs)
            
            if self.stdin_data is not None and self.process.stdin:
                self.process.stdin.write(f"{self.stdin_data}\n{self.stdin_data}\n")
                self.process.stdin.flush()
                import time
                time.sleep(0.2)

            while self.is_running:
                line = self.process.stdout.readline()
                if not line and self.process.poll() is not None:
                    break
                if line:
                    self.output.emit(self._colorize_log(line.strip()))
            
            rc = self.process.poll() if self.process.poll() is not None else 0
            self.finished.emit(rc)
        except Exception as e:
            self.output.emit(self._colorize_log(f"ERROR: {e}"))
            self.finished.emit(1)

    def stop(self):
        self.is_running = False
        if self.process:
            self.process.terminate()

    @staticmethod
    def _colorize_log(line):
        """Apply HTML color formatting to log lines based on severity keywords.
        Enhanced with deep Cemu/Citra emulator connection detection."""
        import html as html_mod
        import re
        safe = html_mod.escape(line)
        low = line.lower()
        # Strip ANSI escape codes for keyword detection
        stripped = re.sub(r'\x1b\[[0-9;]*m', '', low)

        # ── Advanced Path/Context Highlighting ──
        # Highlight paths, code locations, and line numbers (e.g., github.com/../file.go:123 or /home/jan/..)
        safe = re.sub(
            r'([a-zA-Z0-9/\._\-\\]+\.[a-zA-Z]{1,4}(?::\d+)?|/[a-zA-Z0-9/\._\-\\]+)',
            r'<b style="color:#eebafa;">\1</b>',
            safe
        )

        # ── Emulator Connection Events (Cemu/Citra) ── Bright Cyan/Magenta ──
        # PRUDP/NEX protocol layer (core Wii U / 3DS online protocol)
        if any(k in stripped for k in [
            'prudp', 'nex ', 'nex/types', 'prudpserver', 'prudp server',
            'prudpendpoint', 'prudp endpoint', 'prudp connection',
            'virtual server', 'secure server', 'authentication server',
            'prudppacket', 'syn packet', 'connect packet', 'data packet',
            'disconnect packet', 'ping packet',
        ]):
            return f"<span style='color:#b392f0;'>{safe}</span>"  # Purple for NEX/PRUDP

        # Emulator authentication and account events
        if any(k in stripped for k in [
            'client connected', 'client disconnected', 'new connection from',
            'connection from', 'accepted connection', 'incoming connection',
            'user login', 'login request', 'logged in', 'register user',
            'pid:', 'principal id', 'nexaccount', 'nex account',
            'kerberos', 'ticket', 'authentication info',
            'access_token', 'oauth', 'grant_type', 'token_type',
            '/v1/api/oauth', '/v1/api/people', '/v1/api/provider',
            'account.nintendo.net', 'friends service', 'matchmake',
            'secure connection', 'station url',
            'gathering', 'get_session_url',
        ]):
            return f"<span style='color:#58a6ff;'>{safe}</span>"  # Blue for auth/account

        # Friends and presence service events
        if any(k in stripped for k in [
            'friend request', 'friend list', 'presence', 'update_presence',
            'get_all_friends', 'add_friend', 'blacklist',
        ]):
            return f"<span style='color:#d2a8ff;'>{safe}</span>"  # Light purple for friends

        # ── Connection Problems (Cemu/Citra specific) ── Red/Orange ──
        if any(k in stripped for k in [
            'invalid kerberos', 'incorrect password', 'bad decrypt',
            'decryption failed', 'ticket validation failed',
            'invalid signature', 'certificate error', 'ssl error',
            'tls handshake', 'certificate verify failed',
            'invalid credentials', 'authentication failed',
            'access denied', 'account locked', 'account banned',
            'access_level', 'nnid', 'pnid not found',
            'pid not found', 'user not found',
            'invalid mac', 'checksum', 'hmac',
            'device_auth', 'serial_number', 'device_cert',
        ]):
            return f"<span style='color:#ff7b72;'>{safe}</span>"  # Soft red for auth failures

        if any(k in stripped for k in [
            'connection timed out', 'timeout', 'timed out',
            'connection reset', 'network unreachable', 'host unreachable',
            'no route to host', 'enetunreach', 'ehostunreach',
            'econnreset', 'epipe', 'broken pipe',
            'read tcp', 'write tcp', 'socket closed',
            'buffer: read exceeds', 'unexpected eof',
            'premature close', 'stream ended', 'connection refused',
            'address already in use', 'eaddrinuse', 'econnrefused',
        ]):
            # Highlight common network error codes specifically
            if 'eaddrinuse' in stripped or 'address already in use' in stripped:
                return f"<span style='color:#ffa657;'><b>⚠️ PORT CONFLICT:</b> {safe}</span>"
            if 'econnrefused' in stripped or 'connection refused' in stripped:
                return f"<span style='color:#ffa657;'><b>🚫 SERVICE DOWN:</b> {safe}</span>"
            return f"<span style='color:#ffa657;'>{safe}</span>"  # Orange for network/timeout

        # ── Standard Success ──
        if any(k in stripped for k in ['[success]', 'success:', 'started', 'created', 'connected', 'ready to accept', ' started on port']):
            return f"<span style='color:#3fb950;'>{safe}</span>"
        # ── Standard Error ──
        elif any(k in stripped for k in ['[error]', 'error:', 'error]', 'fatal:', 'fatal]', 'panic:', 'panic ', 'segmentation', 'module_not_found', 'typeerror', 'exited with code 1', 'exited with code 2', 'connection refused']):
            return f"<span style='color:#f85149;'>{safe}</span>"
        # ── Standard Warning ──
        elif any(k in stripped for k in ['[warn', 'warning', 'deprecated', 'deprecation']):
            return f"<span style='color:#d29922;'>{safe}</span>"
        # ── Standard Info/Debug ──
        elif any(k in stripped for k in ['[info]', '[debug]']):
            return f"<span style='color:#8b949e;'>{safe}</span>"
        return safe
