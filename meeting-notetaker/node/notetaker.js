#!/usr/bin/env node
/**
 * Silent meeting notetaker — joins a call, writes the transcript live, and leaves
 * when everyone else does. Settings live in config.jsonc; this file is the plumbing.
 * Runs on AgentCall's bridge and never speaks. MIT. https://agentcall.dev
 *
 *   export AGENTCALL_API_KEY="ak_ac_your_key"
 *   npm install
 *   node notetaker.js "https://meet.google.com/abc-def-ghi"
 */

const { spawn } = require("child_process");
const readline = require("readline");
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT_ROOT = path.dirname(__dirname); // config.jsonc, notes/, avatars/ and .env live here

function loadConfig() {
  // One config file, shared by python and node. JSON with // and /* */ comments —
  // strip them, then parse. Edit config.jsonc at the repo root; nothing else.
  try {
    let text = fs.readFileSync(path.join(PROJECT_ROOT, "config.jsonc"), "utf-8");
    text = text.replace(/("(?:\\.|[^"\\])*")|\/\/[^\n]*|\/\*[\s\S]*?\*\//g, (m, s) => (s ? s : ""));
    return JSON.parse(text);
  } catch (e) {
    console.error(`Couldn't read config.jsonc (${e.message}). Check it for typos (trailing commas, missing quotes).`);
    process.exit(1);
  }
}
const CONFIG = loadConfig();

function loadDotenv() {
  for (const d of [PROJECT_ROOT, __dirname]) {
    try {
      const txt = fs.readFileSync(path.join(d, ".env"), "utf-8");
      for (let line of txt.split("\n")) {
        line = line.trim();
        if (line && !line.startsWith("#") && line.includes("=")) {
          const i = line.indexOf("=");
          const k = line.slice(0, i).trim();
          const v = line.slice(i + 1).trim().replace(/^["']|["']$/g, "");
          if (!(k in process.env)) process.env[k] = v;
        }
      }
      return;
    } catch { /* no .env here */ }
  }
}
loadDotenv();

// Your hooks — empty by default. Add your own logic here to build on top.
function onLine(entry) {}
function onMeetingEnd(transcript, meta) {}

const API_BASE = process.env.AGENTCALL_API_URL || "https://api.agentcall.dev";
const DEBUG = !!process.env.NOTETAKER_DEBUG;   // NOTETAKER_DEBUG=1 prints every raw bridge event

function loadApiKey() {
  if (process.env.AGENTCALL_API_KEY) return process.env.AGENTCALL_API_KEY;
  try {
    return JSON.parse(fs.readFileSync(path.join(os.homedir(), ".agentcall", "config.json"), "utf-8")).api_key || "";
  } catch {
    return "";
  }
}
const API_KEY = loadApiKey();

const STATE = { bot: CONFIG.BOT_NAME, status: "starting", present: 0, lines: [] };

function bridgeCommand(display) {
  const override = process.env.NOTETAKER_BRIDGE;
  if (override) return [process.execPath, override];
  const name = display === "audio" ? "bridge.js" : "bridge-visual.js";
  return [process.execPath, path.join(__dirname, "engine", name)];
}

async function endCall(callId) {
  if (!callId || !API_KEY) return;
  let lastErr = "";
  for (let attempt = 0; attempt < 2; attempt++) {   // one quick retry
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), 10000);   // ending a LIVE call can take a few seconds
    try {
      const res = await fetch(`${API_BASE}/v1/calls/${callId}`, { method: "DELETE", headers: { Authorization: `Bearer ${API_KEY}` }, signal: ac.signal });
      if (res.ok || res.status === 404 || res.status === 409) {   // 404 = gone, 409 = "call already ended"
        console.log(`  Call ended cleanly - DELETE /v1/calls/${callId} -> ${res.status}. Billing stopped.`);
        return;
      }
      let body = "";
      try { body = (await res.text()).trim().slice(0, 200); } catch {}
      lastErr = `HTTP ${res.status}${body ? " - " + body : ""}`;
    } catch (e) {
      lastErr = e && e.name === "AbortError" ? "timeout (10s)" : ((e && e.message) || String(e));
    } finally { clearTimeout(t); }
    if (attempt === 0) await new Promise((r) => setTimeout(r, 500));
  }
  console.log(`  (note: couldn't confirm the call stopped - ${lastErr} (call_id=${callId}); the bot has left and the call expires on its own. You can DELETE it manually with that id if needed.)`);
}

