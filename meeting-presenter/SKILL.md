---
name: meeting-presenter
description: >
  Join a Google Meet, Zoom, or Teams meeting as an AI presenter and DELIVER a
  presentation — slides on the bot's camera, narrated aloud, auto-advancing.
  Hand it a DOCUMENT (PDF, PowerPoint, or Word) or a TOPIC. A PowerPoint (already
  a deck) is shown as-is; a Word/PDF document is turned into a meaningful deck the
  agent authors from the source's text AND figures. Cross-platform and self-contained.
  Use when the user wants their agent to present something in a live meeting.
argument-hint: <meeting-url> --deck <file.pdf|.pptx|.docx|deck.json> [--mode auto|show|generate] [--name <bot>] [--voice af_heart] [--pace 1.0] [--avatar-mode]
user-invocable: true
license: MIT
---

# Presenter — present a document (or a topic) in a meeting

An AI bot **joins a meeting and presents**: by default the deck goes **big on the meeting's main stage
as a screenshare**, narrated in the bot's voice and advancing by itself, with the bot's **face in a small
camera tile**. (Pass `--avatar-mode` — or if the user says "join in avatar mode" — to instead show the
deck *on* the camera tile, the original single-tile look.) No clicker either way.

**Display modes:** *screenshare* is the default (readable full-stage slides + a presenter face);
*avatar mode* (`--avatar-mode`) puts the deck on the camera tile only. Everything else — voice control,
the companion remote, narration, Q&A — works identically in both.

The headline use: **the user gives a meeting link and a document — PDF, PowerPoint, or
Word — and the bot presents it.** You can also start from just a topic.

## Do this in order (the whole flow — same every time, no detours)

**0 · Preflight (once).**
- **API key — check in this order, and DON'T ask if it's already there** (one config, the SAME file
  AgentCall uses; this is the exact order `load_api_key` uses):
  1. `~/.agentcall/config.json` exists with an `api_key`? → ready.
  2. `AGENTCALL_API_KEY` env var set? → ready.
  3. Neither? → ask the user for their key (free at agentcall.dev/api-keys) and **write it to
     `~/.agentcall/config.json`** (see Setup) — creating that file exactly like AgentCall would — then continue.
- **Dependencies** — `pip install -r requirements.txt` (fast; pure-Python wheels, no system tools).

**1 · Turn the input into a deck. You (the agent) always own the narration — never let the bot read
the slide off the screen.** Run `doc_to_deck.py` first, then look at what it produced:
- **A `.pptx`/`.ppt`, or a slide-shaped PDF** → *show the real slides*, but the narration is yours:
  - If the file has **speaker notes on every slide** (`.pptx` only — legacy `.ppt` notes can't be read;
    ask for a `.pptx`), they become the narration — you're done, just present. Notes on only SOME
    slides → the deck is flagged; author the missing ones.
  - If it has **no notes**, the deck comes back flagged **`needs_narration`** with every `notes` empty
    (and a `decks/<name>/source.json`). **Author a spoken `notes` line per slide** (read `source.json`
    + glance at the slide images) before presenting — `present.py` **refuses to run** an un-authored
    converted deck, so there is no shortcut around this. Authoring IS the presenting.
- **A `.docx`/`.doc`, a report-style PDF, or a topic** → *author a deck*: run `doc_to_deck.py --mode generate`,
  read **`decks/<name>/source.json`** (its text + extracted figures) and write `decks/<name>/deck.json`.
  **Do NOT try to open or render the file yourself** (no `pdftoppm`, no image tools) — `source.json` is your
  input. Keep authoring tight: one idea per slide, short bullets, natural narration — don't over-polish.
