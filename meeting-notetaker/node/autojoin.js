#!/usr/bin/env node
"use strict";
/*
 * Auto-join: watch your calendar and send the notetaker into meetings by itself.
 * Node mirror of python/autojoin.py — same commands, same behavior.
 *
 *   node autojoin.js start    # turn it ON  — run now AND start at every login
 *   node autojoin.js stop     # turn it OFF — stop now AND stop starting at login
 *   node autojoin.js status | restart | logs | connect
 *   node autojoin.js run      # run once in the foreground, without touching start-at-login
 *   node autojoin.js poll     # check once, now, and print what it sees
 *
 * It wraps notetaker.js; it doesn't change how the notetaker joins. Settings live
 * in config.jsonc under CALENDAR; the secret link is CALENDAR_ICS_URL in .env.
 * Powered by AgentCall — https://agentcall.dev
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn, execFileSync } = require("child_process");
const cs = require("./calendar-source.js");

const HERE = __dirname;
const ROOT = path.dirname(HERE);
const RUNTIME = path.join(ROOT, ".notetaker");
const PID_FILE = path.join(RUNTIME, "autojoin.pid");
const LOG_FILE = path.join(RUNTIME, "autojoin.log");
const BOOT_LOG = path.join(RUNTIME, "autojoin.boot.log");
const STATE_FILE = path.join(RUNTIME, "joined.json");
const STATUS_FILE = path.join(RUNTIME, "status.json");
const MEETING_LOGS = path.join(RUNTIME, "meetings");
const CHILDREN_FILE = path.join(RUNTIME, "children.json");   // live notetaker pids, for `stop --all`

// ── config + env (same tiny loaders notetaker.js uses) ──────────────────────
function loadConfig() {
  try {
    let text = fs.readFileSync(path.join(ROOT, "config.jsonc"), "utf-8");
    text = text.replace(/("(?:\\.|[^"\\])*")|\/\/[^\n]*|\/\*[\s\S]*?\*\//g, (m, s) => (s ? s : ""));
    return JSON.parse(text);
  } catch (e) {
    console.error(`Couldn't read config.jsonc (${e.message}). Check it for typos.`);
    process.exit(1);
  }
}
function loadDotenv() {
  for (const d of [ROOT, HERE]) {
    try {
      for (let line of fs.readFileSync(path.join(d, ".env"), "utf-8").split("\n")) {
        line = line.trim();
        if (line && !line.startsWith("#") && line.includes("=")) {
          const i = line.indexOf("=");
          const k = line.slice(0, i).trim();
          if (!(k in process.env)) process.env[k] = line.slice(i + 1).trim().replace(/^["']|["']$/g, "");
        }
      }
      return;
    } catch { /* no .env here */ }
  }
}
const CONFIG = loadConfig();
loadDotenv();
const calCfg = () => CONFIG.CALENDAR || {};
function cfgInt(key, dflt) { const v = parseInt(calCfg()[key], 10); return Number.isFinite(v) ? v : dflt; }
function loadApiKey() {
  if (process.env.AGENTCALL_API_KEY) return process.env.AGENTCALL_API_KEY;
  try { return JSON.parse(fs.readFileSync(path.join(os.homedir(), ".agentcall", "config.json"), "utf-8")).api_key || ""; }
  catch { return ""; }
}
const apiKeyPresent = () => !!loadApiKey();
const API_BASE = process.env.AGENTCALL_API_URL || "https://api.agentcall.dev";

async function endCall(callId) {
  // Stop a call's billing directly (same DELETE the notetaker does on a clean
  // exit). Used by `stop --all` after force-ending a notetaker. 404/409 = already
  // over — success either way.
  const key = loadApiKey();
  if (!callId || !key) return;
  let lastErr = "";
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const resp = await fetch(`${API_BASE}/v1/calls/${callId}`, {
        method: "DELETE", headers: { Authorization: `Bearer ${key}` },
      });
      if (resp.ok || resp.status === 404 || resp.status === 409) {
        console.log(`     call ${callId} ${resp.ok ? "ended" : "already ended"} — billing stopped.`);
        return;
      }
      lastErr = `HTTP ${resp.status}`;
    } catch (e) { lastErr = e.message; }
    if (attempt === 0) await sleep(500);
  }
  console.log(`     (couldn't confirm call ${callId} stopped — ${lastErr}. The server-side ` +
              "alone-timeout reclaims it once the meeting empties.)");
}