function localISO() {
  // Local time as YYYY-MM-DDTHH:MM:SS — matches Python so md/txt notes read in
  // the user's own timezone (and both languages agree).
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function hhmmss(ts) {
  return typeof ts === "string" && ts.length >= 19 ? ts.slice(11, 19) : ts;
}

function fmtLine(e, bold) {
  const tag = e.kind === "chat" ? " (chat)" : "";
  const name = bold ? `**${e.speaker}**` : e.speaker;
  return `[${hhmmss(e.timestamp)}] ${name}${tag}: ${e.text}`;
}

class LiveNotes {
  // Appends each line to the file the moment it's captured (md/txt); json is
  // written once at the end. Created on the first line.
  constructor() {
    this.fmt = CONFIG.OUTPUT_FORMAT;
    let out = CONFIG.OUTPUT_DIR;
    if (!path.isAbsolute(out)) out = path.join(PROJECT_ROOT, out);
    fs.mkdirSync(out, { recursive: true });
    const iso = localISO();
    const stamp = iso.slice(0, 16).replace("T", "-").replace(":", "");
    this.path = path.join(out, `meeting-notes-${stamp}.${this.fmt}`);
    this._fd = null;
    if (this.fmt === "md" || this.fmt === "txt") {
      const stampStr = iso.slice(0, 16).replace("T", " ");
      const head = this.fmt === "md"
        ? `# Meeting Notes — ${stampStr}\n\n## Transcript\n`
        : `Meeting Notes — ${stampStr}\n\n`;
      this._fd = fs.openSync(this.path, "w");
      fs.writeSync(this._fd, head);
    }
  }
  add(entry) {
    if (this._fd !== null) fs.writeSync(this._fd, fmtLine(entry, this.fmt === "md") + "\n");
  }
  finalize(transcript, meta) {
    if (this.fmt === "json") {
      fs.writeFileSync(this.path, JSON.stringify({ meta, transcript }, null, 2));
      return this.path;
    }
    let foot = [];
    if (this.fmt === "md") {
      foot.push("\n## Participants");
      foot = foot.concat(meta.participants.length ? meta.participants.map((p) => `- ${p}`) : ["- (none detected)"]);
      foot.push("\n## Meeting Info", `- Call ID: ${meta.call_id}`, `- Duration: ${meta.duration}`,
        `- End reason: ${meta.end_reason}`, `- Total utterances: ${transcript.length}`);
    } else {
      foot.push("", "Participants: " + meta.participants.join(", "));
    }
    if (this._fd !== null) {
      fs.writeSync(this._fd, foot.join("\n") + "\n");
      fs.closeSync(this._fd);
    }
    return this.path;
  }
}

// Serve an HTML page at / and the live transcript at /transcript.json. The local
// dashboard and the in-call transcript tile use the same page. {{BOT_NAME}} and
// {{AVATAR_LINES}} are filled in as it's served.
function readHtml(p) {
  try { return fs.readFileSync(p, "utf-8"); }
  catch { return `<h1>${path.basename(p)} missing</h1>`; }
}

const IMAGE_MIME = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml" };

function imageHtml(imagePath) {
  // Wrap a raw image (png/jpg/gif/svg/webp) as a full-tile page, so an image
  // dropped in avatars/ can be the bot's avatar with no HTML needed.
  const b64 = fs.readFileSync(imagePath).toString("base64");
  const mime = IMAGE_MIME[path.extname(imagePath).toLowerCase()] || "image/png";
  return '<!DOCTYPE html><html><head><meta charset="utf-8">'
    + "<style>html,body{margin:0;height:100%;background:#0a0e1a}"
    + "body{display:flex;align-items:center;justify-content:center}"
    + "img{max-width:100%;max-height:100%;object-fit:contain}</style></head>"
    + '<body><img src="data:' + mime + ';base64,' + b64 + '"></body></html>';
}

function avatarProvider(display) {
  // avatars/<display>.html (an HTML tile) or avatars/<display>.<img> (a raw image).
  const base = path.join(PROJECT_ROOT, "avatars", display);
  if (fs.existsSync(base + ".html")) return () => readHtml(base + ".html");
  for (const ext of Object.keys(IMAGE_MIME)) {
    if (fs.existsSync(base + ext)) return () => imageHtml(base + ext);
  }
  return null;
}

function serveHtml(htmlProvider, port) {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      if (req.url.startsWith("/transcript.json")) {
        res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
        res.end(JSON.stringify(STATE));
      } else {
        let html = htmlProvider();
        html = html.split("{{BOT_NAME}}").join(STATE.bot || "AgentCall")
                   .split("{{AVATAR_LINES}}").join(String(CONFIG.AVATAR_LINES || 8));
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
        res.end(html);
      }
    });
    srv.on("error", (e) => {
      console.log(`Web view unavailable (port ${port}: ${e.message}) - continuing without it.`);
      resolve({ srv: null, port: 0 });
    });
    srv.listen(port, "127.0.0.1", () => resolve({ srv, port: srv.address().port }));
  });
}