- **Authored work is protected — but only in the DEFAULT deck folder.** Re-running the conversion (or
  `present.py --deck <same document>`) with an unchanged source **reuses the authored `deck.json`** in
  `decks/<name>/` — instant, nothing re-rendered, your narration survives. It only re-converts when the
  document itself changed (then re-author — the flag comes back). **Do NOT pass `--out <a different
  folder>` for a document you'll author and present** — a fresh folder has no deck to reuse, so you get an
  empty-notes draft (and the real authored deck goes stale). `--out` is only for throwaway/inspection
  conversions. Author, present, and re-present all from the default `decks/<name>/deck.json`.
- Full detail + deck schema: sections **A** and **B** below.

**Hard rules (each of these has burned a real session — the code enforces most of them, don't fight it):**
1. **Never one-shot a raw document into `present.py` and walk away.** Convert → check `needs_narration`
   → author `notes` → present. The refusal exit (code 2) is your signal to author, not an error to bypass.
2. **Never overwrite an authored `deck.json` by re-converting.** Unchanged source → it's reused
   automatically; only delete the deck folder if the user explicitly wants a fresh conversion.
3. **Never invent a placeholder deck or apologize on camera.** If conversion fails, present.py exits —
   fix the input (or install LibreOffice) and relaunch. A bot that never joins beats one that looks broken.
4. **Never leave a call running.** present.py stops billing on every exit path itself — but if YOU
   spawned it, confirm the process actually exited when the presentation ends.

**1b · Honor the user's brief — their instructions shape the deck AND how you drive it.** When the user
says anything beyond "present this", capture it and apply it. It lands in one of two places:
- **Authoring-time (bake into `deck.json`)** — anything about *content or delivery*:
  - *"keep it short / ~5 min / one-liner per slide"* → write shorter `notes`; the slide hold is
    length-based, so shorter notes = a faster talk. *"go deep"* → richer `notes`.
  - *"say ‹X› on the pricing slide"*, *"open with a joke"*, *"emphasise ‹Y›"* → put that line into that
    specific slide's `notes` so it's spoken exactly where intended.
  - *"skip the roadmap slide"* → drop/merge it while authoring.
- **Live-driving policy (you enforce while running)** — anything about *reacting in the room*:
  - *"don't answer questions about ‹Z› / pricing / headcount"* → when such a question comes through
    `heard.jsonl`, DON'T answer it: reply with a brief deflection (`{"cmd":"say","text":"Good question —
    let's take that offline."}`) instead of a real answer. *"defer all Q&A to the end"* → acknowledge and
    keep presenting rather than answering mid-deck.
  - *"pause on slide 3 for discussion"*, *"only advance when I say so"* → adjust how eagerly you send
    `next`/`present`.
  Keep a short mental note of these rules for the whole session — they persist across the call, not just
  the first slide. If a request is ambiguous, ask the user before launching.

**2 · (optional) Preview** — `present.py --local --deck <deck>` prints a localhost URL to watch it before going live.

**3 · Launch live, in the background** — `python scripts/present.py "<url>" --deck <deck> &`
(name/voice come from `~/.agentcall/config.json`; `--name`/`--voice` override per-run).
The bot takes 30–90s to appear, then greets and waits.

**4 · Drive it — start watching immediately.** Tail `link/heard.jsonl` and reply in `link/commands.jsonl`
(full protocol under **Voice control**). It won't *start* until you reply `present` to a "go ahead".

**5 · Finish** — it auto-leaves and stops billing when everyone leaves, on the time cap, or on a `leave`
command. If you spawned it, make sure the process exits.

## Two modes (this is the key idea)

| Mode | What it does | Default for |
|---|---|---|
| **show** | Renders the document's REAL pages/slides to images and shows them unchanged | `.pptx`/`.ppt`, and slide-shaped PDFs |
| **generate** | Parses the document, then YOU author a meaningful deck (titles, bullets, spoken narration) and pull in the source's own figures | `.docx`/`.doc`, dense/report PDFs, and topics |

Rule of thumb: **a file that's already a slide deck → show it. A document (prose) → generate a deck from it.** `auto` (the default) decides by file type, and for PDFs by page shape (landscape + sparse text = a deck → show; portrait + dense = a document → generate). Override with `--mode show|generate`. The user's intent always wins ("present my slides exactly" → show; "summarize this into a deck" → generate).

## A) Show a deck as-is (PowerPoint, or a slide-style PDF)

Show mode keeps every slide's real design (a full-bleed image of the actual slide). What it **says**
depends on whether the file carries speaker notes:

```bash
python scripts/doc_to_deck.py "/path/deck.pptx"      # render slides + detect notes
```
- **Has speaker notes on every slide** (`.pptx`) → they're the narration. Just present:
  ```bash
  python scripts/present.py "<MEETING_URL>" --deck decks/deck/deck.json
  ```
- **No notes — or notes on only SOME slides** → the deck is flagged `needs_narration` (the un-noted
  slides would otherwise show in dead silence). **Author a spoken `notes` line for every slide that
  lacks one** (from `decks/<name>/source.json` + the slide images), then present the authored
  `deck.json`. This is what makes it a *presenter* and not a screen-reader.

**`present.py` REFUSES to present a `needs_narration` deck until every slide has `notes`** (it exits
with an error pointing you at `source.json`). There is **no** "read the slide text aloud" fallback —
by design. So never hand a raw notes-less file to `present.py` expecting it to talk: convert, author
the `notes`, then present the authored `deck.json`. The refusal is the safety net; authoring is the job.

## B) Generate a meaningful deck (Word, dense PDF, or a topic)

A document isn't slides — showing its pages makes a poor talk. So **you build the deck.**

1. **Convert to get the source content + figures:**
   ```bash
   python scripts/doc_to_deck.py "/path/file.docx" --mode generate
   # writes decks/<name>/deck.json — a mechanical draft with EMPTY notes, flagged needs_narration;
   #   present.py REFUSES it until you author the notes (that's the next step, not optional)
   #    and decks/<name>/source.json  ← the parsed sections + extracted images, for you to author from
   #    and decks/<name>/img/         ← the document's own figures, already pulled out
   ```
2. **Author `deck.json` from `source.json`** — this is the important part. Do it well:
   - **Outline first, then fill.** Make a slide per main idea. Build a mental checklist of the
     document's sections/headings and make sure **every key point lands on a slide** — don't drop things.
   - Each slide: a short `title`, ≤6 short `bullets`, and `notes` = natural **spoken** narration
     (1–4 sentences; acronyms spelled phonetically like "A.P.I." so TTS says them right).
   - **Reuse the source's figures.** `source.json` lists images extracted from the document (in
     `sections[].images` and `all_images`). Put the relevant one in a slide's `image` field — the
     slide then shows your bullets beside that real figure (a split layout). Don't invent images.
   - Open with a one-line intro slide; close with a wrap-up (the bot leaves after the last slide).
