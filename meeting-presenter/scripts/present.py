#!/usr/bin/env python3
"""Presenter — an AI bot that DELIVERS a presentation in your meeting.

It joins your Google Meet / Zoom / Teams call, puts your slides on the meeting's MAIN STAGE as a
real screenshare (its face in a small camera tile), and narrates each one in its own voice,
advancing automatically when it finishes speaking. `--avatar-mode` shows the deck on the camera
tile instead. Runs on AgentCall's bundled bridge. No LLM required to present an authored deck.
MIT. https://agentcall.dev

    # key + name live in ~/.agentcall/config.json (the same file AgentCall uses)
    pip install -r requirements.txt
    python scripts/present.py "https://meet.google.com/abc-def-ghi" --deck decks/sample.json

    # preview the slides locally first (no meeting, auto-advances on timing):
    python scripts/present.py --local --deck decks/sample.json   # open the URL it prints
"""

import argparse
import atexit
import base64
import json
import mimetypes
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest, error as urlerror, parse as urlparse

# Emit UTF-8 on every platform so status glyphs (✗ ⚠ → ── ▸) never crash a Windows cp1252 console
# (errors="replace" keeps a legacy console from raising UnicodeEncodeError mid-print).
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))   # scripts/
_ROOT = os.path.dirname(_HERE)                       # the skill folder (scripts/ -> presenter/)
_TILE = os.path.join(_ROOT, "assets", "slides.html")       # the deck: bot's camera (avatar mode) OR the screenshare surface (default)
_AVATAR = os.path.join(_ROOT, "assets", "avatar.html")     # the bot's camera/face + narration audio (screenshare mode only)
_CONTROL_TILE = os.path.join(_ROOT, "assets", "control.html")
_AUDIO_JS = os.path.join(_ROOT, "assets", "agentcall-audio.js")
IMG_DIR = ""   # folder of slide images for image-based decks (set per deck in run())


def _require_assets():
    """Fail LOUD at startup if a bundled asset is missing, instead of discovering it only once the bot
    is in the call serving a '<file> missing' page or a silent '// missing' audio stub. These ship with
    the skill — a miss means a broken install, which should stop the launch, not degrade on-camera."""
    missing = [os.path.basename(p) for p in (_TILE, _AVATAR, _CONTROL_TILE, _AUDIO_JS) if not os.path.isfile(p)]
    if missing:
        print(f"  ✗ Missing bundled asset(s): {', '.join(missing)} (expected under {os.path.join(_ROOT, 'assets')}).",
              file=sys.stderr)
        print("    The skill install looks incomplete — reinstall/repull it before presenting.", file=sys.stderr)
        sys.exit(1)

API_BASE = os.environ.get("AGENTCALL_API_URL") or "https://api.agentcall.dev"
DEBUG = bool(os.environ.get("PRESENT_DEBUG"))
JOIN_DEADLINE = float(os.environ.get("PRESENT_JOIN_TIMEOUT", "300"))   # max seconds of NO progress before giving up on admission
                                                                       # (the clock resets on each join-progress event, so a slow admit never trips it)
SPEAK_WPS = float(os.environ.get("PRESENT_WPS", "2.6"))               # spoken words/sec — drives slide hold time
MIN_HOLD = 4.0                                                        # never show a slide for less than this
ALONE_GRACE = float(os.environ.get("PRESENT_ALONE_GRACE", "30"))     # leave this many seconds after everyone else left
REPLY_TIMEOUT = float(os.environ.get("PRESENT_REPLY_TIMEOUT", "45"))  # stop waiting for the agent's command after this (a late start/leave/answer is still honored)
ENGAGE_WINDOW = float(os.environ.get("PRESENT_ENGAGE_WINDOW", "20"))  # after being addressed, keep listening (no name needed) this long
NAV_NARRATE_DELAY = float(os.environ.get("PRESENT_NAV_DELAY", "0.35"))  # after a manual jump settles, wait this long, then narrate (kept just above the ~0.3s human double-tap gap; step-guard also dedupes)
HUSH_SETTLE = float(os.environ.get("PRESENT_HUSH_SETTLE", "0.35"))      # a confirmation spoken right after a hush waits this long so the hush lands FIRST (else it clears the confirmation); safe with the faster page poll
GREET_DELAY = float(os.environ.get("PRESENT_GREET_DELAY", "1.1"))       # screenshare only: wait for the avatar page to connect before the first greeting (floor ~1s so the first line isn't lost)

# Known meeting-notetaker / bot display names — don't count them as humans for alone-detection.
# Conservative on purpose (no bare "bot") so a real participant is never mistaken for a bot.
_BOT_NAMES = ("otter", "fathom", "fireflies", "grain", "tl;dv", "tldv", "read.ai", "read ai",
              "notetaker", "note taker", "ai notetaker", "avoma", "circleback", "fellow.app",
              "spinach", "sembly", "fireflies.ai", "zoom ai companion", "copilot")


def looks_like_bot(name):
    n = (name or "").lower()
    return any(b in n for b in _BOT_NAMES)


def speech_secs(text):
    """Estimated seconds to speak `text`. Slides are held this long so they never
    advance before the narration finishes (tts.done is unreliable in webpage mode)."""
    return len((text or "").split()) / SPEAK_WPS + 1.4


# ── Spoken confirmations — event-flavored and rotated so repeats don't sound robotic ──
_ACKS_START = ["On it — let's dive in.", "Sure thing — getting started.", "Great, let's get into it."]
_ACKS_MID   = ["One moment.", "Let me check that.", "Sure — one sec.", "On it."]
_LINES_GO    = ["Here we go.", "Let's get into it.", "Alright, starting now."]
_LINES_PAUSE = ["Okay, paused — say 'go ahead' to continue.",
                "Pausing here. Say 'go ahead' to pick back up.",
                "Paused — just say 'go ahead' whenever you're ready."]
_LINES_LEAVE = ["Sounds good — I'll head out. Thanks, everyone!",
                "Will do — heading off now. Thanks, all!",
                "You got it — leaving now. Thanks, everyone!"]
_PICK_I = {}
def pick(seq):
    """Rotate through phrasings in order (deterministic; no back-to-back repeats)."""
    i = _PICK_I.get(seq[0], -1) + 1
    _PICK_I[seq[0]] = i
    return seq[i % len(seq)]


# ── Voice control: the AI agent that launched this IS the brain ──
# There's no keyword matching here. Everything a participant says (once the bot is addressed
# by name, or during a short follow-up window) is handed to the agent via a file link; the
# agent reads it, decides what it means, and replies with a single command the bot runs.
# The one deterministic path is the companion control page (unambiguous buttons).
def _addressed(text, bot_name):
    """True if the bot is spoken to by name (whole word, any case)."""
    if not bot_name:
        return False
    low = " " + (text or "").lower() + " "
    return re.search(r"\b" + re.escape(bot_name.lower()) + r"\b", low) is not None


CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".agentcall", "config.json")