async function run(meetUrl, botName, display) {
  if (process.env.NOTETAKER_BRIDGE) display = "audio";

  // For an avatar, serve its page locally; the visual bridge tunnels it as the
  // bot's video tile. Fall back to audio if the page or a port is unavailable.
  let uiPort = 0;
  if (display !== "audio") {
    const provider = avatarProvider(display);
    if (!provider) {
      console.log(`Avatar '${display}' not found — add avatars/${display}.html or avatars/${display}.<image> (png/jpg/gif/svg/webp). Using audio for now.`);
      display = "audio";
    } else {
      const r = await serveHtml(provider, 0); uiPort = r.port;
      if (!uiPort) { console.log(`Couldn't start the avatar server for '${display}' — using audio for now.`); display = "audio"; }
    }
  }

  return new Promise((resolve) => {
    const [cmd, ...base] = bridgeCommand(display);
    const args = [...base, meetUrl, "--name", botName];
    if (CONFIG.ALONE_SECONDS > 0) args.push("--alone-timeout", String(CONFIG.ALONE_SECONDS));
    if (CONFIG.VAD_TIMEOUT > 0) args.push("--vad-timeout", String(CONFIG.VAD_TIMEOUT));   // lower = snappier
    if (uiPort) args.push("--ui-port", String(uiPort));
    // detached: the bridge runs in its own process group so a terminal Ctrl-C hits
    // only us — we keep it alive long enough to report the call id and leave
    // cleanly, so an aborted call is always DELETEd (never an orphan bot).
    const proc = spawn(cmd, args, { stdio: ["pipe", "pipe", "pipe"], detached: true, windowsHide: true });
    let errTail = [];   // last bridge stderr lines, shown only if it fails to join
    proc.stderr.setEncoding("utf-8");
    proc.stderr.on("data", (d) => {
      for (const ln of d.split("\n")) if (ln.trim()) errTail.push(ln.trim());
      if (errTail.length > 30) errTail = errTail.slice(-30);
    });

    const transcript = [];
    let notes = null, present = new Set();
    const seen = new Set();
    let seenHuman = false, callId = null, endReason = "unknown", joinedAt = null, done = false;
    const botLower = botName.toLowerCase();

    const setStatus = (s) => { STATE.bot = botName; STATE.status = s; STATE.present = present.size; };
    let leaveLogged = false;
    const sendLeave = () => {
      try {
        proc.stdin.write(JSON.stringify({ command: "leave" }) + "\n");
        if (!leaveLogged) { console.log("  Sent the leave command to the bot (asking it to leave the meeting)."); leaveLogged = true; }
      } catch {}
    };

    const record = (speaker, text, kind) => {
      const ts = localISO();
      const entry = { speaker, text, timestamp: ts, kind };
      transcript.push(entry);
      if (speaker.toLowerCase() !== botLower) { seen.add(speaker); seenHuman = true; }
      if (notes === null) notes = new LiveNotes();
      notes.add(entry);
      STATE.lines.push({ speaker, text, time: hhmmss(ts), kind });
      if (STATE.lines.length > 1100) STATE.lines = STATE.lines.slice(-1000);
      onLine(entry);
    };

    // Don't tear down until we know the call id (the bridge reports it ~1s in),
    // so the DELETE always lands — otherwise an aborted call leaves a bot joining.
    const waitForCallId = async () => {
      const deadline = Date.now() + 3000;
      while (callId === null && proc.exitCode === null && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 100));
      }
    };

    // Resolve once the bridge has actually exited (or the timeout elapses), so we
    // never process.exit() out from under a still-running detached child.
    const waitExit = (ms) => new Promise((res) => {
      if (proc.exitCode !== null || proc.signalCode !== null) return res();
      const t = setTimeout(res, ms);
      proc.once("exit", () => { clearTimeout(t); res(); });
    });

    const finish = async (reason) => {
      if (done) return;
      done = true;
      if (reason && endReason === "unknown") endReason = reason;
      sendLeave();
      if (callId === null && proc.exitCode === null) { console.log("Stopping the call..."); await waitForCallId(); }
      try { proc.stdin.end(); } catch {}
      await endCall(callId);
      try { proc.kill(); } catch {}                 // SIGTERM, then make sure it's gone
      await waitExit(5000);
      if (proc.exitCode === null && proc.signalCode === null) {
        try { proc.kill("SIGKILL"); } catch {}      // never leave the detached bridge running
        await waitExit(2000);
      }
      setStatus("ended");
      let duration = "unknown";
      if (joinedAt) { const m = Math.round((Date.now() - joinedAt) / 60000); duration = `${m} minute${m === 1 ? "" : "s"}`; }
      const meta = { call_id: callId, end_reason: endReason, participants: [...seen].sort(), duration };
      onMeetingEnd(transcript, meta);
      if (transcript.length) {
        if (notes === null) notes = new LiveNotes();
        console.log(`\nSaved ${transcript.length} lines to: ${notes.finalize(transcript, meta)}`);
      } else {
        console.log("\nNo transcript captured - nothing to save.");
        if (endReason === "interrupted") {
          console.log("(Stopped before the bot finished joining — nothing was captured.)");
        } else if (!joinedAt) {
          console.log("The bridge exited before joining. Its output:");
          const hot = errTail.filter((l) => ["error", "cannot find", "no module", "not found", "traceback", "exception"].some((k) => l.toLowerCase().includes(k)));
          const show = (hot.length ? hot : errTail).slice(0, 6);
          console.log(show.length ? "  " + show.join("\n  ") : "  (no output captured)");
          console.log("First run? Make sure dependencies are installed:  npm install");
        }
      }
      resolve();
    };

    process.on("SIGINT", () => {
      if (done) { try { proc.kill("SIGKILL"); } catch {} process.exit(1); }  // 2nd Ctrl-C = hard quit
      console.log("\nLeaving the meeting… (press Ctrl+C again to force-quit)");
      finish("interrupted");
    });

    console.log(`Sending '${botName}' in via the bridge... (~30-90s to appear)`);
    if (display !== "audio") console.log(`  showing the '${display}' avatar on the bot's video tile`);
    console.log("(press Ctrl+C, or leave the meeting, to make the bot leave)");

    const rl = readline.createInterface({ input: proc.stdout });
    rl.on("line", (raw) => {
      try {
        raw = raw.trim();
        if (!raw) return;
        let ev;
        try { ev = JSON.parse(raw); } catch { return; }
        const et = ev.event || ev.type || "";
        if (DEBUG) console.log(`  [debug] ${raw}`);

        if (et === "call.created") {
          callId = ev.call_id;
          console.log(`  Call created: ${callId}`);
        } else if (et === "call.bot_ready") {
          joinedAt = Date.now();
          setStatus("in meeting");
          console.log("In the meeting. Listening...\n");
        } else if (et === "participant.joined" || et === "meeting.participant_joined") {
          const name = ev.name || (ev.participant && ev.participant.name) || "";
          if (name && name.toLowerCase() !== botLower) { present.add(name); seen.add(name); seenHuman = true; }
          setStatus("in meeting");
          console.log(`  + ${name || "someone"} joined (${present.size} here)`);
        } else if (et === "participant.left" || et === "meeting.participant_left") {
          const name = ev.name || (ev.participant && ev.participant.name) || "";
          present.delete(name);
          setStatus("in meeting");
          console.log(`  - ${name || "someone"} left (${present.size} here)`);
          if (CONFIG.LEAVE_WHEN_EMPTY && seenHuman && present.size === 0) {
            console.log("\nEveryone left - leaving.");
            endReason = "all_participants_left";
            sendLeave();
          }
        } else if (et === "user.message") {
          const text = (ev.text || "").trim();
          if (text) { const speaker = ev.speaker || "Unknown"; record(speaker, text, "speech"); console.log(`  [${speaker}] ${text}`); }
        } else if (et === "chat.received" && CONFIG.CAPTURE_CHAT) {
          const message = (ev.message || "").trim();
          if (message) { const sender = ev.sender || "Unknown"; record(sender, message, "chat"); console.log(`  [chat] ${sender}: ${message}`); }
        } else if (et === "call.ended") {
          endReason = ev.reason || endReason;
          console.log(`\nCall ended: ${endReason}`);
          finish(endReason);   // tear down now (send leave + DELETE) — don't wait for the bridge to exit
        }
      } catch (e) {
        console.error("notetaker: unexpected error while handling an event -", e && e.message);
        finish("error");       // never let a thrown handler orphan the detached bridge
      }
    });

    proc.on("close", () => finish("disconnected"));
    proc.on("error", (e) => { console.error("Bridge error:", e.message); finish("error"); });
  });
}