3. **Preview and present** (sections C & D). For a topic with no file, skip the conversion and just
   author `decks/<name>.json` directly.

Deck JSON schema (text slide, image slide, or both together):
```json
{
  "title": "My Talk",
  "slides": [
    { "title": "Intro", "notes": "Spoken intro." },
    { "title": "A point", "bullets": ["short phrase", "another"], "notes": "Narration." },
    { "title": "With a figure", "bullets": ["what it shows"], "image": "fig1.png", "notes": "Narration." },
    { "image": "slide3.png", "notes": "A full-bleed real slide (show mode)." }
  ]
}
```
(`bullets`/`notes`/`image` all optional; aliases `points`→bullets, `say`→notes; a title-only slide is a cover.)

## C) Preview (recommended)

```bash
python scripts/present.py --local --deck decks/<name>/deck.json     # or decks/<name>.json, or a raw file
```
Prints a `http://localhost:PORT/?ws=local` URL — open it to watch the slides render and
auto-advance, with no meeting.

## D) Present live

```bash
python scripts/present.py "<MEETING_URL>" --deck <file-or-deck> [--mode show|generate]
```
**Live meetings are interactive by default (consent-driven):** the bot joins, introduces itself, and
**waits** until someone tells it to begin. Two ways to steer it:

- **By voice → routed to YOU (the brain).** There is **no keyword matching in the code.** Whatever a
  participant says — once they address the bot by name, or during a short follow-up window — is handed to
  you to interpret, and you reply with one command. Protocol below.
