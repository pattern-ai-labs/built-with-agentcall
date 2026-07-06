#!/usr/bin/env python3
"""Build your notetaker — the one-time setup that assembles YOUR config.

In a terminal (cmd, PowerShell, VS Code, Cursor, Replit, bash...), just run it and answer:
    python build.py

No terminal (an AI agent, CI, a script)? Pass the answers as flags instead — key first:
    python build.py --key ak_ac_... --name Juno --display ring --format md
    python build.py --key ak_ac_... --image ./logo.png        # your own avatar

Either way it writes .env (your key, gitignored) + config.jsonc into the project folder.
After this one-time build you can just edit config.jsonc directly. Powered by AgentCall.
"""

import argparse
import json
import os
import re
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_AVATARS = os.path.join(_PROJECT_ROOT, "avatars")
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


# ── terminal styling (raw ANSI; degrades to plain text when unsupported) ────────
def _enable_ansi():
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            return False
    return True


_ANSI = _enable_ansi()
BOLD, DIM = "1", "2"

# AgentCall brand (truecolor). Unsupported terminals ignore these and fall back
# to default text — readable everywhere, never garbled.
_E = "\033["
_R = _E + "0m"
_INK = _E + "38;2;28;29;26m"        # ink text on the cream card
_MUTE = _E + "38;2;120;118;108m"    # muted text on the cream card
_CREAM = _E + "48;2;243;240;232m"   # #F3F0E8 paper — the card surface
_LIMEBG = _E + "48;2;200;255;58m"   # #C8FF3A lime — the badge surface
_ONLIME = _E + "38;2;12;13;10m"     # near-black text on lime
_CARD_W = 46


def col(code, s):
    return f"\033[{code}m{s}\033[0m" if _ANSI else s


def _emit_card(rows):
    """Render a cream 'paper' card. Brand colors live only here (non-interactive,
    so no input echo to fight); plain text is the graceful fallback."""
    if not _ANSI:
        print()
        for _, plain in rows:
            if plain:
                print("  " + plain)
        print()
        return
    blank = "  " + _CREAM + " " * _CARD_W + _R
    print()
    print(blank)
    for styled, plain in rows:
        pad = " " * max(0, _CARD_W - 2 - len(plain))
        print("  " + _CREAM + _INK + "  " + styled + pad + _R)
    print(blank)
    print()


def _pill(text):
    # lime badge: lime background, near-black bold text, then back to the cream card
    styled = _LIMEBG + _ONLIME + _E + "1m" + " " + text + " " + _R + _CREAM + _INK
    return styled, " " + text + " "


def banner():
    title = _E + "1m" + "▣  N O T E T A K E R" + _R + _CREAM + _INK
    sub = _MUTE + "build it once · powered by agentcall.dev" + _R + _CREAM
    _emit_card([
        _pill("ONE-TIME SETUP"),
        ("", ""),
        (title, "▣  N O T E T A K E R"),
        (sub, "build it once · powered by agentcall.dev"),
    ])


def _done_card(name):
    ready = _E + "1m" + "✓  " + name + " is ready" + _R + _CREAM + _INK
    sub = _MUTE + "config.jsonc + .env are written" + _R + _CREAM
    _emit_card([
        _pill("BUILT"),
        ("", ""),
        (ready, "✓  " + name + " is ready"),
        (sub, "config.jsonc + .env are written"),
    ])


def _prompt():
    try:
        return input("  " + col(BOLD, "›") + " ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Build cancelled.")
        sys.exit(1)


def ask(question, hint="", default=""):
    print(f"\n  {col(BOLD, question)}" + (f"  {col(DIM, hint)}" if hint else ""))
    return _prompt() or default


def _opt_lines(options, idx, width):
    """One rendered line per option; the selected row is a lime bar with a ▸ pointer."""
    out = []
    for i, (key, desc) in enumerate(options):
        if i == idx:
            inner = " ▸ " + key.ljust(11) + desc
            out.append("  " + _LIMEBG + _ONLIME + inner + " " * max(0, width - len(inner)) + _R)
        else:
            out.append("  " + "   " + key.ljust(11) + col(DIM, desc))
    return out


