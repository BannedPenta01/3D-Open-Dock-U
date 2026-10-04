# 3D-Open-Dock-U
Supported features:

- Creation of Wii U servers.
- Backup System for important Wii U and 3DS files (3DS servers not implemented yet).
- 3 Wii U games are fully supported: Splatoon 1, Smash Wii U and Pokken Tournament, the rest don't work as of now.
- Patch CEMU and Citra forks to be able to connect to anyone's 3D Open Dock U instance (BE CAREFUL not to delete your own 3DS and Wii U online files)
- Made 100% with AI, just in case.

Note: 3D Open Dock U uses Antigravity and Codex AIs in order for all of this to work. Yes this thing actually works

Credits to Pretendo Network for the source code and manual reverse engineering I based the backend on.

https://pretendo.network/

## Easy Mode (for non-technical users)

Use `run-simple.bat` instead of `run.bat`. It opens **one window with 3 steps**:

1. Fill in Username (6-16 chars), invent a NEW password, Mii name. Press **Find automatically** for Cemu.
2. Type any custom Wii U / 3DS **server address + port** and press **Export connection folder**.
   You get a `DockU-Connection-<server>-<port>` folder with exactly two copy-and-pastes:
   - **Paste 1:** copy everything inside `Paste-Into-Cemu` into the Cemu folder
     (network address, your account, console files - `README.txt` walks through it).
   - **Paste 2:** copy the one line from `Paste-Into-3DS-Emulator/web_api_url.txt`
     over the `web_api_url=` line in the 3DS emulator config.
   Running the server on this PC is still there under step 2 as an option:
   one Start Server button downloads the stack from GitHub on first use,
   then starts only the needed services to fit weak PCs.
   While downloading, the button becomes a progress bar; if it fails it turns
   into a bold red FAILURE message showing the key error lines, with Try again.
   Pressing twice does nothing harmful, Docker Desktop is started and waited for
   automatically, and after starting the app checks the containers actually stayed up.
3. Press **Patch Cemu and Play** to patch this PC directly (uses the step 2 address).

To join a friend: paste their address (example `http://192.168.1.40:8070`) into the
"Play on" box first, then press step 3.

Technical notes:
- Fast build is default: core services + Splatoon / Smash Wii U / Pokken only.
  Set environment variable `FULL_BUILD=1` before setup to build every game.
- The advanced 4-tab UI is still available via `run.bat` -> `python -m src.advanced`.
- Backend fix: the missing `mixins/` package (server/vault/utils) is now included,
  and the stray 5.9 MB PostScript file named `os` was removed.
- Local prerequisites: Python 3.11+, Docker Desktop (for the actual servers),
  Git. Your LAN IP is auto-detected; friends on the same network use
  `http://<your-ip>:8070`.

## Project layout

```
run.bat / run-simple.bat   Double-click launchers (Easy Mode / advanced UI)
requirements.txt           Python dependencies (installed into .venv automatically)
host.json                  Saved server list, editable by hand
README.md                  This file
assets/                    logo.png, fonts, preview images
scripts/                   (reserved for helper scripts)
tests/                     test_imports.py - run with:  .venv\Scripts\python -m tests.test_imports
src/                       All application code (launch with python -m src.<name>)
  easy_mode.py             Easy Mode window (run-simple.bat)
  advanced.py              Advanced 4-tab window (run.bat)
  deploy.py                Server stack download / build logic
  patch_emulators.py       Cemu and 3DS config patching + connection-pack builders
  constants.py             App paths, server repo URL, shared styling
  utils.py                 OS / Docker / network detection helpers
  secrets_manager.py       Encrypted local secret storage
  workers.py               Background command runner with colored logs
  dialogs.py               Error popup and password dialogs
  mixins/                  server / vault / utils behaviors shared by both windows
  scripts/run-in-container/postgres-init.sh   Database init used during deploy
```