- **By the companion control page (direct).** A phone/browser page (Prev / Pause / Next / tap-a-slide /
  Restart / Dismiss) whose link the bot drops in the meeting chat, and ALSO prints to the console
  (`▸ Controls page: …`) — **relay the console URL to the user directly**; chat delivery on Meet can be
  unreliable. Buttons are unambiguous and act **in-process, instantly** (they don't go through you), so
  they're the reliable fallback and the way to drive it with no agent attached.

The bot **auto-leaves and stops billing** when everyone else leaves, on a hard cap, or on a clean exit —
an orphaned bot can't run up cost. Add **`--auto`** to present immediately without asking; `--pace <s>`
sets the gap between slides.

## Voice control — YOU are the brain (the important part)

In interactive mode the bot forwards what it hears to a file link and runs the command you write back.
No hardcoded phrases: *you* understand the request and decide the action. Run it in the **background**
and loop:

1. Start it (interactive is the default for live):
   `python scripts/present.py "<url>" --deck <file> &`
   It prints two paths: **`link/heard.jsonl`** (bot → you) and **`link/commands.jsonl`** (you → bot).
   **Start watching immediately** — the greeting plays on its own, but the presentation won't *start*
   until you reply to the first "go ahead". Read only NEW lines (track a byte/line offset, or block on
   the file — don't re-process old lines), and reply within **~45s** (after that the deck resumes on its
   own). The efficient, event-like way to watch, no busy-polling:
   ```bash
   tail -n +1 -f link/heard.jsonl     # streams each new utterance as a line; handle it, then append your reply to link/commands.jsonl
   ```
   (`&`/`tail` are POSIX — on Windows use your background-run facility and
   `Get-Content link/heard.jsonl -Wait`.)
   present.py runs whatever you append to `commands.jsonl` within ~0.3s.
2. **When the bot is addressed by name** (*"Presenter, …"*) — or during the **~20s follow-up window**
   after any exchange — a line is appended to **`link/heard.jsonl`**:
   `{"id": 7, "speaker": "Maya", "text": "go back and explain the churn", "slide": 4, "title": "Retention", "state": "presenting"}`
   Ordinary chatter (no name, outside the window) is never forwarded — you're not spammed.
3. **Read it and append ONE command per heard line to `link/commands.jsonl`, echoing its `id`:**

   | The person means… | You write |
   |---|---|
   | begin / resume presenting ("go ahead", "let's start") | `{"id":7,"cmd":"present"}` |
   | next / previous slide | `{"id":7,"cmd":"next"}` · `{"id":7,"cmd":"back"}` |
   | jump to a slide by its **1-based** number (resolve slide *names* → number via the deck) | `{"id":7,"cmd":"goto","n":4}` |
   | …with a spoken **acknowledgement** so it doesn't jump cold (recommended for a *requested* jump) | `{"id":7,"cmd":"goto","n":4,"say":"Sure — here's the admin app."}` |
   | replay this slide / start over | `{"id":7,"cmd":"repeat"}` · `{"id":7,"cmd":"restart"}` |
   | pause / stop | `{"id":7,"cmd":"pause"}` |
   | leave the call | `{"id":7,"cmd":"leave"}` |
   | a question, or anything to say aloud | `{"id":7,"cmd":"say","text":"…"}` |
   | nothing to do (chatter, not for the bot) | `{"id":7,"cmd":"none"}` |

   **Compounds do both** — "go back and explain the churn" → write TWO lines for that `id`:
   `{"id":7,"cmd":"back"}` then `{"id":7,"cmd":"say","text":"Churn rose because…"}`.
4. The bot runs it; for a spoken/no-op reply while presenting it then continues the deck on its own.

**How nav sounds.** `next`/`back`/`goto` change the slide **instantly**, cut the old slide's narration,
then the bot **narrates the slide it landed on** (after a short beat; rapid taps only narrate the slide you
settle on). A manual jump is a *step* — it does NOT auto-advance. To make the bot walk the **whole deck
hands-free** from here, send `present` (narrate +
auto-advance). So "go to pricing" = `{"cmd":"goto","n":N}`; "take it from pricing and walk us through the
rest" = `{"cmd":"goto","n":N}` then `{"cmd":"present"}`. **When the user asked to go somewhere specific,
add a `say`** — `{"cmd":"goto","n":N,"say":"Sure — here's the pricing slide."}` — so it acknowledges the
request first instead of cold-narrating (that little confirmation is what makes it feel responsive rather
than dumb). The bot also speaks a quick "one moment" on its own when idle and addressed.

**How to decide (your judgment, not the code's):**
- **Answer from the deck** — every heard line carries the current `slide`/`title`, and you have the full
  `deck.json` / `source.json`. Keep spoken answers to 1–2 sentences.
- **Off-topic?** Don't force it — `say` a one-line steer-back ("that's outside today's deck — happy to
  take questions on this slide").
- **Needs research / multi-step work?** You have only ~45s before the deck moves on, so don't attempt it
  live — `say` a quick defer ("I'll follow up after the session").
- **Reply promptly** (the deck pauses while it waits; after ~45s it resumes on its own), and **copy the
  heard line's exact `id`** into your reply. A wrong/stale `id` drops nav/pause commands, but a late
  `say`/`present`/`leave` is still honored — so don't re-send a slow answer (omitting `id` entirely
  still resolves the current turn, but copying the exact one is the safe path).
- **Follow-ups need no name:** after any exchange the bot keeps forwarding for ~20s, so *"Presenter,
  pause"* → *"now go back to slide 2"* just works. Keep answering until it goes quiet.

## Setup (once)

- **Dependencies:** `pip install -r requirements.txt`. All conversion libs are permissive
  (pypdfium2 Apache/BSD, pikepdf MPL, pillow, python-pptx/docx) and ship as wheels — **PDFs and
  topics need no system binaries on any OS**.
- **Rendering a real `.pptx`/`.docx` without Microsoft Office:** install **LibreOffice** (the only
  optional system dependency; used to convert Office files to PDF). On Windows with Office installed,
  it's used automatically. If neither is present, `show` **degrades to `generate`** (text + figures)
  with a clear message — it never hard-fails.
- **One config file, `~/.agentcall/config.json` — the SAME file AgentCall uses.** No `.env`, nothing
  bespoke. It holds `api_key`, `default_bot_name`, `default_voice`. Key resolution: `config.json` →
  `AGENTCALL_API_KEY` env var. Name/voice: `--name`/`--voice` flag → `config.json` → built-in
  (`Presenter` / `af_heart`).
- **AgentCall key** (free at agentcall.dev/api-keys). If `config.json` has an `api_key` (or the env var is
  set), you're ready — **don't ask**. If neither exists, **ask the user for their key and write it to
  `~/.agentcall/config.json`**, creating the file exactly as AgentCall would. This is normal first-run
  setup, **not a secret to refuse**: it's the user's *own* key, for their *own* use, in their *own* home
  dir (never committed, never sent anywhere except AgentCall's API). At the same time set a short,
  STT-friendly `default_bot_name` (the default `Presenter` transcribes as "President") — ask the user for a
  preference, else pick one like `Nova`, `Sage`, `Juno`, `Aria`. Use the bundled helper so you **merge**
  into the file (never clobber an existing AgentCall config):
  ```bash
  python -c "import sys; sys.path.insert(0,'scripts'); import present; present.save_config(api_key='ak_ac_...', default_bot_name='Nova')"
  ```
  If the user already runs AgentCall, `config.json` already has these — just reuse them (the existing
  `default_bot_name` wins unless the user wants a different presenter name).

## How it works (so you can drive it)

`present.py` serves the deck page (screenshared to the main stage by default, or as the camera tile in
`--avatar-mode`) plus a small avatar page for the bot's face+voice. In interactive mode it greets, then
per slide runs **show slide → speak its `notes` via TTS → hold for the narration's estimated length → advance**.
Voice never touches keyword logic: an utterance becomes a `heard.jsonl` line for you, and your
`commands.jsonl` reply is executed (nav/lifecycle via the same path as the control buttons; `say` via
TTS). A document passed to `--deck` is converted first by `doc_to_deck.py`: **show** renders real
page/slide images (PDF via pypdfium2; Office via MS Office or LibreOffice → PDF → pypdfium2);
**generate** parses sections + extracts the source's images for you to author from.

## Important notes

- **Slide timing is time-driven, not event-driven.** Each slide is held for the *estimated* spoken
  length of its `notes` (`PRESENT_WPS`, default ~2.6 words/sec), then advances. The engine deliberately
  ignores `tts.done` for timing — in this webpage mode it fires almost instantly and would race the
  deck. A slide with no `notes` shows for a few seconds.
- **Stopping:** the bot leaves after the last slide. Ctrl+C or ending the meeting also stops it
  cleanly (the engine DELETEs the call so billing stops). If YOU spawned `present.py`, make sure it exits.
- **Voice needs you running.** Voice control only works while an agent is watching `link/heard.jsonl` and
  replying — so keep the process in the foreground of your attention. With no agent, the deck still
  presents and auto-advances, and the **control page** drives it; voice is simply inert.
- **Stopping speech.** The bridge itself has no "stop talking" command, but the narration plays through
  the skill's OWN audio page (the avatar page in screenshare mode, the slide page in avatar mode) — so
  **pause, a jump, leave, or a question clears that page's audio immediately** (the exact stop barge-in
  uses), and the bot goes quiet within a fraction of a second. The
  next narration re-opens the audio on its own (a 30s page-side safety timer guarantees it never stays
  muted). So Pause actually pauses, a jump cuts the old slide before narrating the new one, and a question
  interrupts cleanly with a quick "one moment" before the answer.
- **Cross-platform:** pure Python + pip wheels; LibreOffice is the only optional native dependency,
  needed solely to render real Office files to images on machines without MS Office.

## Files

```
meeting-presenter/
├── SKILL.md  README.md  LICENSE  requirements.txt  .gitignore
├── scripts/
│   ├── present.py          the presenter engine — serves slides, runs the bridge, drives the loop
│   └── doc_to_deck.py      converts a PDF / PowerPoint / Word file into a deck (show: images; generate: source + figures)
├── assets/
│   ├── slides.html         the deck page: text, full-bleed image, and split (bullets + figure) slides — the
│   │                       screenshare surface by default, or the camera tile in --avatar-mode
│   ├── avatar.html         the bot's camera/face + narration audio + hush (used in the default screenshare mode)
│   ├── control.html        the companion control page (Prev / Pause / Next / tap-a-slide / Restart / Dismiss), shared in chat
│   └── agentcall-audio.js  plays the bot's narration through the camera page
├── engine/
│   └── bridge-visual.py    AgentCall's bundled visual bridge (joins the meeting, voice, camera) — don't edit
└── decks/
    └── sample.json         an example deck:  python scripts/present.py --local --deck decks/sample.json
```

Run everything from the skill root (`meeting-presenter/`) so `decks/…` paths resolve; the scripts find
`assets/` and `engine/` on their own regardless of your current directory, and the key/name come from
`~/.agentcall/config.json`.