def load_config():
    """Read ~/.agentcall/config.json — the SAME file AgentCall uses. One config, one place: `api_key`,
    `default_bot_name`, `default_voice`. Missing/unreadable → {}. (No separate .env — see save_config.)"""
    if os.path.exists(CONFIG_PATH):
        try:
            data = json.loads(open(CONFIG_PATH, encoding="utf-8").read())
            if isinstance(data, dict):
                return data
            print(f"  ⚠ {CONFIG_PATH} isn't a JSON object — ignoring it.", file=sys.stderr)
        except (OSError, ValueError) as e:
            # A malformed config would SILENTLY drop the user's saved key/name/voice. Say so — don't
            # let a corrupt file look like a fresh install. (A genuinely missing file stays silent.)
            print(f"  ⚠ {CONFIG_PATH} exists but is unreadable/invalid JSON ({e}) — ignoring it.",
                  file=sys.stderr)
    return {}


def save_config(**updates):
    """Merge non-empty `updates` into ~/.agentcall/config.json (create the dir + file if absent,
    preserve every existing key). This is the AgentCall config, in AgentCall's format — so a brand-new
    presenter user ends up with exactly the file AgentCall itself would create, nothing bespoke. Use it
    to persist a first-run api_key (and an STT-friendly default_bot_name). Returns the merged dict."""
    cfg = load_config()
    cfg.update({k: v for k, v in updates.items() if v})
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return cfg


CONFIG = load_config()


def load_api_key():
    """Key resolution, AgentCall-style: the AGENTCALL_API_KEY env var (an explicit per-run override)
    wins, else config.json's `api_key`. SAME order as the bundled bridge resolves it, so both
    processes always use the same key. If neither, setup asks the user and saves it (see save_config)."""
    return os.environ.get("AGENTCALL_API_KEY", "") or CONFIG.get("api_key", "")


API_KEY = load_api_key()


def load_deck(path):
    """A deck is JSON: {"title": "...", "slides": [{"title","bullets":[...],"notes":"..."}]}
    or just a list of those slide objects. `notes` (a.k.a. `say`) is what the bot speaks.
    Returns (title, slides, needs_narration) — the flag is set by doc_to_deck when a converted deck
    has no authored narration, and drives the hard refusal in run() (parsed here once, no re-read).
    utf-8-sig: agents on Windows often author deck.json via PowerShell, which writes a UTF-8 BOM."""
    data = json.loads(open(path, encoding="utf-8-sig").read())
    title = ""
    slides = data
    needs_narration = False
    if isinstance(data, dict):
        title = data.get("title", "")
        slides = data.get("slides", [])
        needs_narration = bool(data.get("needs_narration"))
    norm = []
    for s in slides:
        if isinstance(s, str):
            s = {"title": s}
        norm.append({
            "title": s.get("title", ""),
            "bullets": s.get("bullets") or s.get("points") or [],
            "image": s.get("image", ""),
            "notes": (s.get("notes") or s.get("say") or "").strip(),
        })
    return title, norm, needs_narration


STATE = {"mode": "present", "deckTitle": "", "slide": 0, "total": 0,
         "status": "loading", "speaking": False, "bot": "", "rev": 0, "hush": 0, "playing": 0,
         "ctrl_url": ""}   # the phone-remote URL; pages show it on the standby screen (chat send is flaky)
STATE_LOCK = threading.Lock()
DECK_JSON = "[]"   # slides (images inlined as data URIs) injected into slides.html — see build_deck_json


