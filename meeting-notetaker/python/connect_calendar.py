#!/usr/bin/env python3
"""Connect a calendar: paste your secret iCal link, we check it, and save it.

Shared by the builder (python build.py) and `python autojoin.py connect`. The link
is a credential, so it's written to .env (gitignored) as CALENDAR_ICS_URL — never
to the shared config.jsonc. Connecting also flips CALENDAR.AUTO_JOIN on.
"""

import datetime as _dt
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import calendar_source as cs   # noqa: E402

UTC = _dt.timezone.utc

WALKTHROUGH = """\
Connect a calendar and the notetaker joins your meetings on its own. You'll paste
a private "secret iCal" link — it's read-only and just for this app.

  Google Calendar
    1. Open calendar.google.com on a computer.
    2. Hover the calendar under "My calendars" (left) -> ⋮ -> "Settings and sharing".
    3. Scroll to "Integrate calendar".
    4. Copy the "Secret address in iCal format" (it ends in .ics).

  Outlook / Microsoft 365
    Settings -> Calendar -> Shared calendars -> Publish a calendar -> publish, then
    copy the ICS link.

  Apple iCloud
    Share the calendar as a Public Calendar and copy the webcal:// link.

This app reads the link on your own computer — your calendar is never sent to us or
anywhere else. Keep the link private, though: anyone who has it can read your calendar."""


def validate(url):
    """Fetch + parse the feed. Returns (events, None) on success (events may be an
    empty list), or (None, human-error) if the link isn't a working calendar."""
    url = _clean(url)
    if not url:
        return None, "no link given."
    try:
        src = cs.ICSCalendarSource(url)
        text = src._fetch()
    except FileNotFoundError:
        return None, "couldn't find that file."
    except Exception as e:
        return None, f"couldn't fetch that link ({type(e).__name__}: {e})."
    if "BEGIN:VCALENDAR" not in text.upper():
        return None, ("that link didn't return a calendar feed. Make sure you copied the "
                      "*secret iCal* address (it usually ends in .ics), not the calendar's web page.")
    now = _dt.datetime.now(UTC)
    events = cs.parse_ics(text, now - _dt.timedelta(hours=1), now + _dt.timedelta(days=14))
    return events, None


def _clean(url):
    # Strip the invisible bidi/zero-width marks Windows' "Copy" can prepend, plus
    # surrounding quotes/space, so a pasted link actually resolves.
    url = re.sub(r"[​-‏‪-‮﻿]", "", url or "").strip().strip('"').strip("'").strip()
    return url


def save_ics_url(url):
    """Write CALENDAR_ICS_URL into .env, keeping every other line (the API key etc.)."""
    url = _clean(url)
    p = os.path.join(_PROJECT_ROOT, ".env")
    keep = []
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                keep = [ln.rstrip("\n") for ln in f
                        if ln.strip() and not ln.strip().startswith("CALENDAR_ICS_URL=")]
        except OSError:
            keep = []
    with open(p, "w", encoding="utf-8") as f:
        for ln in keep:
            f.write(ln + "\n")
        f.write(f"CALENDAR_ICS_URL={url}\n")
    if os.name != "nt":
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def set_auto_join(on=True):
    """Flip CALENDAR.AUTO_JOIN in config.jsonc (comments and layout preserved)."""
    p = os.path.join(_PROJECT_ROOT, "config.jsonc")
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    new = re.sub(r'("AUTO_JOIN"\s*:\s*)(?:true|false)',
                 lambda m: m.group(1) + ("true" if on else "false"), text, count=1)
    with open(p, "w", encoding="utf-8") as f:
        f.write(new)


def summarize(events, out=print, limit=5):
    """Print a short 'here's what I can see' confirmation."""
    joinable = [e for e in events if e.url and not e.cancelled and not e.all_day]
    out(f"  Connected. {len(events)} event(s) in the next 14 days, "
        f"{len(joinable)} with a meeting link the notetaker can join.")
    for e in joinable[:limit]:
        when = e.start.astimezone().strftime("%a %d %b, %H:%M")
        out(f"    · {when}  {e.title}")
    if not joinable:
        out("    (nothing joinable yet — new meetings will be picked up automatically.)")


def interactive():
    """Standalone flow for `python autojoin.py connect` (plain prompts)."""
    print()
    print(WALKTHROUGH)
    print()
    url = ""
    while True:
        try:
            url = input("  Paste your secret iCal link (Enter to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return 1
        if not url:
            print("  Cancelled — no calendar connected.")
            return 1
        print("  Checking that link…")
        events, err = validate(url)
        if err:
            print(f"  ✗ {err}")
            print("  Let's try again (or press Enter to cancel).")
            continue
        break

    save_ics_url(url)
    set_auto_join(True)
    print()
    summarize(events)

    try:
        ans = input("\n  Start auto-join automatically when you log in? (Y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"
    if ans in ("", "y", "yes"):
        try:
            import autostart
            autostart.enable()
        except Exception as e:
            print(f"  (couldn't set up start-on-login: {e})")
            print("  Start it yourself any time with:  python autojoin.py start")
    else:
        import subprocess
        print("  Starting it for now (won't survive a reboot — run "
              "`python autojoin.py enable` for that):")
        subprocess.run([sys.executable, os.path.join(_HERE, "autojoin.py"), "start"])
    print("\n  Done — auto-join is connected. Check it any time with: python autojoin.py status")
    return 0


if __name__ == "__main__":
    raise SystemExit(interactive())
