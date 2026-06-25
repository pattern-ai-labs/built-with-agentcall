#!/usr/bin/env python3
"""Silent meeting notetaker — joins a call, writes the transcript live, and leaves
when everyone else does. Settings live in config.jsonc; this file is the plumbing.
Runs on AgentCall's bridge and never speaks. MIT. https://agentcall.dev

    export AGENTCALL_API_KEY="ak_ac_your_key"
    pip install -r requirements.txt
    python notetaker.py "https://meet.google.com/abc-def-ghi"
"""

import argparse
import base64
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)  # config.jsonc, notes/, avatars/ and .env live here


def _load_config():
    """Read config.jsonc from the repo root — one file, shared by python and node.
    It's JSON with // and /* */ comments; strip the comments, then parse."""
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


CONFIG = _load_config()


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


_load_dotenv()


# Your hooks — empty by default. Add your own logic here to build on top.
def on_line(entry):
    pass


def on_meeting_end(transcript, meta):
    pass


API_BASE = os.environ.get("AGENTCALL_API_URL") or "https://api.agentcall.dev"
DEBUG = bool(os.environ.get("NOTETAKER_DEBUG"))   # NOTETAKER_DEBUG=1 prints every raw bridge event


def load_api_key():
    key = os.environ.get("AGENTCALL_API_KEY", "")
    if key:
        return key
    p = os.path.join(os.path.expanduser("~"), ".agentcall", "config.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.loads(fh.read()).get("api_key", "")
        except (OSError, ValueError):
            pass
    return ""


API_KEY = load_api_key()

STATE = {"bot": CONFIG["BOT_NAME"], "status": "starting", "present": 0, "lines": []}
STATE_LOCK = threading.Lock()


def bridge_command(display):
    override = os.environ.get("NOTETAKER_BRIDGE")
    if override:
        return [sys.executable, override]
    name = "bridge.py" if display == "audio" else "bridge-visual.py"
    return [sys.executable, os.path.join(_HERE, "engine", name)]


def end_call(call_id):
    if not call_id or not API_KEY:
        return
    req = urlrequest.Request(
        f"{API_BASE}/v1/calls/{call_id}", method="DELETE",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    last_err = ""
    for attempt in range(2):   # one quick retry
        try:
            resp = urlrequest.urlopen(req, timeout=10)   # ending a LIVE call can take a few seconds
            resp.read()
            print(f"  Call ended cleanly - DELETE /v1/calls/{call_id} -> {resp.getcode()}. Billing stopped.")
            return
        except Exception as e:
            code = getattr(e, "code", None)
            if code in (404, 409):   # 404 = gone, 409 = "call already ended" — either way it's stopped
                print(f"  Call already ended - DELETE /v1/calls/{call_id} -> {code}. Billing stopped.")
                return
            last_err = f"HTTP {code}" if code else f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            try:                                  # an HTTPError carries the API's error body
                if hasattr(e, "read"):
                    body = e.read().decode("utf-8", "replace").strip()
                    if body:
                        last_err += f" - {body[:200]}"
            except Exception:
                pass
            if attempt == 0:
                time.sleep(0.5)
    print(f"  (note: couldn't confirm the call stopped - {last_err} (call_id={call_id}); the bot has "
          "left and the call expires on its own. You can DELETE it manually with that id if needed.)")


def hhmmss(ts):
    return ts[11:19] if isinstance(ts, str) and len(ts) >= 19 else ts


def _fmt_line(e, bold):
    tag = " (chat)" if e.get("kind") == "chat" else ""
    name = f"**{e['speaker']}**" if bold else e["speaker"]
    return f"[{hhmmss(e['timestamp'])}] {name}{tag}: {e['text']}"


class LiveNotes:
    """Appends each line to the file the moment it's captured (md/txt); json is
    written once at the end. The file is created on the first line."""

    def __init__(self):
        self.fmt = CONFIG["OUTPUT_FORMAT"]
        out = CONFIG["OUTPUT_DIR"]
        if not os.path.isabs(out):
            out = os.path.join(_PROJECT_ROOT, out)
        os.makedirs(out, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        self.path = os.path.join(out, f"meeting-notes-{stamp}.{self.fmt}")
        self._f = None
        if self.fmt in ("md", "txt"):
            self._f = open(self.path, "w", encoding="utf-8")
            head = (f"# Meeting Notes — {datetime.now():%Y-%m-%d %H:%M}\n\n## Transcript\n"
                    if self.fmt == "md"
                    else f"Meeting Notes — {datetime.now():%Y-%m-%d %H:%M}\n\n")
            self._f.write(head)
            self._f.flush()

    def add(self, entry):
        if self._f:
            self._f.write(_fmt_line(entry, bold=(self.fmt == "md")) + "\n")
            self._f.flush()

    def finalize(self, transcript, meta):
        if self.fmt == "json":
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"meta": meta, "transcript": transcript}, f, indent=2)
            return self.path
        foot = []
        if self.fmt == "md":
            foot.append("\n## Participants")
            foot += [f"- {p}" for p in meta["participants"]] or ["- (none detected)"]
            foot += ["\n## Meeting Info",
                     f"- Call ID: {meta['call_id']}",
                     f"- Duration: {meta['duration']}",
                     f"- End reason: {meta['end_reason']}",
                     f"- Total utterances: {len(transcript)}"]
        else:
            foot += ["", "Participants: " + ", ".join(meta["participants"])]
        if self._f:
            self._f.write("\n".join(foot) + "\n")
            self._f.flush()
            self._f.close()
        return self.path


# Serve an HTML page at / and the live transcript at /transcript.json. The local
# dashboard and the in-call transcript tile use the same page. {{BOT_NAME}} and
# {{AVATAR_LINES}} are filled in as it's served.
def _read_html(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return f"<h1>{os.path.basename(path)} missing</h1>"


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def _image_html(image_path):
    """Wrap a raw image (png/jpg/gif/svg/webp) as a full-tile page, so an image
    dropped in avatars/ can be the bot's avatar with no HTML needed."""
    with open(image_path, "rb") as fh:
        data = fh.read()
    mime = mimetypes.guess_type(image_path)[0] or "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            "<style>html,body{margin:0;height:100%;background:#0a0e1a}"
            "body{display:flex;align-items:center;justify-content:center}"
            "img{max-width:100%;max-height:100%;object-fit:contain}</style></head>"
            '<body><img src="data:' + mime + ";base64," + b64 + '"></body></html>')


def _avatar_provider(display):
    """avatars/<display>.html (an HTML tile) or avatars/<display>.<img> (a raw
    image). Returns a function that yields the page HTML, or None if neither exists."""
    base = os.path.join(_PROJECT_ROOT, "avatars", display)
    if os.path.exists(base + ".html"):
        return lambda: _read_html(base + ".html")
    for ext in _IMAGE_EXTS:
        if os.path.exists(base + ext):
            return lambda p=base + ext: _image_html(p)
    return None


def _make_handler(html_provider):
    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/transcript.json"):
                with STATE_LOCK:
                    body = json.dumps(STATE).encode("utf-8")
                ctype = "application/json"
            else:
                html = html_provider()
                html = html.replace("{{BOT_NAME}}", str(STATE.get("bot", "AgentCall")))
                html = html.replace("{{AVATAR_LINES}}", str(CONFIG.get("AVATAR_LINES", 8)))
                body = html.encode("utf-8")
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
    return _H


def serve_html(html_provider, port):
    """Serve html_provider() at / (and STATE at /transcript.json) on 127.0.0.1:port
    (port=0 picks a free one). Returns (server, port)."""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(html_provider))
    except OSError as e:
        print(f"Web view unavailable (port {port}: {e}) - continuing without it.")
        return None, 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def run(meet_url, bot_name, display):
    if os.environ.get("NOTETAKER_BRIDGE"):   # test harness uses a fake stdio bridge
        display = "audio"

    # For an avatar, serve its page locally; the visual bridge tunnels it as the
    # bot's video tile. Fall back to audio if the page or a port is unavailable.
    ui_port = 0
    if display != "audio":
        provider = _avatar_provider(display)
        if not provider:
            print(f"Avatar '{display}' not found — add avatars/{display}.html or "
                  f"avatars/{display}.<image> (png/jpg/gif/svg/webp). Using audio for now.")
            display = "audio"
        else:
            _, ui_port = serve_html(provider, 0)
            if not ui_port:
                print(f"Couldn't start the avatar server for '{display}' — using audio for now.")
                display = "audio"

    cmd = bridge_command(display) + [meet_url, "--name", bot_name]
    if CONFIG["ALONE_SECONDS"] and CONFIG["ALONE_SECONDS"] > 0:
        cmd += ["--alone-timeout", str(CONFIG["ALONE_SECONDS"])]
    if CONFIG.get("VAD_TIMEOUT") and CONFIG["VAD_TIMEOUT"] > 0:
        cmd += ["--vad-timeout", str(CONFIG["VAD_TIMEOUT"])]   # pause after you stop talking; lower = snappier
    if ui_port:
        cmd += ["--ui-port", str(ui_port)]

    # Run the bridge in its own process group so a terminal Ctrl-C hits only us,
    # not the bridge — we need it alive to report the call id and leave cleanly,
    # so an aborted call is always DELETEd (never an orphan bot left joining).
    popen_kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, bufsize=1)
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)

    # A background reader drains the bridge and captures the call id the instant
    # it's reported, so we can always DELETE the call on exit — even a fast Ctrl-C
    # — and never leave an orphaned bot to finish joining.
    events = queue.Queue()
    box = {"call_id": None}

    def reader():
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if box["call_id"] is None and (ev.get("event") or ev.get("type")) == "call.created":
                    box["call_id"] = ev.get("call_id")
                events.put(ev)
        finally:
            events.put(None)

    threading.Thread(target=reader, daemon=True).start()

    err_tail = []   # last bridge stderr lines, shown only if it fails to join

    def err_reader():
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    err_tail.append(line)
                    if len(err_tail) > 30:
                        del err_tail[0]
        except Exception:
            pass

    threading.Thread(target=err_reader, daemon=True).start()

    transcript = []
    notes = None
    present = set()
    seen = set()
    seen_human = False
    end_reason = "unknown"
    joined_at = None
    bot_lower = bot_name.lower()

    def set_status(status):
        with STATE_LOCK:
            STATE["bot"] = bot_name
            STATE["status"] = status
            STATE["present"] = len(present)

    def record(speaker, text, kind):
        nonlocal notes, seen_human
        ts = datetime.now().isoformat(timespec="seconds")
        entry = {"speaker": speaker, "text": text, "timestamp": ts, "kind": kind}
        transcript.append(entry)
        if speaker.lower() != bot_lower:
            seen.add(speaker)
            seen_human = True
        if notes is None:
            notes = LiveNotes()
        notes.add(entry)
        with STATE_LOCK:
            STATE["lines"].append({"speaker": speaker, "text": text, "time": hhmmss(ts), "kind": kind})
            if len(STATE["lines"]) > 1100:
                STATE["lines"] = STATE["lines"][-1000:]
        on_line(entry)

    leave_logged = {"v": False}

    def send_leave():
        try:
            proc.stdin.write(json.dumps({"command": "leave"}) + "\n")
            proc.stdin.flush()
            if not leave_logged["v"]:
                print("  Sent the leave command to the bot (asking it to leave the meeting).")
                leave_logged["v"] = True
        except Exception:
            pass

    print(f"Sending '{bot_name}' in via the bridge... (~30-90s to appear)")
    if display != "audio":
        print(f"  showing the '{display}' avatar on the bot's video tile")
    print("(press Ctrl+C, or leave the meeting, to make the bot leave)")
    try:
        while True:
            try:
                # Poll with a timeout so Ctrl-C is delivered within ~0.5s even during a
                # quiet stretch. A blocking get() with no timeout swallows the interrupt
                # on Windows until the next event arrives — that was the "Ctrl-C does
                # nothing until I leave the meeting" bug.
                ev = events.get(timeout=0.5)
            except queue.Empty:
                continue
            if ev is None:
                break
            et = ev.get("event") or ev.get("type") or ""
            if DEBUG:
                print(f"  [debug] {json.dumps(ev, ensure_ascii=False)}")

            if et == "call.created":
                print(f"  Call created: {ev.get('call_id')}")

            elif et == "call.bot_ready":
                joined_at = datetime.now()
                set_status("in meeting")
                print("In the meeting. Listening...\n")

            elif et in ("participant.joined", "meeting.participant_joined"):
                name = ev.get("name") or (ev.get("participant") or {}).get("name", "")
                if name and name.lower() != bot_lower:
                    present.add(name)
                    seen.add(name)
                    seen_human = True
                set_status("in meeting")
                print(f"  + {name or 'someone'} joined ({len(present)} here)")

            elif et in ("participant.left", "meeting.participant_left"):
                name = ev.get("name") or (ev.get("participant") or {}).get("name", "")
                present.discard(name)
                set_status("in meeting")
                print(f"  - {name or 'someone'} left ({len(present)} here)")
                if CONFIG["LEAVE_WHEN_EMPTY"] and seen_human and not present:
                    print("\nEveryone left - leaving.")
                    end_reason = "all_participants_left"
                    send_leave()

            elif et == "user.message":
                text = (ev.get("text") or "").strip()
                if text:
                    speaker = ev.get("speaker", "Unknown")
                    record(speaker, text, "speech")
                    print(f"  [{speaker}] {text}")

            elif et == "chat.received" and CONFIG["CAPTURE_CHAT"]:
                message = (ev.get("message") or "").strip()
                if message:
                    sender = ev.get("sender", "Unknown")
                    record(sender, message, "chat")
                    print(f"  [chat] {sender}: {message}")

            elif et == "call.ended":
                end_reason = ev.get("reason", end_reason)
                print(f"\nCall ended: {end_reason}")
                break
    except KeyboardInterrupt:
        end_reason = "interrupted"
        print("\nLeaving the meeting… (this can take a few seconds)")
    finally:
        send_leave()
        # Capture the call id (the bridge is still alive — it's in its own process
        # group) and DELETE the call. Wrapped so even an impatient second Ctrl-C
        # during teardown can't skip the DELETE and leave an orphan bot.
        call_id = box["call_id"]
        if call_id is None and proc.poll() is None:
            # Interrupted before the bridge reported the id — wait briefly for it
            # (Ctrl-C skips the wait; you're never stuck here).
            try:
                deadline = time.time() + 3
                while box["call_id"] is None and proc.poll() is None and time.time() < deadline:
                    time.sleep(0.1)
            except KeyboardInterrupt:
                pass
            call_id = box["call_id"]
        # Stop billing FIRST. DELETE ends the call now, while it's still live, so it
        # reliably returns 200 — deleting only after the bridge has already torn the
        # call down can miss it. Ending the call also makes the bridge exit promptly,
        # so we don't sit waiting on it (that wait is what made Ctrl-C feel frozen). A
        # Ctrl-C here just exits — the server-side alone-timeout reclaims the call.
        try:
            end_call(call_id)
        except KeyboardInterrupt:
            pass
        # Now close out the bridge. It exits on its own once the call ends (it's in its
        # own process group, so close it explicitly); kill it if it lingers.
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except (Exception, KeyboardInterrupt):
            try:
                proc.kill()
                proc.wait(timeout=4)
            except Exception:
                pass
        set_status("ended")
        duration = "unknown"
        if joined_at:
            mins = round((datetime.now() - joined_at).total_seconds() / 60)
            duration = f"{mins} minute" + ("" if mins == 1 else "s")
        meta = {"call_id": call_id, "end_reason": end_reason,
                "participants": sorted(seen), "duration": duration}
        on_meeting_end(transcript, meta)
        if transcript:
            if notes is None:
                notes = LiveNotes()
            print(f"\nSaved {len(transcript)} lines to: {notes.finalize(transcript, meta)}")
        else:
            print("\nNo transcript captured - nothing to save.")
            if end_reason == "interrupted":
                print("(Stopped before the bot finished joining — nothing was captured.)")
            elif joined_at is None:
                print("The bridge exited before joining. Its output:")
                _keys = ("error", "cannot find", "no module", "not found", "traceback", "exception")
                hot = [l for l in err_tail if any(k in l.lower() for k in _keys)]
                for line in (hot if hot else err_tail)[:6] or ["(no output captured)"]:
                    print("  " + line)
                print("First run? Make sure dependencies are installed:  pip install -r requirements.txt")