def _data_uri(path):
    """Read an image and return a base64 data URI. It's TEXT, so it survives the bridge's
    text-only tunnel intact — a normal binary <img> fetch gets corrupted (broken-image icon)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        # A referenced slide image is missing — the slide would render blank. Don't abort the whole
        # deck over one image, but say so loudly so a dropped slide isn't invisible to the operator.
        print(f"  ⚠ slide image not readable: {path} — that slide will be blank.", file=sys.stderr)
        return ""
    mt = mimetypes.guess_type(path)[0] or "image/png"
    return f"data:{mt};base64," + base64.b64encode(raw).decode("ascii")


def build_deck_json(slides, img_dir):
    """The deck the page renders: title, bullets, and the slide image inlined as a data URI."""
    out = []
    for s in slides:
        img = s.get("image", "")
        if img and img_dir:
            img = _data_uri(os.path.join(img_dir, img))
        out.append({"title": s.get("title", ""), "bullets": s.get("bullets") or [], "image": img})
    return json.dumps(out, ensure_ascii=False).replace("</", "<\\/")  # safe inside an inline <script>"


def _make_handler(root_file, audio_on=True):
    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?", 1)[0]
            if p.startswith("/state.json"):
                with STATE_LOCK:
                    return self._send(json.dumps(STATE).encode("utf-8"), "application/json")
            if p.endswith("agentcall-audio.js"):
                try:
                    return self._send(open(_AUDIO_JS, encoding="utf-8").read().encode("utf-8"),
                                      "application/javascript")
                except OSError:
                    return self._send(b"// missing", "application/javascript")
            try:
                html = (open(root_file, encoding="utf-8").read()
                        .replace("__DECK_JSON__", DECK_JSON)
                        .replace("__AUDIO_ON__", "true" if audio_on else "false"))
                return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            except OSError:
                return self._send(f"<h1>{os.path.basename(root_file)} missing</h1>".encode("utf-8"),
                                  "text/html; charset=utf-8")
    return _H


class _QuietServer(ThreadingHTTPServer):
    """A browser tab closing mid-poll (very common at call-end, and more so with our fast 80ms polls)
    aborts its in-flight request — the stdlib server dumps a full traceback per abort, a scary flood
    that looks like a crash but is completely benign. Swallow the connection-reset/abort/broken-pipe
    family; let any genuinely unexpected error still surface."""
    daemon_threads = True
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], ConnectionError):   # reset / aborted / broken-pipe (WinError 10053 etc.)
            return
        super().handle_error(request, client_address)


def serve(port=0, root_file=_TILE, audio_on=True):
    """Serve one page (the slide page or the avatar page) + /state.json + the audio JS on its own port.
    audio_on=False is for the mute screenshare surface (slides on the meeting's main stage)."""
    try:
        srv = _QuietServer(("127.0.0.1", port), _make_handler(root_file, audio_on))
    except OSError as e:
        print(f"Couldn't start a page server ({e}).")
        return None, 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def serve_control(events_q, deck_json, port=0):
    """A second tiny server for the companion control page: serves control.html + /state.json,
    and turns a GET /cmd into the SAME internal 'control' event the voice handler produces.
    The full deck is injected so the page can show the current slide live (a 'TV' of the talk)."""
    class _C(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?", 1)[0]
            # Commands come in as GET ?intent=…&n=… — query strings survive the bridge's tunnel,
            # whereas a POST body gets dropped (the tunnel forwards GET cleanly, like /state.json).
            if p.endswith("/cmd"):
                q = urlparse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                events_q.put({"event": "control", "intent": q.get("intent", [""])[0], "n": q.get("n", [None])[0]})
                return self._send(b'{"ok":true}', "application/json")
            if p.startswith("/state.json"):
                with STATE_LOCK:
                    return self._send(json.dumps(STATE).encode("utf-8"), "application/json")
            try:
                html = open(_CONTROL_TILE, encoding="utf-8").read().replace("__DECK_JSON__", deck_json)
                return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            except OSError:
                return self._send(b"<h1>control.html missing</h1>", "text/html; charset=utf-8")

        def do_POST(self):
            if not self.path.split("?", 1)[0].endswith("/cmd"):
                return self._send(b"", "text/plain", 404)
            try:
                ln = int(self.headers.get("Content-Length", 0) or 0)
                cmd = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
            except Exception:
                cmd = {}
            events_q.put({"event": "control", "intent": cmd.get("intent", ""), "n": cmd.get("n")})
            return self._send(b'{"ok":true}', "application/json")
    try:
        srv = _QuietServer(("127.0.0.1", port), _C)
    except OSError as e:
        print(f"  (couldn't start the control server: {e})")
        return None, 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def end_call(call_id):
    """DELETE the call to stop billing. Returns True only when the teardown is CONFIRMED (200, or
    404/409 = already gone). Returns False if all retries are exhausted unconfirmed — the caller
    must then leave the atexit backstop armed so a last-resort DELETE still fires on exit."""
    if not call_id or not API_KEY:
        return True                           # nothing to tear down
    req = urlrequest.Request(f"{API_BASE}/v1/calls/{call_id}", method="DELETE",
                             headers={"Authorization": f"Bearer {API_KEY}"})
    for attempt in range(4):
        try:
            urlrequest.urlopen(req, timeout=10).read()
            print("  Call ended cleanly. Billing stopped.")
            return True
        except urlerror.HTTPError as e:
            if e.code in (404, 409):          # already gone — that counts as success
                print("  Call already ended. Billing stopped.")
                return True
            # other HTTP status — fall through and retry
        except Exception:
            # network error / timeout — these carry no .code; retry
            pass
        if attempt < 3:
            time.sleep(0.6 * (attempt + 1))   # 0.6 / 1.2 / 1.8s backoff
    print("  ⚠ Couldn't confirm the call stopped — it MAY still be billing.")
    print(f"    Stop it with:  DELETE {API_BASE}/v1/calls/{call_id}   (otherwise it expires on its own.)")
    return False


def run(meet_url, deck_path, bot_name, pace, voice, local=False, port=0, mode="auto", interactive=False,
        alone_timeout=120, max_duration=120, avatar_mode=False):
    global IMG_DIR, DECK_JSON
    _require_assets()   # a broken install should stop here, not go silent/blank on-camera
    # A document (PDF / PowerPoint / Word)? Convert it to a deck (real slide images + text) first.
    import doc_to_deck
    if deck_path.lower().endswith(doc_to_deck.SUPPORTED):
        # Convert up front, and FAIL FAST on any problem. We never walk into a live meeting with a
        # placeholder/apology deck — a bot that never joins beats one that presents something broken.
        print(f"Converting {os.path.basename(deck_path)} to slides… this can take a few seconds.")
        try:
            deck_path = doc_to_deck.convert(deck_path, mode=mode)
            print(f"  Built deck: {deck_path}")
        except Exception as e:
            print(f"\n  ✗ Couldn't convert {os.path.basename(deck_path)}: {e}", file=sys.stderr)
            print("    Fix the file (or install LibreOffice for Office rendering), or hand a deck.json.",
                  file=sys.stderr)
            sys.exit(1)
    try:
        deck_title, slides, needs_narration = load_deck(deck_path)
    except Exception as e:
        print(f"Couldn't read the deck '{deck_path}': {e}")
        sys.exit(1)
    if not slides:
        print("The deck has no slides.")
        sys.exit(1)
    # HARD STOP — never present un-narrated slides. A deck converted from a designed file with no
    # speaker notes (show OR generate) is flagged `needs_narration`; we refuse rather than read the
    # layout aloud (a word-reader, not a presenter). The agent authors `notes` from source.json, reruns.
    if needs_narration and not all(s["notes"] for s in slides):
        base = os.path.dirname(os.path.abspath(deck_path))
        print("\n  ✗ Refusing to present: these slides have NO narration authored.", file=sys.stderr)
        print(f"    Write a spoken `notes` line for every slide in:  {deck_path}", file=sys.stderr)
        print(f"    using the reference in:  {os.path.join(base, 'source.json')}  (+ the slide images).",
              file=sys.stderr)
        print("    Then relaunch. (This tool never reads slide text aloud — see SKILL.md: you own the narration.)",
              file=sys.stderr)
        sys.exit(2)
    # Inline the slide images as data URIs so they survive the bridge's text tunnel.
    if any(s.get("image") for s in slides):
        IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(deck_path)), "img")
    DECK_JSON = build_deck_json(slides, IMG_DIR)

    no_notes_seconds = 5.0
    with STATE_LOCK:
        STATE.update(deckTitle=deck_title, total=len(slides),
                     bot=bot_name, status="ready", slide=0, speaking=False)

    # Screenshare is the DEFAULT: the deck goes on the meeting's MAIN STAGE (a real screenshare) while a
    # small avatar page is the bot's camera/face + the narration voice. --avatar-mode falls back to the
    # old behaviour (the deck IS the camera tile). Screenshare only applies to a live meeting; --local
    # just previews the deck page. AgentCall routes TTS audio to the camera page, so the audio player +
    # the "hush" (instant pause/cut) live on the avatar page; the screenshare page is a mute renderer.
    screenshare_mode = not avatar_mode and not local
    ui_port = 0
    if screenshare_mode:
        _, ui_port = serve(0, root_file=_AVATAR, audio_on=True)   # bot's camera/face; TTS + hush play here
        if not ui_port:
            print("  (couldn't start the avatar page — falling back to avatar-tile mode)")
            screenshare_mode = False
    _, slides_port = serve(port, root_file=_TILE, audio_on=not screenshare_mode)
    if not slides_port:
        sys.exit(1)
    ss_port = 0
    if screenshare_mode:
        ss_port = slides_port        # the deck page is the screenshare surface
    else:
        ui_port = slides_port        # the deck page is the camera tile itself
    # Spacing a confirmation after a hush is only needed in screenshare mode, where the hush reaches the
    # separate avatar page via a tunnel-lagged poll. In avatar mode audio+hush share one local page, so
    # confirmations can fire INSTANTLY (keeps it snappy). Greeting likewise waits only in screenshare.
    settle = HUSH_SETTLE if screenshare_mode else 0.0
    greet_delay = GREET_DELAY if screenshare_mode else 0.0

    events = queue.Queue()
    box = {"call_id": None}
    proc = None
    err_tail = []

    # Companion control page (interactive only): buttons inject the SAME events as the voice handler.
    ctrl_port = 0
    if interactive:
        _, ctrl_port = serve_control(events, DECK_JSON)   # full deck → the control page's live "TV"

    if not local:
        bridge = os.environ.get("PRESENT_BRIDGE") or os.path.join(_ROOT, "engine", "bridge-visual.py")
        cmd = [sys.executable, bridge, meet_url, "--name", bot_name, "--ui-port", str(ui_port)]
        if screenshare_mode:
            cmd += ["--screenshare-port", str(ss_port)]   # the deck renders on the meeting's main stage
        # Surface short spoken commands ("next", "back") ~0.5s sooner than the 1.25s default by
        # shortening the bridge's end-of-utterance VAD window (still long enough not to split a phrase).
        cmd += ["--vad-timeout", os.environ.get("PRESENT_VAD_TIMEOUT", "0.8")]
        # Server-enforced safety nets: the AgentCall backend ends the call (and billing) on its own
        # if everyone else leaves, or after a hard cap — even if THIS process dies unexpectedly.
        if alone_timeout and alone_timeout > 0:
            cmd += ["--alone-timeout", str(int(alone_timeout))]   # seconds alone → auto-leave
        if max_duration and max_duration > 0:
            cmd += ["--max-duration", str(int(max_duration))]     # minutes — hard cap
        # Pin the bridge's pipes to UTF-8 with replacement so a stray non-ASCII byte from the bridge
        # (a participant name, an accented word) can never raise UnicodeDecodeError and kill a reader
        # thread. No-op on POSIX (pipes are already UTF-8); pure hardening on Windows.
        kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  text=True, encoding="utf-8", errors="replace", bufsize=1,
                  env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kw)

        def reader():
            try:
                for raw in proc.stdout:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        # Not an event — a plain diagnostic the bridge printed. Keep it for the error tail.
                        err_tail.append("[bridge stdout] " + raw)
                        if len(err_tail) > 30:
                            del err_tail[0]
                        continue
                    if box["call_id"] is None and (ev.get("event") or ev.get("type")) == "call.created":
                        box["call_id"] = ev.get("call_id")
                    events.put(ev)
            finally:
                events.put(None)
        threading.Thread(target=reader, daemon=True).start()

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

    G = {"i": -1, "step": 0, "adv_timer": None, "conv": "boot",
         "ready": False, "bridge_ok": True, "alive": True, "torn": False,
         "humans": set(), "had_human": False, "alone_timer": None,
         "ctrl_url": "", "ctrl_posted": False, "last_goto_t": 0.0, "last_goto_i": -1,
         "speak_until": 0.0, "nav_timer": None, "autoplay": False, "last_utt": ("", 0.0),
         "heard_seq": 0, "awaiting": set(), "reply_timer": None, "engaged_until": 0.0}

    # ── Agent link (interactive only): what the bot HEARS goes to heard.jsonl for the launching
    #    agent (Claude Code / Codex) to interpret; the agent writes a single command per line to
    #    commands.jsonl, which the bot runs. Fresh files each run so nothing stale leaks in. ──
    link_on = interactive
    link_dir = os.path.join(_ROOT, "link")
    heard_path = os.path.join(link_dir, "heard.jsonl")
    commands_path = os.path.join(link_dir, "commands.jsonl")
    if link_on:
        try:
            os.makedirs(link_dir, exist_ok=True)
            open(heard_path, "w", encoding="utf-8").close()
            open(commands_path, "w", encoding="utf-8").close()
        except OSError as e:
            print(f"  (couldn't set up the agent link folder {link_dir}: {e}) — voice control off.")
            link_on = False

    def send(obj):
        if not proc:
            return
        try:
            proc.stdin.write(json.dumps(obj) + "\n"); proc.stdin.flush()
        except Exception:
            # The bridge's stdin is gone — it has very likely exited. Don't fail silently.
            if G["alive"] and G["bridge_ok"]:
                print("  [present] lost the connection to the bridge — it may have exited.", file=sys.stderr)
            G["bridge_ok"] = False

    def push(status=None, speaking=None, ctrl_url=None):
        with STATE_LOCK:
            STATE["slide"] = max(0, G["i"])
            STATE["playing"] = 1 if G["autoplay"] else 0   # auto-advancing vs just showing a slide (drives the remote's Start/Pause)
            if status is not None:
                STATE["status"] = status
            if speaking is not None:
                STATE["speaking"] = speaking
            if ctrl_url is not None:
                STATE["ctrl_url"] = ctrl_url    # phone-remote URL, shown on the standby screen
            STATE["rev"] += 1

    def hush():
        """Cut the bot's CURRENT speech immediately. The bridge has no stop-TTS command, but the
        narration plays through our own slides page — bumping this tells that page to clear its audio
        player (same path barge-in uses). So pause / manual nav / leave / a question actually silence
        the bot now, instead of it talking on. The next tts.speak re-opens the audio gate on its own."""
        G["speak_until"] = 0.0
        with STATE_LOCK:
            STATE["hush"] += 1
            STATE["rev"] += 1

    def cancel_timers(*keys):
        """Cancel pending threading.Timers so a stale one can't fire onto the queue after a state change."""
        for key in (keys or ("adv_timer", "nav_timer", "reply_timer", "alone_timer")):
            tmr = G.get(key)
            if tmr:
                try: tmr.cancel()
                except Exception: pass
            G[key] = None

    def say(text):
        """Send the narration to the meeting (real TTS via the bridge; a log line in --local)."""
        push(speaking=True)
        G["speak_until"] = time.time() + speech_secs(text)   # ~when this finishes (can't stop TTS; gate on it)
        if proc:
            send({"command": "tts.speak", "text": text, "voice": voice})
        else:
            print(f"  🔊 {text[:90]}{'…' if len(text) > 90 else ''}")

    def tts(text):
        """Speak a conversational line (greeting/offer/acknowledgement — not slide narration)."""
        G["speak_until"] = time.time() + speech_secs(text)
        if proc:
            send({"command": "tts.speak", "text": text, "voice": voice})
        else:
            print(f"  🗣  {text}")

    def speak_after(delay, text, then=None, tail=0.6):
        """Speak `text` after `delay` seconds, on the MAIN loop (via a 'speak' event, so send() stays
        single-threaded). Used to space a confirmation just after a hush() — the hush reaches the audio
        page via a tunnel poll that can lag, so a confirmation sent immediately can be cleared by it.
        `then`: 'begin_deck' or 'endcall' to chain the next step once the line finishes."""
        ev = {"event": "speak", "text": text, "then": then, "tail": tail}
        if delay and delay > 0:
            t = threading.Timer(delay, lambda: events.put(ev)); t.daemon = True; t.start()
        else:
            events.put(ev)

    def show(i, narrate=True):
        """Show slide i. narrate=True (auto-play): speak the notes and arm the auto-advance.
        narrate=False (a manual jump): show it silently and stay put — no new speech piles up,
        which is the ONLY way to keep the bot from talking over a slide change in this mode
        (the bridge can't stop in-flight TTS; see the manual-nav note in show/goto)."""
        if G["conv"] == "leaving":
            return                       # a leave was requested — don't start another slide
        G["i"] = i
        G["step"] += 1
        step = G["step"]
        s = slides[i]
        print(f"  ── slide {i + 1}/{len(slides)}: {s['title'] or '(no title)'}{'' if narrate else '  (silent jump)'}")
        push(status="presenting")
        if narrate and s["notes"]:
            say(s["notes"])
            hold = max(MIN_HOLD, speech_secs(s["notes"]))
        else:
            push(speaking=False)
            hold = no_notes_seconds
        if not narrate:
            return                       # manual jump: no narration, no auto-advance — stay on this slide
        # Time-driven advance: hold each slide for its full estimated narration, THEN advance.
        # We deliberately do NOT advance on tts.done — in webpage mode it fires early/unreliably,
        # which raced the deck and made the bot leave mid-sentence. A timer can't race.
        t = threading.Timer(hold + pace, lambda: events.put({"event": "advance", "step": step}))
        t.daemon = True; t.start()
        G["adv_timer"] = t

    def advance(step=None):
        if G["conv"] == "leaving":
            return                       # leaving — stop advancing the deck
        if step is not None and step != G["step"]:
            return                       # stale timer from a slide we've already left
        ni = G["i"] + 1
        if ni < len(slides):
            show(ni)
        elif interactive:
            G["conv"] = "idle"
            push(status="idle", speaking=False)
            G["engaged_until"] = time.time() + ENGAGE_WINDOW   # let an immediate follow-up skip the name
            print("  Presentation complete — back to idle (waiting for the next request).")
            tts(f"That's the deck. Anything else, {'just say my name' if bot_name else 'just say the word'}.")
        else:
            push(status="done", speaking=False)
            print("  Presentation complete — wrapping up.")
            t = threading.Timer(max(3.0, pace + 2.0), lambda: events.put({"event": "endcall"}))
            t.daemon = True; t.start()

    # ── Interactive presenter: consent + control via meeting voice (deterministic, no LLM) ──
    DECK_LABEL = (deck_title or "").strip()

    def offer():
        """Introduce the bot and ask permission to present. Bot stays idle until told to go."""
        push(status="idle", speaking=False)
        greeting = (f"Hi everyone, I'm {bot_name}, your A I presenter."
                    + (f" I've got a deck ready on {DECK_LABEL}." if DECK_LABEL else " I've got a deck ready.")
                    + f" Just say my name — '{bot_name}, go ahead' — whenever you'd like me to present, and ask me"
                      " anything as we go.")
        # In screenshare mode the voice plays through the separate avatar page, which may still be
        # connecting when we join — wait a moment so the greeting isn't spoken into a page that can't
        # hear it yet. (Avatar-tile mode: the audio page is already up, so speak right away.)
        speak_after(greet_delay, greeting)
        if proc:
            send({"command": "raise_hand"})
        G["conv"] = "offered"
        G["engaged_until"] = time.time() + ENGAGE_WINDOW + greet_delay   # window covers the delayed greeting

    def start_presenting():
        G["conv"] = "presenting"
        G["autoplay"] = True         # walking the deck hands-free (narrate + auto-advance)
        # A pending manual-jump narration must not fire once auto-play takes over.
        nt = G.get("nav_timer")
        if nt:
            try: nt.cancel()
            except Exception: pass
        G["nav_timer"] = None
        # Reflect "presenting" in the shared state IMMEDIATELY. Otherwise the companion page's
        # optimistic Start→Pause flips back to "Start" after its ~1.3s hold, because the server only
        # reaches "presenting" when begin_deck → show(0) fires ~3s later (after the spoken greeting).
        push(status="presenting")
        # A question/nav just before this may have hushed — space "here we go" so it isn't cleared.
        speak_after(settle, pick(_LINES_GO), then="begin_deck", tail=0.6)

    def stop_presenting(say_line=True):
        for key in ("adv_timer", "nav_timer"):
            tmr = G.get(key)
            if tmr:
                try: tmr.cancel()
                except Exception: pass
            G[key] = None
        _clear_link()
        G["conv"] = "idle"
        G["autoplay"] = False
        hush()                       # actually stop the narration NOW, don't let it finish the sentence
        push(status="idle", speaking=False)
        if say_line:
            speak_after(settle, pick(_LINES_PAUSE))   # after the hush lands, so it isn't cleared too

    def goto(i, announce=None):
        """Manual jump to slide i (0-based): change the slide INSTANTLY (silent), then narrate the
        landed slide once the jump settles. `announce` (from the agent) is spoken as a lead-in before
        the slide's narration — e.g. "Sure, here's slide nine." — so a requested jump feels acknowledged
        instead of just cold-narrating. It does NOT auto-advance — a manual jump is a step, not a run."""
        i = max(0, min(len(slides) - 1, i))
        now = time.time()
        # Collapse only TRUE duplicates (same target within 0.25s — a double-fire of one press). A
        # distinct press targets a different slide and is never dropped.
        if i == G.get("last_goto_i", -1) and now - G.get("last_goto_t", 0) < 0.25:
            return
        G["last_goto_t"] = now
        G["last_goto_i"] = i
        for key in ("adv_timer", "nav_timer"):
            tmr = G.get(key)
            if tmr:
                try: tmr.cancel()
                except Exception: pass
            G[key] = None
        _clear_link()
        G["conv"] = "presenting"
        G["autoplay"] = False                      # a manual jump is a step, not a hands-free run
        hush()                                     # cut the old slide's narration so it doesn't bleed over
        show(i, narrate=False)                     # instant, silent slide change
        # Narrate the landed slide once the jump settles (rapid taps keep re-arming, so only the slide
        # you stop on speaks). hush() above already silenced the old narration, so no overlap.
        step = G["step"]
        t = threading.Timer(NAV_NARRATE_DELAY,
                            lambda: events.put({"event": "nav_narrate", "step": step, "i": i, "announce": announce}))
        t.daemon = True; t.start()
        G["nav_timer"] = t

    def nav_narrate(step, i, announce=None):
        """Speak the slide a manual jump landed on — but only if we're still there (no newer jump,
        no auto-play took over). An `announce` lead-in (if the agent gave one) is spoken first, joined
        with the slide's narration into ONE utterance so there's no gap. No auto-advance: it stays put."""
        if step != G["step"] or G["conv"] != "presenting" or G["i"] != i:
            return
        notes = slides[i]["notes"]
        text = ((announce.strip() + " " + notes).strip() if announce else notes)
        if text:
            say(text)

    # ── Agent-link helpers: forward what we heard; run the command the agent sends back ──
    def _append_jsonl(path, obj):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _clear_link():
        G["awaiting"] = set()
        t = G.get("reply_timer")
        if t:
            try: t.cancel()
            except Exception: pass
        G["reply_timer"] = None

    def resume_after_reply():
        # continue the deck from the current slide after a spoken reply (its narration already played)
        G["step"] += 1
        step = G["step"]
        t = threading.Timer(1.2, lambda: events.put({"event": "advance", "step": step}))
        t.daemon = True; t.start()
        G["adv_timer"] = t

    def forward(text, speaker=""):
        """Hand what we heard to the agent (the brain): pause the deck, write it to heard.jsonl, and
        wait for a command back. Extends the follow-up window so replies flow without re-addressing."""
        G["engaged_until"] = time.time() + ENGAGE_WINDOW
        # Pause both the auto-advance AND any pending manual-jump narration — while we're handling
        # what was said, nothing else should start speaking (no overlap with the agent's reply).
        for key in ("adv_timer", "nav_timer"):
            tmr = G.get(key)
            if tmr:
                try: tmr.cancel()
                except Exception: pass
            G[key] = None
        # Stop whatever the bot is currently saying, then acknowledge — so a question mid-presentation
        # gets feedback (not silence) and the bot isn't talking over the asker while the agent thinks.
        # Spaced just after the hush (so the hush doesn't clear the ack); flavored by context and rotated.
        hush()
        speak_after(settle, pick(_ACKS_START if G["conv"] == "offered" else _ACKS_MID))
        G["heard_seq"] += 1
        hid = G["heard_seq"]
        G["awaiting"].add(hid)
        i = max(0, G["i"])
        _append_jsonl(heard_path, {
            "id": hid, "speaker": speaker or "someone", "text": text,
            "slide": i + 1, "title": (slides[i].get("title", "") if slides else ""),
            "state": G["conv"], "ts": time.time()})
        if proc:
            send({"command": "set_state", "state": "thinking"})   # AgentCall convention: show it's processing
        print(f"  → heard #{hid} from {speaker or 'someone'}: {text!r}  (waiting for the agent)")
        old = G.get("reply_timer")
        if old:
            try: old.cancel()
            except Exception: pass
        # Timer threads must not touch send()/G directly (stdin writes aren't thread-safe) — route
        # the timeout through the event queue like every other async path.
        t = threading.Timer(REPLY_TIMEOUT, lambda: events.put({"event": "reply_timeout"}))
        t.daemon = True; t.start()
        G["reply_timer"] = t

    # commands the agent may send, mapped onto the same actions the control-page buttons use
    _CMD_ALIAS = {"resume": "present", "start": "present", "go": "present",
                  "pause": "stop", "hold": "stop", "exit": "leave"}

    def apply_command(cmd):
        """Run one command from the agent: say <text>, a nav/lifecycle action, or nothing."""
        hid = cmd.get("id")
        action = (cmd.get("cmd") or "").lower().strip()
        if hid is not None and hid not in G["awaiting"]:
            # The deck already moved past this exchange (we waited the reply window). Still honor the
            # replies that are valid ANY time — a spoken answer, and start/leave — so a slow "go ahead"
            # still starts and a slow "leave" still leaves. Only a stale nav/pause is dropped (the deck
            # has moved on, so applying it would jump to the wrong place).
            if action == "say":
                txt = (cmd.get("text") or "").strip()
                if txt:
                    hush()
                    speak_after(settle, txt)   # space it past the hush, like every post-hush utterance
            elif action in ("present", "start", "resume", "continue", "go", "yes"):
                if not G["autoplay"]:
                    start_presenting()
            elif action in ("leave", "exit"):
                start_leaving()
            return
        if hid is not None:
            G["awaiting"].discard(hid)
        else:
            G["awaiting"] = set()         # no id echoed — treat as resolving the exchange
        G["engaged_until"] = time.time() + ENGAGE_WINDOW
        spoke_only = action in ("say", "none", "")
        if action == "say":
            txt = (cmd.get("text") or "").strip()
            if txt:
                say(txt)
        elif action == "goto":
            # Jump straight to a slide, with an optional spoken lead-in ("say") so it feels acknowledged.
            try:
                goto(int(cmd.get("n")) - 1, announce=(str(cmd["say"]) if cmd.get("say") else None))
            except (TypeError, ValueError):
                pass
        elif action:
            intent = _CMD_ALIAS.get(action, action)
            if intent in ("leave", "present", "start", "yes", "resume", "continue",
                          "stop", "pause", "no", "next", "back", "restart", "repeat", "goto"):
                handle_control(intent, cmd.get("n"))
            else:
                # An unrecognized verb must not brick the deck (adv timer was cancelled while we
                # awaited this reply) — warn and treat it as a no-op so auto-play resumes below.
                print(f"  ⚠ unknown command {action!r} from the agent — ignoring it.")
                spoke_only = True
        # Resume the deck only for a spoken/no-op reply; nav & lifecycle commands re-arm their own
        # timing (goto→show, present→begin_deck, stop→idle, leave→leaving).
        if not G["awaiting"]:
            if proc:
                send({"command": "set_state", "state": "listening"})
            if spoke_only and G["conv"] == "presenting" and G["autoplay"]:
                resume_after_reply()

    def link_watch():
        """Tail commands.jsonl (the agent writes here) and feed each command into the event loop.
        Fast pickup, and robust to a truncated/rewritten file or a half-written final line."""
        seen = 0
        while G["alive"]:
            try:
                if os.path.exists(commands_path):
                    lines = open(commands_path, encoding="utf-8").read().splitlines()
                    if len(lines) < seen:       # file shrank (truncated/rotated) — re-read from the top
                        seen = 0
                    i = seen
                    while i < len(lines):
                        line = lines[i].strip()
                        if not line:
                            i += 1; continue
                        try:
                            c = json.loads(line)
                        except ValueError:
                            break               # likely a not-yet-flushed final line — retry it next tick
                        events.put({"event": "agent_command", "cmd": c})
                        i += 1
                    seen = i
            except OSError:
                pass
            time.sleep(0.06)   # tight poll so an agent command lands fast (local file read; cheap)

    def start_leaving(tail=0.5):
        """Leave the call cleanly: stop everything NOW (cancel every pending timer + any Q&A, cut the
        current speech), say goodbye, then end the call. No stale timer can fire during the exit."""
        cancel_timers()
        _clear_link()
        G["conv"] = "leaving"
        G["autoplay"] = False
        hush()
        push(status="leaving")
        # Space the goodbye after the hush lands (so it isn't cleared), then end the call.
        speak_after(settle, pick(_LINES_LEAVE), then="endcall", tail=tail)

    def handle_utterance(text, speaker=""):
        # No keyword matching. If the bot is addressed by name, or we're still inside the
        # follow-up window from a recent exchange, hand it to the agent (the brain) to interpret.
        # Otherwise stay quiet — ordinary meeting chatter is ignored.
        if not link_on or G["conv"] == "leaving":
            return
        if not (_addressed(text, bot_name) or time.time() < G["engaged_until"]):
            return
        # Being addressed keeps the follow-up window open NOW (not only after forward()), so a burst of
        # bare "next"/"back" repeats right after the name stays in-window instead of getting dropped.
        G["engaged_until"] = max(G["engaged_until"], time.time() + ENGAGE_WINDOW)
        # Collapse a TRUE STT double-fire — the same final transcript emitted twice within a short
        # window (STT repeats land sub-second). A DELIBERATE human retry ("it didn't take, say it
        # again") comes >1.2s later and MUST pass through — the old 2.5s window swallowed those, which
        # is a big part of "I said it 10 times and it only took once."
        norm = " ".join((text or "").lower().split())
        last_text, last_t = G["last_utt"]
        now = time.time()
        if norm and norm == last_text and now - last_t < 1.2:
            return
        G["last_utt"] = (norm, now)
        forward(text, speaker)

    def handle_control(intent, n=None):
        """A button on the companion page — same actions as voice, but unambiguous (no NLU)."""
        if DEBUG:
            print(f"  [control] {intent!r} n={n}")
        if intent == "leave":
            start_leaving(tail=0.6)
        elif intent in ("present", "start", "yes", "resume", "continue"):
            if not G["autoplay"]:                 # start/continue hands-free auto-play (works from idle OR a browsed slide)
                start_presenting()
        elif intent in ("stop", "pause", "no"):
            if G["conv"] == "presenting":
                stop_presenting()
        elif intent == "next":
            goto(0 if G["i"] < 0 else G["i"] + 1)  # before any slide, Next lands on slide 1 (not 2)
        elif intent == "back":
            goto(max(0, G["i"]) - 1)
        elif intent == "restart":
            goto(0)
        elif intent == "repeat":
            goto(max(0, G["i"]))
        elif intent == "goto" and n:
            try:
                goto(int(n) - 1)
            except (TypeError, ValueError):
                pass

    # Teardown safety nets so a bot is never orphaned (billing keeps running):
    #   • SIGTERM (orchestrator/`kill`) → behave like Ctrl+C so the finally block leaves + DELETEs.
    #   • atexit → last-resort DELETE if the finally somehow didn't run.
    #   • (server-side --alone-timeout/--max-duration already cover a hard kill / power loss.)
    if not local and proc:
        def _on_sigterm(*_a):
            raise KeyboardInterrupt
        try:
            signal.signal(signal.SIGTERM, _on_sigterm)
        except (ValueError, OSError):
            pass
        atexit.register(lambda: (end_call(box["call_id"]) if box.get("call_id") and not G["torn"] else None))

    push(status="ready")
    if link_on:
        threading.Thread(target=link_watch, daemon=True).start()
        print("  Voice control is ON — you (the agent) are the brain. Watch what the bot hears:")
        print(f"    {heard_path}")
        print("  …and reply with ONE command per line (e.g. {\"id\": 1, \"cmd\": \"next\"} or")
        print("   {\"id\": 1, \"cmd\": \"say\", \"text\": \"…\"}) here:")
        print(f"    {commands_path}")
    if local:
        print(f"\n[--local preview — no meeting]\n  Open this to watch:")
        print(f"  http://localhost:{slides_port}/?ws=local")
        if interactive and ctrl_port:
            print(f"  Controls:  http://localhost:{ctrl_port}/")
        if interactive:
            print(f"  Interactive test — type what a participant would SAY (address the bot by name, e.g.")
            print(f"  '{bot_name}, go ahead', '{bot_name}, next', '{bot_name} what's this?'). The agent watching")
            print("  the link files decides what to do. Ctrl+C to stop.\n")

            def _sim_input():
                for line in sys.stdin:
                    line = line.strip()
                    if line:
                        events.put({"event": "user.message", "speaker": "Tester", "text": line})
            threading.Thread(target=_sim_input, daemon=True).start()
        else:
            print("  (Ctrl+C to stop)\n")
        events.put({"event": "call.bot_ready"})       # kick off immediately
    else:
        verb = "join and offer to present" if interactive else "present"
        print(f"Sending '{bot_name}' in to {verb} '{deck_title or deck_path}' "
              f"({len(slides)} slides)… (~30-90s to appear)")
        print("(press Ctrl+C, or end the meeting, to stop)\n")

    launch_t = time.time()
    try:
        while True:
            try:
                ev = events.get(timeout=0.5)
            except queue.Empty:
                # Never got admitted? Don't block forever waiting on a bot that's stuck outside.
                if not local and not G["ready"] and (time.time() - launch_t) > JOIN_DEADLINE:
                    print(f"\nTimed out after {int(JOIN_DEADLINE)}s waiting to be admitted to the meeting. Leaving.")
                    break
                # Bridge process died after admission? End promptly (and stop billing) instead of
                # idling until stdout EOF happens to arrive.
                if proc is not None and proc.poll() is not None:
                    print("\nThe bridge process exited — wrapping up.")
                    break
                continue
            if ev is None:
                break
            et = ev.get("event") or ev.get("type") or ""
            if DEBUG:
                print(f"  [debug] {json.dumps(ev, ensure_ascii=False)}")
            if et == "call.created":
                print(f"  Call created: {ev.get('call_id')}")
            elif et in ("call.bot_joining", "call.bot_joining_meeting", "call.tunnel_ready",
                        "call.bot_waiting_room", "call.bot_joined"):
                # Still making progress toward joining — reset the no-progress clock so a slow admit
                # (waiting-room, host takes a while to click Admit) never trips the join deadline.
                launch_t = time.time()
                if et == "call.bot_waiting_room":
                    print(f"  In the lobby — waiting to be admitted. Please click 'Admit' for '{bot_name}'.")
            elif et == "call.bot_ready":
                G["ready"] = True
                # Screenshare mode: put the deck on the meeting's main stage — ONCE. Slides then advance
                # in place via state.json (never stop/swap), which avoids the known screenshare-wedge bug.
                if screenshare_mode and proc and ss_port:
                    send({"command": "screenshare.start", "port": ss_port})
                    print("  ▸ Sharing the deck to the meeting (screenshare).")
                if interactive:
                    print("In the meeting. Idle — offering to present (waiting for 'go ahead').\n")
                    offer()
                    if proc and ctrl_port:
                        send({"command": "webpage.open", "port": ctrl_port})   # share the control page
                else:
                    print("In the meeting. Starting the presentation…\n")
                    # In screenshare mode the audio plays on the SEPARATE avatar page, which may still
                    # be connecting its WebSocket at join — wait the same greet floor the interactive
                    # path uses so the first slide's narration isn't lost. (0 in avatar/local mode.)
                    if greet_delay > 0:
                        threading.Timer(greet_delay, lambda: events.put({"event": "auto_begin"})).start()
                    else:
                        show(0)
            elif et == "auto_begin":
                show(0)   # --auto screenshare: start the deck after the greet floor (avatar WS connected)
            elif et == "user.message":
                if interactive:
                    handle_utterance(ev.get("text", ""), ev.get("speaker", ""))
            elif et == "control":
                if interactive:
                    handle_control(ev.get("intent", ""), ev.get("n"))
            elif et == "webpage.opened":
                url = ev.get("url", "")
                if url:
                    G["ctrl_url"] = url
                    print(f"  ▸ Controls page (relay this to the user — it's the instant remote): {url}")
                    # Show the remote ON the standby screen the meeting already sees — the reliable path,
                    # since Meet chat-send drops non-deterministically. Both page servers serve STATE, so
                    # this needs no extra plumbing.
                    push(ctrl_url=url)
                    # AND post it to the meeting chat ONCE (a clickable convenience). The on-screen URL
                    # above is the reliable path, so no noisy retries — a single dropped chat is fine.
                    if proc and not G["ctrl_posted"]:
                        G["ctrl_posted"] = True
                        send({"command": "send_chat",
                              "message": f"Steer this presentation from your phone → {url}"})
            elif et == "screenshare.started":
                print("  ▸ Screenshare is live — the deck is on the meeting's main stage.")
            elif et == "screenshare.error":
                print(f"  [screenshare] {ev.get('message', 'error')} — the deck may not be visible on the main stage.")
            elif et == "speak":
                tts(ev.get("text", ""))
                nxt = ev.get("then")
                if nxt in ("begin_deck", "endcall"):
                    t = threading.Timer(speech_secs(ev.get("text", "")) + ev.get("tail", 0.6),
                                        lambda n=nxt: events.put({"event": n}))
                    t.daemon = True; t.start()
            elif et == "begin_deck":
                # Guard: the chain timer that queued this isn't cancellable, so a pause/leave between
                # "Here we go" and now must win — only start the deck if auto-play is still wanted.
                if G["autoplay"] and G["conv"] == "presenting":
                    show(max(0, G["i"]))     # from the current slide (0 fresh, or where browsing left off)
            elif et == "nav_narrate":
                nav_narrate(ev.get("step"), ev.get("i"), ev.get("announce"))
            elif et == "reply_timeout":
                # The agent didn't answer within REPLY_TIMEOUT — stop waiting and resume the deck.
                if G["awaiting"]:
                    G["awaiting"] = set()
                    if proc:
                        send({"command": "set_state", "state": "listening"})
                    if G["conv"] == "presenting" and G["autoplay"]:
                        resume_after_reply()
            elif et == "tts.done":
                push(speaking=False)        # visual only — slide timing is time-driven, not tts.done-driven
            elif et == "advance":
                advance(ev.get("step"))
            elif et == "agent_command":
                if interactive:
                    apply_command(ev.get("cmd") or {})
            elif et == "participant.joined":
                nm = ev.get("name", "")
                if interactive and nm and nm.lower() != bot_name.lower() and not looks_like_bot(nm):
                    G["humans"].add(nm); G["had_human"] = True
                    if G["alone_timer"]:
                        G["alone_timer"].cancel(); G["alone_timer"] = None     # someone's back
            elif et == "participant.left":
                nm = ev.get("name", "")
                if interactive:
                    G["humans"].discard(nm)
                    # everyone (human) has left after at least one was here → leave after a short grace
                    if G["had_human"] and not G["humans"] and not G["alone_timer"]:
                        t = threading.Timer(ALONE_GRACE, lambda: events.put({"event": "alone_timeout"}))
                        t.daemon = True; t.start(); G["alone_timer"] = t
            elif et == "alone_timeout":
                if G["had_human"] and not G["humans"]:
                    print(f"\nEveryone else has left — leaving (after {int(ALONE_GRACE)}s alone).")
                    break
            elif et == "endcall":
                send({"command": "leave"})
                if local:
                    events.put(None)
            elif et == "error":
                print(f"  [bridge] error: {ev.get('message', '')}")
            elif et == "tts.error":
                print(f"  [tts] narration failed: {ev.get('reason', ev.get('message', ''))} — "
                      "the current slide may play silent.")
            elif et == "webpage.error":
                print(f"  [controls] {ev.get('message', 'error')} — the remote-control page may be unavailable.")
            elif et == "call.max_duration_warning":
                print(f"  [call] {ev.get('message', 'approaching the max-duration cap — the call will end soon.')}")
            elif et == "call.credits_low":
                print(f"  [call] {ev.get('message', 'AgentCall credits are low — the call may be cut short.')}")
            elif et == "call.ended":
                print(f"\nCall ended: {ev.get('reason', 'ended')}")
                break
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        G["alive"] = False
        cancel_timers()                 # no stale timer fires during teardown
        # NOTE: no `return` in this finally — a return here would swallow an in-flight exception
        # (in --local, proc is None and a handler crash would silently exit 0). Guard with `if proc:`.
        if proc:
            send({"command": "leave"})
            call_id = box["call_id"]
            if call_id is None and proc.poll() is None:
                try:
                    deadline = time.time() + 3
                    while box["call_id"] is None and proc.poll() is None and time.time() < deadline:
                        time.sleep(0.1)
                except KeyboardInterrupt:
                    pass
                call_id = box["call_id"]
            try:
                # Only disarm the atexit backstop if the DELETE was CONFIRMED. If it wasn't (abrupt
                # drop, network blip), leave torn=False so the last-resort atexit DELETE still fires.
                G["torn"] = end_call(call_id)
            except KeyboardInterrupt:
                pass
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except (Exception, KeyboardInterrupt):
                try:
                    proc.kill(); proc.wait(timeout=4)
                except Exception:
                    pass
            if call_id is None:
                print("The bridge exited before joining. Recent output:")
                for line in err_tail[-6:] or ["(no output)"]:
                    print("  " + line)
                print("First run? Install dependencies:  pip install -r requirements.txt")