def _read_key():
    """Block for one keypress; return up | down | enter | cancel | num:<n> | other."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):              # arrow / special-key prefix
            return {b"H": "up", b"P": "down"}.get(msvcrt.getch(), "other")
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b"\x03":
            return "cancel"
        return ("num:" + ch.decode()) if ch.isdigit() else "other"
    import select
    ch = sys.stdin.read(1)
    if ch == "\x1b":                              # ESC: arrow sequence, or bare ESC = cancel
        if select.select([sys.stdin], [], [], 0.05)[0] and sys.stdin.read(1) == "[" \
                and select.select([sys.stdin], [], [], 0.05)[0]:
            return {"A": "up", "B": "down"}.get(sys.stdin.read(1), "other")
        return "cancel"
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":
        return "cancel"
    return ("num:" + ch) if ch.isdigit() else "other"


def _choose_arrows(question, options, default):
    idx = next((i for i, (k, _) in enumerate(options) if k == default), 0)
    n = len(options)
    width = 15 + max(len(d) for _, d in options)
    print(f"\n  {col(BOLD, question)}")
    print(col(DIM, "  ↑/↓ move · enter select"))
    for line in _opt_lines(options, idx, width):
        print(line)

    old, fd = None, None
    if os.name != "nt":
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    cancelled = False
    try:
        if old is not None:
            tty.setcbreak(fd)
        sys.stdout.write("\033[?25l")             # hide cursor
        sys.stdout.flush()
        while True:
            k = _read_key()
            if k == "up":
                idx = (idx - 1) % n
            elif k == "down":
                idx = (idx + 1) % n
            elif k.startswith("num:") and 1 <= int(k[4:]) <= n:
                idx = int(k[4:]) - 1
            elif k == "enter":
                break
            elif k == "cancel":
                cancelled = True
                break
            else:
                continue
            sys.stdout.write(f"\033[{n}A")        # back to first row, repaint
            for line in _opt_lines(options, idx, width):
                sys.stdout.write("\033[K" + line + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        cancelled = True
    finally:
        sys.stdout.write("\033[?25h")             # show cursor
        sys.stdout.flush()
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    sys.stdout.write(f"\033[{n + 1}A\033[J")       # collapse the hint + rows
    if cancelled:
        print("\n  Build cancelled.")
        sys.exit(1)
    chosen = options[idx][0]
    print(f"    {col(DIM, '›')} {col(BOLD, chosen)}")
    return chosen


def _choose_typed(question, options, default):
    print(f"\n  {col(BOLD, question)}")
    for i, (key, desc) in enumerate(options, 1):
        mark = col(DIM, "  (default)") if key == default else ""
        print(f"    {col(BOLD, str(i))}  {key.ljust(11)}{col(DIM, desc)}{mark}")
    raw = _prompt()
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1][0]
    for key, _ in options:
        if raw.lower() == key.lower():
            return key
    return default


def choose(question, options, default):
    # Arrow-key picker on a real terminal; falls back to typed input everywhere else.
    if _ANSI and sys.stdin.isatty():
        try:
            return _choose_arrows(question, options, default)
        except Exception as e:
            if os.environ.get("NOTETAKER_DEBUG"):
                print(col(DIM, f"  (arrow picker unavailable: {e} — using typed input)"))
    return _choose_typed(question, options, default)


def slug(name):
    s = "".join(c for c in name.lower() if c.isalnum())
    return s or "brand"


def copy_image(path, name):
    # Strip invisible bidi/zero-width marks that Windows' "Copy as path" prepends,
    # plus surrounding quotes/space, so a pasted path actually resolves.
    path = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", path or "").strip().strip('"').strip("'").strip()
    path = os.path.expanduser(path)
    ext = os.path.splitext(path)[1].lower()
    if not path:
        print("   (no image given — using the Pattern mark)")
        return "pattern"
    if not os.path.isfile(path):
        print("   (couldn't find that file — using the Pattern mark)\n     " + path)
        return "pattern"
    if ext not in _IMG_EXTS:
        print(f"   (that's a '{ext or '?'}' file — use an image: png, jpg, jpeg, gif, webp, or svg. Using the Pattern mark.)")
        return "pattern"
    dest = slug(name)
    try:
        shutil.copyfile(path, os.path.join(_AVATARS, dest + ext))
        print("   " + col(BOLD, "✓") + f" copied your image to avatars/{dest}{ext}")
        return dest
    except Exception as e:
        print(f"   (couldn't copy: {e} — using the Pattern mark)")
        return "pattern"


def set_value(text, key, value):
    # json.dumps escapes quotes/backslashes/control chars so any name stays valid JSON;
    # the value pattern allows backslash-escapes so a re-run matches a previously-escaped value.
    return re.sub(rf'("{key}"\s*:\s*)"(?:[^"\\]|\\.)*"',
                  lambda m: m.group(1) + json.dumps(value, ensure_ascii=False), text, count=1)


def write_config(name, display, fmt):
    p = os.path.join(_PROJECT_ROOT, "config.jsonc")
    with open(p, encoding="utf-8") as f:
        text = f.read()
    text = set_value(text, "BOT_NAME", name)
    text = set_value(text, "DISPLAY", display)
    text = set_value(text, "OUTPUT_FORMAT", fmt)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def _existing_key():
    """A key the notetaker could already use — env var, an existing .env, or
    ~/.agentcall/config.json. Lets the build skip re-asking when one's already set."""
    k = os.environ.get("AGENTCALL_API_KEY", "").strip()
    if k:
        return k
    p = os.path.join(_PROJECT_ROOT, ".env")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("AGENTCALL_API_KEY="):
                        v = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if v:
                            return v
        except OSError:
            pass
    cfg = os.path.join(os.path.expanduser("~"), ".agentcall", "config.json")
    if os.path.isfile(cfg):
        try:
            import json
            with open(cfg, encoding="utf-8") as f:
                v = (json.load(f) or {}).get("api_key", "")
                if v:
                    return v
        except (OSError, ValueError):
            pass
    return ""


