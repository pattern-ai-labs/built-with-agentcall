---
name: meeting-standup
description: >
  Turns your AI agent into a STANDUP MANAGER: it joins your Google Meet, Zoom, or Teams
  call as an audio-only bot and runs the whole daily standup. Calls on each person in the
  call by name, keeps time, tracks blockers across days, then posts a summary with action
  items to the chat and stays on to manage it. Use when someone wants their agent to run,
  facilitate, or host a daily standup or scrum.
argument-hint: <meeting-url> [--auto] [--name <bot>] [--voice <id>]  (or --local to dry-run)
user-invocable: true
license: MIT
---

# Standup Manager — a skill that runs your team's standup, then manages the summary

> **This file is the rulebook for how you behave in the meeting. Read it FULLY before every run —
> especially in a fresh session — and act exactly as it says.** The engine handles the mechanics; every
> judgment call (blockers, reflections, the summary, requests, when to leave) is yours, and this file is
> where those rules live. Don't improvise a different protocol mid-call.

**For managers and team leads.** This turns your AI agent into the person who *runs* standup. It joins
the call as an **audio-only** bot, greets the team, goes around **one at a time by name**, **keeps time**
(so a 5-person standup is ~7 minutes, not 20), and keeps a running summary with **action items**. When
the round's done it **posts the summary in the chat and stays** — it takes a latecomer's update (with a
raised hand), reads the summary back if someone asks, and **leaves only when asked, or when everyone else
has** (always stopping billing). It also **remembers blockers across days** and follows up on them.

## You are the brain — but stay OUT of the round's way (this is the important part)

**The bot runs the round itself, fast and deterministically** — it auto-starts the moment anyone speaks,
times each turn, drops a quick filler ("Got it, thanks") between people, and never waits on you. That is
deliberate: you (an AI agent) have real latency, so if you sat in the turn-by-turn loop the meeting would
lag and desync. **So don't drive the round. Do your thinking asynchronously and in standby.**

Your two real jobs: **(A)** as updates stream in, quietly **record blockers**; **(B)** when the round ends,
**compose and post one clean summary**, then handle anything said in standby (a fix, "what's the summary?",
"you can leave"). Everything is a tiny file link: the bot appends to `link/heard.jsonl`; you append one
line to `link/commands.jsonl`. **Start watching the moment you launch:**
```bash
tail -n +1 -f link/heard.jsonl      # each new line is an event; append your reply to link/commands.jsonl
```
(POSIX; on Windows see the alternatives just below.) The bot runs what you append within ~0.12s.

**Windows alternatives:**
- **Git Bash** (ships with Git for Windows): all commands above work as-is.
- **WSL**: all commands above work as-is.
- **PowerShell:** replace `tail -f` with `Get-Content -Wait`:
  ```powershell
  Get-Content -Wait "$PWD\link\heard.jsonl"
  ```
- **Native cmd (no alternatives installed):** fall back to polling the file.

**Lines the bot sends you** (`link/heard.jsonl`) — each has an `id`, a `kind`, and context:

| `kind` | when | what you do |
|---|---|---|
| `update` | a person's turn ended; the bot is **paused briefly for you** to reflect it back | **reflect their update back in one human line** and advance: `{"cmd":"reflect","for":"<name>","text":"So you shipped X and you're blocked on Y — got it."}`. If it implies a blocker (semantically), send `{"cmd":"blocker","for":"<name>","text":"<blocker>"}` FIRST, then the `reflect`. Don't just say "got it" — mirror what they actually said. (If you're slow, the bot advances with a quick filler. If the person resumes talking mid-pause, your reflect is ignored and their turn continues — you'll get a fresh `update` with the full text; just reflect that one.) **If `no_update` is true, the bot already speaks a short "no update from <name>" line and advances on its own — don't reflect it, just note it.** |
| `round_done` | everyone's done; the bot has **already spoken a recap and posted the full summary to chat itself** (it is built during the round) | you do NOT re-post the summary. Just: (1) **if there are blockers**, ask the room `{"cmd":"say","text":"On <blocker>, anyone got a quick workaround?","listen":true}` and attach any answer with `solution`; (2) optionally add ONE **labelled suggestion** of your own: `{"cmd":"chat","text":"Suggestion (from <bot>): …"}`. Ask for fixes ONLY here, after the whole round — never mid-standup |
| `standby` | someone spoke while standing by | interpret: **"what's the summary?" → `{"cmd":"say","text":"<the FULL summary, spoken>"}`**; "you can head out" → `{"cmd":"leave"}`; a fix someone offers → `{"cmd":"solution","for":"<name>","text":"<fix>"}`; **a missed/muted person giving their standup now ("sorry, I was on mute — my update is …") → `{"cmd":"record","for":"<speaker>","text":"…"}` (+ `blocker` if implied), acknowledge with a short `say`, and re-post the refreshed summary to chat** — or `{"cmd":"ask","person":"<speaker>"}` to give them a proper spoken turn; else `{"cmd":"none"}` |
| `chat` | someone posted in the meeting **chat** (`sender` + `text`) | **be conservative — most chat is links, asides, or banter.** `{"cmd":"record","for":"<sender>","text":"…"}` only when it clearly reads as that person's OWN update (or they said they'd post it, e.g. bad mic). **If someone accounts for a quiet teammate** ("Arjun didn't do anything"): don't record it as the sender's; HOLD it, and only if that teammate ends with **no update of their own**, record it for THEM, sourced — `{"cmd":"record","for":"<teammate>","text":"(per <sender>) …"}`. Their own update always wins: if they later give one, record theirs with `"replace":true`. A fix → `solution`. Else → `{"cmd":"none"}` |
| `addendum` | the *previous* person kept talking after their turn closed (a long thinking pause) | already folded into their update for you — no reply needed; just update your notes (and send a `blocker` if the new bit implies one) |
| `silent` | someone was asked but no speech has come through yet | usually just wait (`{"cmd":"none"}`). If they're clearly away → `{"cmd":"skip"}`; a gentle nudge is fine but optional |
| `joined` | a fresh participant joined (fires once per person) | to take a latecomer's standup: `{"cmd":"say","text":"Welcome <name>…"}` then `{"cmd":"ask","person":"<name>"}`. Otherwise nothing |
| `summary_updated` | a latecomer's update was folded in | re-post the refreshed summary with `{"cmd":"chat","text":"…"}` |
| `followup` | a cross-day follow-up answer (their `prior` blockers are listed) | judge it: `{"cmd":"resolve","for":"<name>","cleared":true｜false}` |
| `addressed` | someone said the bot's **name** during a turn | interpret and reply: "skip me" → `skip`; a question → `say`; **"I was on mute earlier — here's my standup: …" → `{"cmd":"record","for":"<speaker>","text":"…"}`, or `{"cmd":"ask","person":"<speaker>"}` for a proper turn (mid-round this queues them politely after the current speaker — it never interrupts)** |

**Commands you can send** (`link/commands.jsonl`, one JSON per line, echo the `id` when replying to one):

