#!/usr/bin/env python3
"""Presenter — an AI bot that DELIVERS a presentation in your meeting.

It joins your Google Meet / Zoom / Teams call, shows your slides ON ITS CAMERA,
and narrates each one in its own voice, advancing automatically when it finishes
speaking. Runs on AgentCall's bundled bridge. No LLM required to present a deck.
MIT. https://agentcall.dev

    export AGENTCALL_API_KEY="ak_ac_your_key"
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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))   # scripts/
_ROOT = os.path.dirname(_HERE)                       # the skill folder (scripts/ -> presenter/)
_TILE = os.path.join(_ROOT, "assets", "slides.html")
_CONTROL_TILE = os.path.join(_ROOT, "assets", "control.html")
_AUDIO_JS = os.path.join(_ROOT, "assets", "agentcall-audio.js")
IMG_DIR = ""   # folder of slide images for image-based decks (set per deck in run())

API_BASE = os.environ.get("AGENTCALL_API_URL") or "https://api.agentcall.dev"
DEBUG = bool(os.environ.get("PRESENT_DEBUG"))
JOIN_DEADLINE = float(os.environ.get("PRESENT_JOIN_TIMEOUT", "180"))   # max seconds to wait for admission
SPEAK_WPS = float(os.environ.get("PRESENT_WPS", "2.6"))               # spoken words/sec — drives slide hold time
MIN_HOLD = 4.0                                                        # never show a slide for less than this
ALONE_GRACE = float(os.environ.get("PRESENT_ALONE_GRACE", "30"))     # leave this many seconds after everyone else left
REPLY_TIMEOUT = float(os.environ.get("PRESENT_REPLY_TIMEOUT", "30"))  # stop waiting for the agent's command after this
ENGAGE_WINDOW = float(os.environ.get("PRESENT_ENGAGE_WINDOW", "20"))  # after being addressed, keep listening (no name needed) this long
NAV_NARRATE_DELAY = float(os.environ.get("PRESENT_NAV_DELAY", "0.55"))  # after a manual jump settles, wait this long, then narrate (kept above the ~0.3s human double-tap gap)

# Known meeting-notetaker / bot display names — don't count them as humans for alone-detection.
# Conservative on purpose (no bare "bot") so a real participant is never mistaken for a bot.
_BOT_NAMES = ("otter", "fathom", "fireflies", "grain", "tl;dv", "tldv", "read.ai", "read ai",
              "notetaker", "note taker", "ai notetaker", "avoma", "circleback", "fellow.app",
              "spinach", "sembly", "fireflies.ai", "zoom ai companion", "copilot")


def looks_like_bot(name):
    n = (name or "").lower()
    return any(b in n for b in _BOT_NAMES)


def _fallback_deck(src):
    """A minimal one-slide deck so a conversion failure NEVER stops the bot from presenting."""
    base = os.path.splitext(os.path.basename(src))[0]
    name = (base.replace("_", " ").replace("-", " ").strip() or "Your document").title()
    out_dir = os.path.join(_ROOT, "decks", base or "deck")
    os.makedirs(out_dir, exist_ok=True)
    deck = {"title": name, "mode": "fallback",
            "slides": [{"title": name,
                        "notes": f"Here is {name}. I had trouble fully converting the file, "
                                 f"so this is a minimal version of it."}]}
    path = os.path.join(out_dir, "deck.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(deck, f, ensure_ascii=False, indent=2)
    return path


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


def load_api_key():
    key = os.environ.get("AGENTCALL_API_KEY", "")
    if key:
        return key
    for p in (os.path.join(_ROOT, ".env"), os.path.join(_HERE, ".env")):
        if os.path.exists(p):
            try:
                for line in open(p, encoding="utf-8"):
                    line = line.strip()
                    if line.startswith("AGENTCALL_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
            except OSError:
                pass
    cfg = os.path.join(os.path.expanduser("~"), ".agentcall", "config.json")
    if os.path.exists(cfg):
        try:
            return json.loads(open(cfg, encoding="utf-8").read()).get("api_key", "")
        except (OSError, ValueError):
            pass
    return ""


API_KEY = load_api_key()


def load_deck(path):
    """A deck is JSON: {"title": "...", "slides": [{"title","bullets":[...],"notes":"..."}]}
    or just a list of those slide objects. `notes` (a.k.a. `say`) is what the bot speaks."""
    data = json.loads(open(path, encoding="utf-8").read())
    title = ""
    slides = data
    if isinstance(data, dict):
        title = data.get("title", "")
        slides = data.get("slides", [])
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
    return title, norm


STATE = {"mode": "present", "deckTitle": "", "slide": 0, "total": 0,
         "status": "loading", "speaking": False, "bot": "", "rev": 0, "hush": 0, "playing": 0}
STATE_LOCK = threading.Lock()
DECK_JSON = "[]"   # slides (images inlined as data URIs) injected into slides.html — see build_deck_json


def _data_uri(path):
    """Read an image and return a base64 data URI. It's TEXT, so it survives the bridge's
    text-only tunnel intact — a normal binary <img> fetch gets corrupted (broken-image icon)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
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


