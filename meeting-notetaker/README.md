# Meeting Notetaker

**Build your own notetaker — start here for free, then build on top.** No more renting a closed,
locked-down bot you can't open up or change. Own the code, and make it do exactly what you want.

Drop a bot into any Google Meet, Zoom, or Teams call and it quietly writes the whole thing down —
every word and every chat message, live as it happens — then slips out when the meeting's over.
It never talks. It just takes notes.

And it's **yours**: fork it, name it, give it a face, and wire it into whatever you want next.
Python **or** Node, one config file. Powered by **[AgentCall](https://agentcall.dev)**.

<p align="center">
  <img src="assets/notetaker-bot.gif" width="100%" alt="Build your own notetaker with AgentCall — a bot quietly taking notes all through a meeting">
</p>

![License](https://img.shields.io/badge/license-MIT-blue) ![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![Node](https://img.shields.io/badge/node-18%2B-green)

---

## What it does

- **Joins** a Google Meet / Zoom / Teams link as a named participant.
- **Writes the transcript to a file in real time** — speech **and** chat (`.md` / `.txt` / `.json`).
- **Shows it live** — in your browser at `localhost:8080`, or right on screen in the call (the transcript tile).
- **On-camera tile** (optional): customize the avatar it shows in the meeting — a logo, a pattern, the live transcript, or nothing at all.
- **Leaves** the moment the last human leaves — never lingers in an empty room.

<p align="center">
  <img src="assets/meetview.png" width="49%" alt="The notetaker bot sitting in your meeting" />
  <img src="assets/transcript.png" width="49%" alt="Your notes filling in live as people talk" />
</p>
<p align="center"><sub>The bot joins your call <b>(left)</b> — your notes write themselves, live <b>(right)</b>.</sub></p>

---

## Prerequisites

Three things, all free:

- **Python 3.10+** *or* **Node.js 18+** — your choice
- A free **[AgentCall API key](https://app.agentcall.dev/api-keys)**
- A meeting to drop it into — Google Meet, Zoom, or Teams

No coding agent required — it's a standalone app you clone and run. *(Though if you
use one — Claude Code, Cursor, Gemini CLI… — it can do the whole setup for you: see below.)*

## Setup

Two ways in — pick one:
- 🖥️ **Run it yourself** — the four steps below.
- 🤖 **Have an AI assistant do it** — on Claude Code, Cursor, Gemini CLI, or similar? [One prompt sets it all up.](#build-it-with-one-prompt)

**1. Get it on your computer.**

It lives in the **`built-with-agentcall`** repo, in the **`meeting-notetaker/`** folder. Grab it either way:

*Clone everything (simplest):*
```bash
git clone https://github.com/pattern-ai-labs/built-with-agentcall
cd built-with-agentcall/meeting-notetaker/python      # ...or:  .../node
```
*Just this folder (skip the other use-cases):*
```bash
git clone --filter=blob:none --sparse https://github.com/pattern-ai-labs/built-with-agentcall
cd built-with-agentcall && git sparse-checkout set meeting-notetaker
cd meeting-notetaker/python                           # ...or:  .../node
```

> Run these in a terminal — standalone, or your editor's built-in one (VS Code / Cursor: **Terminal → New Terminal**).
> Want it as your own project? It's MIT — clone it and push to a repo of your own. Yours to take.

**2. Install**

*Python (3.10+):*
```bash
python -m venv venv
source venv/bin/activate        # Windows:  venv\Scripts\activate
pip install -r requirements.txt
```
*Node (18+):*
```bash
npm install
```
> The venv keeps deps isolated — and on modern Linux/macOS a plain `pip install` is blocked without
> one (PEP 668). If `python` / `pip` aren't found, use `python3` / `pip3`.

**3. Build it** 🛠 — a one-time wizard that makes it *yours*:

```bash
python build.py        # or:  npm run build
```

It asks a few quick things — your free [AgentCall key](https://app.agentcall.dev/api-keys) first (it
writes a gitignored `.env`), a **name**, a **face on camera**, and the **notes format** — then fills in
your `config.jsonc`. **You built it.**

> Change anything later by editing [`config.jsonc`](config.jsonc) directly — the build is just for first-time setup.

**4. Run it** — join the meeting yourself first, then:

```bash
python notetaker.py "https://meet.google.com/your-link"
#  or:  node notetaker.js "https://meet.google.com/your-link"
```

Admit the bot (~30–90s), talk, drop a chat message — and watch `notes/` fill in live, plus the page at
**http://localhost:8080**. To stop: **leave the meeting** (the bot follows) or press **Ctrl+C**.

---

## Build it with one prompt 
#### (For developers who hates commands)

> [!TIP]
> **Using an AI coding assistant?** Give it two things and it does the rest — in any folder, no terminal for you:
> 1. **Copy this repo's URL** — it's in your browser's address bar (e.g. `https://github.com/pattern-ai-labs/built-with-agentcall`).
> 2. Paste that URL **and the prompt below** into [Claude Code](https://claude.com/claude-code), Cursor, Gemini CLI, or Windsurf.
>
> It clones the repo, installs, builds, and runs it — you just answer a few questions in chat.

**The prompt** — copy it (the button in the corner of the box) and paste it with your repo URL:

```text
You're going to set up and run a silent meeting notetaker that runs on AgentCall. It's already
written and complete — your job is to get it, configure it, and run it, not to write a notetaker.
Never modify notetaker.py / notetaker.js or anything in engine/. Run every command yourself —
never tell me to run something in a terminal.

1. Get the code. Clone the GitHub repo URL I gave you, then cd into this use-case's folder — it's a
   monorepo, so the notetaker is in meeting-notetaker/: run git clone <THE URL I PASTED>, then
   cd into the repo and into meeting-notetaker/. If I didn't give you a URL, ask me for it first.
   (If build.py / build.js is already in the current folder, skip the clone.)
2. Python or Node? Ask me, then cd into that folder (python/ or node/) and install deps —
   Python: create and activate a venv, then pip install -r requirements.txt; Node: npm install.
3. Ask me for my AgentCall API key (free at app.agentcall.dev/api-keys). If AGENTCALL_API_KEY is
   already set, or ~/.agentcall/config.json exists, use that and don't ask. A key is required —
   never proceed without one.
4. Ask me three quick things: a name (default: AgentCall); its on-camera face — audio (no video,
   just listens), pattern (Pattern AI Labs logo), ring, transcript, or image (my own logo/photo);
   and the notes format — md, txt, or json.
5. Run the builder once with my answers — it writes a gitignored .env (the key) and config.jsonc
   (the settings): python build.py --key <KEY> --name <NAME> --display <FACE> --format <FORMAT>
   (Node: node build.js …). If I already had a key set, omit --key. Show me the output.
6. Tell me to join my meeting; when I send the link, run it:
   python notetaker.py "<MEET_LINK>"  (Node: node notetaker.js "<MEET_LINK>").
   notes/ fills in live; the bot leaves when everyone else does.

Do each step yourself, in order. If a step fails, stop and show me the exact error — don't guess
or fake success. After this one-time setup I can change any setting by editing config.jsonc directly.
```

Your assistant clones, configures, and runs a tested notetaker — Modify the prompt if you want something more.

---

## Commands

```bash
python notetaker.py "<url>" --name Nova --display transcript
node   notetaker.js "<url>" --name Nova --display transcript     # or:  npm start -- "<url>"
```

| Flag | Overrides | Options |
|---|---|---|
| `--name` | `BOT_NAME` | any short name |
| `--display` | `DISPLAY` | `audio` · `pattern` · `ring` · `transcript` |
| `--format` | `OUTPUT_FORMAT` | `md` · `txt` · `json` |
| `--out` / `--port` | `OUTPUT_DIR` / `WEB_PORT` | folder / port |
| `--web` / `--no-web` | `WEB` | live page on / off |

---

## The on-camera tile

What the bot shows on camera is the **`DISPLAY`** setting in [`config.jsonc`](config.jsonc). **Change it
anytime — edit the file and re-run, no rebuild needed.** Built-in choices:

| `DISPLAY` | The tile shows |
|---|---|
| `"audio"` | nothing — audio only · **lightest, the default** |
| `"pattern"` | the Pattern AI Labs logo + bot name |
| `"ring"` | a glowing neon ring + bot name |
| `"transcript"` | the live transcript, on screen in the call |

### Use your own logo or photo

Two steps, no code:

1. Drop your image in the [`avatars/`](avatars/) folder — e.g. `avatars/acme.png` (`.png` · `.jpg` · `.gif` · `.svg` · `.webp`).
2. Set `"DISPLAY": "acme"` in `config.jsonc` — the file name **without** the extension.

That image becomes the bot's tile. (The builder can do this for you too — pick **image** and give it the path.)
Want an animated or live-updating tile instead of a still image? Use an **HTML page** — see just below.

### Animated or live-data tile (advanced)

Drop an HTML page `avatars/<name>.html` and set `DISPLAY` to `<name>`. Start from
[`avatars/pattern.html`](avatars/pattern.html) or [`avatars/transcript.html`](avatars/transcript.html) —
`{{BOT_NAME}}` and `{{AVATAR_LINES}}` are filled in for you. It's your own HTML/CSS/JS, tunnelled in as the bot's video.

---

## Build on top

The notetaker hands you every line the instant it's spoken, and the full transcript when the call
ends. Two small hooks are all it takes to make it do more:

- auto-email or Slack the notes the moment the meeting wraps
- live summaries or action items as people talk
- a searchable archive of every meeting in a database
- a one-click web UI, so you never open a terminal
- a Notion / Linear / CRM sync

**Want to go beyond a notetaker?** It all rides on **[AgentCall](https://agentcall.dev)** — the meeting
layer underneath: voice, video, avatars, the works. The docs, examples, and full API are right there.
Whatever you can imagine for a meeting, that's where you build it.

---

## How it works

```
  notetaker ──spawns──▶ engine/bridge ──▶ AgentCall ──▶ joins the meeting
      ▲                      │
      └──── clean events ◀───┘   participant joined/left · speech · chat · call ended
```

The notetaker runs AgentCall's **bridge** as the transport, reads its events, and writes your file —
your transcript stays on **your** computer. For an avatar it runs the visual bridge, which tunnels a
page you serve as the bot's video. The bot only runs while it's actually in your meeting, and your
notes never leave your machine.

---

## License

MIT. The bundled `engine/` bridge is AgentCall's, also MIT. Powered by
[AgentCall](https://agentcall.dev) · [FirstCall](https://firstcall.dev).