def main():
    parser = argparse.ArgumentParser(description="A silent meeting notetaker (runs on the AgentCall bridge).")
    parser.add_argument("meet_url", nargs="?", help="Google Meet / Zoom / Teams link")
    parser.add_argument("--name", default=CONFIG["BOT_NAME"], help="Bot display name")
    parser.add_argument("--format", default=CONFIG["OUTPUT_FORMAT"], choices=["md", "txt", "json"])
    parser.add_argument("--out", default=CONFIG["OUTPUT_DIR"], help="Folder to save notes in")
    parser.add_argument("--web", action=argparse.BooleanOptionalAction, default=CONFIG["WEB"],
                        help="Serve a live transcript page (--no-web to disable)")
    parser.add_argument("--port", type=int, default=CONFIG["WEB_PORT"], help="Port for the web page")
    parser.add_argument("--display", default=CONFIG["DISPLAY"],
                        help="Bot video tile: audio, pattern, ring, transcript, or your own (avatars/<name>.html or image)")
    args = parser.parse_args()

    # Not built yet? Send them to the builder (the test harness bypasses this).
    if not args.name and not os.environ.get("NOTETAKER_BRIDGE"):
        print("\nYour notetaker isn't built yet — let's build it:\n   python build.py\n")
        sys.exit(0)
    if not args.meet_url:
        print('Usage: python notetaker.py "<meeting-link>"     (build first: python build.py)')
        sys.exit(0)

    CONFIG["OUTPUT_FORMAT"] = args.format
    CONFIG["OUTPUT_DIR"] = args.out
    CONFIG["WEB"] = args.web
    CONFIG["WEB_PORT"] = args.port
    CONFIG["DISPLAY"] = args.display
    CONFIG["BOT_NAME"] = args.name
    STATE["bot"] = args.name

    if not API_KEY:
        print("No AgentCall API key found.")
        print("  Get one (free) at https://app.agentcall.dev/api-keys, then either:")
        print('    export AGENTCALL_API_KEY="ak_ac_..."')
        print('    or save {"api_key": "ak_ac_..."} to ~/.agentcall/config.json')
        sys.exit(1)

    if not (CONFIG.get("ALONE_SECONDS") and CONFIG["ALONE_SECONDS"] > 0):
        print("WARNING: ALONE_SECONDS is 0 in config.jsonc — the server-side auto-leave is OFF.")
        print("         If a shutdown is interrupted, the bot could keep billing. Set ALONE_SECONDS > 0.")

    if CONFIG["WEB"]:
        srv, _ = serve_html(lambda: _read_html(os.path.join(_PROJECT_ROOT, "avatars", "transcript.html")), CONFIG["WEB_PORT"])
        if srv:
            print(f"Live transcript: http://localhost:{CONFIG['WEB_PORT']}\n")

    try:
        run(args.meet_url, args.name, CONFIG["DISPLAY"])
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
