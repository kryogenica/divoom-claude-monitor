# MiniToo Claude Code monitor

A small status light for [Claude Code](https://claude.com/claude-code) on the 160×128 screen of a
[Divoom MiniToo](https://divoom.com/products/minitoo) Bluetooth speaker. Every running session gets a
glowing green square with its running time, grouped by project. A session that needs you turns amber
and plays a chime. After a long stretch of work the speaker tells you to take a break.

<p>
  <img src="docs/overview.gif" width="320" alt="Overview: projects with running, waiting and finished sessions">
  <img src="docs/alert.png" width="320" alt="Full-screen amber alert when a session needs you">
</p>

> This is a community project. It is not made by, endorsed by or affiliated with Divoom or Anthropic.

## What it shows

| Square | Meaning |
|---|---|
| Green number on a slowly glowing square | Running, with how long it has been running (`47m`, `2h`) |
| Blinking amber block | Waiting for you (a permission prompt or a question) |
| Grey block | Finished; clears after 10 minutes |

- **Grouped like the Claude desktop app.** If you use the desktop app, each session is filed under
  the same project name as the app's sidebar, and sessions opened without a folder show as
  *This computer*. Sessions from a plain terminal use their git repository or folder name.
- **Adapts to space.** Up to three projects get a roomy layout. More projects switch to one compact
  row each, and past seven rows the rest are counted as `+N MORE PROJECTS`.
- **Needs-you alert.** When a session starts waiting, the whole screen turns amber for a few seconds
  with the project, the session title and what it is asking for.
- **Take-a-break screen.** The timer starts with the first job. After 2 h 15 m the screen switches to
  a coffee mug and plays a short footsteps-pour-gulp-"aaah" sound. It clears once no new job has come
  in for an hour. Anything that needs you still takes over the screen.

<p>
  <img src="docs/overview-compact.gif" width="320" alt="Compact layout with many projects">
  <img src="docs/break.png" width="320" alt="Take-a-break screen">
</p>

### Sounds

Played through the MiniToo's own speaker only. Your default audio output is never changed.

| Sound | When |
|---|---|
| [start](docs/sounds/start.mp3) | A session starts, or a finished one starts again |
| [needs-you](docs/sounds/needs-you.mp3) | A session is waiting for you |
| [all-done](docs/sounds/all-done.mp3) | A session finishes |
| [take-a-break](docs/sounds/take-a-break.mp3) | The break screen first appears |

All sounds are synthesized from sine waves and noise by the code in this repo, so there are no
recordings to license.

## Requirements

- Linux with BlueZ and PipeWire or PulseAudio (`bluetoothctl`, `pactl`, `paplay`) and systemd.
- Python 3.9+ with Pillow and numpy. On Debian/Ubuntu: `sudo apt install python3-pil python3-numpy`.
- DejaVu fonts (`fonts-dejavu-core`), installed by default on most distributions.
- A Divoom MiniToo paired and connected over Bluetooth.

macOS and Windows are not supported yet.

## Install

1. Pair the MiniToo in your Bluetooth settings and connect it.
2. Close the Divoom phone app. The speaker accepts only one control connection, and the monitor
   needs it.
3. Run:

```bash
git clone https://github.com/kryogenica/minitoo-claude-monitor.git
cd minitoo-claude-monitor
python3 install.py
```

The installer:

- copies the program to `~/.local/share/minitoo-monitor`;
- adds a `minitoo` command to `~/.local/bin`;
- adds five hooks to `~/.claude/settings.json`, keeping a backup as `settings.json.before-minitoo`;
- starts a systemd user service that runs whenever you're logged in.

Sessions start showing up from their next prompt.

To remove everything (your config is kept):

```bash
python3 install.py --uninstall
```

## Everyday use

```bash
minitoo status    # what the monitor knows about
minitoo pause     # free the speaker so the Divoom app can connect
minitoo resume    # take it back
journalctl --user -u minitoo-monitor -f
```

## Settings

Optional. Create `~/.config/minitoo/config.json` with any of these, then run
`systemctl --user restart minitoo-monitor`:

```json
{
  "mac": "",
  "glow": true,
  "break_screen": true,
  "break_after_minutes": 135,
  "break_reset_minutes": 60,
  "break_sound": "coffee",
  "quiet_hours": [22, 7],
  "no_folder_label": "This computer",
  "sound_start": true,
  "sound_needs_you": true,
  "sound_done": true,
  "sound_break": true
}
```

| Setting | Default | What it does |
|---|---|---|
| `mac` | `""` | Speaker address. Empty means the first paired device named "MiniToo". |
| `glow` | `false` | The three-step glow on running squares. See the warning below. |
| `break_screen` | `true` | Turns the take-a-break screen on or off. |
| `break_after_minutes` | `135` | How long after the first job the break screen appears. |
| `break_reset_minutes` | `60` | How long without a new job before the break timer resets. |
| `break_sound` | `"coffee"` | `"coffee"` or `"chime"`. |
| `quiet_hours` | `[]` | `[start, end]` hours with no sounds, e.g. `[22, 7]`. |
| `no_folder_label` | `"This computer"` | Project name for sessions opened without a folder. |
| `sound_*` | `true` | Turn individual sounds off. |

## Be careful with the speaker

The MiniToo's screen protocol is undocumented and the firmware is easy to upset:

- **Keep animations small.** An 8-frame, 20 KB animation froze the screen on a loading spinner, and
  the power button stopped responding until the speaker restarted. Single frames, the 2-frame amber
  blink and the 3-frame glow (about 7.5 KB) have been fine. The glow is off by default. If a glow
  upload ever fails, the monitor switches the glow off by itself and backs off for a minute.
- **Only one opcode is used for drawing:** the live animation upload (`0x8B`). The community notes
  this project relies on report that several other commands (`0x40`, `0xA3`, `0xA4`, `0xAD`, `0xAE`)
  and custom icon uploads (`0x3C`) can brick the device. Don't add them.
- If the screen freezes, run `minitoo pause`, then hold the power button for 10–15 seconds.

## How it works

```
Claude Code hooks ──► minitoo.py hook <state> ──► ~/.cache/minitoo/sessions/<id>.json
                                                          │
systemd user service: minitoo.py daemon ◄─────────────────┘
   ├─ reads the Claude desktop app's session records for project names and titles
   ├─ draws the screen with Pillow, sends it as JPEG frames over Bluetooth RFCOMM
   └─ plays chimes on the speaker's own audio output with paplay
```

The hooks only write a small JSON file and return immediately. All Bluetooth work happens in the
background service, so a missing or busy speaker never slows Claude Code down.

Hook mapping: `UserPromptSubmit` and `PreToolUse` mean running, `Notification` means waiting,
`Stop` means finished, and `SessionEnd` removes the session. If you interrupt a turn with Esc,
Claude Code sends no `Stop`, so that session stays "running" until its next prompt or until you close it.

## Credits

The MiniToo's Bluetooth protocol (RFCOMM channel 1, frame format, the `0x8B` animation upload and the
list of dangerous commands) comes from the reverse-engineering notes in
[bugzmanov/divoom-minitoo](https://github.com/bugzmanov/divoom-minitoo). No code from that project is
included.

## License

[MIT](LICENSE)
