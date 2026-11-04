#!/usr/bin/env python3
"""Install or remove the MiniToo Claude Code monitor for the current user.

  python3 install.py               install (or update) and start the monitor
  python3 install.py --uninstall   stop it, remove the hooks, the service and the program

Installs into ~/.local/share/minitoo-monitor, adds five hooks to ~/.claude/settings.json
(a backup is kept as settings.json.before-minitoo) and runs a systemd user service.
Your config in ~/.config/minitoo/ is never touched.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("HOME", Path.home()))
APP = HOME / ".local" / "share" / "minitoo-monitor"
BIN = HOME / ".local" / "bin" / "minitoo"
UNIT = HOME / ".config" / "systemd" / "user" / "minitoo-monitor.service"
SETTINGS = HOME / ".claude" / "settings.json"
MARK = "minitoo-monitor/minitoo.py"  # identifies our hooks in settings.json

HOOKS = {  # Claude Code hook event -> state the monitor records
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "Notification": "waiting",
    "Stop": "done",
    "SessionEnd": "end",
}

UNIT_TEXT = """[Unit]
Description=MiniToo Claude Code session monitor
After=bluetooth.target pipewire.service

[Service]
ExecStart={python} -I %h/.local/share/minitoo-monitor/minitoo.py daemon
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def say(msg):
    print(f"  {msg}")


def check_requirements():
    ok = True
    probe = subprocess.run([sys.executable, "-I", "-c", "import PIL, numpy"], capture_output=True)
    if probe.returncode:
        print("Pillow and numpy must be importable by this Python (for example: sudo apt install python3-pil python3-numpy).")
        ok = False
    for tool, why in (("bluetoothctl", "find the speaker"), ("pactl", "find its audio output"),
                      ("paplay", "play the chimes"), ("systemctl", "run the monitor in the background")):
        if not shutil.which(tool):
            print(f"Missing '{tool}' (used to {why}).")
            ok = ok and tool in ("pactl", "paplay", "systemctl")  # sounds and the service are optional
    return ok


def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except FileNotFoundError:
        return {}


def without_our_hooks(settings):
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        groups = []
        for g in hooks[event]:
            kept = [h for h in g.get("hooks", []) if MARK not in h.get("command", "")]
            if kept:
                groups.append({**g, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return settings


def write_settings(settings):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(SETTINGS)


def systemctl(*args):
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", *args], check=False)


def install(start=True):
    if not check_requirements():
        return 1
    print("Installing the MiniToo monitor")
    APP.mkdir(parents=True, exist_ok=True)
    for name in ("minitoo.py", "clawd.py", "make_break_sound.py"):
        shutil.copy2(HERE / name, APP / name)
    (APP / "minitoo.py").chmod(0o755)
    say(f"program: {APP}")

    BIN.parent.mkdir(parents=True, exist_ok=True)
    if BIN.is_symlink() or not BIN.exists():
        BIN.unlink(missing_ok=True)
        BIN.symlink_to(APP / "minitoo.py")
        say(f"command: {BIN}")

    if SETTINGS.exists() and not SETTINGS.with_name("settings.json.before-minitoo").exists():
        shutil.copy2(SETTINGS, SETTINGS.with_name("settings.json.before-minitoo"))
        say("backup: ~/.claude/settings.json.before-minitoo")
    settings = without_our_hooks(load_settings())
    hooks = settings.setdefault("hooks", {})
    for event, state in HOOKS.items():
        entry = {"hooks": [{"type": "command", "timeout": 5,
                            "command": f"{sys.executable} -I {APP / 'minitoo.py'} hook {state}"}]}
        if event == "PreToolUse":
            entry = {"matcher": "*", **entry}
        hooks.setdefault(event, []).append(entry)
    write_settings(settings)
    say("hooks: added to ~/.claude/settings.json")

    UNIT.parent.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(UNIT_TEXT.format(python=sys.executable))
    say(f"service: {UNIT}")
    if start and not shutil.which("systemctl"):
        say("no systemd here: start the monitor yourself with 'minitoo daemon'")
    elif start:
        systemctl("daemon-reload")
        systemctl("enable", "minitoo-monitor.service")
        systemctl("restart", "minitoo-monitor.service")
        say("service started; follow it with: journalctl --user -u minitoo-monitor -f")
    print("Done. New and resumed Claude Code sessions will show up on the speaker.")
    return 0


def uninstall():
    print("Removing the MiniToo monitor")
    systemctl("disable", "--now", "minitoo-monitor.service")
    UNIT.unlink(missing_ok=True)
    systemctl("daemon-reload")
    if SETTINGS.exists():
        write_settings(without_our_hooks(load_settings()))
        say("hooks removed from ~/.claude/settings.json")
    if BIN.is_symlink():
        BIN.unlink()
    shutil.rmtree(APP, ignore_errors=True)
    shutil.rmtree(HOME / ".cache" / "minitoo", ignore_errors=True)
    print("Done. Your config in ~/.config/minitoo/ was left in place.")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--uninstall" in args:
        sys.exit(uninstall())
    sys.exit(install(start="--no-start" not in args))
