#!/usr/bin/env python3
"""Auto-join: watch your calendar and send the notetaker into meetings by itself.

Connect a calendar once (python build.py, or python autojoin.py connect), then
this little scheduler polls it and launches notetaker.py for each meeting as it
starts. It wraps the notetaker — it doesn't change how the notetaker joins.

    python autojoin.py start         # turn it ON  — run now AND start whenever you log in
    python autojoin.py stop          # turn it OFF — stop now AND stop starting at login
    python autojoin.py status        # is it on? what's next?
    python autojoin.py restart       # bounce the watcher (stays on)
    python autojoin.py logs          # what it's been doing
    python autojoin.py connect       # connect (or re-connect) a calendar
    python autojoin.py run           # run once in the foreground, WITHOUT touching start-at-login
    python autojoin.py poll          # check once, right now, and print what it sees

Settings live in config.jsonc under CALENDAR; the secret calendar link lives in
.env as CALENDAR_ICS_URL. Powered by AgentCall — https://agentcall.dev
"""

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib import request as _urlrequest

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)                        # find calendar_source.py next to us

import calendar_source as cs                      # noqa: E402

UTC = timezone.utc
log = logging.getLogger("notetaker.autojoin")

RUNTIME_DIR = os.path.join(_PROJECT_ROOT, ".notetaker")
PID_FILE = os.path.join(RUNTIME_DIR, "autojoin.pid")
LOG_FILE = os.path.join(RUNTIME_DIR, "autojoin.log")
BOOT_LOG = os.path.join(RUNTIME_DIR, "autojoin.boot.log")  # daemon's raw stdout/stderr — catches crashes before logging is up
STATE_FILE = os.path.join(RUNTIME_DIR, "joined.json")     # dedupe: meetings we've already sent the bot to
STATUS_FILE = os.path.join(RUNTIME_DIR, "status.json")    # a snapshot the `status` command reads
MEETING_LOGS = os.path.join(RUNTIME_DIR, "meetings")      # per-meeting notetaker output
CHILDREN_FILE = os.path.join(RUNTIME_DIR, "children.json")  # live notetaker pids, for `stop --all`


# ── config + env (same tiny loaders the notetaker uses) ─────────────────────
def _load_config():
    p = os.path.join(_PROJECT_ROOT, "config.jsonc")
    try:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        text = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                      lambda m: m.group(1) or "", text, flags=re.S)
        return json.loads(text)
    except Exception as e:
        print(f"Couldn't read config.jsonc ({e}). Check it for typos "
              "(trailing commas, missing quotes).")
        sys.exit(1)


def _load_dotenv():
    for d in (_PROJECT_ROOT, _HERE):
        p = os.path.join(d, ".env")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            except (OSError, ValueError):
                pass
            return


CONFIG = _load_config()
_load_dotenv()


def cal_cfg():
    return CONFIG.get("CALENDAR") or {}


def _cfg_int(key, default):
    try:
        return int(cal_cfg().get(key, default))
    except (TypeError, ValueError):
        return default


