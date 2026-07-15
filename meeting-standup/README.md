# Standup Manager

<p align="center">
  <img src="assets/standup-bot.gif" width="100%" alt="Let your AI agents handle standups for you — a bot that goes around the team, calling on each person for their update">
</p>

**Let your AI agent run your team's daily standup — so you don't have to.**

Built for **engineering managers and team leads**: point it at your Google Meet / Zoom / Teams call and it
joins as an **audio-only** bot, greets the team, then goes around **one at a time by name** — asking what
each person finished, what's next, and what's blocking them. It **keeps time** (a gentle nudge, then it
moves on, so a 5-person standup is ~7 minutes, not 20), captures every update, and **posts a clean
summary to the meeting chat** when it's done — plus saves it to a dated file you can send anywhere.

And because it's a *manager*, not just a note-taker, it **remembers blockers across days**: next standup
it asks whether last time's blocker is cleared before taking the new update, and carries the open ones
forward — so nothing quietly falls through the cracks.

The point: **make your standup fast, consistent, and effortless.** No camera, no screen-share, no app for
anyone to install. It just talks and listens.

Powered by **[AgentCall](https://agentcall.dev)** · no LLM, no database — just the audio bridge and one script.

---

## Why you'll want it

Standups are the most expensive fifteen minutes of the day — it's everyone's time at once, every
single morning. This gets you the value without the drag:

- **Reclaim the time.** A tight, timeboxed round instead of a meandering 20-minute meeting — a
  5-person standup lands in ~7 minutes, and your team gets those minutes back every day.
- **Nobody has to run it.** No facilitator, no note-taker, no *"…who's next?"* — it runs the same
  way every day, whether or not you're in the room.
- **Nothing falls through the cracks.** Every update is captured, blockers become action items with
  owners, and open blockers get chased the next day until they're actually cleared.
- **The follow-up writes itself.** The summary and action items are in the meeting chat before
  anyone's back at their desk — no *"can someone send the notes?"*
- **Meetings stop eating your focus.** Each person gives sixty seconds of update and gets back to
  work; you still get the full picture, without chasing anyone for it.

---

## What it does

- **Runs the round-robin** — calls on each teammate in your configured order, only the ones actually in
  the call.
- **Keeps time** — a soft per-person cap with a nudge near the end, so standup stays short.
- **Keeps a live summary + action items** — captures each update, attributed by name, and turns blockers
  into action items.
- **Asks the room for a fix** — when someone's blocked, it asks if anyone has a quick unblock and folds
  the answer straight into the summary as the action item (catches fixes offered later, too).
- **Catches chat updates too** — bad mic or joined silent? Post your update in the meeting chat and it's
  folded into the summary (thoughtfully — links and banter are ignored).
- **Posts the summary in the chat** when the round's done — no copy-paste afterward.
- **Then stays on as your summary manager** — if someone joins late it raises a hand and takes their
  update too; if someone asks, it reads the summary back.
- **Tracks blockers across days** — “Last time you flagged the staging access — is that cleared?” — and
  carries open ones forward.
- **Leaves only when you tell it, or when everyone else has** — and always stops billing on the way out.

---

## Install it

It's a **skill for your AI agent** — so the easiest install is to
**give your agent the repo and say "install it":**

> **Tell your agent:** *"Install the meeting-standup skill from
> `https://github.com/pattern-ai-labs/built-with-agentcall` (the `meeting-standup` folder)."*

Or, one command (works with any agent, needs Node 18+):

```bash
npx skills add pattern-ai-labs/built-with-agentcall --skill meeting-standup
```

**Your AgentCall key** — your agent asks you for it once (grab a free one at
[agentcall.dev/api-keys](https://agentcall.dev/api-keys)) and saves it to `~/.agentcall/config.json`
— the same config file AgentCall uses. If you already use AgentCall, it's already there; nothing to do.

<details>
<summary><b>For devs (optional):</b> clone and run it yourself — no agent needed, just Python 3.10+</summary>

```bash
git clone --filter=blob:none --sparse https://github.com/pattern-ai-labs/built-with-agentcall
cd built-with-agentcall && git sparse-checkout set meeting-standup
cd meeting-standup
python -m venv venv
source venv/bin/activate        # Windows:  venv\Scripts\activate
pip install -r requirements.txt
export AGENTCALL_API_KEY="ak_ac_..."     # Windows:  set AGENTCALL_API_KEY=ak_ac_...
```
</details>

---

## Run it

**1. Set your team** (once). Open [`config.jsonc`](config.jsonc) and list your team under `TEAM`, in the
order you want them called:

```jsonc
"TEAM": [
  { "name": "Alex" },
  { "name": "Priya" },
  { "name": "Sam" }
],
```

Use the name each person appears as in the meeting. While you're there you can tweak the questions,
the per-person time limit, and whether it waits for a "go ahead" — but the defaults are fine.

**2. Send it in.** The agent way — just say:

> *"Run my standup: `<meeting link>`"*

…or the terminal way — join your meeting first, then:

```bash
python scripts/standup.py "https://meet.google.com/your-link"
```

Admit the bot (~30–90s). It greets the room and waits for a **"go ahead"** (or pass `--auto` to start
right away). It runs the round, **posts the summary in the chat**, and **stays** — ready to take a
latecomer's update or read the summary back. It leaves when you tell it (*"Nova, you can head out"*),
when everyone else leaves, or on **Ctrl+C**. Your summary also lands in [`standups/`](standups/).

> **Try it first, no meeting needed:** `python scripts/standup.py --local` plays a scripted standup right
> in your terminal — round-robin, timeboxing, the chat summary, a **latecomer** joining, and a clean
> leave — so you can see exactly how it behaves before going live.

<!-- VIDEO: swap YOUTUBE_ID once the walkthrough is uploaded (out/standup-setup.mp4 in the video project)
<p align="center">
  <a href="https://youtu.be/YOUTUBE_ID"><img src="https://img.youtube.com/vi/YOUTUBE_ID/maxresdefault.jpg" width="66%" alt="Watch: set it up in under two minutes"></a>
</p>
<p align="center"><sub>▶ <b><a href="https://youtu.be/YOUTUBE_ID">Watch the 2-minute walkthrough</a></b></sub></p>
-->


---

## What you get

A summary posted straight to the meeting chat, and saved to `standups/standup-2026-07-15.md`:

```markdown
# Standup — 2026-07-15 09:30

**Facilitated by** Nova · **Present:** Alex, Priya, Sam · **Duration:** 7m 04s

## Alex
- _Follow-up:_ “blocked on staging access” — ✅ resolved
Shipped the login API and reviewed two PRs. Today: the dashboard.

## Priya
QA'd the signup flow. Next up, the billing emails.
🚩 **Blockers:** waiting on design for the empty states

## Action items
- ☐ Priya: waiting on design for the empty states
```

…plus a `history.json` that quietly remembers who's blocked on what, so tomorrow it can follow up.

---

## How it works

```
  standup.py ──spawns──▶ engine/bridge.py ──▶ AgentCall ──▶ joins the meeting (audio only)
      ▲  │                      │
      │  └── speaks (TTS) ──────┘   calls on people, asks the questions, keeps time
      │
      └── clean events ◀── who joined/left · who said what (with their name)
```

- **AgentCall's audio bridge** gives the bot a voice, a live speaker-attributed transcript, meeting chat,
  and a raised hand — and its built-in barge-in means it won't talk over anyone.
- **`standup.py` runs the meeting** — the round-robin, timeboxing, capture, chat summary, action items,
  cross-day memory, and safe leaving are all deterministic and run on their own, no LLM required.
- **Your AI agent can be the brain** (optional, and recommended) — the bot forwards what it hears to a
  small `link/` file and your agent writes back one line, so it can sharpen the action items, answer
  “what did Alex say?”, and leave when asked. No brittle keyword matching — the agent decides.

The whole thing is turn-based, so only speech crosses the network — it never feels laggy.

---

## Make it yours

- **Different questions?** Edit `QUESTIONS` in `config.jsonc`.
- **Longer or shorter turns?** `PER_PERSON_SECONDS` and `NUDGE_AT_SECONDS`.
- **Post the summary to Slack/email?** Hand the finished `standups/…` file to your AI agent and ask it
  to send it — it's plain Markdown.
- **Async updates for people who can't make it?** A natural next step on top of the base.

---

## Cross-platform

Pure Python + pip wheels (`aiohttp`, `websockets` for the bridge) — no system binaries, no OS-specific
paths. Runs the same on Windows, macOS, and Linux.

---

## License

MIT. The bundled `engine/` bridge is AgentCall's, also MIT. Powered by
[AgentCall](https://agentcall.dev) · [FirstCall](https://firstcall.dev).