def _make_handler():
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
                html = open(_TILE, encoding="utf-8").read().replace("__DECK_JSON__", DECK_JSON)
                return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            except OSError:
                return self._send(b"<h1>slides.html missing</h1>", "text/html; charset=utf-8")
    return _H


def serve(port=0):
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _make_handler())
    except OSError as e:
        print(f"Couldn't start the slide server ({e}).")
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
        srv = ThreadingHTTPServer(("127.0.0.1", port), _C)
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
        alone_timeout=120, max_duration=120):
    global IMG_DIR, DECK_JSON
    # A document (PDF / PowerPoint / Word)? Convert it to a deck (real slide images + text) first.
    import doc_to_deck
    if deck_path.lower().endswith(doc_to_deck.SUPPORTED):
        try:
            print(f"Converting {os.path.basename(deck_path)} to slides… this can take a few seconds.")
            deck_path = doc_to_deck.convert(deck_path, mode=mode)
            print(f"  Built deck: {deck_path}")
        except Exception as e:
            # Never hard-fail: degrade to a minimal deck so the bot can still present something.
            print(f"  Conversion problem ({e}) — falling back to a minimal deck so the bot can still present.")
            deck_path = _fallback_deck(deck_path)
    try:
        deck_title, slides = load_deck(deck_path)
    except Exception as e:
        print(f"Couldn't read the deck '{deck_path}': {e}")
        sys.exit(1)
    if not slides:
        print("The deck has no slides.")
        sys.exit(1)
    # Inline the slide images as data URIs so they survive the bridge's text tunnel.
    if any(s.get("image") for s in slides):
        IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(deck_path)), "img")
    DECK_JSON = build_deck_json(slides, IMG_DIR)

    no_notes_seconds = 5.0
    with STATE_LOCK:
        STATE.update(deckTitle=deck_title, total=len(slides),
                     bot=bot_name, status="ready", slide=0, speaking=False)

    _, port = serve(port)
    if not port:
        sys.exit(1)

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
        cmd = [sys.executable, bridge, meet_url, "--name", bot_name, "--ui-port", str(port)]
        # Server-enforced safety nets: the AgentCall backend ends the call (and billing) on its own
        # if everyone else leaves, or after a hard cap — even if THIS process dies unexpectedly.
        if alone_timeout and alone_timeout > 0:
            cmd += ["--alone-timeout", str(int(alone_timeout))]   # seconds alone → auto-leave
        if max_duration and max_duration > 0:
            cmd += ["--max-duration", str(int(max_duration))]     # minutes — hard cap
        kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
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

    def push(status=None, speaking=None):
        with STATE_LOCK:
            STATE["slide"] = max(0, G["i"])
            STATE["playing"] = 1 if G["autoplay"] else 0   # auto-advancing vs just showing a slide (drives the remote's Start/Pause)
            if status is not None:
                STATE["status"] = status
            if speaking is not None:
                STATE["speaking"] = speaking
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

    def speak_then(text, cb, tail=0.5):
        """Speak a line, then run cb after its estimated duration so the action follows the speech.
        (speech_secs already bakes in +1.4s of margin, so a small tail is plenty.)"""
        tts(text)
        t = threading.Timer(speech_secs(text) + tail, cb)
        t.daemon = True; t.start()

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
        tts(f"Hi everyone, I'm {bot_name}, your A I presenter."
            + (f" I've got a deck ready on {DECK_LABEL}." if DECK_LABEL else " I've got a deck ready.")
            + f" Just say my name — '{bot_name}, go ahead' — whenever you'd like me to present, and ask me"
              " anything as we go.")
        if proc:
            send({"command": "raise_hand"})
        G["conv"] = "offered"
        G["engaged_until"] = time.time() + ENGAGE_WINDOW   # right after greeting, the first reply needn't repeat the name

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
        speak_then(pick(_LINES_GO), lambda: events.put({"event": "begin_deck"}), tail=0.6)

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
            tts(pick(_LINES_PAUSE))

    def goto(i):
        """Manual jump to slide i (0-based): change the slide INSTANTLY (silent), then narrate the
        landed slide once the jump settles. It does NOT auto-advance — a manual jump is a step, not
        a run. Rapid taps keep re-arming, so only the slide you land on narrates; and the narration
        waits for any in-flight speech to finish first, so voices never overlap (the bridge can't cut
        TTS mid-sentence). To auto-advance through the deck hands-free, say 'go ahead'/'continue'."""
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
        t = threading.Timer(NAV_NARRATE_DELAY, lambda: events.put({"event": "nav_narrate", "step": step, "i": i}))
        t.daemon = True; t.start()
        G["nav_timer"] = t

    def nav_narrate(step, i):
        """Speak the slide a manual jump landed on — but only if we're still there (no newer jump,
        no auto-play took over) and it isn't a leave. No auto-advance: a manual jump stays put."""
        if step != G["step"] or G["conv"] != "presenting" or G["i"] != i:
            return
        s = slides[i]
        if s["notes"]:
            say(s["notes"])

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
        # Stop whatever the bot is currently saying, then an instant "I heard you" — so a question
        # mid-presentation gets immediate feedback (not silence) and the bot isn't talking over the
        # asker while the agent thinks. Flavored by context (about-to-start vs mid-talk), rotated so
        # it isn't the same words every time. The agent's reply plays right after.
        hush()
        tts(pick(_ACKS_START if G["conv"] == "offered" else _ACKS_MID))
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
        def _timeout():
            if G["awaiting"]:
                G["awaiting"] = set()
                if proc: send({"command": "set_state", "state": "listening"})
                if G["conv"] == "presenting" and G["autoplay"]:
                    resume_after_reply()
        t = threading.Timer(REPLY_TIMEOUT, _timeout)
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
            # The deck already moved past this exchange (we waited the reply window). Still VOICE a
            # late answer — better late than silently dropped — but never run a stale nav/lifecycle
            # command (it would fight the current slide/state).
            if action == "say":
                txt = (cmd.get("text") or "").strip()
                if txt:
                    hush(); say(txt)
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
        elif action:
            handle_control(_CMD_ALIAS.get(action, action), cmd.get("n"))
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
            time.sleep(0.12)

    def start_leaving(tail=0.5):
        """Leave the call cleanly: stop everything NOW (cancel every pending timer + any Q&A, cut the
        current speech), say goodbye, then end the call. No stale timer can fire during the exit."""
        cancel_timers()
        _clear_link()
        G["conv"] = "leaving"
        G["autoplay"] = False
        hush()
        push(status="leaving")
        speak_then(pick(_LINES_LEAVE), lambda: events.put({"event": "endcall"}), tail=tail)

    def handle_utterance(text, speaker=""):
        # No keyword matching. If the bot is addressed by name, or we're still inside the
        # follow-up window from a recent exchange, hand it to the agent (the brain) to interpret.
        # Otherwise stay quiet — ordinary meeting chatter is ignored.
        if not link_on or G["conv"] == "leaving":
            return
        if not (_addressed(text, bot_name) or time.time() < G["engaged_until"]):
            return
        # STT often emits the same utterance twice in quick succession — collapse a repeat of the same
        # words within ~2.5s to one (mirrors the goto duplicate-collapse), so it doesn't double-ack/answer.
        norm = " ".join((text or "").lower().split())
        last_text, last_t = G["last_utt"]
        now = time.time()
        if norm and norm == last_text and now - last_t < 2.5:
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
        print(f"  http://localhost:{port}/?ws=local")
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
            elif et == "call.bot_ready":
                G["ready"] = True
                if interactive:
                    print("In the meeting. Idle — offering to present (waiting for 'go ahead').\n")
                    offer()
                    if proc and ctrl_port:
                        send({"command": "webpage.open", "port": ctrl_port})   # share the control page
                else:
                    print("In the meeting. Starting the presentation…\n")
                    show(0)
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
                    print(f"  ▸ Controls page: {url}")
                    # Post the remote link to the meeting chat exactly ONCE (right after it's ready),
                    # not on every start/resume.
                    if proc and not G["ctrl_posted"]:
                        send({"command": "send_chat", "message": f"Steer this presentation from your phone → {url}"})
                        G["ctrl_posted"] = True
            elif et == "begin_deck":
                show(max(0, G["i"]))     # auto-play from the current slide (0 on a fresh start, or where browsing left off)
            elif et == "nav_narrate":
                nav_narrate(ev.get("step"), ev.get("i"))
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
            elif et == "call.ended":
                print(f"\nCall ended: {ev.get('reason', 'ended')}")
                break
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        G["alive"] = False
        cancel_timers()                 # no stale timer fires during teardown
        if not proc:
            return
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
            # Only disarm the atexit backstop if the DELETE was CONFIRMED. If it wasn't (abrupt drop,
            # network blip), leave torn=False so the last-resort atexit DELETE still fires on exit.
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
    ap.add_argument("--name", default="Presenter", help="bot display name")
    ap.add_argument("--voice", default="af_heart", help="TTS voice")
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
    args = ap.parse_args()

    # Live meetings are consent-driven (interactive) by default; --local auto-plays for a quick preview.
    if args.auto:
        interactive = False
    elif args.interactive:
        interactive = True
    else:
        interactive = not args.local

    if args.local:
        try:
            run("", args.deck, args.name, args.pace, args.voice, local=True, port=args.port,
                mode=args.mode, interactive=interactive)
        except KeyboardInterrupt:
            print("\nStopped.")
        return
    if not args.meet_url:
        print('Usage: python scripts/present.py "<meeting-link>" --deck <deck.json>   (or --local to preview)')
        sys.exit(0)
    if not API_KEY:
        print("No AgentCall API key found. Set AGENTCALL_API_KEY, or save it to ~/.agentcall/config.json,")
        print("or add AGENTCALL_API_KEY=... to a .env file in the presenter folder.")
        sys.exit(1)
    try:
        run(args.meet_url, args.deck, args.name, args.pace, args.voice, port=args.port,
            mode=args.mode, interactive=interactive,
            alone_timeout=args.alone_timeout, max_duration=args.max_duration)
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
