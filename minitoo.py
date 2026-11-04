#!/usr/bin/env python3
"""MiniToo session monitor: shows Claude Code session status on a Divoom MiniToo.

The Divoom Tiivoo 2 is matched too, as an experiment: community notes describe it as
protocol-identical with the same 160x128 screen, but that is untested here.

Subcommands:
  hook <working|waiting|done|end>   called by Claude Code hooks (reads hook JSON on stdin)
  daemon                            keeps the Bluetooth link open and redraws the screen
  preview [out.png]                 renders the current screen to a PNG without sending it
  send-test                         sends one test screen to the speaker
  pause | resume                    frees / retakes the speaker's control channel
  status                            prints the sessions the monitor knows about
  config [NAME [VALUE | --default]] lists, shows or changes settings (applied within a second)
  break [reset]                     shows the break timer, or restarts it after a real break
  clawd [MINUTES | off]             shows Clawd now for a few minutes (default 5), even while busy

Protocol notes come from the community reverse-engineering work at
https://github.com/bugzmanov/divoom-minitoo (FINDINGS.md). Only commands documented
there as safe on the MiniToo are used.
"""
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
CACHE = HOME / ".cache" / "minitoo"
SESSIONS = CACHE / "sessions"
PAUSED = CACHE / "paused"
STRETCH = CACHE / "break.json"
CLAWD_DEMO = CACHE / "clawd-demo"  # holds the time until which `minitoo clawd` keeps Clawd out
CONFIG = HOME / ".config" / "minitoo" / "config.json"

# name: (default, description). `minitoo config` lists and changes these.
SETTINGS = {
    "break_screen": (True, "show the take-a-break screen after a long stretch of work"),
    "break_after_minutes": (135, "minutes from the first job of a stretch until the break screen"),
    "break_screen_minutes": (5, "minutes the break screen stays up before it clears and the timer starts over; 0 keeps it up"),
    "break_reset_minutes": (60, "minutes with no new job that end the stretch and clear the break screen"),
    "break_sound": ("coffee", 'break screen sound: "coffee" (footsteps, pour, gulp, aaah) or "chime"'),
    "glow": (False, "glow running squares (3 steps, or 2 when the screen is busy); big animations have frozen the speaker"),
    "clawd": (False, "show Clawd, the Claude Code mascot, once nothing has run for a while"),
    "clawd_after_minutes": (20, "minutes with nothing running or waiting before Clawd comes out"),
    "clawd_awake_minutes": (60, "minutes Clawd plays before falling asleep; asleep, the screen updates once a minute"),
    "alert_seconds": (6, "seconds the full-screen NEEDS YOU alert stays up"),
    "done_expire_minutes": (10, "minutes finished sessions stay on screen as grey blocks"),
    "stale_expire_hours": (12, "hours without news before a session is dropped"),
    "no_folder_label": ("This computer", "project name for sessions opened without a folder"),
    "volume": (100, "sound volume, percent of the speaker's current level (0-100)"),
    "quiet_hours": ([], "[start, end] hours with no sounds, e.g. [22, 7]; [] for none"),
    "sound_start": (True, "chime when a session starts or a finished one starts again"),
    "sound_needs_you": (True, "chime when a session is waiting for you"),
    "sound_done": (True, "chime when a session finishes"),
    "sound_break": (True, "play the break sound when the break screen first appears"),
    "mac": ("", "speaker Bluetooth address; empty finds the first paired device in device_names"),
    "device_names": ("MiniToo,Tiivoo", "comma-separated name parts to look for among paired devices, in order of preference; Tiivoo is experimental"),
    "channel": (1, "Bluetooth RFCOMM control channel"),
}
DEFAULTS = {name: default for name, (default, _) in SETTINGS.items()}
LIMITS = {"volume": (0, 100), "break_after_minutes": (1, 24 * 60), "break_screen_minutes": (0, 24 * 60), "break_reset_minutes": (1, 24 * 60),
          "alert_seconds": (0, 600), "done_expire_minutes": (0, 24 * 60), "stale_expire_hours": (1, 24 * 14),
          "channel": (1, 30), "clawd_after_minutes": (1, 24 * 60), "clawd_awake_minutes": (1, 24 * 60)}
CHOICES = {"break_sound": ("coffee", "chime")}

W, H = 160, 128
ORDER = {"waiting": 0, "working": 1, "done": 2}


def find_speaker(cfg):
    """Return the speaker's Bluetooth address: from the config, else the first paired device whose
    name contains one of device_names, trying the names in order."""
    if cfg.get("mac"):
        return cfg["mac"]
    try:
        out = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    devices = []
    for line in out.splitlines():
        parts = line.split(" ", 2)  # "Device XX:XX:XX:XX:XX:XX Divoom MiniToo-Audio"
        if len(parts) == 3 and parts[0] == "Device":
            devices.append((parts[1], parts[2].lower()))
    for want in str(cfg.get("device_names") or DEFAULTS["device_names"]).split(","):
        want = want.strip().lower()
        for mac, name in devices:
            if want and want in name:
                return mac
    return ""


