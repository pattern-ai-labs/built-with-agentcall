# Built with AgentCall

Real, working things people build on **[AgentCall](https://agentcall.dev)** — the API that gives an AI
agent a real seat in a live meeting: it listens (live transcripts), speaks, shows an on-camera tile, and
shares its screen — the same way across Google Meet, Zoom, and Teams.

Every project here is a **self-contained folder** you can clone, run, and build on.

> **Built something on AgentCall?** [Add it →](CONTRIBUTING.md)

## Projects

| Project | What it does | Stack |
|---|---|---|
| **[meeting-notetaker](meeting-notetaker)** | A silent bot that joins your call, writes the whole transcript live, and leaves when everyone else does. | Python · Node |
| **[meeting-presenter](meeting-presenter)** | A skill that turns your AI agent into a presenter: it joins your call and delivers your slides on its camera, narrated aloud and advancing on their own, and you steer it by voice or a control page. | Python |
| **[meeting-standup](meeting-standup)** | A manager bot that runs your team's daily standup: it calls on each person by name, keeps time, remembers blockers across days and follows up, then posts a summary with action items in the chat. | Python |

*More coming — yours could be next.*

## Run one

Clone the whole repo:

```bash
git clone https://github.com/pattern-ai-labs/built-with-agentcall
cd built-with-agentcall/folder-name
```

…or grab just one folder:

```bash
git clone --filter=blob:none --sparse https://github.com/pattern-ai-labs/built-with-agentcall
cd built-with-agentcall && git sparse-checkout set folder-name
```

Each folder has its own `README.md` with setup steps.

## Contribute

Fork this repo, add your project as a new top-level folder (with its own `README.md`), and open a pull
request. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the conventions — one folder per project,
kebab-case name, no committed secrets.

## About AgentCall

[AgentCall](https://agentcall.dev) is the meeting layer under all of this — one API to put an AI agent
into a call with voice, an on-camera presence, screen-share, and real-time transcription, identically on
Meet, Zoom, and Teams. These projects are what you build on top of it.

---

MIT — see [LICENSE](LICENSE). By [Pattern AI Labs](https://agentcall.dev).