def write_env(key):
    """Write/refresh the gitignored .env: set AGENTCALL_API_KEY, keep any other lines."""
    p = os.path.join(_PROJECT_ROOT, ".env")
    keep = []
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                keep = [ln.rstrip("\n") for ln in f
                        if ln.strip() and not ln.strip().startswith("AGENTCALL_API_KEY=")]
        except OSError:
            keep = []
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"AGENTCALL_API_KEY={key}\n")
        for ln in keep:
            f.write(ln + "\n")
    if os.name != "nt":  # keep the secret owner-only on POSIX (no-op on Windows)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def _next_steps():
    print("  " + col(BOLD, "Run it"))
    print("    python notetaker.py " + col(DIM, '"https://meet.google.com/your-link"'))
    print()
    print("  " + col(BOLD, "Change anything later"))
    print("    " + col(DIM, "·") + " name, face, or notes format  " + col(DIM, "→") + "  edit " + col(BOLD, "config.jsonc"))
    print("    " + col(DIM, "·") + " your AgentCall key  " + col(DIM, "→") + "  edit " + col(BOLD, ".env"))
    print("    " + col(DIM, "·") + " your own camera tile  " + col(DIM, "→") + "  drop an image in " + col(BOLD, "avatars/") + " and set " + col(BOLD, "DISPLAY"))
    print("    " + col(DIM, "·") + " auto-join from your calendar  " + col(DIM, "→") + "  " + col(BOLD, "python autojoin.py connect"))
    print()


def _connect_interactive():
    """After the notetaker is built, offer to connect a calendar so it auto-joins."""
    import connect_calendar as cc
    if ask("Auto-join meetings from your calendar?",
           "it'll join them by itself · y/N", "n").lower() not in ("y", "yes"):
        return
    print()
    for line in cc.WALKTHROUGH.splitlines():
        print(("  " + col(DIM, line)) if line.strip() else "")
    print()
    while True:
        url = ask("Paste your secret iCal link", "read-only · Enter to skip")
        if not url:
            print("  " + col(DIM, "Skipped — connect later with: python autojoin.py connect"))
            return
        print("  " + col(DIM, "checking that link…"))
        events, err = cc.validate(url)
        if err:
            print("   " + col(BOLD, "✗") + " " + err)
            continue
        break
    cc.save_ics_url(url)
    cc.set_auto_join(True)
    print()
    cc.summarize(events)
    if ask("Turn auto-join on now?", "runs it now and at every login · Y/n", "y").lower() in ("", "y", "yes"):
        import subprocess
        subprocess.run([sys.executable, os.path.join(_HERE, "autojoin.py"), "start"])
    else:
        print("  " + col(DIM, "turn it on any time with: python autojoin.py start"))


