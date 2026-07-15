#!/usr/bin/env python3
"""Standup Manager — an audio-only bot that RUNS your daily standup, then stays
on as your meeting summary manager.

It joins your Google Meet / Zoom / Teams call as a voice-only participant (no
camera, no screen-share — the cheapest, lightest mode) and:

  • greets the room and waits for a go-ahead,
  • calls on each teammate BY NAME, in order (only those present),
  • KEEPS TIME — a gentle nudge near the cap, then it moves on,
  • listens to each update and keeps a running summary + action items,
  • remembers blockers ACROSS DAYS and follows up on them,
  • when the round's done, POSTS THE SUMMARY IN THE CHAT and STAYS in the call —
    if someone new joins it raises a hand and takes their standup too; if someone
    asks, it reads the summary back,
  • leaves only when asked by name, or when everyone else has left — and always
    stops billing on the way out.

The launching agent is the brain (like a great chair): the bot forwards what it
hears to `link/heard.jsonl`, and the agent writes back one line to
`link/commands.jsonl`. No brittle keyword matching — the agent interprets. With
no agent attached it still runs the whole round on timing alone.

Runs on AgentCall's audio bridge. MIT. https://agentcall.dev

    pip install -r requirements.txt
    export AGENTCALL_API_KEY="ak_ac_your_key"      # or ~/.agentcall/config.json
    python scripts/standup.py "https://meet.google.com/abc-def-ghi"

    # Dry-run the whole flow with no meeting and no bot:
    python scripts/standup.py --local
"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib import request as urlrequest

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)                       # repo root (parent of scripts/)
_BRIDGE = os.path.join(_ROOT, "engine", "bridge.py")


# ── config + secrets ─────────────────────────────────────────────────────────
def _load_config():
    p = os.path.join(_ROOT, "config.jsonc")
    try:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        text = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                      lambda m: m.group(1) or "", text, flags=re.S)
        return json.loads(text)
    except FileNotFoundError:
        print(f"config.jsonc not found at {p}. Copy the sample and edit it.")
        sys.exit(1)
    except Exception as e:
        print(f"Couldn't read config.jsonc ({e}). Check it for typos (JSON + // comments).")
        sys.exit(1)


CONFIG = _load_config()


def _load_dotenv():
    for d in (_ROOT, _HERE):
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

API_BASE = os.environ.get("AGENTCALL_API_URL") or "https://api.agentcall.dev"
DEBUG = bool(os.environ.get("STANDUP_DEBUG"))


def _agentcall_config():
    p = os.path.join(os.path.expanduser("~"), ".agentcall", "config.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.loads(fh.read())
        except (OSError, ValueError):
            pass
    return {}


_ACCFG = _agentcall_config()
API_KEY = os.environ.get("AGENTCALL_API_KEY", "") or _ACCFG.get("api_key", "")


# ── keyword heuristics — used ONLY by the --local simulator to stand in for a ──
# real agent's judgment. The ENGINE never calls these: what's a blocker, what's a
# fix, and whether a follow-up cleared are all the launching agent's calls. They
# live here so `--local` (which plays the agent) can dry-run without a model.
def _has(text, words):
    t = " " + re.sub(r"[^a-z0-9' ]+", " ", text.lower()) + " "
    return any(f" {w} " in t for w in words)


_NO_BLOCKER = ["no blockers", "no blocker", "nothing blocking", "not blocked", "no blocks",
               "nothing", "none", "all good", "im good", "i'm good", "no issues", "nope"]
_BLOCKER_CUE = ["blocked", "blocker", "blockers", "stuck", "waiting on", "waiting for",
                "cant", "can't", "cannot", "unable", "need help", "need access",
                "need a review", "depends on", "dependent on", "held up", "issue with",
                "problem with", "bug in", "broken", "failing", "not working", "waiting"]
_YES = ["yes", "yeah", "yep", "yup", "resolved", "cleared", "clear", "sorted", "fixed",
        "unblocked", "done", "good now", "all set", "handled", "sorted out"]
_NO = ["no", "not yet", "nope", "still", "same", "ongoing", "blocked", "not done",
       "in progress", "still stuck", "still waiting", "not resolved"]


def detect_blockers(text):
    """Pull blocker-ish sentences out of a free-form update. Conservative; the
    agent can rewrite these into cleaner blockers/action items."""
    if not text.strip():
        return []
    if len(text.split()) <= 6 and _has(text, _NO_BLOCKER) and not _has(text, _BLOCKER_CUE):
        return []
    out = []
    for raw in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = raw.strip(" .,-")
        if not s:
            continue
        low = s.lower()
        if _has(low, _BLOCKER_CUE):
            if re.search(r"\b(no|not|nothing|zero)\b[^.]{0,20}\b(block|stuck|issue|wait)", low):
                continue
            out.append(s if len(s) <= 160 else s[:157] + "…")
    seen, uniq = set(), []
    for b in out:
        k = b.lower()
        if k not in seen:
            seen.add(k); uniq.append(b)
    return uniq[:4]


def resolution_of(text):
    """Interpret a yes/no answer to 'is that cleared?'. True / False / None."""
    yes, no = _has(text, _YES), _has(text, _NO)
    if no and not text.strip().lower().startswith(("yes", "yeah", "yep")):
        return False
    if yes:
        return True
    return False if no else None


def match_name(spoken, roster_name):
    a, b = (spoken or "").strip().lower(), (roster_name or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    at, bt = a.split(), b.split()
    if at and bt and at[0] == bt[0]:
        return True
    return a.startswith(b) or b.startswith(a)


def addressed_to(text, bot_name):
    """True if the utterance calls the bot by name (the wake word)."""
    if not bot_name:
        return False
    return re.search(rf"\b{re.escape(bot_name.lower())}\b", (text or "").lower()) is not None


# ── cross-day history ──────────────────────────────────────────────────────────
def load_history(path):
    try:
        with open(path, encoding="utf-8") as fh:
            h = json.loads(fh.read())
        h.setdefault("people", {})
        h.setdefault("sessions", [])
        return h
    except (OSError, ValueError):
        return {"people": {}, "sessions": []}


def save_history(path, hist):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(hist, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print(f"  (couldn't save cross-day history: {e})")


def open_blockers_for(hist, roster_name):
    for person, rec in hist.get("people", {}).items():
        if match_name(person, roster_name) or match_name(roster_name, person):
            return person, list(rec.get("open_blockers", []))
    return roster_name, []


# ── AgentCall REST: stop billing on the way out ────────────────────────────────
def end_call(call_id):
    if not call_id or not API_KEY:
        return False
    req = urlrequest.Request(f"{API_BASE}/v1/calls/{call_id}", method="DELETE",
                             headers={"Authorization": f"Bearer {API_KEY}"})
    for attempt in range(4):
        try:
            resp = urlrequest.urlopen(req, timeout=10)
            resp.read()
            print(f"  Call ended cleanly ({resp.getcode()}). Billing stopped.")
            return True
        except Exception as e:
            code = getattr(e, "code", None)
            if code in (404, 409):
                print(f"  Call already ended ({code}). Billing stopped.")
                return True
            if attempt < 3:
                time.sleep(0.6 * (attempt + 1))
    print(f"  (couldn't confirm the call stopped; it expires on its own. call_id={call_id})")
    return False


# ── blockers are {"text": str, "solution": str|None} ──────────────────────────
def _blk_text(b):
    return b["text"] if isinstance(b, dict) else str(b)


def _blk_solution(b):
    return b.get("solution") if isinstance(b, dict) else None


# ── summary + action items ─────────────────────────────────────────────────────
def action_items(present, updates):
    """One action item per blocker. If a fix surfaced in the room, the item is the
    fix (owner + plan); otherwise it's the open blocker to chase."""
    items = []
    for n in present:
        for b in updates.get(n, {}).get("blockers", []):
            t, sol = _blk_text(b), _blk_solution(b)
            items.append({"owner": n, "text": f"{t} → {sol}" if sol else t})
    return items