def user_config():
    """Only the settings the user changed."""
    try:
        data = json.loads(CONFIG.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def config():
    return {**DEFAULTS, **user_config()}


def file_mtime(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def parse_setting(name, text):
    """Turn command-line text into a valid value for a setting, or raise ValueError."""
    default = DEFAULTS[name]
    t = text.strip()
    if isinstance(default, bool):
        if t.lower() in ("true", "on", "yes", "1"):
            return True
        if t.lower() in ("false", "off", "no", "0"):
            return False
        raise ValueError("use true or false")
    if isinstance(default, int):
        try:
            v = int(t)
        except ValueError:
            raise ValueError("use a whole number") from None
        lo, hi = LIMITS.get(name, (0, 10 ** 6))
        if not lo <= v <= hi:
            raise ValueError(f"use a number from {lo} to {hi}")
        return v
    if isinstance(default, list):  # quiet_hours
        if t.lower() in ("", "[]", "none", "off"):
            return []
        try:
            v = [int(x) for x in t.strip("[]").replace(",", " ").split()]
        except ValueError:
            raise ValueError("use two hours like 22 7, or none") from None
        if len(v) != 2 or not all(0 <= h <= 23 for h in v):
            raise ValueError("use two hours from 0 to 23, like 22 7")
        return v
    if name in CHOICES and t not in CHOICES[name]:
        raise ValueError("use " + " or ".join(CHOICES[name]))
    return t


def save_user_config(data):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(CONFIG)


# ---------------------------------------------------------------- hook side

def _git(cwd, *args):
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def identify(cwd):
    """Return (project, session label) for a working directory.

    Worktrees are traced back to their repository so parallel sessions share a row.
    """
    label = config()["no_folder_label"]
    if not cwd:
        return label, "session"
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        common = Path(common)
        project = common.parent.name if common.name == ".git" else common.name.removesuffix(".git")
        label = _git(cwd, "branch", "--show-current") or Path(cwd).name
        return project, label
    if "scratch-workspaces" in cwd:
        return label, "session"
    return Path(cwd).name, "main"


def reason_from(message):
    m = (message or "").strip()
    low = m.lower()
    if "permission to use" in low:
        return "PERMISSION: " + m[low.index("permission to use") + len("permission to use"):].strip().rstrip(".").upper()
    if "waiting for your input" in low:
        return "WAITING FOR REPLY"
    return m.upper()[:40] or "NEEDS YOUR INPUT"


def hook(state):
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    sid = str(data.get("session_id") or "unknown")
    path = SESSIONS / f"{sid}.json"
    if state == "end":
        path.unlink(missing_ok=True)
        return
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        rec = None

    now = time.time()
    reason = ""
    if state == "waiting":
        msg = data.get("message", "")
        # The "idle for a while" notification after a finished turn is not a request for attention.
        if rec and rec.get("state") == "done" and "waiting for your input" in msg.lower():
            return
        reason = reason_from(msg)

    if rec is None:
        env, job = identify(data.get("cwd") or os.getcwd())
        rec = {"session": sid, "env": env, "job": job, "state": None, "since": now}
    elif rec.get("state") == state and state != "waiting":
        # Most PreToolUse calls land here; just mark the session alive.
        if now - rec.get("updated", 0) < 60:
            return
    if rec.get("state") != state:
        rec["since"] = now
    rec.update(state=state, reason=reason, updated=now)
    SESSIONS.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec))
    tmp.replace(path)


# ---------------------------------------------------------------- sessions

def load_sessions(cfg):
    out = []
    now = time.time()
    for p in SESSIONS.glob("*.json"):
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        age = now - rec.get("updated", 0)
        if age > cfg["stale_expire_hours"] * 3600 or (
            rec.get("state") == "done" and now - rec.get("since", 0) > cfg["done_expire_minutes"] * 60
        ):
            p.unlink(missing_ok=True)
            continue
        if rec.get("state") in ORDER:
            out.append(rec)
    return out


APP_DIRS = [HOME / ".config" / "Claude", HOME / "Library" / "Application Support" / "Claude"]
if os.environ.get("APPDATA"):
    APP_DIRS.append(Path(os.environ["APPDATA"]) / "Claude")


class AppNames:
    """Project names and titles as the Claude desktop app's sidebar shows them.

    The app keeps a JSON record per Code session holding the Claude Code session id, the
    folder the session was opened on and its title. Sessions opened without a folder run
    in the app's scratch workspaces and are listed under "This computer".
    """

    def __init__(self, cfg):
        self.no_folder = cfg["no_folder_label"]
        self.mtimes, self.names = {}, {}
        self.next_scan = 0.0

    def scan(self):
        self.next_scan = time.time() + 5
        for base in APP_DIRS:
            scratch = base / "scratch-workspaces"
            for p in base.glob("claude-code-sessions/*/*/local_*.json"):
                try:
                    mtime = p.stat().st_mtime
                    if self.mtimes.get(p) == mtime:
                        continue
                    self.mtimes[p] = mtime
                    d = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                sid = d.get("cliSessionId")
                if not sid:
                    continue
                folder = Path(d.get("originCwd") or d.get("cwd") or scratch)
                project = self.no_folder if folder.is_relative_to(scratch) else folder.name
                self.names[sid] = (project, d.get("title") or "")

    def apply(self, sessions):
        if time.time() >= self.next_scan:
            self.scan()
        for s in sessions:
            hit = self.names.get(s["session"])
            if hit:
                s["env"] = hit[0]
                s["job"] = hit[1] or s.get("job", "")
        return sessions