def _load_api_key():
    key = os.environ.get("AGENTCALL_API_KEY", "")
    if key:
        return key
    p = os.path.join(os.path.expanduser("~"), ".agentcall", "config.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.loads(fh.read()).get("api_key", "")
    except (OSError, ValueError):
        return ""


def api_key_present():
    return bool(_load_api_key())


API_BASE = os.environ.get("AGENTCALL_API_URL") or "https://api.agentcall.dev"


def _end_call(call_id):
    """Stop a call's billing directly (same DELETE the notetaker itself does on a
    clean exit). Used by `stop --all` after force-ending a notetaker, so a bot we
    tore down can't keep a call alive. 404/409 mean it's already over — success."""
    key = _load_api_key()
    if not call_id or not key:
        return
    req = _urlrequest.Request(f"{API_BASE}/v1/calls/{call_id}", method="DELETE",
                              headers={"Authorization": f"Bearer {key}"})
    last_err = ""
    for attempt in range(2):
        try:
            resp = _urlrequest.urlopen(req, timeout=10)
            resp.read()
            print(f"     call {call_id} ended — billing stopped.")
            return
        except Exception as e:
            code = getattr(e, "code", None)
            if code in (404, 409):
                print(f"     call {call_id} already ended — billing stopped.")
                return
            last_err = f"HTTP {code}" if code else f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            if attempt == 0:
                time.sleep(0.5)
    print(f"     (couldn't confirm call {call_id} stopped — {last_err}. The server-side "
          "alone-timeout reclaims it once the meeting empties.)")


# ── runtime dir, logging, state ─────────────────────────────────────────────
def _ensure_runtime():
    os.makedirs(RUNTIME_DIR, exist_ok=True)


def _rotate_if_big(path, max_bytes=2_000_000):
    try:
        if os.path.getsize(path) > max_bytes:
            bak = path + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(path, bak)
    except OSError:
        pass


def setup_logging(to_console=True):
    _ensure_runtime()
    _rotate_if_big(LOG_FILE)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if to_console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        log.addHandler(ch)


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def load_joined():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    # Forget anything older than a day so the file stays small and yesterday's
    # meeting can recur tomorrow.
    cutoff = datetime.now(UTC) - timedelta(days=1)
    out = {}
    for k, v in data.items():
        try:
            if datetime.fromisoformat(v) > cutoff:
                out[k] = v
        except (TypeError, ValueError):
            pass
    return out


def save_joined(joined):
    _ensure_runtime()
    _write_json(STATE_FILE, joined)


def load_children():
    """Notetaker processes we launched that are still alive (dead ones pruned)."""
    try:
        with open(CHILDREN_FILE, encoding="utf-8") as fh:
            kids = json.load(fh)
    except (OSError, ValueError):
        return []
    return [k for k in kids if _pid_alive(k.get("pid"))]


def save_children(kids):
    _ensure_runtime()
    _write_json(CHILDREN_FILE, kids)


# ── launching the notetaker ─────────────────────────────────────────────────
def notetaker_cmd(url):
    """The command that joins one meeting. Override with AUTOJOIN_NOTETAKER_CMD
    (used by tests to avoid real calls)."""
    override = os.environ.get("AUTOJOIN_NOTETAKER_CMD")
    if override:
        return shlex.split(override, posix=(os.name != "nt")) + [url]
    return [sys.executable, os.path.join(_HERE, "notetaker.py"), url]


def launch_notetaker(ev):
    """Start notetaker.py for one meeting as its own detached process, with its
    console output tucked into .notetaker/meetings/ so nothing is lost."""
    os.makedirs(MEETING_LOGS, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9]+", "-", (ev.title or "meeting"))[:40].strip("-") or "meeting"
    logpath = os.path.join(MEETING_LOGS, f"{stamp}-{safe}.log")
    logfile = open(logpath, "w", encoding="utf-8")
    logfile.write(f"# {ev.title}\n# {ev.url}\n# launched {datetime.now().isoformat(timespec='seconds')}\n\n")
    logfile.flush()

    kwargs = dict(stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT, cwd=_HERE)
    if sys.platform == "win32":
        # CREATE_NO_WINDOW (not DETACHED_PROCESS): the notetaker — and the AgentCall
        # bridge it spawns — run with a hidden console, so no empty terminal window
        # pops up for each auto-joined meeting. CREATE_NEW_PROCESS_GROUP keeps our
        # Ctrl-C from reaching it; `stop --all` still ends it via the process tree.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(notetaker_cmd(ev.url), **kwargs)
    kids = load_children()
    kids.append({"pid": proc.pid, "title": ev.title, "log": logpath,
                 "started": datetime.now().isoformat(timespec="seconds")})
    save_children(kids)
    return proc.pid, logpath


def _skip_reason(ev):
    c = cal_cfg()
    if not ev.url:
        return "no meeting link"
    if ev.cancelled:
        return "cancelled"
    if ev.all_day and c.get("SKIP_ALL_DAY", True):
        return "all-day event"
    if ev.declined and c.get("SKIP_DECLINED", True):
        return "you declined it"
    return None


# ── one polling pass ────────────────────────────────────────────────────────
def poll_once(cal, joined, now=None):
    """Check the calendar once. Join anything starting in the [now-grace, now+lead]
    window; note the next upcoming meeting for `status`. Returns how many we joined."""
    now = now or datetime.now(UTC)
    lead = _cfg_int("JOIN_LEAD_SECONDS", 120)
    grace = _cfg_int("JOIN_GRACE_SECONDS", 300)
    join_from = now - timedelta(seconds=grace)
    join_to = now + timedelta(seconds=lead)
    look_end = now + timedelta(seconds=max(lead, 3600))   # also see ~1h out for "next up"

    events = cal.events(join_from - timedelta(minutes=1), look_end)
    joined_count = 0
    next_up = None
    for ev in events:
        if ev.start is None:
            continue
        if join_from <= ev.start <= join_to:
            reason = _skip_reason(ev)
            if reason:
                log.info("skip  %-30s (%s)", (ev.title or "?")[:30], reason)
                continue
            if ev.key() in joined:
                continue
            pid, logpath = launch_notetaker(ev)
            joined[ev.key()] = now.isoformat()
            save_joined(joined)
            joined_count += 1
            log.info("JOIN  %-30s  %s  (pid %s)", (ev.title or "?")[:30], ev.url, pid)
        elif ev.start > join_to and _skip_reason(ev) is None and next_up is None:
            next_up = ev

    _write_status(now, cal, next_up, len(joined))
    return joined_count


def _write_status(now, cal, next_up, joined_total):
    _ensure_runtime()
    nxt = None
    if next_up is not None:
        nxt = {"title": next_up.title,
               "start": next_up.start.astimezone().isoformat(timespec="minutes"),
               "url": next_up.url}
    _write_json(STATUS_FILE, {
        "pid": os.getpid(),
        "last_poll": now.astimezone().isoformat(timespec="seconds"),
        "source": cal.describe(),
        "poll_seconds": _cfg_int("POLL_SECONDS", 60),
        "joined_remembered": joined_total,
        "next_meeting": nxt,
    })


# ── the foreground daemon (`run`) ───────────────────────────────────────────
def _make_source_or_explain():
    try:
        return cs.make_source(cal_cfg(), os.environ)
    except Exception as e:
        log.error("Can't start auto-join: %s", e)
        return None


def cmd_run(_args):
    # Console only on a real terminal. When `start` launches us detached our stdout
    # is a file, so a console handler there would double-write the log (in the wrong
    # encoding); the file handler alone owns autojoin.log.
    setup_logging(to_console=sys.stdout.isatty())
    if not api_key_present():
        log.error("No AgentCall API key found. Set AGENTCALL_API_KEY (or ~/.agentcall/config.json), "
                  "then start again. Get one free at https://app.agentcall.dev/api-keys.")
        return 1
    cal = _make_source_or_explain()
    if cal is None:
        return 1

    interval = _cfg_int("POLL_SECONDS", 60)
    lead = _cfg_int("JOIN_LEAD_SECONDS", 120)
    grace = _cfg_int("JOIN_GRACE_SECONDS", 300)
    joined = load_joined()

    _ensure_runtime()
    with open(PID_FILE, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    stop = {"v": False}

    def _sig(_signum, _frame):
        stop["v"] = True

    import signal
    signal.signal(signal.SIGINT, _sig)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except (ValueError, AttributeError):
        pass

    log.info("Auto-join started — watching your %s every %ss (join %ss before, "
             "up to %ss late). Ctrl-C to stop.", cal.describe(), interval, lead, grace)
    if not cal_cfg().get("AUTO_JOIN", False):
        log.info("(config.jsonc CALENDAR.AUTO_JOIN is false — you started it by hand, so it's running "
                 "anyway. Set it true to have `enable` start it on boot.)")
    try:
        while not stop["v"]:
            try:
                poll_once(cal, joined)
            except Exception as e:
                log.warning("This poll failed (%s) — will try again next cycle.", e)
            # Interruptible sleep so Ctrl-C / stop is felt within ~0.5s.
            slept = 0.0
            while slept < interval and not stop["v"]:
                time.sleep(0.5)
                slept += 0.5
    finally:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        try:
            os.remove(STATUS_FILE)
        except OSError:
            pass
    log.info("Auto-join stopped.")
    return 0


def cmd_poll(_args):
    """One pass, right now, printed — the quickest way to see what it would do."""
    setup_logging(to_console=True)
    cal = _make_source_or_explain()
    if cal is None:
        return 1
    if not api_key_present():
        log.warning("Heads up: no AgentCall API key set yet — joining will fail until you set one "
                    "(https://app.agentcall.dev/api-keys).")
    joined = load_joined()
    n = poll_once(cal, joined)
    try:
        with open(STATUS_FILE, encoding="utf-8") as fh:
            st = json.load(fh)
        nxt = st.get("next_meeting")
        if nxt:
            print(f"\nNext up: {nxt['title']} at {nxt['start']}")
            print(f"         {nxt['url']}")
        else:
            print("\nNothing else on the calendar within the next hour.")
    except (OSError, ValueError):
        pass
    print(f"Joined this pass: {n}.")
    return 0


# ── process control (start / stop / status / restart / logs) ────────────────
def _read_pid():
    try:
        with open(PID_FILE, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    if not pid:
        return False
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate(pid):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, text=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        for _ in range(20):
            if not _pid_alive(pid):
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _spawn_daemon():
    """Launch the background watcher as a detached process (Windows-style: we own it;
    the pid file is written by `run`). On mac/linux the service manager does this."""
    _ensure_runtime()
    # The daemon owns autojoin.log via its own handler; send its raw stdout/stderr to
    # a separate boot log so an early crash (e.g. a bad import) is still captured.
    bootfile = open(BOOT_LOG, "w", encoding="utf-8")
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=bootfile, stderr=subprocess.STDOUT, cwd=_HERE)
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.join(_HERE, "autojoin.py"), "run"], **kwargs)