def _connect_flags(ics_url, autostart_flag):
    import connect_calendar as cc
    events, err = cc.validate(ics_url)
    if err:
        print("   calendar not connected: " + err)
        return
    cc.save_ics_url(ics_url)
    cc.set_auto_join(True)
    cc.summarize(events)
    if autostart_flag:
        import subprocess
        subprocess.run([sys.executable, os.path.join(_HERE, "autojoin.py"), "start"])


def assemble(name, display, fmt, key, reused=False, connect=None):
    print()
    print("  " + col(BOLD, f"Building {name}") + col(DIM, " …"))
    print("   " + col(BOLD, "✓") + " wired the AgentCall listener")
    print("   " + col(BOLD, "✓") + f' set the "{display}" face')
    write_config(name, display, fmt)
    print("   " + col(BOLD, "✓") + " wrote config.jsonc")
    write_env(key)
    print("   " + col(BOLD, "✓") + (" copied your AgentCall key into .env" if reused else " saved your key to .env"))

    if connect:
        connect()

    _done_card(name)
    _next_steps()


def main():
    parser = argparse.ArgumentParser(
        description="Build your notetaker. No flags in a terminal = interactive; "
                    "pass flags for non-interactive / AI-agent / CI use.")
    parser.add_argument("--key", help="your AgentCall API key (written to .env)")
    parser.add_argument("--name")
    parser.add_argument("--display", help="audio | pattern | ring | transcript | <your avatar name>")
    parser.add_argument("--format", choices=["md", "txt", "json"])
    parser.add_argument("--image", help="path to a logo/photo to use as the avatar")
    parser.add_argument("--calendar-ics", help="connect a calendar: your secret iCal URL (turns on auto-join)")
    parser.add_argument("--autostart", action="store_true",
                        help="with --calendar-ics: also start auto-join when you log in")
    args = parser.parse_args()

    has_flags = any([args.key, args.name, args.display, args.format, args.image, args.calendar_ics])

    if not has_flags and not sys.stdin.isatty():
        print("This builder asks you questions, but there's no terminal here")
        print("(an AI agent, CI, or piped input). Run it non-interactively with flags:\n")
        print("  python build.py --key ak_ac_... --name Juno --display audio --format md")
        print("  (--display: audio | pattern | ring | transcript, or --image ./logo.png)")
        print("  A key is required: --key, or AGENTCALL_API_KEY / ~/.agentcall/config.json.\n")
        sys.exit(0)

    banner()

    if has_flags:
        name = args.name or "AgentCall"
        display = copy_image(args.image, name) if args.image else (args.display or "audio")
        fmt = args.format or "md"
        key = args.key or ""
    else:
        print(col(DIM, "  A few quick questions and it's yours."))
        if _existing_key():
            print("  " + col(BOLD, "✓") + col(DIM, " found your AgentCall key already set — using it."))
            key = ""
        else:
            key = ""
            while not key:
                key = ask("First — paste your AgentCall key",
                          "free at app.agentcall.dev/api-keys · Ctrl-C to cancel")
                if not key:
                    print("  " + col(DIM, "A key is required to run the notetaker — paste it, or Ctrl-C to cancel."))
        name = ask("Name your notetaker", "e.g. Juno · enter to keep AgentCall", "AgentCall")
        face = choose("How should it show up on camera?", [
            ("audio", "no video — just listens"),
            ("pattern", "the Pattern AI Labs logo"),
            ("ring", "a glowing neon ring"),
            ("transcript", "the live transcript, on screen"),
            ("image", "your own logo or photo"),
        ], "audio")
        if face == "image":
            ipath = ask("Where's your image?", "png · jpg · gif · svg · webp  (enter to skip)")
            display = copy_image(ipath, name) if ipath else "pattern"
        else:
            display = face
        fmt = choose("How should it save the notes?", [
            ("md", "Markdown"), ("txt", "plain text"), ("json", "JSON"),
        ], "md")

    found = _existing_key()
    effective = key or found
    if not effective:
        print()
        print("  An AgentCall key is required — the notetaker can't run without one.")
        print("  Pass --key ak_ac_...  (free at app.agentcall.dev/api-keys),")
        print("  or set AGENTCALL_API_KEY, or add it to .env, then build again.")
        sys.exit(1)

    if has_flags:
        connect = (lambda: _connect_flags(args.calendar_ics, args.autostart)) if args.calendar_ics else None
    else:
        connect = _connect_interactive
    assemble(name, display, fmt, effective, reused=(not key and bool(found)), connect=connect)


if __name__ == "__main__":
    main()