// ── runtime dir, logging, state ─────────────────────────────────────────────
const ensureRuntime = () => fs.mkdirSync(RUNTIME, { recursive: true });
function rotateIfBig(p, max = 2_000_000) {
  try { if (fs.statSync(p).size > max) fs.renameSync(p, p + ".1"); } catch { /* fine */ }
}
function stamp() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function logLine(msg) {
  ensureRuntime(); rotateIfBig(LOG_FILE);
  const line = `${stamp()}  ${msg}\n`;
  try { fs.appendFileSync(LOG_FILE, line); } catch { /* ignore */ }
  if (process.stdout.isTTY) process.stdout.write(line);
}
cs.setLogger({ warn: (...a) => logLine(a.join(" ")), info: () => {} });

function writeJson(p, data) { const tmp = p + ".tmp"; fs.writeFileSync(tmp, JSON.stringify(data, null, 2)); fs.renameSync(tmp, p); }
function loadJoined() {
  let data;
  try { data = JSON.parse(fs.readFileSync(STATE_FILE, "utf-8")); } catch { return {}; }
  const cutoff = Date.now() - 86400000, out = {};
  for (const [k, v] of Object.entries(data)) { const t = Date.parse(v); if (t && t > cutoff) out[k] = v; }
  return out;
}
function saveJoined(j) { ensureRuntime(); writeJson(STATE_FILE, j); }

function loadChildren() {
  // Notetaker processes we launched that are still alive (dead ones pruned).
  let kids;
  try { kids = JSON.parse(fs.readFileSync(CHILDREN_FILE, "utf-8")); } catch { return []; }
  return kids.filter((k) => pidAlive(k.pid));
}
function saveChildren(kids) { ensureRuntime(); writeJson(CHILDREN_FILE, kids); }

// ── launching the notetaker ─────────────────────────────────────────────────
function notetakerCmd(url) {
  const override = process.env.AUTOJOIN_NOTETAKER_CMD;
  if (override) return override.trim().split(/\s+/).concat([url]);
  return [process.execPath, path.join(HERE, "notetaker.js"), url];
}
function launchNotetaker(ev) {
  fs.mkdirSync(MEETING_LOGS, { recursive: true });
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  const st = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  const safe = (ev.title || "meeting").replace(/[^A-Za-z0-9]+/g, "-").slice(0, 40).replace(/^-|-$/g, "") || "meeting";
  const logpath = path.join(MEETING_LOGS, `${st}-${safe}.log`);
  const fd = fs.openSync(logpath, "w");
  fs.writeSync(fd, `# ${ev.title}\n# ${ev.url}\n# launched ${new Date().toISOString()}\n\n`);
  const cmd = notetakerCmd(ev.url);
  const opts = { cwd: HERE, stdio: ["ignore", fd, fd], detached: true, windowsHide: true };
  const child = spawn(cmd[0], cmd.slice(1), opts);
  child.unref();
  const kids = loadChildren();
  kids.push({ pid: child.pid, title: ev.title, log: logpath, started: new Date().toISOString() });
  saveChildren(kids);
  return { pid: child.pid, logpath };
}
function skipReason(ev) {
  const c = calCfg();
  if (!ev.url) return "no meeting link";
  if (ev.cancelled) return "cancelled";
  if (ev.allDay && c.SKIP_ALL_DAY !== false) return "all-day event";
  if (ev.declined && c.SKIP_DECLINED !== false) return "you declined it";
  return null;
}