def cmd_start(_args):
    """Turn auto-join ON: run it now AND have it start when you log in."""
    import autostart
    if not api_key_present():
        print("No AgentCall API key found. Set AGENTCALL_API_KEY (or add it via python build.py) first.")
        return 1
    try:
        cs.make_source(cal_cfg(), os.environ)
    except Exception as e:
        print(f"Can't turn on auto-join: {e}")
        return 1

    already = _pid_alive(_read_pid())
    # On Windows we start the process ourselves; on mac/linux autostart.on() hands it
    # to launchd/systemd, which starts it now and registers boot in one go.
    if autostart.MANUAL_PROCESS and not already:
        _spawn_daemon()
    autostart.on()

    for _ in range(20):                            # wait for the daemon to write its pid
        time.sleep(0.1)
        pid = _read_pid()
        if _pid_alive(pid):
            print(f"● Auto-join is ON ({'already running' if already else 'started'}, pid {pid}).")
            print("  It's watching now, and it'll start automatically when you log in.")
            print("  python autojoin.py status     see what's next")
            print("  python autojoin.py stop       turn it off (now and at login)")
            return 0
    print("Registered start-on-login, but couldn't confirm it's running — check `python autojoin.py logs`.")
    return 1


def _call_id_from_log(logpath):
    """The notetaker prints 'Call created: <id>' — its per-meeting log has it."""
    try:
        with open(logpath, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.search(r"Call created:\s*(\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _stop_children():
    """Make every notetaker WE launched leave its meeting, and confirm billing
    stopped. POSIX: a Ctrl-C-equivalent (SIGINT) lets the notetaker do its own
    clean leave+DELETE. Windows: no way to deliver Ctrl-C to a detached process,
    so we end the process tree and DELETE its call ourselves (404/409 = already
    ended, fine either way)."""
    kids = load_children()
    if not kids:
        print("No meetings in progress.")
        return
    for k in kids:
        pid, title = k.get("pid"), (k.get("title") or "meeting")
        print(f"  leaving '{title}' (pid {pid})…")
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True)
        else:
            import signal
            try:
                os.kill(pid, signal.SIGINT)          # its Ctrl-C path: leave + DELETE
            except ProcessLookupError:
                pid = None
            if pid:
                for _ in range(100):                 # give it up to 10s to exit cleanly
                    if not _pid_alive(pid):
                        break
                    time.sleep(0.1)
                if _pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        # Belt and braces on every platform: DELETE the call by id from the meeting
        # log. If the notetaker already ended it, the API answers 404/409 — harmless.
        cid = _call_id_from_log(k.get("log", ""))
        if cid:
            _end_call(cid)
        else:
            print("     (no call id in its log — it hadn't finished joining, or the "
                  "notetaker already cleaned up. The server-side auto-leave covers it.)")
    save_children([])


def cmd_stop(args):
    """Turn auto-join OFF: stop it now AND stop it starting when you log in."""
    import autostart
    stop_all = getattr(args, "all", False)
    pid = _read_pid()
    running = _pid_alive(pid)
    if autostart.MANUAL_PROCESS and running:       # Windows: we own the process
        _terminate(pid)
    autostart.off()                                # remove boot (mac/linux: also stops it)
    for f in (PID_FILE, STATUS_FILE):
        try:
            os.remove(f)
        except OSError:
            pass
    print("○ Auto-join is OFF — stopped, and it won't start when you log in.")
    if stop_all:
        _stop_children()
    else:
        print("  (meetings already in progress keep running until they empty — "
              "`stop --all` makes their bots leave too.)")
    return 0


def cmd_restart(args):
    """Bounce the watcher; it stays ON."""
    import autostart
    pid = _read_pid()
    if autostart.MANUAL_PROCESS and _pid_alive(pid):
        _terminate(pid)
        for f in (PID_FILE, STATUS_FILE):
            try:
                os.remove(f)
            except OSError:
                pass
    time.sleep(0.5)
    return cmd_start(args)


def _fmt_ago(iso):
    try:
        t = datetime.fromisoformat(iso)
        secs = (datetime.now(t.tzinfo) - t).total_seconds()
        if secs < 90:
            return f"{int(secs)}s ago"
        if secs < 5400:
            return f"{int(secs // 60)}m ago"
        return f"{int(secs // 3600)}h ago"
    except (TypeError, ValueError):
        return iso


def cmd_status(_args):
    pid = _read_pid()
    alive = _pid_alive(pid)
    st = {}
    try:
        with open(STATUS_FILE, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        pass

    import autostart
    try:
        boot = autostart.is_on()
    except Exception:
        boot = False

    if alive and boot:
        print(f"● auto-join is ON (running, pid {pid}) — and starts when you log in")
    elif alive and not boot:
        print(f"● auto-join is running (pid {pid}), but is NOT set to start at login")
        print("    make it start at login too:  python autojoin.py start")
    elif boot and not alive:
        print("◐ auto-join is set to start at login, but isn't running right now")
        print("    turn it on now:  python autojoin.py start")
    else:
        print("○ auto-join is OFF")
        print("    turn it on:  python autojoin.py start")

    try:
        with open(os.path.join(_PROJECT_ROOT, ".env"), encoding="utf-8") as fh:
            connected = any(l.startswith("CALENDAR_ICS_URL=") and l.split("=", 1)[1].strip()
                            for l in fh)
    except OSError:
        connected = bool(os.environ.get("CALENDAR_ICS_URL"))
    print(f"    calendar: {'connected (' + st.get('source', 'iCal feed') + ')' if connected else 'NOT connected — run python autojoin.py connect'}")
    print(f"    auto-join in config: {'on' if cal_cfg().get('AUTO_JOIN') else 'off'}")

    if st.get("last_poll"):
        print(f"    last checked: {_fmt_ago(st['last_poll'])}")
    nxt = st.get("next_meeting")
    if nxt:
        print(f"    next meeting: {nxt['title']}  at {nxt['start']}")
        print(f"                  {nxt['url']}")
    elif alive:
        print("    next meeting: nothing within the next hour")
    return 0


def cmd_logs(args):
    n = getattr(args, "lines", 40) or 40
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        print("No log yet — auto-join hasn't run. Start it with: python autojoin.py start")
        # A daemon that died on startup leaves its trail here.
        if os.path.exists(BOOT_LOG) and os.path.getsize(BOOT_LOG):
            print(f"\nStartup output ({BOOT_LOG}):")
            with open(BOOT_LOG, encoding="utf-8", errors="replace") as fh:
                sys.stdout.write(fh.read())
        return 0
    for line in lines[-n:]:
        sys.stdout.write(line)
    print(f"\n({LOG_FILE})")
    return 0


# ── connect is wired in by its own module ───────────────────────────────────
def cmd_connect(args):
    import connect_calendar
    if getattr(args, "from_env", False):
        # Agent/CI-safe path: the link was pasted into .env by the user themself
        # (CALENDAR_ICS_URL=...), so it never has to appear in a chat or a command
        # line. We just validate it and switch auto-join on.
        url = (os.environ.get("CALENDAR_ICS_URL") or "").strip()
        if not url:
            print("CALENDAR_ICS_URL isn't set. Add this line to the .env file next to "
                  "config.jsonc, then run this again:")
            print("    CALENDAR_ICS_URL=<your secret iCal link>")
            return 1
        events, err = connect_calendar.validate(url)
        if err:
            print(f"✗ {err}")
            return 1
        connect_calendar.set_auto_join(True)
        connect_calendar.summarize(events)
        print("\nCalendar connected. Turn auto-join on with:  python autojoin.py start")
        print("  (that runs it now and starts it whenever you log in; `stop` turns it off.)")
        return 0
    return connect_calendar.interactive()


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv):
    parser = argparse.ArgumentParser(
        prog="autojoin", description="Watch your calendar and auto-join meetings with the notetaker.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("start", help="turn auto-join ON — run now AND start at login")
    st = sub.add_parser("stop", help="turn auto-join OFF — stop now AND stop starting at login")
    st.add_argument("--all", action="store_true",
                    help="also make bots leave any meetings still in progress")
    sub.add_parser("restart", help="bounce the watcher (stays on)")
    sub.add_parser("status", help="is it on, and what's next?")
    lg = sub.add_parser("logs", help="show recent activity")
    lg.add_argument("-n", "--lines", type=int, default=40)
    cn = sub.add_parser("connect", help="connect a calendar (paste your secret iCal link)")
    cn.add_argument("--from-env", action="store_true", dest="from_env",
                    help="non-interactive: use the CALENDAR_ICS_URL already saved in .env")
    sub.add_parser("run", help="run in the foreground, once, without touching start-at-login")
    sub.add_parser("poll", help="check the calendar once, now, and print what it sees")

    args = parser.parse_args(argv)
    handlers = {
        "run": cmd_run, "poll": cmd_poll, "start": cmd_start, "stop": cmd_stop,
        "restart": cmd_restart, "status": cmd_status, "logs": cmd_logs,
        "connect": cmd_connect,
    }
    if not args.cmd:
        parser.print_help()
        return 0
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