def _session_of(bot, present, absent, updates, start_ts):
    return {"bot": bot, "present": list(present), "absent": list(absent),
            "duration_sec": int(time.time() - start_ts),
            "updates": {n: {k: v for k, v in updates[n].items()
                            if k != "utterances" and not k.startswith("_")}
                        for n in present if n in updates},
            "action_items": action_items(present, updates)}


def build_summary_text(session, fmt, stamp):
    date, tstr = stamp.strftime("%Y-%m-%d"), stamp.strftime("%H:%M")
    present, absent = session["present"], session["absent"]
    updates = session["updates"]
    items = session.get("action_items", [])
    dur = session["duration_sec"]
    if fmt == "json":
        return json.dumps({**session, "date": date, "time": tstr}, ensure_ascii=False, indent=2)

    md = fmt != "txt"
    L = []
    head = f"Standup — {date} {tstr}"
    L += ([f"# {head}", ""] if md else [head, "=" * len(head), ""])
    meta = (f"**Facilitated by** {session['bot']} · **Present:** {', '.join(present) or '—'} "
            f"· **Duration:** {dur // 60}m {dur % 60}s") if md else \
           (f"Facilitated by {session['bot']} | Present: {', '.join(present) or '-'} "
            f"| {dur // 60}m {dur % 60}s")
    L.append(meta)
    if absent:
        L.append((f"**Absent:** {', '.join(absent)}") if md else f"Absent: {', '.join(absent)}")
    L.append("")
    for n in present:
        u = updates.get(n)
        if not u:
            continue
        L.append(f"## {n}" if md else f"{n}:")
        for r in u.get("resolved_followups", []):
            mark = ("✅ resolved" if r["resolved"] else "⏳ still open")
            L.append((f"- _Follow-up:_ “{r['text']}” — {mark}") if md
                     else f"  (follow-up) {r['text']} -> {'resolved' if r['resolved'] else 'still open'}")
        body = "_No update._" if u.get("no_update") else ((u.get("text") or "").strip() or "_(nothing captured)_")
        L.append(body if md else "  " + body.replace("_No update._", "[no update]"))
        if u.get("blockers"):
            parts = [_blk_text(b) + (f" (fix: {_blk_solution(b)})" if _blk_solution(b) else "")
                     for b in u["blockers"]]
            L.append(("🚩 **Blockers:** " if md else "  Blockers: ") + "; ".join(parts))
        L.append("")
    L.append("## Action items" if md else "Action items:")
    L += ([("- ☐ " if md else "  - ") + f"{it['owner']}: {it['text']}" for it in items]
          or [("_None._" if md else "  none")])
    return "\n".join(L) + "\n"


def chat_summary(session, stamp):
    """A compact, single chat message (Meet chat drops rapid bursts — send ONE)."""
    L = [f"📋 Standup summary — {stamp.strftime('%Y-%m-%d %H:%M')}"]
    for n in session["present"]:
        u = session["updates"].get(n, {})
        if u.get("no_update"):
            L.append(f"• {n}: (no update)")
        else:
            txt = (u.get("text") or "").strip()
            L.append(f"• {n}: {txt[:180] + '…' if len(txt) > 180 else txt}")
    items = session.get("action_items", [])
    if items:
        L.append("Action items:")
        L += [f"  ☐ {it['owner']}: {it['text']}" for it in items]
    else:
        L.append("No blockers today ✅")
    return "\n".join(L)


# short, deterministic acknowledgements — keep the room feeling alive without
# waiting on the agent (rotated, never the same one twice in a row)
_FILLERS = ["Thanks.", "Got it, thanks.", "Nice, thanks.", "Great, thanks.", "Cheers, noted."]