def group(sessions):
    envs = {}
    for s in sessions:
        envs.setdefault(s["env"], []).append(s)
    rows = []
    for name, jobs in envs.items():
        jobs.sort(key=lambda j: (ORDER[j["state"]], j["since"]))
        rows.append({"name": name, "jobs": jobs, "top": jobs[0]["state"]})
    rows.sort(key=lambda r: (ORDER[r["top"]], r["name"].lower()))
    return rows


# ---------------------------------------------------------------- drawing

C = {
    "bg": (11, 15, 20), "bar": (24, 33, 44), "label": (111, 130, 150), "text": (223, 231, 239),
    "head": (159, 179, 200), "ok": (74, 222, 128), "warn": (251, 191, 36), "done": (91, 102, 118),
    "dim": (50, 60, 73), "amber": (245, 165, 36), "black": (17, 17, 17),
    "okbg": (14, 46, 28), "okglow": (36, 112, 66), "donebg": (58, 68, 82), "calm": (125, 211, 252), "calmdk": (60, 46, 34),
}
STATE_COLOR = {"working": C["ok"], "waiting": C["warn"], "done": C["done"]}
_fonts = {}


def font_paths(name):
    """Where DejaVu fonts live on common Linux distributions, then whatever fontconfig finds."""
    for d in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu-sans-fonts",
              "/usr/share/fonts/dejavu-sans-mono-fonts", "/usr/share/fonts/TTF", "/usr/share/fonts/dejavu"):
        yield f"{d}/{name}"
    try:
        style = ":style=Bold" if "Bold" in name else ""
        family = "DejaVu Sans Mono" if "Mono" in name else "DejaVu Sans"
        yield subprocess.run(["fc-match", "-f", "%{file}", family + style],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        pass


def font(size, bold=False, mono=False):
    from PIL import ImageFont
    key = (size, bold, mono)
    if key not in _fonts:
        name = "DejaVuSans" + ("Mono" if mono else "") + ("-Bold" if bold else "") + ".ttf"
        _fonts[key] = ImageFont.load_default()
        for path in font_paths(name):
            try:
                _fonts[key] = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
    return _fonts[key]


def fit(draw, text, f, max_w):
    if draw.textlength(text, font=f) <= max_w:
        return text
    while len(text) > 1 and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text + "…"


def canvas(bg):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"  # crisp pixels, no anti-aliasing
    return img, d


def fmt_elapsed(seconds, short=False):
    m = max(0, int(seconds // 60))
    if m < 60:
        return f"{m}" if short else f"{m}m"
    return f"{min(m // 60, 9)}h"


def text_at(d, box, text, f, fill):
    """Draw text centred in box (x, y, w, h)."""
    x, y, w, h = box
    l, t, r, b = d.textbbox((0, 0), text, font=f)
    d.text((x + (w - (r - l)) / 2 - l, y + (h - (b - t)) / 2 - t), text, font=f, fill=fill)


def mix(a, b, k):
    return tuple(round(p + (q - p) * k) for p, q in zip(a, b))


def draw_cell(d, x, y, w, h, s, now, blink_on, f, glow=0.0):
    """Running: elapsed time in green on a slowly glowing square. Waiting: amber block. Done: grey block."""
    st = s["state"]
    if st == "working":
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=mix(C["okbg"], C["okglow"], glow))
        t = fmt_elapsed(now - s["since"])
        if d.textlength(t, font=f) > w - 2:
            t = fmt_elapsed(now - s["since"], short=True)
        text_at(d, (x, y, w, h), t, f, C["ok"])
    elif st == "waiting":
        if blink_on:
            d.rectangle([x, y, x + w - 1, y + h - 1], fill=C["warn"])
            text_at(d, (x, y, w, h), "!", f, C["black"])
        else:
            d.rectangle([x, y, x + w - 1, y + h - 1], outline=C["warn"])
    else:
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C["donebg"])


def header(d, sessions):
    d.rectangle([0, 0, W - 1, 12], fill=C["bar"])
    f, fs = font(9, True), font(8, True)
    d.text((3, 1), time.strftime("%H:%M"), font=f, fill=C["head"])
    working = sum(s["state"] == "working" for s in sessions)
    waiting = sum(s["state"] == "waiting" for s in sessions)
    parts = []
    if waiting:
        parts += [(str(waiting), C["warn"]), (" WAIT   ", C["label"])]
    parts += [(str(working), C["ok"] if working else C["label"]), (" RUN", C["label"])]
    x = W - 3 - sum(d.textlength(t, font=fs) for t, _ in parts)
    for t, col in parts:
        d.text((x, 2), t, font=fs, fill=col)
        x += d.textlength(t, font=fs)


def draw_overview(sessions, blink_on=True, now=None, glow=0.0):
    """Roomy layout (name over a line of cells) for up to 3 projects, compact rows beyond."""
    now = now or time.time()
    img, d = canvas(C["bg"])
    header(d, sessions)
    rows = group(sessions)
    nf, cf = font(9, True), font(10, True, mono=True)
    if not rows:
        text_at(d, (0, 60, W, 11), "NOTHING RUNNING", font(8, True), C["dim"])
        return img

    def name_color(r):
        return C["warn"] if r["top"] == "waiting" else C["label"] if r["top"] == "done" else C["text"]

    if len(rows) <= 3:
        cw, ch, gap, maxc = 22, 11, 3, 6
        y = 17
        for r in rows:
            d.text((4, y), fit(d, r["name"].upper(), nf, W - 8), font=nf, fill=name_color(r))
            jobs = r["jobs"]
            show = jobs if len(jobs) <= maxc else jobs[: maxc - 1]
            x, cy = 4, y + 13
            for j in show:
                draw_cell(d, x, cy, cw, ch, j, now, blink_on, cf, glow)
                x += cw + gap
            if len(show) < len(jobs):
                text_at(d, (x, cy, cw, ch), f"+{len(jobs) - len(show)}", cf, C["label"])
            y += 13 + ch + 7
        return img

    cw, ch, gap, maxc, row_h, max_rows = 22, 11, 2, 3, 16, 7
    nf = font(8, True)
    shown = rows if len(rows) <= max_rows else rows[: max_rows - 1]
    for i, r in enumerate(shown):
        y = 16 + i * row_h
        jobs = r["jobs"]
        show = jobs if len(jobs) <= maxc else jobs[: maxc - 1]
        n = len(show) + (len(show) < len(jobs))
        x0 = W - 3 - n * (cw + gap) + gap
        for k, j in enumerate(show):
            draw_cell(d, x0 + k * (cw + gap), y + 1, cw, ch, j, now, blink_on, cf, glow)
        if len(show) < len(jobs):
            text_at(d, (x0 + len(show) * (cw + gap), y + 1, cw, ch), f"+{len(jobs) - len(show)}", cf, C["label"])
        text_y = y + 1 + (ch - 9) // 2
        d.text((4, text_y), fit(d, r["name"].upper(), nf, x0 - 8), font=nf, fill=name_color(r))
    if len(shown) < len(rows):
        n = len(rows) - len(shown)
        d.text((4, 16 + (max_rows - 1) * row_h), f"+{n} MORE PROJECT{'S' if n > 1 else ''}", font=nf, fill=C["label"])
    return img


def draw_alert(s, sessions):
    img, d = canvas(C["amber"])
    d.rectangle([74, 8, 85, 27], fill=C["black"])
    d.rectangle([74, 31, 85, 36], fill=C["black"])

    def center(text, y, f, fill=C["black"]):
        text = fit(d, text, f, W - 8)
        d.text(((W - d.textlength(text, font=f)) / 2, y), text, font=f, fill=fill)

    center("NEEDS YOU", 42, font(17, True))
    center(s["env"].upper(), 66, font(9, True))
    center(s["job"], 78, font(9))
    center(s.get("reason") or "", 92, font(8, True))
    others = sum(x["state"] == "working" for x in sessions if x is not s)
    d.rectangle([0, 112, W - 1, H - 1], fill=C["black"])
    center(f"{others} OTHER{'S' if others != 1 else ''} RUNNING" if others else "NOTHING ELSE RUNNING",
           114, font(8, True), C["amber"])
    return img


def fmt_span(seconds):
    m = max(0, int(seconds // 60))
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h" if m % 60 == 0 else f"{m // 60}h {m % 60:02d}m"


def draw_break(sessions, stretch_start, finished_today=0, reset_minutes=60, now=None):
    """Shown after a long stretch of work, until no new job has come in for a while."""
    now = now or time.time()
    img, d = canvas(C["bg"])
    header(d, sessions)
    text_at(d, (0, 17, W, 16), "TAKE A BREAK", font(14, True), C["calm"])
    # pixel mug
    x, y = 67, 46
    d.rectangle([x, y, x + 21, y + 24], fill=C["head"])
    d.rectangle([x + 22, y + 5, x + 27, y + 7], fill=C["head"])
    d.rectangle([x + 25, y + 5, x + 27, y + 17], fill=C["head"])
    d.rectangle([x + 22, y + 15, x + 27, y + 17], fill=C["head"])
    d.rectangle([x + 3, y + 3, x + 18, y + 6], fill=C["calmdk"])
    for sx, top in ((5, 9), (11, 12), (17, 9)):
        d.rectangle([x + sx, y - top, x + sx + 1, y - top + 5], fill=C["label"])
    d.rectangle([x - 6, y + 25, x + 33, y + 26], fill=C["dim"])
    f = font(8, True)
    text_at(d, (0, 84, W, 11), f"WORKING FOR {fmt_span(now - stretch_start)}", f, C["text"])
    text_at(d, (0, 97, W, 11), f"{finished_today} RUN{'S' if finished_today != 1 else ''} FINISHED TODAY", f, C["label"])
    text_at(d, (0, 112, W, 11), f"CLEARS AFTER {fmt_span(reset_minutes * 60)} QUIET", f, C["dim"])
    return img


GLOW = [1.0, 0.5, 0.0]  # bright, middle, dim; 8-frame animations froze the speaker, so keep this short
GLOW_SHORT = [1.0, 0.0]  # used when the 3-step glow would be too big
# Uploads of 9.5 KB and 20 KB have stalled the speaker; about 7.5 KB has been fine.
MAX_ANIMATION_BYTES = 7600
ANIMATION_GAP = 10  # seconds between animation uploads
CLAWD_EVERY, CLAWD_ASLEEP_EVERY = 10, 60  # seconds between Clawd's clips, awake and asleep
CLAWD_SIM = 3.0  # seconds of Clawd's life per clip, so it moves on calmly between uploads


def compose(sessions, alert, stretch_start=None, finished_today=0, reset_minutes=60, glow=None, blink=True):
    """Return (frames, frame_ms, mode) for the current state.

    stretch_start is set only when a break is due. glow is a list of glow levels, one per frame.
    """
    if alert:
        return [draw_alert(alert, sessions)], 1000, "alert"
    waiting = any(s["state"] == "waiting" for s in sessions)
    if stretch_start and not waiting:
        return [draw_break(sessions, stretch_start, finished_today, reset_minutes)], 1000, "break"
    if glow and any(s["state"] == "working" for s in sessions):
        # Waiting blocks are off on the first frame only, so the last frame is "on"
        # in case the device plays animations only once.
        return [draw_overview(sessions, not waiting or i > 0, glow=g)
                for i, g in enumerate(glow)], 600, "overview"
    if waiting and blink:
        return [draw_overview(sessions, False), draw_overview(sessions, True)], 600, "overview"
    return [draw_overview(sessions)], 1000, "overview"


# ---------------------------------------------------------------- protocol

def frame(op, args=b""):
    body = (len(args) + 3).to_bytes(2, "little") + bytes([op]) + bytes(args)
    return b"\x01" + body + (sum(body) & 0xFFFF).to_bytes(2, "little") + b"\x02"


def animation_payload(images, frame_ms, quality=80):
    blob = bytearray([0x23, len(images)]) + frame_ms.to_bytes(2, "big") + bytes([0x08, 0x0A])
    for img in images:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        jpg = buf.getvalue()
        blob += b"\x01" + len(jpg).to_bytes(4, "big") + jpg
    return bytes(blob)


class MiniToo:
    def __init__(self, mac, channel):
        self.mac, self.channel = mac, channel
        self.sock = None
        self.replies = []
        self.cv = threading.Condition()

    def connect(self):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        s.settimeout(10)
        s.connect((self.mac, self.channel))
        s.settimeout(None)
        self.sock = s
        threading.Thread(target=self._reader, args=(s,), daemon=True).start()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    @property
    def connected(self):
        return self.sock is not None

    def _reader(self, s):
        buf = bytearray()
        while True:
            try:
                chunk = s.recv(1024)
            except OSError:
                chunk = b""
            if not chunk:
                if self.sock is s:
                    self.sock = None
                with self.cv:
                    self.cv.notify_all()
                return
            buf += chunk
            while True:
                start = buf.find(1)
                if start < 0:
                    buf.clear()
                    break
                del buf[:start]
                if len(buf) < 3:
                    break
                total = int.from_bytes(buf[1:3], "little") + 4
                if total < 7 or total > 4096:
                    del buf[:1]
                    continue
                if len(buf) < total:
                    break
                data = bytes(buf[3:total - 3])
                del buf[:total]
                with self.cv:
                    self.replies.append(data)
                    self.cv.notify_all()

    def send(self, op, args=b""):
        if not self.sock:
            raise ConnectionError("not connected")
        self.sock.sendall(frame(op, args))

    def wait_for(self, pred, timeout):
        end = time.time() + timeout
        with self.cv:
            while True:
                for i, r in enumerate(self.replies):
                    if pred(r):
                        return self.replies.pop(i)
                left = end - time.time()
                if left <= 0 or not self.sock:
                    return None
                self.cv.wait(left)

    def upload_animation(self, payload, pacing=0.012):
        """Live animation upload (opcode 0x8B): announce, then 256-byte chunks."""
        total = len(payload).to_bytes(4, "little")
        with self.cv:
            self.replies.clear()
        for _ in range(3):  # the speaker may still be busy with the previous animation
            self.send(0x8B, b"\x00" + total)
            if self.wait_for(lambda r: r[:4] == b"\x04\x8b\x55\x00", 3):
                break
        else:
            raise TimeoutError("speaker did not accept the upload")
        chunks = [payload[i:i + 256] for i in range(0, len(payload), 256)]

        def send_chunk(idx):
            self.send(0x8B, b"\x01" + total + idx.to_bytes(2, "little") + chunks[idx])

        for idx in range(len(chunks)):
            send_chunk(idx)
            time.sleep(pacing)
        # Answer any chunk the speaker asks for again.
        deadline = time.time() + 0.8
        while time.time() < deadline:
            req = self.wait_for(lambda r: r[:4] == b"\x04\x8b\x55\x01", deadline - time.time())
            if req is None:
                break
            idx = int.from_bytes(req[4:6], "little")
            if idx < len(chunks):
                send_chunk(idx)
                deadline = time.time() + 0.8
        return len(chunks)


# ---------------------------------------------------------------- sound

SOUNDS = Path(__file__).resolve().parent / "sounds"


def make_chime(path, notes, volume=0.45):
    """Write a short sine chime. Leading silence gives Bluetooth audio time to wake."""
    import math
    import struct
    import wave
    rate = 44100
    samples = [0] * int(rate * 0.35)
    for freq, dur in notes:
        n = int(rate * dur)
        for i in range(n):
            env = min(1.0, i / (rate * 0.01)) * math.exp(-4.0 * i / n)
            samples.append(int(32767 * volume * env * math.sin(2 * math.pi * freq * i / rate)))
    samples += [0] * int(rate * 0.1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", s) for s in samples))


def ensure_sounds():
    if not (SOUNDS / "start.wav").exists():
        make_chime(SOUNDS / "start.wav", [(523.3, 0.07), (659.3, 0.07), (784, 0.18)], volume=0.3)
    if not (SOUNDS / "needs-you.wav").exists():
        make_chime(SOUNDS / "needs-you.wav", [(880, 0.16), (1318.5, 0.32)])
    if not (SOUNDS / "all-done.wav").exists():
        make_chime(SOUNDS / "all-done.wav", [(784, 0.18), (659.3, 0.18), (523.3, 0.4)], volume=0.35)
    if not (SOUNDS / "take-a-break.wav").exists():
        try:
            subprocess.run([sys.executable, "-I", str(SOUNDS.parent / "make_break_sound.py")], timeout=60, check=True)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"could not build the take-a-break sound ({e}); using the chime")


def muted(cfg):
    q = cfg.get("quiet_hours") or []
    if len(q) != 2:
        return False
    h = time.localtime().tm_hour
    start, end = q
    return start <= h < end if start < end else h >= start or h < end


def play(cfg, name):
    """Play a chime on the speaker's own audio output only, leaving the default output alone."""
    if muted(cfg):
        return
    sink = "bluez_output." + cfg["mac"].replace(":", "_")
    try:
        sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=3).stdout
        match = next((ln.split("\t")[1] for ln in sinks.splitlines() if sink in ln), None)
        if not match:
            log(f"chime skipped: speaker audio not connected ({name})")
            return
        volume = max(0, min(100, int(cfg.get("volume", 100))))
        subprocess.Popen(["paplay", f"--device={match}", f"--volume={65536 * volume // 100}", str(SOUNDS / f"{name}.wav")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"chime: {name}")
    except (OSError, subprocess.SubprocessError) as e:
        log(f"chime failed ({e})")


# ---------------------------------------------------------------- daemon

def read_stretch():
    """The break timer: {"start", "last_job", "announced", "shown_at"}, or {} when no stretch is running."""
    try:
        data = json.loads(STRETCH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_stretch(stretch):
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = STRETCH.with_suffix(".tmp")
    tmp.write_text(json.dumps(stretch))
    tmp.replace(STRETCH)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def clawd_demo_until():
    try:
        return float(CLAWD_DEMO.read_text())
    except (OSError, ValueError):
        return 0.0


def daemon():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from clawd import Clawd, is_night
    cfg = config()
    cfg["mac"] = find_speaker(cfg)
    while not cfg["mac"]:
        log("no paired MiniToo or Tiivoo 2 found; pair it in your Bluetooth settings or set \"mac\" in the config")
        time.sleep(30)
        cfg = config()
        cfg["mac"] = find_speaker(cfg)
    dev = MiniToo(cfg["mac"], cfg["channel"])
    SESSIONS.mkdir(parents=True, exist_ok=True)
    ensure_sounds()
    seen_waiting, prev_states = {}, {}
    alert, alert_until = None, 0.0
    last_sig, next_connect, next_upload = None, 0.0, 0.0
    first = True
    finished, finished_day = set(), time.strftime("%Y-%m-%d")
    names = AppNames(cfg)
    glow = cfg["glow"]
    clawd, clawd_clip, next_clawd, clawd_ok, last_busy = None, None, 0.0, True, time.time()
    stretch, stretch_mtime = read_stretch(), file_mtime(STRETCH)
    cfg_mtime = file_mtime(CONFIG)
    log(f"monitor started for {cfg['mac']} channel {cfg['channel']}")
    while True:
        now = time.time()
        if file_mtime(CONFIG) != cfg_mtime:  # `minitoo config` or a hand edit
            cfg_mtime = file_mtime(CONFIG)
            new = config()
            new["mac"] = find_speaker(new) or cfg["mac"]
            if (new["mac"], new["channel"]) != (cfg["mac"], cfg["channel"]):
                dev.close()
                dev, next_connect = MiniToo(new["mac"], new["channel"]), 0.0
            cfg, glow, names, last_sig = new, new["glow"], AppNames(new), None
            log("settings reloaded")
        if file_mtime(STRETCH) != stretch_mtime:  # `minitoo break reset`
            stretch, stretch_mtime = read_stretch(), file_mtime(STRETCH)
            log("break timer changed from the command line")
        if PAUSED.exists():
            if dev.connected:
                dev.close()
                log("paused: control channel released")
                last_sig = None
            time.sleep(1)
            continue

        sessions = names.apply(load_sessions(cfg))
        sound = None  # at most one sound per pass, most important wins
        # A session that just started waiting gets the full-screen alert for a few seconds.
        current = {s["session"]: s for s in sessions if s["state"] == "waiting"}
        for sid, s in current.items():
            if seen_waiting.get(sid) != s["since"]:
                alert, alert_until = s, now + cfg["alert_seconds"]
                if cfg["sound_needs_you"]:
                    sound = "needs-you"
        seen_waiting = {sid: s["since"] for sid, s in current.items()}

        states = {s["session"]: s["state"] for s in sessions}
        started = any(st == "working" and prev_states.get(sid, "done") == "done" for sid, st in states.items())
        finished_now = any(st == "done" and prev_states.get(sid) in ("working", "waiting") for sid, st in states.items())
        prev_states = states
        if alert and (now > alert_until or alert["session"] not in current):
            alert = None

        today = time.strftime("%Y-%m-%d")
        if today != finished_day:
            finished, finished_day = set(), today
        finished |= {(s["session"], s["since"]) for s in sessions
                     if s["state"] == "done" and time.strftime("%Y-%m-%d", time.localtime(s["since"])) == today}
        # Break timer: starts with the first job, ends after an hour with no new job.
        old = dict(stretch)
        if started and not first or first and not stretch and "working" in states.values():
            if stretch.get("start"):
                stretch["last_job"] = now
            else:
                stretch = {"start": now, "last_job": now, "announced": False}
                log("break timer started")
        elif stretch.get("start") and now - stretch["last_job"] >= cfg["break_reset_minutes"] * 60:
            stretch = {}
            log("break timer reset: no new job for a while")
        # The break screen clears itself after a few minutes; the timer then starts over with the next job.
        shown = stretch.get("shown_at")
        if shown and cfg["break_screen_minutes"] and now - shown >= cfg["break_screen_minutes"] * 60:
            stretch = {}
            log(f"break screen cleared after {cfg['break_screen_minutes']} min; the timer starts over with the next job")
        due = cfg["break_screen"] and stretch.get("start") and now - stretch["start"] >= cfg["break_after_minutes"] * 60

        # Clawd comes out once nothing has run for a while, or on demand with `minitoo clawd`.
        if "working" in states.values() or "waiting" in states.values():
            last_busy = now
        idle = now - last_busy
        demo = clawd_demo_until() > now
        if (demo or cfg["clawd"] and idle >= cfg["clawd_after_minutes"] * 60) and not alert and clawd_ok:
            if clawd is None:
                clawd, next_clawd = Clawd(time.time_ns(), is_night(time.localtime().tm_hour), pace=0.45), 0.0
                log("Clawd came out")
            if now >= next_clawd:
                asleep = not demo and idle >= (cfg["clawd_after_minutes"] + cfg["clawd_awake_minutes"]) * 60
                clawd.night = is_night(time.localtime().tm_hour)
                if asleep:
                    clawd.sleep()
                clawd.advance(CLAWD_SIM)
                clawd_clip = clawd.clip()
                next_clawd = now + (CLAWD_ASLEEP_EVERY if asleep else CLAWD_EVERY)
        elif clawd is not None:
            clawd = None
            log("Clawd went home")

        if clawd is not None:
            (frames, ms), mode, quality = clawd_clip, "clawd", 80
            payload = animation_payload(frames, ms)
            if len(payload) > MAX_ANIMATION_BYTES:
                frames = frames[:1]
                payload = animation_payload(frames, ms)
        else:
            # Use the longest animation that stays under the size the speaker has handled.
            for levels, blink, quality in ((GLOW, True, 80), (GLOW, True, 60), (GLOW_SHORT, True, 80),
                                           (GLOW_SHORT, True, 60), (None, True, 80), (None, False, 80)):
                if levels and not glow:
                    continue
                frames, ms, mode = compose(sessions, alert, stretch["start"] if due else None, len(finished),
                                           cfg["break_reset_minutes"], levels, blink)
                payload = animation_payload(frames, ms, quality)
                if len(frames) == 1 or len(payload) <= MAX_ANIMATION_BYTES:
                    break
        if mode == "break":
            stretch.setdefault("shown_at", now)
        if mode == "break" and not stretch.get("announced"):
            stretch["announced"] = True
            if sound is None and cfg["sound_break"]:
                coffee = cfg.get("break_sound") == "coffee" and (SOUNDS / "take-a-break.wav").exists()
                sound = "take-a-break" if coffee else "all-done"
        if stretch != old:
            write_stretch(stretch)
            stretch_mtime = file_mtime(STRETCH)
        if sound is None:
            if finished_now and cfg["sound_done"]:
                sound = "all-done"
            elif started and cfg["sound_start"]:
                sound = "start"
        if sound and not first:
            play(cfg, sound)
        first = False
        sig = (mode, ms, quality, tuple(f.tobytes() for f in frames))

        if not dev.connected and now >= next_connect:
            try:
                dev.connect()
                log("connected")
                last_sig = None
            except OSError as e:
                next_connect = now + 15
                log(f"speaker not reachable ({e}); retrying in 15 s")

        if dev.connected and sig != last_sig and now >= next_upload:
            try:
                n = dev.upload_animation(payload)
                last_sig, next_upload = sig, time.time() + (ANIMATION_GAP if len(frames) > 1 else 3)
                log(f"sent {mode} screen: {len(frames)} frame(s), {len(payload)} bytes, {n} chunks")
            except (OSError, TimeoutError, ConnectionError) as e:
                dev.close()
                if len(frames) > 1 and (glow or mode == "clawd"):
                    # A stalled speaker can freeze on its loading screen; stop animating and let it rest.
                    what = "Clawd" if mode == "clawd" else "glow"
                    if mode == "clawd":
                        clawd, clawd_ok = None, False
                    else:
                        glow = False
                    next_connect = time.time() + 60
                    log(f"send failed ({e}); {what} switched off until the monitor restarts, reconnecting in 60 s")
                else:
                    log(f"send failed ({e}); reconnecting")
                    next_connect = time.time() + 3
        time.sleep(0.5)


# ---------------------------------------------------------------- CLI

def main(argv):
    if not argv:
        print(__doc__)
        return 64
    cmd = argv[0]
    if cmd == "hook" and len(argv) > 1:
        try:
            hook(argv[1])
        except Exception:  # never break Claude Code over a status light
            pass
        return 0
    if cmd == "daemon":
        daemon()
        return 0
    if cmd == "preview":
        cfg = config()
        sessions = AppNames(cfg).apply(load_sessions(cfg))
        frames, _, mode = compose(sessions, None)
        out = argv[1] if len(argv) > 1 else "minitoo-preview.png"
        frames[-1].resize((W * 4, H * 4), 0).save(out)
        print(f"{mode} screen saved to {out}")
        return 0
    if cmd == "send-test":
        cfg = config()
        fake = [
            {"session": "a", "env": "MiniToo test", "job": "main", "state": "working", "since": 0},
            {"session": "b", "env": "MiniToo test", "job": "check", "state": "done", "since": 0},
        ]
        mac = find_speaker(cfg)
        if not mac:
            print("No paired MiniToo or Tiivoo 2 found. Pair it in your Bluetooth settings or set \"mac\" in the config.")
            return 1
        dev = MiniToo(mac, cfg["channel"])
        dev.connect()
        payload = animation_payload([draw_overview(fake)], 1000)
        n = dev.upload_animation(payload)
        print(f"sent test screen: {len(payload)} bytes in {n} chunks")
        time.sleep(1)
        dev.close()
        return 0
    if cmd == "pause":
        CACHE.mkdir(parents=True, exist_ok=True)
        PAUSED.touch()
        print("Paused. The Divoom app can connect to the speaker now. Run 'minitoo resume' to continue.")
        return 0
    if cmd == "resume":
        PAUSED.unlink(missing_ok=True)
        print("Resumed.")
        return 0
    if cmd == "status":
        cfg = config()
        for r in group(AppNames(cfg).apply(load_sessions(cfg))):
            print(r["name"])
            for j in r["jobs"]:
                print(f"  {j['state']:<8} {j['job']}  {j.get('reason', '')}")
        if PAUSED.exists():
            print("(paused)")
        return 0
    if cmd == "config":
        return config_command(argv[1:])
    if cmd == "break":
        return break_command(argv[1:])
    if cmd == "clawd":
        return clawd_command(argv[1:])
    print(__doc__)
    return 64


def clawd_command(args):
    if args and args[0] == "off":
        CLAWD_DEMO.unlink(missing_ok=True)
        print("Clawd went home.")
        return 0
    try:
        minutes = float(args[0]) if args else 5.0
    except ValueError:
        print("Usage: minitoo clawd [MINUTES | off]")
        return 64
    if not 0 < minutes <= 240:
        print("Pick between 1 and 240 minutes.")
        return 64
    CACHE.mkdir(parents=True, exist_ok=True)
    CLAWD_DEMO.write_text(str(time.time() + minutes * 60))
    print(f"Clawd is out for {minutes:g} minutes, even while sessions are running. "
          "Alerts still take over. 'minitoo clawd off' sends it home.")
    if PAUSED.exists():
        print("The monitor is paused; run 'minitoo resume' to see it.")
    return 0


def show_value(v):
    return json.dumps(v)


def config_command(args):
    mine = user_config()
    if not args:
        print(f"Settings ({CONFIG}). * = changed from the default.")
        print("Change one with: minitoo config NAME VALUE    Back to default: minitoo config NAME --default\n")
        for name, (default, desc) in SETTINGS.items():
            value = mine.get(name, default)
            mark = "*" if name in mine and value != default else " "
            print(f"  {name:<20} {show_value(value):<16}{mark} {desc}")
        return 0
    name = args[0]
    if name not in SETTINGS:
        import difflib
        near = difflib.get_close_matches(name, SETTINGS, n=1)
        print(f"No setting called {name!r}." + (f" Did you mean {near[0]}?" if near else "") +
              " Run 'minitoo config' to list them.")
        return 2
    default, desc = SETTINGS[name]
    if len(args) == 1:
        print(f"{name} = {show_value(mine.get(name, default))}   (default {show_value(default)})\n  {desc}")
        return 0
    if args[1] == "--default":
        mine.pop(name, None)
        save_user_config(mine)
        print(f"{name} is back to its default, {show_value(default)}.")
        return 0
    try:
        value = parse_setting(name, " ".join(args[1:]))
    except ValueError as e:
        print(f"Can't set {name}: {e}.")
        return 2
    mine[name] = value
    save_user_config(mine)
    print(f"{name} = {show_value(value)}. The monitor picks this up within a second.")
    return 0


def break_command(args):
    cfg = config()
    if args[:1] == ["reset"]:
        write_stretch({})
        print("Break timer cleared. It starts again with the next job.")
        return 0
    if args:
        print("Usage: minitoo break [reset]")
        return 64
    st = read_stretch()
    if not cfg["break_screen"]:
        print("The break screen is off (minitoo config break_screen true turns it on).")
    if not st.get("start"):
        print("No stretch running. The break timer starts with the next job.")
        return 0
    now = time.time()
    hm = lambda t: time.strftime("%H:%M", time.localtime(t))
    due = st["start"] + cfg["break_after_minutes"] * 60
    clears = st["last_job"] + cfg["break_reset_minutes"] * 60
    print(f"Working since {hm(st['start'])} ({fmt_span(now - st['start'])}).")
    if now < due:
        print(f"Break screen at {hm(due)}, in {fmt_span(due - now)}.")
    else:
        print(f"Break screen due since {hm(due)}.")
    if st.get("shown_at") and cfg["break_screen_minutes"]:
        gone = st["shown_at"] + cfg["break_screen_minutes"] * 60
        print(f"Break screen up since {hm(st['shown_at'])}; it clears by itself at {hm(gone)}.")
    print(f"Last new job at {hm(st['last_job'])}. With no new job, the timer resets at {hm(clears)}.")
    print("Took a real break? Run 'minitoo break reset'.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