// ── one polling pass ────────────────────────────────────────────────────────
async function pollOnce(cal, joined, now) {
  now = now || new Date();
  const lead = cfgInt("JOIN_LEAD_SECONDS", 120), grace = cfgInt("JOIN_GRACE_SECONDS", 300);
  const joinFrom = new Date(now.getTime() - grace * 1000);
  const joinTo = new Date(now.getTime() + lead * 1000);
  const lookEnd = new Date(now.getTime() + Math.max(lead, 3600) * 1000);

  const events = await cal.events(new Date(joinFrom.getTime() - 60000), lookEnd);
  let joinedCount = 0, nextUp = null;
  for (const ev of events) {
    if (!ev.start) continue;
    if (ev.start >= joinFrom && ev.start <= joinTo) {
      const reason = skipReason(ev);
      if (reason) { logLine(`skip  ${(ev.title || "?").slice(0, 30).padEnd(30)} (${reason})`); continue; }
      if (joined[ev.key()]) continue;
      let pid;
      try {
        ({ pid } = launchNotetaker(ev));
      } catch (e) {
        // One meeting failing to launch must never affect the others. Log it, leave
        // it un-joined so the next poll can retry, and move on.
        logLine(`Couldn't launch the notetaker for "${(ev.title || "?").slice(0, 40)}" (${e.message}) ` +
                `— skipping just this one; your other meetings are unaffected.`);
        continue;
      }
      joined[ev.key()] = now.toISOString();
      saveJoined(joined);
      joinedCount += 1;
      logLine(`JOIN  ${(ev.title || "?").slice(0, 30).padEnd(30)}  ${ev.url}  (pid ${pid})`);
    } else if (ev.start > joinTo && skipReason(ev) === null && nextUp === null) {
      nextUp = ev;
    }
  }
  writeStatus(now, cal, nextUp, Object.keys(joined).length);
  return joinedCount;
}
function localWhen(date) {
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function writeStatus(now, cal, nextUp, joinedTotal) {
  ensureRuntime();
  writeJson(STATUS_FILE, {
    pid: process.pid,
    last_poll: now.toISOString(),
    source: cal.describe(),
    poll_seconds: cfgInt("POLL_SECONDS", 60),
    joined_remembered: joinedTotal,
    next_meeting: nextUp ? { title: nextUp.title, start: localWhen(nextUp.start), url: nextUp.url } : null,
  });
}

// ── foreground daemon (`run`) ───────────────────────────────────────────────
function makeSourceOrExplain() {
  try { return cs.makeSource(calCfg(), process.env); }
  catch (e) { logLine(`Can't start auto-join: ${e.message}`); return null; }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cmdRun() {
  if (!apiKeyPresent()) {
    logLine("No AgentCall API key found. Set AGENTCALL_API_KEY (or ~/.agentcall/config.json), then start again.");
    return 1;
  }
  const cal = makeSourceOrExplain();
  if (!cal) return 1;
  const interval = cfgInt("POLL_SECONDS", 60), lead = cfgInt("JOIN_LEAD_SECONDS", 120), grace = cfgInt("JOIN_GRACE_SECONDS", 300);
  const joined = loadJoined();
  ensureRuntime();
  fs.writeFileSync(PID_FILE, String(process.pid));

  const stop = { v: false };
  process.on("SIGINT", () => { stop.v = true; });
  process.on("SIGTERM", () => { stop.v = true; });

  logLine(`Auto-join started — watching your ${cal.describe()} every ${interval}s (join ${lead}s before, up to ${grace}s late). Ctrl-C to stop.`);
  if (!calCfg().AUTO_JOIN) logLine("(config.jsonc CALENDAR.AUTO_JOIN is false — you started it by hand, so it's running anyway. Set it true to have `enable` start it on boot.)");

  try {
    while (!stop.v) {
      try { await pollOnce(cal, joined); }
      catch (e) { logLine(`This poll failed (${e.message}) — will try again next cycle.`); }
      let slept = 0;
      while (slept < interval && !stop.v) { await sleep(500); slept += 0.5; }
    }
  } finally {
    for (const f of [PID_FILE, STATUS_FILE]) { try { fs.unlinkSync(f); } catch { /* gone */ } }
  }
  logLine("Auto-join stopped.");
  return 0;
}

async function cmdPoll() {
  const cal = makeSourceOrExplain();
  if (!cal) return 1;
  if (!apiKeyPresent()) logLine("Heads up: no AgentCall API key set yet — joining will fail until you set one (https://app.agentcall.dev/api-keys).");
  const joined = loadJoined();
  const n = await pollOnce(cal, joined);
  try {
    const st = JSON.parse(fs.readFileSync(STATUS_FILE, "utf-8"));
    if (st.next_meeting) { console.log(`\nNext up: ${st.next_meeting.title} at ${st.next_meeting.start}`); console.log(`         ${st.next_meeting.url}`); }
    else console.log("\nNothing else on the calendar within the next hour.");
  } catch { /* no status */ }
  console.log(`Joined this pass: ${n}.`);
  return 0;
}

// ── process control ─────────────────────────────────────────────────────────
function readPid() { try { return parseInt(fs.readFileSync(PID_FILE, "utf-8").trim(), 10) || null; } catch { return null; } }
function pidAlive(pid) {
  if (!pid) return false;
  if (process.platform === "win32") {
    try { return execFileSync("tasklist", ["/FI", `PID eq ${pid}`, "/NH"], { encoding: "utf-8" }).includes(String(pid)); }
    catch { return false; }
  }
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; }
}
function terminate(pid) {
  if (process.platform === "win32") {
    try { execFileSync("taskkill", ["/F", "/PID", String(pid)], { stdio: "ignore" }); } catch { /* already gone */ }
  } else {
    try { process.kill(pid, "SIGTERM"); } catch { return; }
  }
}

function spawnDaemon() {
  ensureRuntime();
  const fd = fs.openSync(BOOT_LOG, "w");
  const child = spawn(process.execPath, [path.join(HERE, "autojoin.js"), "run"],
    { cwd: HERE, stdio: ["ignore", fd, fd], detached: true, windowsHide: true });
  child.unref();
}

async function cmdStart() {
  // Turn auto-join ON: run it now AND have it start when you log in.
  const autostart = require("./autostart.js");
  if (!apiKeyPresent()) { console.log("No AgentCall API key found. Set AGENTCALL_API_KEY (or add it via npm run build) first."); return 1; }
  try { cs.makeSource(calCfg(), process.env); } catch (e) { console.log(`Can't turn on auto-join: ${e.message}`); return 1; }

  const already = pidAlive(readPid());
  // On Windows we start the process ourselves; on mac/linux autostart.on() hands it
  // to launchd/systemd, which starts it now and registers boot in one go.
  if (autostart.MANUAL_PROCESS && !already) spawnDaemon();
  autostart.on();

  for (let i = 0; i < 20; i++) {
    await sleep(100);
    const pid = readPid();
    if (pidAlive(pid)) {
      console.log(`● Auto-join is ON (${already ? "already running" : "started"}, pid ${pid}).`);
      console.log("  It's watching now, and it'll start automatically when you log in.");
      console.log("  node autojoin.js status     see what's next");
      console.log("  node autojoin.js stop       turn it off (now and at login)");
      return 0;
    }
  }
  console.log("Registered start-on-login, but couldn't confirm it's running — check `node autojoin.js logs`.");
  return 1;
}
function callIdFromLog(logpath) {
  // The notetaker prints 'Call created: <id>' — its per-meeting log has it.
  try {
    const m = fs.readFileSync(logpath, "utf-8").match(/Call created:\s*(\S+)/);
    return m ? m[1] : null;
  } catch { return null; }
}

async function stopChildren() {
  // Make every notetaker WE launched leave its meeting, and confirm billing
  // stopped. POSIX: SIGINT lets the notetaker do its own clean leave+DELETE.
  // Windows: no way to deliver Ctrl-C to a detached process, so end the process
  // tree and DELETE its call ourselves (404/409 = already ended, fine).
  const kids = loadChildren();
  if (!kids.length) { console.log("No meetings in progress."); return; }
  for (const k of kids) {
    console.log(`  leaving '${k.title || "meeting"}' (pid ${k.pid})…`);
    if (process.platform === "win32") {
      try { execFileSync("taskkill", ["/F", "/T", "/PID", String(k.pid)], { stdio: "ignore" }); } catch { /* already gone */ }
    } else {
      try { process.kill(k.pid, "SIGINT"); } catch { /* already gone */ }
      for (let i = 0; i < 100 && pidAlive(k.pid); i++) await sleep(100);  // up to 10s to exit cleanly
      if (pidAlive(k.pid)) { try { process.kill(k.pid, "SIGKILL"); } catch { /* gone */ } }
    }
    // Belt and braces on every platform: DELETE the call by id from the meeting
    // log. If the notetaker already ended it, the API answers 404/409 — harmless.
    const cid = callIdFromLog(k.log || "");
    if (cid) await endCall(cid);
    else console.log("     (no call id in its log — it hadn't finished joining, or the " +
                     "notetaker already cleaned up. The server-side auto-leave covers it.)");
  }
  saveChildren([]);
}

async function cmdStop(opts) {
  // Turn auto-join OFF: stop it now AND stop it starting when you log in.
  const autostart = require("./autostart.js");
  const stopAll = !!(opts && opts.all);
  const pid = readPid();
  if (autostart.MANUAL_PROCESS && pidAlive(pid)) terminate(pid);   // Windows: we own the process
  autostart.off();                                                 // remove boot (mac/linux: also stops it)
  for (const f of [PID_FILE, STATUS_FILE]) { try { fs.unlinkSync(f); } catch {} }
  console.log("○ Auto-join is OFF — stopped, and it won't start when you log in.");
  if (stopAll) await stopChildren();
  else console.log("  (meetings already in progress keep running until they empty — " +
                   "`stop --all` makes their bots leave too.)");
  return 0;
}
async function cmdRestart() {
  // Bounce the watcher; it stays ON.
  const autostart = require("./autostart.js");
  const pid = readPid();
  if (autostart.MANUAL_PROCESS && pidAlive(pid)) {
    terminate(pid);
    for (const f of [PID_FILE, STATUS_FILE]) { try { fs.unlinkSync(f); } catch {} }
  }
  await sleep(500);
  return cmdStart();
}

function fmtAgo(iso) {
  const t = Date.parse(iso);
  if (!t) return iso;
  const s = (Date.now() - t) / 1000;
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}
function calendarConnected() {
  try {
    for (const l of fs.readFileSync(path.join(ROOT, ".env"), "utf-8").split("\n"))
      if (l.startsWith("CALENDAR_ICS_URL=") && l.split("=", 2)[1].trim()) return true;
  } catch { /* fall through */ }
  return !!process.env.CALENDAR_ICS_URL;
}
function cmdStatus() {
  const pid = readPid(), alive = pidAlive(pid);
  let boot = false;
  try { boot = require("./autostart.js").isOn(); } catch {}
  let st = {};
  try { st = JSON.parse(fs.readFileSync(STATUS_FILE, "utf-8")); } catch {}
  if (alive && boot) console.log(`● auto-join is ON (running, pid ${pid}) — and starts when you log in`);
  else if (alive && !boot) { console.log(`● auto-join is running (pid ${pid}), but is NOT set to start at login`); console.log("    make it start at login too:  node autojoin.js start"); }
  else if (boot && !alive) { console.log("◐ auto-join is set to start at login, but isn't running right now"); console.log("    turn it on now:  node autojoin.js start"); }
  else { console.log("○ auto-join is OFF"); console.log("    turn it on:  node autojoin.js start"); }
  console.log(`    calendar: ${calendarConnected() ? "connected (" + (st.source || "iCal feed") + ")" : "NOT connected — run node autojoin.js connect"}`);
  console.log(`    auto-join in config: ${calCfg().AUTO_JOIN ? "on" : "off"}`);
  if (st.last_poll) console.log(`    last checked: ${fmtAgo(st.last_poll)}`);
  if (st.next_meeting) { console.log(`    next meeting: ${st.next_meeting.title}  at ${st.next_meeting.start}`); console.log(`                  ${st.next_meeting.url}`); }
  else if (alive) console.log("    next meeting: nothing within the next hour");
  return 0;
}
function cmdLogs(args) {
  const n = (args && args.lines) || 40;
  let text;
  try { text = fs.readFileSync(LOG_FILE, "utf-8"); }
  catch {
    console.log("No log yet — auto-join hasn't run. Start it with: node autojoin.js start");
    try { const boot = fs.readFileSync(BOOT_LOG, "utf-8"); if (boot.trim()) { console.log(`\nStartup output (${BOOT_LOG}):`); process.stdout.write(boot); } } catch {}
    return 0;
  }
  const lines = text.split("\n");
  process.stdout.write(lines.slice(Math.max(0, lines.length - n - 1)).join("\n"));
  console.log(`\n(${LOG_FILE})`);
  return 0;
}

// ── connect (its own module) ────────────────────────────────────────────────
async function cmdConnect(opts) {
  const cc = require("./connect-calendar.js");
  if (opts && opts.fromEnv) {
    // Agent/CI-safe path: the link was pasted into .env by the user themself
    // (CALENDAR_ICS_URL=...), so it never has to appear in a chat or a command line.
    const url = (process.env.CALENDAR_ICS_URL || "").trim();
    if (!url) {
      console.log("CALENDAR_ICS_URL isn't set. Add this line to the .env file next to " +
                  "config.jsonc, then run this again:");
      console.log("    CALENDAR_ICS_URL=<your secret iCal link>");
      return 1;
    }
    const r = await cc.validate(url);
    if (r.error) { console.log(`✗ ${r.error}`); return 1; }
    cc.setAutoJoin(true);
    cc.summarize(r.events);
    console.log("\nCalendar connected. Turn auto-join on with:  node autojoin.js start");
    console.log("  (that runs it now and starts it whenever you log in; `stop` turns it off.)");
    return 0;
  }
  return cc.interactive();
}

// ── CLI ─────────────────────────────────────────────────────────────────────
const HELP = `autojoin — watch your calendar and auto-join meetings with the notetaker.

  node autojoin.js start          turn it ON  — run now AND start whenever you log in
  node autojoin.js stop           turn it OFF — stop now AND stop starting at login
  node autojoin.js stop --all     …and also make bots leave meetings in progress
  node autojoin.js status         is it on, and what's next?
  node autojoin.js restart        bounce the watcher (stays on)
  node autojoin.js logs [-n N]
  node autojoin.js connect        connect (or re-connect) a calendar
  node autojoin.js connect --from-env   use the CALENDAR_ICS_URL already saved in .env
  node autojoin.js run            run once in the foreground, WITHOUT touching start-at-login
  node autojoin.js poll           check the calendar once, now`;

async function main(argv) {
  const cmd = argv[0];
  let lines = 40;
  const li = argv.indexOf("-n") >= 0 ? argv.indexOf("-n") : argv.indexOf("--lines");
  if (li >= 0 && argv[li + 1]) lines = parseInt(argv[li + 1], 10) || 40;
  switch (cmd) {
    case "run": return cmdRun();
    case "poll": return cmdPoll();
    case "start": return cmdStart();
    case "stop": return cmdStop({ all: argv.includes("--all") });
    case "restart": return cmdRestart();
    case "status": return cmdStatus();
    case "logs": return cmdLogs({ lines });
    case "connect": return cmdConnect({ fromEnv: argv.includes("--from-env") });
    default: console.log(HELP); return 0;
  }
}

if (require.main === module) {
  main(process.argv.slice(2)).then((c) => process.exit(c || 0)).catch((e) => { console.error(e.stack || e.message); process.exit(1); });
}
module.exports = { pollOnce, parseArgsForTest: null };
