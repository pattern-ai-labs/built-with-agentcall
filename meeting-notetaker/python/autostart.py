#!/usr/bin/env python3
"""Start auto-join automatically when you log in — the OS-specific bit.

Each platform gets its native, no-admin mechanism, and we tell you exactly what
file we created so nothing happens behind your back:

    Windows   a hidden launcher in your Startup folder      (Start Menu\\Programs\\Startup)
    macOS     a LaunchAgent                                  (~/Library/LaunchAgents)
    Linux     a systemd --user service                       (~/.config/systemd/user)

enable() also starts it now; disable() also stops it now. Called by
`python autojoin.py enable` / `disable`.
"""

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
AUTOJOIN = os.path.join(_HERE, "autojoin.py")

APP_NAME = "MeetingNotetaker-AutoJoin"          # Windows launcher filename
LABEL = "dev.agentcall.notetaker-autojoin"      # macOS/Linux service id


# ── Windows: a hidden .vbs in the Startup folder ────────────────────────────
def _win_startup_dir():
    return os.path.join(os.environ.get("APPDATA", ""),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _win_vbs():
    return os.path.join(_win_startup_dir(), APP_NAME + ".vbs")


def _win_enable():
    vbs = _win_vbs()
    py = sys.executable
    # VBS string literals escape a double-quote by doubling it. This launches
    #   "<python>" "<autojoin.py>" start
    # hidden (window style 0); `start` then spawns the detached daemon and exits.
    run_arg = f'"""{py}"" ""{AUTOJOIN}"" start"'
    content = ('Set sh = CreateObject("WScript.Shell")\r\n'
               f'sh.CurrentDirectory = "{_HERE}"\r\n'
               f'sh.Run {run_arg}, 0, False\r\n')
    os.makedirs(_win_startup_dir(), exist_ok=True)
    with open(vbs, "w", encoding="utf-8") as f:
        f.write(content)
    print("  " + "✓" + " added a startup launcher so auto-join starts when you log in:")
    print(f"    {vbs}")
    return 0


def _win_disable():
    vbs = _win_vbs()
    try:
        os.remove(vbs)
        print("  removed the startup launcher — auto-join won't start on login anymore.")
    except FileNotFoundError:
        print("  (it wasn't set to start on login.)")
    except OSError as e:
        print(f"  couldn't remove {vbs}: {e}")
        return 1
    return 0


def _win_is_enabled():
    return os.path.exists(_win_vbs())


# ── macOS: a LaunchAgent plist ──────────────────────────────────────────────
def _mac_plist():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LABEL + ".plist")


def _mac_enable():
    import plistlib
    plist = _mac_plist()
    os.makedirs(os.path.dirname(plist), exist_ok=True)
    data = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, AUTOJOIN, "run"],
        "WorkingDirectory": _HERE,
        "RunAtLoad": True,
        # Restart if it crashes, but a clean `stop` (exit 0) stays stopped.
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": os.path.join(os.path.dirname(_HERE), ".notetaker", "autojoin.boot.log"),
        "StandardErrorPath": os.path.join(os.path.dirname(_HERE), ".notetaker", "autojoin.boot.log"),
    }
    with open(plist, "wb") as f:
        plistlib.dump(data, f)
    subprocess.run(["launchctl", "unload", plist], capture_output=True)   # in case it's already loaded
    r = subprocess.run(["launchctl", "load", "-w", plist], capture_output=True, text=True)
    print("  " + "✓" + f" installed a LaunchAgent: {plist}")
    if r.returncode != 0:
        print(f"  (launchctl load said: {r.stderr.strip() or r.stdout.strip()})")
        print(f"  You can load it yourself with:  launchctl load -w \"{plist}\"")
    return 0


def _mac_disable():
    plist = _mac_plist()
    subprocess.run(["launchctl", "unload", "-w", plist], capture_output=True)
    try:
        os.remove(plist)
    except FileNotFoundError:
        print("  (it wasn't set to start on login.)")
        return 0
    except OSError as e:
        print(f"  couldn't remove {plist}: {e}")
        return 1
    print("  removed the LaunchAgent — auto-join won't start on login anymore.")
    return 0


def _mac_is_enabled():
    return os.path.exists(_mac_plist())


# ── Linux: a systemd --user service ─────────────────────────────────────────
def _linux_unit():
    return os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", LABEL + ".service")


def _has_systemctl():
    from shutil import which
    return which("systemctl") is not None


def _linux_enable():
    if not _has_systemctl():
        print("  systemd isn't available here. Add this to your login startup by hand:")
        print(f"    {sys.executable} {AUTOJOIN} start")
        return 1
    unit = _linux_unit()
    os.makedirs(os.path.dirname(unit), exist_ok=True)
    content = (
        "[Unit]\n"
        "Description=Meeting Notetaker auto-join (watches your calendar)\n\n"
        "[Service]\n"
        f"WorkingDirectory={_HERE}\n"
        f"ExecStart={sys.executable} {AUTOJOIN} run\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    with open(unit, "w", encoding="utf-8") as f:
        f.write(content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", LABEL + ".service"],
                       capture_output=True, text=True)
    print("  " + "✓" + f" installed a systemd --user service: {unit}")
    if r.returncode != 0:
        print(f"  (systemctl said: {r.stderr.strip() or r.stdout.strip()})")
    print("  Tip: to run it even before you log in, enable lingering once:")
    print("    loginctl enable-linger $USER")
    return 0


def _linux_disable():
    if _has_systemctl():
        subprocess.run(["systemctl", "--user", "disable", "--now", LABEL + ".service"], capture_output=True)
    try:
        os.remove(_linux_unit())
    except FileNotFoundError:
        print("  (it wasn't set to start on login.)")
        return 0
    except OSError as e:
        print(f"  couldn't remove the unit: {e}")
        return 1
    if _has_systemctl():
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print("  removed the systemd service — auto-join won't start on login anymore.")
    return 0


def _linux_is_enabled():
    return os.path.exists(_linux_unit())


# ── platform dispatch ───────────────────────────────────────────────────────
def enable():
    if sys.platform == "win32":
        rc = _win_enable()
        _start_now()                             # Startup only fires at login; start it now too
        return rc
    if sys.platform == "darwin":
        return _mac_enable()                     # launchd RunAtLoad starts it now
    return _linux_enable()                        # systemd --now starts it now


def disable():
    if sys.platform == "win32":
        _stop_now()
        return _win_disable()
    if sys.platform == "darwin":
        return _mac_disable()
    return _linux_disable()


def is_enabled():
    if sys.platform == "win32":
        return _win_is_enabled()
    if sys.platform == "darwin":
        return _mac_is_enabled()
    return _linux_is_enabled()


def _start_now():
    subprocess.run([sys.executable, AUTOJOIN, "start"])


def _stop_now():
    subprocess.run([sys.executable, AUTOJOIN, "stop"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "enable":
        raise SystemExit(enable())
    if cmd == "disable":
        raise SystemExit(disable())
    if cmd == "status":
        print("start-on-login:", "yes" if is_enabled() else "no")
        raise SystemExit(0)
    print("usage: python autostart.py [enable|disable|status]")