# ══════════════════════════════════════════════════════════════════════════════
#  The facilitator + summary manager
# ══════════════════════════════════════════════════════════════════════════════
def run(meet_url, bot_name, voice, local=False, auto=False, sim=None):
    out_dir = os.path.join(_ROOT, CONFIG.get("OUTPUT_DIR", "standups"))
    fmt = CONFIG.get("OUTPUT_FORMAT", "md")
    hist_path = os.path.join(out_dir, "history.json")
    link_dir = os.path.join(_ROOT, "link")
    heard_path = os.path.join(link_dir, "heard.jsonl")
    cmd_path = os.path.join(link_dir, "commands.jsonl")
    track = bool(CONFIG.get("TRACK_ACROSS_DAYS", True))
    roster = [str(m.get("name", "")).strip() for m in CONFIG.get("TEAM", []) if m.get("name")]
    questions = CONFIG.get("QUESTIONS") or ["Any updates?"]

    per_person = float(CONFIG.get("PER_PERSON_SECONDS", 90))
    nudge_at = float(CONFIG.get("NUDGE_AT_SECONDS", 20))
    silence_end = float(CONFIG.get("SILENCE_ENDS_TURN", 6))
    no_resp = float(CONFIG.get("NO_RESPONSE_SECONDS", 20))
    wait_go = bool(CONFIG.get("WAIT_FOR_GO_AHEAD", True)) and not auto
    listen_secs = float(CONFIG.get("LISTEN_SECONDS", 25))      # standby: how long to forward speech
                                                               # to the agent after it opens an ear
    reflect_secs = float(CONFIG.get("REFLECT_SECONDS", 8))     # pause for the agent to reflect a turn back
    GO_TIMEOUT = 45.0

    hist = load_history(hist_path) if track else {"people": {}, "sessions": []}
    events = queue.Queue()
    box = {"call_id": None}
    proc = None
    err_tail = []

    # ── launch the AUDIO bridge (skipped in --local) ──
    if not local:
        cmd = [sys.executable, _BRIDGE, meet_url, "--name", bot_name]
        if voice:
            cmd += ["--voice", voice]
        if CONFIG.get("ALONE_SECONDS", 0) and CONFIG["ALONE_SECONDS"] > 0:
            cmd += ["--alone-timeout", str(CONFIG["ALONE_SECONDS"])]
        pk = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  text=True, bufsize=1)
        if sys.platform == "win32":
            pk["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            pk["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **pk)
        except OSError as e:
            print(f"Couldn't start the bridge ({e}). Did you `pip install -r requirements.txt`?")
            sys.exit(1)

        def reader():
            try:
                for rawln in proc.stdout:
                    rawln = rawln.strip()
                    if not rawln:
                        continue
                    try:
                        ev = json.loads(rawln)
                    except json.JSONDecodeError:
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
                        if len(err_tail) > 40:
                            del err_tail[0]
            except Exception:
                pass
        threading.Thread(target=err_reader, daemon=True).start()

    # ── bot I/O ──
    def send(obj):
        if proc:
            try:
                proc.stdin.write(json.dumps(obj) + "\n"); proc.stdin.flush()
            except Exception:
                pass

    def say(text):
        if not text:
            return
        print(f"  🔊 {text}")
        send({"command": "tts.speak", "text": text, "voice": voice or "af_heart"})
        if sim:
            sim.on_say(text)

    def chat(text):
        if not text:
            return
        print(f"  💬 (chat) {text.splitlines()[0][:80]}…" if len(text) > 80 else f"  💬 (chat) {text}")
        send({"command": "send_chat", "message": text})

    # ── agent link (the brain): forward what we hear, run what it writes back ──
    def _append(path, obj):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except OSError:
            pass

    S = {"phase": "greeting", "order": [], "idx": -1, "sub": "", "token": 0,
         "present": [], "seen_human": False, "end_reason": "unknown", "leaving": False,
         "updates": {}, "start_ts": time.time(), "heard_seq": 0,
         "nudged": False, "reasked": False, "cur_blockers": [], "wrote": False,
         "posted": False, "solution_for": "", "listen_open": False, "fill_i": 0,
         "agent_seen": False, "timers": {}}

    def forward(kind, **fields):
        S["heard_seq"] += 1
        hid = S["heard_seq"]
        rec = {"id": hid, "kind": kind, "phase": S["phase"], **fields}
        _append(heard_path, rec)
        if kind in ("addressed", "waiting"):
            print(f"  → heard #{hid} ({kind}) {fields.get('speaker','')}: "
                  f"{fields.get('text','')!r}  (agent's turn)")
        return hid

    def arm(kind, delay, extra=None):
        cancel(kind)
        ev = {"event": "timer", "kind": kind, "token": S["token"]}
        if extra:
            ev.update(extra)
        t = threading.Timer(delay, lambda: events.put(ev)); t.daemon = True
        S["timers"][kind] = t; t.start()

    def cancel(kind):
        t = S["timers"].pop(kind, None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass

    def cancel_all():
        for kind in list(S["timers"]):
            cancel(kind)

    def humans_present():
        return list(S["present"])

    # ── round-robin ──
    def maybe_start():
        if S["phase"] == "greeting" and not wait_go and humans_present():
            start_standup()

    def start_standup():
        present = humans_present()
        order, used = [], set()
        for r in roster:
            for p in present:
                if p not in used and (match_name(p, r) or match_name(r, p)):
                    order.append(r); used.add(p); break
        for p in present:
            if p not in used and not any(match_name(p, r) for r in roster):
                order.append(p)
        S["order"] = order or list(present)
        S["idx"] = -1
        S["phase"] = "turn"
        cancel("go_timeout")
        say(f"Great, let's run standup. I'll go one at a time and keep us moving — "
            f"{len(S['order'])} to get through.")
        next_turn()

    def next_turn():
        cancel_all()
        S["idx"] += 1
        if S["idx"] >= len(S["order"]):
            return standby()
        person = S["order"][S["idx"]]
        S["token"] += 1
        S["sub"] = ""
        S["nudged"] = S["reasked"] = False
        S["updates"].setdefault(person, {"text": "", "utterances": [], "blockers": [],
                                         "resolved_followups": [], "no_update": False})
        _, open_blk = open_blockers_for(hist, person) if track else (person, [])
        S["cur_blockers"] = open_blk
        if open_blk:
            S["sub"] = "followup"
            S["updates"][person]["_followup_blk"] = [b["text"] for b in open_blk]
            say(f"{person}, before your update — last time you flagged: "
                f"{'; '.join(b['text'] for b in open_blk[:3])}. Is that cleared?")
            arm("noresp", no_resp); arm("turn", per_person)
            if sim:
                sim.floor(person, "followup", events)
        else:
            prompt(person)

    def prompt(person):
        S["sub"] = "prompted"
        say(f"{person}, {_combine_questions(questions)}")
        arm("noresp", no_resp); arm("turn", per_person)
        if per_person - nudge_at > 1:
            arm("nudge", per_person - nudge_at)
        if sim:
            sim.floor(person, "update", events)

    def _combine_questions(qs):
        low = [(q.rstrip("? ").strip()) for q in qs]
        low = [(c[0].lower() + c[1:]) if c else c for c in low]
        if len(low) == 1:
            return low[0] + "?"
        if len(low) == 2:
            return " and ".join(low) + "?"
        return ", ".join(low[:-1]) + ", and " + low[-1] + "?"

    def capture(person, speaker, text):
        u = S["updates"][person]
        u["no_update"] = False               # speaking again clears an earlier skip/no-response
        u["utterances"].append({"speaker": speaker, "text": text})
        u["text"] = (u["text"] + " " + text).strip()

    def end_turn(person, why):
        cancel_all()
        u = S["updates"][person]
        if not u["utterances"]:
            u["no_update"] = True
        print(f"  ✓ {person} — {why}")
        forward("update", person=person, text=u["text"], no_update=u["no_update"])
        write_output()
        # Briefly pause for the agent to reflect the update back ("so you did X — got it"),
        # then move on. A stale/absent agent falls back to a quick filler (bounded — never
        # hangs). Until an agent has actually sent a command, keep the pause short so a
        # no-agent run stays snappy. This window doubles as pause-tolerance: if the person
        # resumes talking during it, their turn continues (see the user.message router).
        if reflect_secs > 0 and not u["no_update"] and why in ("done", "timebox"):
            S["sub"] = "reflect"
            arm("reflect", reflect_secs if S["agent_seen"] else min(2.5, reflect_secs))
        else:
            next_turn()

    def add_blocker(who, text):
        """Record a blocker the agent spotted (async — doesn't interrupt the round)."""
        if not who or not text:
            return
        u = S["updates"].setdefault(who, {"text": "", "utterances": [], "blockers": [],
                                          "resolved_followups": [], "no_update": False})
        if not any(_blk_text(b).lower() == text.lower() for b in u["blockers"]):
            u["blockers"].append({"text": text, "solution": None})
            print(f"    🚩 {who}: {text}")
            write_output()

    def attach_solution(who, sol):
        """Attach a fix to `who`'s latest open blocker (or the most recent open one)."""
        for name in ([who] if who else list(reversed(S["order"]))):
            for b in reversed(S["updates"].get(name, {}).get("blockers", [])):
                if isinstance(b, dict) and not b.get("solution"):
                    b["solution"] = sol
                    print(f"    💡 {name}: “{b['text']}” → {sol}")
                    write_output()
                    return True
        return False

    def standby():
        cancel_all()
        S["phase"] = "standby"
        S["idx"] = len(S["order"]) - 1        # so an agent-added person appends + becomes next
        write_output()
        sess = _live_session()
        if not S["posted"]:                   # first time — the round just finished
            S["posted"] = True
            say("That's everyone — let me pull the summary together.")
            # the AGENT composes + posts the summary (it has every update). The engine
            # deliberately does NOT dump raw transcript into the chat.
            forward("round_done", present=sess["present"], updates=sess["updates"])
        else:
            forward("summary_updated", present=sess["present"], updates=sess["updates"])

    def _live_session():
        present = list(S["order"])
        for name in S["updates"]:                 # include chat-only updaters not in the round order
            if (S["updates"][name].get("text")
                    and not any(match_name(name, o) or match_name(o, name) for o in present)):
                present.append(name)
        absent = [r for r in roster
                  if not any(match_name(p, r) or match_name(r, p) for p in present)]
        return _session_of(bot_name, present, absent, S["updates"], S["start_ts"])

    # ── take a specific person's standup (agent-driven, e.g. a latecomer via `ask`) ──
    def take_person(name):
        if S["phase"] == "turn":
            # mid-round: queue them politely — never derail whoever is speaking now
            if not any(match_name(name, o) or match_name(o, name) for o in S["order"]):
                S["order"].append(name)
                print(f"    (queued {name} for after the current turn)")
            return
        for i, o in enumerate(S["order"]):    # already in the order (e.g. re-ask a skipped
            if match_name(name, o) or match_name(o, name):   # person) → move them to the end
                name = S["order"].pop(i)
                break
        S["order"].append(name)
        S["phase"] = "turn"
        S["idx"] = len(S["order"]) - 2        # next_turn lands on the appended name → prompt asks
        next_turn()

    # ── the agent's commands ──
    def _target_ok(c):
        """A person-targeted command (skip/next/reflect) only applies if the round is
        still on that person — so a slow command can't land on whoever came next."""
        who = (c.get("for") or "").strip()
        if not who:
            return True
        cur_p = S["order"][S["idx"]] if (S["phase"] == "turn" and 0 <= S["idx"] < len(S["order"])) else None
        return bool(cur_p and (match_name(cur_p, who) or match_name(who, cur_p)))

    def apply_command(c):
        cmd = (c.get("cmd") or "").lower()
        cur = S["order"][S["idx"]] if (S["phase"] == "turn" and 0 <= S["idx"] < len(S["order"])) else None
        if cmd in ("say", "speak"):
            say(c.get("text", ""))
            if c.get("listen"):                       # open an ear (standby) to catch a reply/fix
                S["listen_open"] = True; arm("listen", listen_secs)
        elif cmd == "chat":
            chat(c.get("text", ""))
        elif cmd in ("blocker", "note"):              # record a blocker the agent spotted (async)
            add_blocker((c.get("for") or cur or "").strip(), (c.get("text") or "").strip())
        elif cmd == "record":                         # store a person's update (e.g. posted in chat)
            who, txt = (c.get("for") or "").strip(), (c.get("text") or "").strip()
            if who and txt:
                u = S["updates"].setdefault(who, {"text": "", "utterances": [], "blockers": [],
                                                  "resolved_followups": [], "no_update": False})
                u["utterances"].append({"speaker": who, "text": txt, "via": "chat"})
                u["text"] = (u["text"] + " " + txt).strip()
                u["no_update"] = False                # appears in the summary via _live_session,
                print(f"    📝 recorded {who}'s update (from chat)")   # without being re-prompted
                write_output()
        elif cmd == "solution":                       # attach a fix to a blocker
            sol = (c.get("text") or c.get("solution") or "").strip()
            if sol:
                attach_solution((c.get("for") or S["solution_for"] or "").strip(), sol)
        elif cmd == "resolve":                        # agent judged a cross-day follow-up
            who = (c.get("for") or cur or "").strip()
            u = S["updates"].get(who)
            if u:
                for bt in (c.get("blockers") or u.get("_followup_blk", [])):
                    u["resolved_followups"].append({"text": bt, "resolved": bool(c.get("cleared"))})
                u["_followup_blk"] = []
                write_output()
        elif cmd in ("start", "begin", "go"):
            if S["phase"] in ("greeting", "waiting_go"):
                start_standup()
        elif cmd == "reflect":                        # agent reflected a turn back → say it + advance
            if S["sub"] == "reflect" and _target_ok(c):
                cancel("reflect"); say(c.get("text", "")); next_turn()
        elif cmd in ("next", "skip"):
            if S["phase"] == "turn" and cur and _target_ok(c):
                if S["sub"] == "reflect":
                    cancel("reflect"); next_turn()    # turn already ended → just advance
                elif cmd == "skip":
                    S["updates"][cur]["no_update"] = True; cancel_all(); next_turn()
                else:
                    end_turn(cur, "next")
        elif cmd == "ask":                            # take a specific person (e.g. a latecomer)
            who = (c.get("person") or "").strip()
            if who:
                take_person(who)
        elif cmd in ("leave", "bye", "dismiss"):
            do_leave("asked to leave")
        # "none" / unknown → no-op

    # ── proper, billing-safe leaving ──
    def do_leave(reason):
        if S["leaving"]:
            return
        S["leaving"] = True
        S["end_reason"] = reason
        cancel_all()
        write_output()
        say("Thanks everyone — I'll head out now. Have a good one.")
        send({"command": "leave"})
        print(f"  Leaving ({reason}). Sent leave; will confirm billing stopped.")
        events.put(None)                      # break the loop → teardown runs end_call

    def write_output():
        stamp = datetime.now()
        session = _live_session()
        ext = fmt if fmt in ("md", "txt", "json") else "md"
        fname = f"standup-{stamp.strftime('%Y-%m-%d')}.{ext}"     # one file per day, kept current
        fpath = os.path.join(out_dir, fname)
        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write(build_summary_text(session, fmt, stamp))
        except OSError as e:
            print(f"  (couldn't write the summary file: {e})")
            return
        if not S["wrote"]:
            S["wrote"] = True
            try:
                shown = os.path.relpath(fpath, _ROOT)
            except ValueError:
                shown = fpath
            print(f"  📝 Summary: {shown}")
        if track:
            _update_history(session, fname)
            save_history(hist_path, hist)

    def _update_history(session, fname):
        today = datetime.now().strftime("%Y-%m-%d")
        for name in session["present"]:
            u = session["updates"].get(name, {})
            key = name
            for existing in list(hist["people"]):
                if match_name(existing, name) or match_name(name, existing):
                    key = existing; break
            rec = hist["people"].setdefault(key, {"open_blockers": [], "last_seen": ""})
            rec["last_seen"] = today
            resolved = {r["text"].lower() for r in u.get("resolved_followups", []) if r["resolved"]}
            rec["open_blockers"] = [b for b in rec.get("open_blockers", [])
                                    if b["text"].lower() not in resolved]
            by_text = {b["text"].lower(): b for b in rec["open_blockers"]}
            for b in u.get("blockers", []):
                bt, sol = _blk_text(b), _blk_solution(b)
                exist = by_text.get(bt.lower())
                if exist:
                    if sol:
                        exist["solution"] = sol                # fold in a fix found later
                else:
                    entry = {"text": bt, "since": today}
                    if sol:
                        entry["solution"] = sol                # carry the plan forward
                    rec["open_blockers"].append(entry); by_text[bt.lower()] = entry
        sessions = [s for s in hist["sessions"] if s.get("date") != today]     # replace today's
        sessions.append({"date": today, "present": session["present"], "absent": session["absent"],
                         "summary_file": fname})
        hist["sessions"] = sessions[-60:]

    def greet():
        say(f"Good morning everyone, I'm {bot_name}, and I'll be running standup today.")
        if wait_go:
            S["phase"] = "waiting_go"
            say("Just let me know when you're ready to start.")
            arm("go_timeout", GO_TIMEOUT)
        else:
            maybe_start()

    # ── the agent link (bot ⇄ brain) — runs in every mode ──
    for p in (heard_path, cmd_path):         # fresh link each run
        try:
            os.makedirs(link_dir, exist_ok=True); open(p, "w").close()
        except OSError:
            pass

    def link_watch():
        pos = 0
        while not S["leaving"]:
            try:
                with open(cmd_path, encoding="utf-8") as fh:
                    fh.seek(pos)
                    for line in fh:
                        if line.strip():
                            try:
                                events.put({"event": "agent_command", "c": json.loads(line)})
                            except json.JSONDecodeError:
                                pass
                    pos = fh.tell()
            except OSError:
                pass
            time.sleep(0.12)                 # snappy, and free (local file read, no model)
    threading.Thread(target=link_watch, daemon=True).start()

    # ── boot ──
    if local:
        print("\n[--local — no bridge, no meeting: a scripted dry run]\n")
        if sim is None:
            sim = _DefaultSim()
        sim.begin(events, bot_name)
        events.put({"event": "call.bot_ready"})
    else:
        if not API_KEY:
            print("No AgentCall API key. Get one free at https://app.agentcall.dev/api-keys, then")
            print('  export AGENTCALL_API_KEY="ak_ac_..."   (Windows: set AGENTCALL_API_KEY=...)')
            sys.exit(1)
        print(f"Sending '{bot_name}' into the meeting… (~30-90s to appear). Audio-only.")
        print(f"  Brain link:  {os.path.relpath(heard_path, _ROOT)}  ⇄  {os.path.relpath(cmd_path, _ROOT)}")
        print("(press Ctrl+C, or leave the meeting, to make the bot leave)\n")

    # ── main event loop ──
    try:
        while True:
            try:
                ev = events.get(timeout=0.5)
            except queue.Empty:
                continue
            if ev is None:
                break
            et = ev.get("event") or ev.get("type") or ""
            if DEBUG and et not in ("timer",):
                print(f"  [debug] {json.dumps(ev, ensure_ascii=False)[:200]}")

            if et == "call.created":
                print(f"  Call created: {ev.get('call_id')}")
            elif et == "call.bot_ready":
                print("  In the meeting. Greeting the room…")
                greet()
            elif et in ("participant.joined", "meeting.participant_joined"):
                name = ev.get("name") or (ev.get("participant") or {}).get("name", "")
                if name and name.lower() != bot_name.lower():
                    fresh = name not in S["present"]
                    if fresh:
                        S["present"].append(name)
                    S["seen_human"] = True
                    print(f"  + {name} joined ({len(S['present'])} present)")
                    if fresh:
                        forward("joined", person=name)   # re-joins don't re-fire (no "restart" spam)
                    if S["phase"] == "greeting":
                        maybe_start()                    # auto mode: start once someone's here
                    elif S["phase"] == "turn" and not any(
                            match_name(name, o) or match_name(o, name) for o in S["order"]):
                        S["order"].append(name)          # latecomer mid-round → queue them
                        print(f"    (added {name} to the queue)")
                    # standby: no auto-take, no hand-raise — the agent chooses via `ask`
            elif et in ("participant.left", "meeting.participant_left"):
                name = ev.get("name") or (ev.get("participant") or {}).get("name", "")
                S["present"] = [p for p in S["present"] if p != name]
                if S["phase"] == "turn" and 0 <= S["idx"] < len(S["order"]):
                    cur = S["order"][S["idx"]]
                    if match_name(name, cur) or match_name(cur, name):
                        print(f"  {cur} left mid-turn."); end_turn(cur, "left")
                if (CONFIG.get("LEAVE_WHEN_EMPTY", True) and S["seen_human"]
                        and not humans_present() and not S["leaving"]):
                    print("\n  Everyone left — wrapping up.")
                    do_leave("all participants left")
            elif et == "user.message":
                text = ev.get("text", "")
                speaker = ev.get("speaker", "") or "someone"
                if DEBUG:
                    print(f"    🗣  {speaker}: {text}")
                cur = S["order"][S["idx"]] if (S["phase"] == "turn" and 0 <= S["idx"] < len(S["order"])) else None
                if S["phase"] in ("greeting", "waiting_go"):
                    start_standup()                               # FAST start on the first thing said
                elif addressed_to(text, bot_name):
                    forward("addressed", speaker=speaker, text=text, current=cur)
                elif S["phase"] == "turn" and cur:
                    prev = S["order"][S["idx"] - 1] if S["idx"] > 0 else None
                    if S["sub"] == "followup":
                        cancel("noresp")
                        forward("followup", person=cur, text=text,    # agent judges → `resolve`
                                prior=[b["text"] for b in S["cur_blockers"]])
                        prompt(cur)
                    elif (S["sub"] == "prompted" and prev and speaker
                          and (match_name(speaker, prev) or match_name(prev, speaker))
                          and not (match_name(speaker, cur) or match_name(cur, speaker))):
                        # the PREVIOUS person kept talking after their turn closed (a long
                        # thinking pause) — fold it back into THEIR update, don't steal
                        # the new person's turn. Ambiguous attribution prefers `cur`.
                        u = S["updates"][prev]
                        u["utterances"].append({"speaker": speaker, "text": text})
                        u["text"] = (u["text"] + " " + text).strip()
                        u["no_update"] = False
                        print(f"    ↩ folded into {prev}'s update")
                        forward("addendum", person=prev, text=text)
                        write_output()
                    else:
                        # normal capture — and if we were in the reflect pause, the person
                        # resumed after a thinking pause: cancel the reflect, keep their turn.
                        cancel("noresp"); cancel("reflect")
                        S["sub"] = "listening"
                        capture(cur, speaker, text)
                        arm("silence", silence_end)
                elif S["phase"] == "standby":
                    # standby is low-traffic → forward everything so the agent catches a fix,
                    # a "what's the summary?", or a "you can leave" even if the name was mis-heard.
                    forward("standby", speaker=speaker, text=text)
            elif et == "agent_command":
                S["agent_seen"] = True               # a brain is driving → full reflect window
                apply_command(ev.get("c", {}))
            elif et == "timer":
                kind = ev.get("kind")
                if kind == "go_timeout":
                    if S["phase"] == "waiting_go" and humans_present():
                        print("  (no go-ahead yet — starting anyway)")
                        start_standup()
                    continue
                if kind == "listen":
                    S["listen_open"] = False
                    continue
                if ev.get("token") != S["token"] or S["phase"] != "turn":
                    continue
                if not (0 <= S["idx"] < len(S["order"])):
                    continue
                person = S["order"][S["idx"]]
                if kind == "silence":
                    end_turn(person, "done")
                elif kind == "reflect":                        # agent didn't reflect in time → quick ack
                    S["fill_i"] += 1
                    say(_FILLERS[S["fill_i"] % len(_FILLERS)])
                    next_turn()
                elif kind == "turn":
                    if S["sub"] == "followup":
                        prompt(person)                         # no answer in time → blocker stays open
                    elif S["sub"] == "reflect":
                        pass                                   # the reflect timer owns the pacing
                    else:
                        end_turn(person, "timebox")            # hard cap → move on
                elif kind == "nudge":
                    if S["sub"] in ("prompted", "listening") and not S["nudged"]:
                        S["nudged"] = True
                        say(f"{person}, about {int(nudge_at)} seconds left.")
                elif kind == "noresp":
                    # No coalesced speech yet. Don't say a hardcoded re-ask (it fired mid-update
                    # because the bridge only emits after a pause) — hand it to the agent.
                    if not S["reasked"]:
                        S["reasked"] = True
                        forward("silent", person=person)     # agent: wait / gentle nudge / skip
                        arm("noresp", no_resp)               # safety window, then move on
                    else:
                        S["updates"][person]["no_update"] = True
                        end_turn(person, "no response")      # genuinely absent → move on, no line
            elif et in ("chat.received", "meeting.chat"):
                sender = ev.get("sender") or ev.get("speaker") or ""
                msg = ev.get("message") or ev.get("text") or ""
                # ignore the bot's own posts even if the platform decorates the name ("Nova (bot)")
                if msg and not sender.lower().startswith(bot_name.lower()):
                    if DEBUG:
                        print(f"    💬 in  {sender}: {msg}")
                    forward("chat", sender=sender, text=msg)      # agent: is this someone's update?
            elif et == "call.ended":
                S["end_reason"] = ev.get("reason", S["end_reason"])
                print(f"\n  Call ended: {S['end_reason']}")
                break
            elif et == "error":
                print(f"  [bridge] error: {ev.get('message', '(no message)')}")
            elif et == "tts.error":
                print(f"  [tts] {ev.get('reason', ev.get('message', 'speech failed'))}")
    except KeyboardInterrupt:
        S["end_reason"] = "interrupted"
        print("\nLeaving…")
    except Exception as e:
        S["end_reason"] = f"error: {e}"
        print(f"\n  Unexpected error — leaving cleanly anyway: {e}")
        import traceback
        traceback.print_exc()
    finally:
        cancel_all()
        if sim and hasattr(sim, "stop"):
            sim.stop()                        # stop the --local brain thread cleanly
        if not S["wrote"] and S["updates"]:
            write_output()
        if not proc:
            return
        send({"command": "leave"})            # ENSURE the leave command is sent
        call_id = box["call_id"]
        if call_id is None and proc.poll() is None:
            deadline = time.time() + 3
            while box["call_id"] is None and proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            call_id = box["call_id"]
        try:
            end_call(call_id)                 # ENSURE billing stops (retries + confirm)
        except KeyboardInterrupt:
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill(); proc.wait(timeout=4)
            except Exception:
                pass
        if call_id is None:
            print("The bridge exited before joining. Recent output:")
            for line in err_tail[-8:] or ["(no output)"]:
                print("  " + line)
            print("First run? Install dependencies:  pip install -r requirements.txt")


# ══════════════════════════════════════════════════════════════════════════════
#  --local simulator (no meeting, no bot) — drives attendees AND plays the agent
# ══════════════════════════════════════════════════════════════════════════════
class _Sim:
    """Drives attendees AND plays the launching agent (the brain): it tails
    heard.jsonl and writes commands.jsonl exactly as a real agent would — so
    --local exercises the whole brain-driven flow, blocker beat included."""
    def __init__(self, attendees, responses, resolutions=None, latecomer=None,
                 solvers=None, chat_from=None, speed=1.0):
        self.attendees = attendees
        self.responses = responses
        self.resolutions = resolutions or {}
        self.latecomer = latecomer               # (name, response) to join during standby
        self.solvers = solvers or {}             # person -> [solver_name, fix_text]
        self.chat_from = chat_from               # (sender, message) posted in the meeting chat
        self.speed = speed
        self.events = None
        self.bot = ""
        self._did_late = False
        self._stop = False
        self._hpos = 0
        self._heard = os.path.join(_ROOT, "link", "heard.jsonl")
        self._cmds = os.path.join(_ROOT, "link", "commands.jsonl")

    def stop(self):
        self._stop = True

    def _emit(self, speaker, text, delay):
        t = threading.Timer(max(0.01, delay * self.speed),
                            lambda: self.events.put({"event": "user.message",
                                                     "speaker": speaker, "text": text}))
        t.daemon = True; t.start()

    def _join(self, name, delay):
        t = threading.Timer(max(0.01, delay * self.speed),
                            lambda: self.events.put({"event": "participant.joined", "name": name}))
        t.daemon = True; t.start()


    def _cmd(self, obj):
        try:
            with open(self._cmds, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj) + "\n")
        except OSError:
            pass

    def begin(self, events, bot_name):
        self.events, self.bot = events, bot_name
        for i, a in enumerate(self.attendees):
            self._join(a, 0.3 + i * 0.15)
        if self.chat_from:                       # simulate someone posting their update in the chat
            t = threading.Timer(max(0.01, 0.5 * self.speed),
                                lambda: events.put({"event": "chat.received",
                                                    "sender": self.chat_from[0],
                                                    "message": self.chat_from[1]}))
            t.daemon = True; t.start()
        threading.Thread(target=self._brain, daemon=True).start()

    def floor(self, person, sub, events):
        if sub == "followup":
            self._emit(person, self.resolutions.get(person, "yes, that's cleared now"), 1.0)
            return
        resp = self.responses.get(person)
        if resp is None and self.latecomer and person == self.latecomer[0]:
            resp = self.latecomer[1]         # the latecomer's scripted update
        if resp is None:
            return
        chunks = resp if isinstance(resp, list) else [resp]
        d = 1.0
        for c in chunks:
            self._emit(person, c, d); d += 2.0

    def on_say(self, text):
        pass                                   # driving now happens off heard.jsonl (see _on_heard)

    # ── the brain: read heard.jsonl, reply on commands.jsonl (stand-in for the agent) ──
    def _brain(self):
        while not self._stop:
            try:
                with open(self._heard, encoding="utf-8") as fh:
                    fh.seek(self._hpos)
                    lines = fh.readlines()
                    self._hpos = fh.tell()
            except OSError:
                lines = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._on_heard(json.loads(line))
                except json.JSONDecodeError:
                    pass
            time.sleep(0.08)

    def _leave_cmd(self, delay):
        t = threading.Timer(max(0.01, delay * self.speed), lambda: self._cmd({"cmd": "leave"}))
        t.daemon = True; t.start()

    def _on_heard(self, rec):
        kind = rec.get("kind")
        if kind == "update":                               # async: record blockers, attach any fix
            person, text = rec.get("person", ""), rec.get("text", "")
            if not rec.get("no_update"):
                for b in detect_blockers(text):
                    self._cmd({"cmd": "blocker", "for": person, "text": b})
                    fix = self.solvers.get(person)
                    if fix:
                        self._cmd({"cmd": "solution", "for": person, "text": fix[1]})
                self._cmd({"cmd": "reflect", "for": person,     # reflect their update back, then advance
                           "text": f"Thanks {person} — got it."})
        elif kind in ("round_done", "summary_updated"):    # compose + post the summary (the agent's job)
            pres, ups = rec.get("present", []), rec.get("updates", {})
            sess = {"present": pres, "updates": ups, "action_items": action_items(pres, ups)}
            self._cmd({"cmd": "chat", "text": chat_summary(sess, datetime.now())})
            recap = "Here's the recap. " + ". ".join(     # full-ish spoken summary at the end
                f"{p}, {'a blocker to follow up' if ups.get(p, {}).get('blockers') else 'no blockers'}"
                for p in pres) + ". Full summary's in the chat."
            self._cmd({"cmd": "say", "text": recap})
            if kind == "round_done" and self.latecomer and not self._did_late:
                self._did_late = True
                self._join(self.latecomer[0], 1.0)         # bring in a latecomer
            else:
                self._leave_cmd(1.5)                       # then wind the call down
        elif kind == "silent":                             # asked but quiet → skip that person
            self._cmd({"cmd": "skip", "for": rec.get("person", "")})
        elif kind == "chat":                               # someone posted their update in the chat
            sender, text = rec.get("sender", ""), rec.get("text", "")
            self._cmd({"cmd": "record", "for": sender, "text": text})
            for b in detect_blockers(text):
                self._cmd({"cmd": "blocker", "for": sender, "text": b})
        elif kind == "addendum":                           # late continuation of a closed turn —
            for b in detect_blockers(rec.get("text", "")): # already folded in; re-check blockers
                self._cmd({"cmd": "blocker", "for": rec.get("person", ""), "text": b})
        elif kind == "joined":                             # a latecomer → agent chooses to take them
            name = rec.get("person", "")
            if self._did_late and self.latecomer and name == self.latecomer[0]:
                self._cmd({"cmd": "say", "text": f"Welcome {name} — let me grab your update."})
                self._cmd({"cmd": "ask", "person": name})
        elif kind == "followup":
            self._cmd({"cmd": "resolve", "for": rec.get("person", ""),
                       "cleared": bool(resolution_of(rec.get("text", "")))})
        elif kind == "standby":                            # e.g. "you can leave"
            if resolution_of(rec.get("text", "")) is None and "leav" in rec.get("text", "").lower():
                self._cmd({"cmd": "leave"})