| command | effect |
|---|---|
| `{"cmd":"blocker","for":"<name>","text":"<blocker>"}` | record a blocker you spotted in an update (async — doesn't interrupt anything) |
| `{"cmd":"record","for":"<name>","text":"…"}` | store a person's update text (e.g. one they posted in the chat) so it lands in the summary + file |
| `{"cmd":"reflect","for":"<name>","text":"…"}` | say a one-line reflection of their update and advance to the next person |
| `{"cmd":"chat","text":"…"}` | post your composed summary + action items into the meeting chat |
| `{"cmd":"say","text":"…","listen":true}` | speak (read the summary, greet a latecomer, ask the room about a blocker). `"listen":true` keeps forwarding standby speech so you catch the reply |
| `{"cmd":"solution","for":"<name>","text":"<fix>"}` | attach a fix to that person's blocker (turns it into the action item) |
| `{"cmd":"ask","person":"Sam"}` | take a specific person's standup (e.g. a latecomer) |
| `{"cmd":"resolve","for":"<name>","cleared":true｜false}` | close (or keep) a cross-day blocker after a follow-up |
| `{"cmd":"leave"}` | leave the meeting — billing stops. Use when asked to head out |
| `{"cmd":"skip"}` / `{"cmd":"next"}` | override the timing: skip the current person, or wrap them early. Rarely needed |
| `{"cmd":"say","text":"…"}` / `{"cmd":"none"}` | speak / do nothing |

**Rhythm:** on each `update`, **reflect it back** (record any blocker first). The bot pauses briefly for
you, so keep the reflection to one quick human line — mirror what they said, don't just say "got it". When
everyone's done (`round_done`): **`chat` the full summary, `say` a spoken summary** of each person + the
blockers, then **ask the room for workarounds** to those blockers and attach what comes back. In standby,
answer any question, catch late fixes, and **leave when asked**. **Never block the round** beyond the
reflection — everything else is async or in standby. **Never ask for a fix mid-round** — only after everyone.

**Two rules for the summary:**
1. **Reflect per person; full summary at the end.** After each person, mirror their update in a line ("so
   you did X, blocked on Y — got it"). At the end, *speak* a real summary (everyone + blockers) AND put the
   full thing in chat. If someone later asks "what's the summary?", read the full one aloud again.
2. **Attribute honestly — keep YOUR ideas separate from what people said.** A workaround a person stated is
   *their* plan. A fix *you're* proposing is a **suggestion** — label it (a separate "Suggestion (from
   <bot>):" line). Never fold your own idea into a person's update or action item as though they said it.

## Do this in order

**0 · Preflight (once).** API key — check `~/.agentcall/config.json` → `AGENTCALL_API_KEY` env; if neither,
ask the user and write it to `~/.agentcall/config.json`. Then `pip install -r requirements.txt`.

**1 · Set the roster.** In `config.jsonc`, fill `TEAM` with the people to call on, **in order**, using the
name each shows as in the meeting. If the user didn't give you one, ask once. (Absent roster members are
skipped and noted; anyone present who isn't on the roster still gets a turn.)

**2 · (optional) Dry-run:** `python scripts/standup.py --local` — a scripted run (round → standby →
latecomer → leave) with no meeting or billing.

**3 · Launch, and immediately start watching the link:**
```bash
python scripts/standup.py "https://meet.google.com/abc-def-ghi" &
tail -n +1 -f link/heard.jsonl
```
It appears in ~30–90s, greets, and waits for a go-ahead — which comes to you as a `waiting` line. Pass
`--auto` to skip waiting. `--name` / `--voice` override the config.

**4 · Drive it.** Reply `start` on the go-ahead; let the round run (it times itself); when `round_done`
arrives, post your polished summary + action items to chat; answer name-addressed questions; `leave` when
asked. Keep watching until the bot has left.

**5 · Finish.** It leaves on your `leave`, when everyone else leaves, or on Ctrl+C — **always stopping
billing** (it sends the leave command and DELETEs the call, with retries, on every exit path).

## What's deterministic vs. yours

- **Deterministic (the bot, no keywords):** join, greet, the round-robin *timing* (a nudge near
  `PER_PERSON_SECONDS`, then it moves on; a turn ends on `SILENCE_ENDS_TURN` of quiet), capturing each
  update, the fix-beat *plumbing* (ask → listen → attach → park), a heuristic summary + action items to
  disk and chat, raising a hand for a latecomer, the cross-day follow-up, leaving on an empty room, and
  billing-safe teardown.
- **Yours (the brain):** judging what's a blocker, what to ask, and what's a real fix; starting on a
  go-ahead; sharp action items and the polished chat summary; answering questions; leaving on request.
  All interpreted by you — never by brittle keyword lists.

**Token cost is bounded to decisions, not talk.** Mid-turn speech is captured locally (free); the bot only
forwards to you at decision points — one `update` per turn, the fix beat's few utterances, name-addressed
lines, and lifecycle events. Idle chatter is dropped. So a 5-person standup is ~a dozen round-trips, not
one per sentence.

## Config (`config.jsonc`)

`TEAM` (names + order) · `QUESTIONS` (asked as one prompt) · `PER_PERSON_SECONDS` / `NUDGE_AT_SECONDS` /
`SILENCE_ENDS_TURN` / `NO_RESPONSE_SECONDS` (the timeboxing feel) · `WAIT_FOR_GO_AHEAD` ·
`TRACK_ACROSS_DAYS` (the cross-day blocker memory) · `POST_SUMMARY_TO_CHAT` · `OUTPUT_DIR`/`OUTPUT_FORMAT`.
Key/name/voice come from `~/.agentcall/config.json` (the shared AgentCall config; bot name falls back to
`Scrum` — pick a short, STT-friendly name, long ones get misheard).

## How it works

`standup.py` spawns AgentCall's **audio bridge** (`engine/bridge.py`, `mode: audio`) and drives it over
stdin/stdout: it **speaks** via `tts.speak`, **hears** each person as a VAD-coalesced `user.message`
(speaker + text), **posts chat** via `send_chat`, and **raises a hand** via `raise_hand`. A single-threaded
event loop runs the round; per-turn-token timers handle the nudge / timebox / silence / no-response so a
stale timer can never fire into the next turn. What it hears goes to `link/heard.jsonl`; your
`link/commands.jsonl` replies are applied within ~0.12s. Capture, blocker heuristics, cross-day
reconciliation, action items, and the summary are all local — no model, no network beyond the meeting.

## Files

```
meeting-standup/
├── SKILL.md  README.md  LICENSE  requirements.txt  .gitignore
├── config.jsonc          the one file you edit — roster, questions, timeboxes, tracking, chat
├── scripts/
│   └── standup.py        the facilitator + summary-manager engine
├── engine/
│   └── bridge.py         AgentCall's bundled AUDIO bridge (join, TTS, transcription, chat) — don't edit
├── link/                 the agent⇄bot link (heard.jsonl / commands.jsonl) — runtime scratch
└── standups/             daily summaries + history.json — stays local
```

Run from the skill root (`meeting-standup/`) so `config.jsonc`, `link/`, and `standups/` resolve; the
script finds `engine/` on its own, and the key/name come from `~/.agentcall/config.json`.

## Checking for updates (optional)

Optional — the skill is backwards-compatible and runs fine without ever checking. To see whether there's
a newer version, look at the source repo `github.com/pattern-ai-labs/built-with-agentcall` (folder
`meeting-standup`) and compare it with your copy. How to update depends on how it was installed:

- **An agent cloned the repo** → `git pull` in that repo (or just re-fetch the `meeting-standup` folder).
- **Installed via `npx skills add`** → re-run the same command to pull the latest:
  `npx skills add pattern-ai-labs/built-with-agentcall --skill meeting-standup`

Nothing to configure and no version check to run — updating only swaps in newer files.