function parseArgs(argv) {
  const a = { meetUrl: null, name: CONFIG.BOT_NAME, format: CONFIG.OUTPUT_FORMAT, out: CONFIG.OUTPUT_DIR, web: CONFIG.WEB, port: CONFIG.WEB_PORT, display: CONFIG.DISPLAY };
  const rest = argv.slice(2);
  for (let i = 0; i < rest.length; i++) {
    const x = rest[i];
    if (x === "--name") a.name = rest[++i];
    else if (x === "--format") a.format = rest[++i];
    else if (x === "--out") a.out = rest[++i];
    else if (x === "--web") a.web = true;
    else if (x === "--no-web") a.web = false;
    else if (x === "--port") a.port = parseInt(rest[++i], 10);
    else if (x === "--display") a.display = rest[++i];
    else if (!a.meetUrl) a.meetUrl = x;
  }
  return a;
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.name && !process.env.NOTETAKER_BRIDGE) {
    console.log("\nYour notetaker isn't built yet — let's build it:\n   npm run build\n");
    process.exit(0);
  }
  if (!args.meetUrl) {
    console.log('Usage: node notetaker.js "<meeting-link>"  [--name N] [--display audio|pattern|ring|transcript] [--format md|txt|json] [--out DIR] [--no-web] [--port P]');
    process.exit(0);
  }
  if (!["md", "txt", "json"].includes(args.format)) {
    console.log(`Invalid --format "${args.format}". Use md, txt, or json.`);
    process.exit(1);
  }
  if (!Number.isInteger(args.port) || args.port < 0 || args.port > 65535) {
    console.log(`Invalid --port "${args.port}". Use a number 0-65535.`);
    process.exit(1);
  }
  CONFIG.OUTPUT_FORMAT = args.format;
  CONFIG.OUTPUT_DIR = args.out;
  CONFIG.WEB = args.web;
  CONFIG.WEB_PORT = args.port;
  CONFIG.DISPLAY = args.display;
  STATE.bot = args.name;

  if (!API_KEY) {
    console.log("No AgentCall API key found.");
    console.log("  Get one (free) at https://app.agentcall.dev/api-keys, then either:");
    console.log('    export AGENTCALL_API_KEY="ak_ac_..."');
    console.log('    or save {"api_key": "ak_ac_..."} to ~/.agentcall/config.json');
    process.exit(1);
  }

  if (!(CONFIG.ALONE_SECONDS > 0)) {
    console.log("WARNING: ALONE_SECONDS is 0 in config.jsonc — the server-side auto-leave is OFF.");
    console.log("         If a shutdown is interrupted, the bot could keep billing. Set ALONE_SECONDS > 0.");
  }

  if (CONFIG.WEB) {
    const { srv } = await serveHtml(() => readHtml(path.join(PROJECT_ROOT, "avatars", "transcript.html")), CONFIG.WEB_PORT);
    if (srv) console.log(`Live transcript: http://localhost:${CONFIG.WEB_PORT}\n`);
  }

  await run(args.meetUrl, args.name, CONFIG.DISPLAY);
  process.exit(0);
}

main().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