class _DefaultSim(_Sim):
    def __init__(self):
        super().__init__(
            attendees=["Alex", "Priya"],
            responses={
                "Alex": ["Yesterday I shipped the login API and reviewed two PRs.",
                         "Today the dashboard. I'm blocked on staging access, waiting on ops."],
                "Priya": "Finished the onboarding emails, today I'll QA the signup flow, no blockers.",
            },
            resolutions={"Priya": "yep, sorted"},
            solvers={"Alex": ["Priya", "For staging, just ping DevOps in the platform channel — "
                              "they grant it in a minute."]},
            latecomer=("Sam", "Wrapped the billing migration, next is metrics. No blockers. That's me."))


def _load_sim(path):
    with open(path, encoding="utf-8") as fh:
        d = json.loads(fh.read())
    late, chat = d.get("latecomer"), d.get("chat_from")
    return _Sim(d.get("attendees", []), d.get("responses", {}), d.get("resolutions", {}),
                tuple(late) if late else None, d.get("solvers", {}),
                tuple(chat) if chat else None, d.get("speed", 1.0))


# ══════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(
        description="Standup Manager — an audio-only bot that runs your daily standup (AgentCall).")
    ap.add_argument("meet_url", nargs="?", help="Google Meet / Zoom / Teams link")
    ap.add_argument("--name", default=None, help="Bot display name (overrides config)")
    ap.add_argument("--voice", default=None, help="TTS voice id (overrides config)")
    ap.add_argument("--auto", action="store_true", help="Start without waiting for a go-ahead")
    ap.add_argument("--local", action="store_true", help="Dry-run the whole flow, no meeting")
    ap.add_argument("--sim", default=None, help="A JSON scenario for --local (see README)")
    args = ap.parse_args()

    bot_name = (args.name or CONFIG.get("BOT_NAME") or _ACCFG.get("default_bot_name") or "Scrum").strip()
    voice = (args.voice or CONFIG.get("VOICE") or _ACCFG.get("default_voice") or "").strip()

    if args.local:
        # --local starts immediately (the sim plays the agent's go-ahead reply itself)
        sim = _load_sim(args.sim) if args.sim else None
        try:
            run("", bot_name, voice, local=True, auto=True, sim=sim)
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    if not args.meet_url:
        print('Usage: python scripts/standup.py "<meeting-link>"     (or --local to dry-run)')
        sys.exit(0)
    if not (CONFIG.get("ALONE_SECONDS") and CONFIG["ALONE_SECONDS"] > 0):
        print("WARNING: ALONE_SECONDS is 0 in config.jsonc — the server-side auto-leave is OFF.\n")

    try:
        run(args.meet_url, bot_name, voice, local=False, auto=args.auto)
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