def main():
    ap = argparse.ArgumentParser(description="Presenter — an AI bot that delivers your slides in a meeting (AgentCall).")
    ap.add_argument("meet_url", nargs="?", help="Google Meet / Zoom / Teams link")
    ap.add_argument("--deck", required=True,
                    help="a deck JSON, or a document to present: .pdf, .pptx/.ppt, .docx/.doc")
    ap.add_argument("--name", default=None,
                    help="bot display name. Default: `default_bot_name` from ~/.agentcall/config.json, else "
                         "'Presenter'. Pick a short, distinctive, STT-friendly name — long/generic names get "
                         "mis-transcribed (steer/wake by voice suffers).")
    ap.add_argument("--voice", default=None,
                    help="TTS voice. Default: `default_voice` from ~/.agentcall/config.json, else af_heart")
    ap.add_argument("--pace", type=float, default=1.0, help="seconds to pause between slides")
    ap.add_argument("--port", type=int, default=0, help="serve the slide page on a fixed port (0 = pick a free one)")
    ap.add_argument("--mode", choices=["auto", "show", "generate"], default="auto",
                    help="for a document: show real pages, generate a deck to author, or auto (default)")
    ap.add_argument("--interactive", action="store_true",
                    help="join, introduce, and wait for a spoken 'go ahead' before presenting (default for live)")
    ap.add_argument("--auto", action="store_true",
                    help="present immediately on join without asking (the original one-shot behaviour)")
    ap.add_argument("--alone-timeout", type=int, default=120,
                    help="bot auto-leaves this many seconds after everyone else has left (0 = never)")
    ap.add_argument("--max-duration", type=int, default=120,
                    help="hard cap in minutes; the server ends the call after this no matter what (0 = none)")
    ap.add_argument("--local", action="store_true", help="preview the deck locally, no meeting")
    ap.add_argument("--avatar-mode", action="store_true",
                    help="show the deck on the bot's CAMERA tile (the original mode). Default is screenshare "
                         "— the deck on the meeting's main stage with the bot's face in a small tile.")
    args = ap.parse_args()

    # Resolve name/voice: explicit --flag → ~/.agentcall/config.json → built-in default.
    bot_name = args.name or CONFIG.get("default_bot_name") or "Presenter"
    voice = args.voice or CONFIG.get("default_voice") or "af_heart"

    # Live meetings are consent-driven (interactive) by default; --local auto-plays for a quick preview.
    if args.auto:
        interactive = False
    elif args.interactive:
        interactive = True
    else:
        interactive = not args.local

    if args.local:
        try:
            run("", args.deck, bot_name, args.pace, voice, local=True, port=args.port,
                mode=args.mode, interactive=interactive, avatar_mode=args.avatar_mode)
        except KeyboardInterrupt:
            print("\nStopped.")
        return
    if not args.meet_url:
        print('Usage: python scripts/present.py "<meeting-link>" --deck <deck.json>   (or --local to preview)',
              file=sys.stderr)
        sys.exit(2)   # a usage error is an error — never exit 0 without doing anything
    if not API_KEY:
        print("No AgentCall API key found. Add it to ~/.agentcall/config.json as {\"api_key\": \"ak_ac_...\"}")
        print("(the same file AgentCall uses), or set the AGENTCALL_API_KEY env var. Free key: agentcall.dev/api-keys.")
        sys.exit(1)
    try:
        run(args.meet_url, args.deck, bot_name, args.pace, voice, port=args.port,
            mode=args.mode, interactive=interactive,
            alone_timeout=args.alone_timeout, max_duration=args.max_duration, avatar_mode=args.avatar_mode)
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
