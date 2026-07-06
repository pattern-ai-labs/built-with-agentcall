"use strict";
/*
 * Connect a calendar: paste your secret iCal link, we check it, and save it.
 * Node mirror of python/connect_calendar.py. Shared by `npm run build` and
 * `node autojoin.js connect`. The link is a credential -> .env (gitignored) as
 * CALENDAR_ICS_URL, never config.jsonc. Connecting flips CALENDAR.AUTO_JOIN on.
 */
const fs = require("fs");
const path = require("path");
const readline = require("readline");
const { spawnSync } = require("child_process");
const cs = require("./calendar-source.js");

const HERE = __dirname;
const ROOT = path.dirname(HERE);

const WALKTHROUGH = `\
Connect a calendar and the notetaker joins your meetings on its own. You'll paste
a private "secret iCal" link — it's read-only and just for this app.

  Google Calendar
    1. Open calendar.google.com on a computer.
    2. Hover the calendar under "My calendars" (left) -> ⋮ -> "Settings and sharing".
    3. Scroll to "Integrate calendar".
    4. Copy the "Secret address in iCal format" (it ends in .ics).

  Outlook / Microsoft 365
    Settings -> Calendar -> Shared calendars -> Publish a calendar -> publish, then
    copy the ICS link.

  Apple iCloud
    Share the calendar as a Public Calendar and copy the webcal:// link.

This app reads the link on your own computer — your calendar is never sent to us or
anywhere else. Keep the link private, though: anyone who has it can read your calendar.`;

function clean(url) {
  return (url || "").replace(/[​-‏‪-‮﻿]/g, "").trim().replace(/^["']|["']$/g, "").trim();
}

async function validate(url) {
  url = clean(url);
  if (!url) return { events: null, error: "no link given." };
  let text;
  try {
    text = await new cs.ICSCalendarSource(url)._fetch();
  } catch (e) {
    if (e.code === "ENOENT") return { events: null, error: "couldn't find that file." };
    return { events: null, error: `couldn't fetch that link (${e.message}).` };
  }
  if (!text.toUpperCase().includes("BEGIN:VCALENDAR")) {
    return { events: null, error: "that link didn't return a calendar feed. Make sure you copied the " +
             "*secret iCal* address (it usually ends in .ics), not the calendar's web page." };
  }
  const now = new Date();
  const events = cs.parseIcs(text, new Date(now - 3600000), new Date(now.getTime() + 14 * 86400000));
  return { events, error: null };
}

function saveIcsUrl(url) {
  url = clean(url);
  const p = path.join(ROOT, ".env");
  let keep = [];
  try {
    keep = fs.readFileSync(p, "utf-8").split("\n").map((l) => l.replace(/\r$/, ""))
      .filter((l) => l.trim() && !l.trim().startsWith("CALENDAR_ICS_URL="));
  } catch { keep = []; }
  fs.writeFileSync(p, keep.concat([`CALENDAR_ICS_URL=${url}`, ""]).join("\n"));
  if (process.platform !== "win32") { try { fs.chmodSync(p, 0o600); } catch {} }
}

function setAutoJoin(on = true) {
  const p = path.join(ROOT, "config.jsonc");
  let text;
  try { text = fs.readFileSync(p, "utf-8"); } catch { return; }
  fs.writeFileSync(p, text.replace(/("AUTO_JOIN"\s*:\s*)(?:true|false)/, `$1${on ? "true" : "false"}`));
}

function summarize(events, out = console.log, limit = 5) {
  const joinable = events.filter((e) => e.url && !e.cancelled && !e.allDay);
  out(`  Connected. ${events.length} event(s) in the next 14 days, ${joinable.length} with a meeting link the notetaker can join.`);
  for (const e of joinable.slice(0, limit)) {
    out(`    · ${e.start.toLocaleString(undefined, { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}  ${e.title}`);
  }
  if (!joinable.length) out("    (nothing joinable yet — new meetings will be picked up automatically.)");
}

function question(q) {
  return new Promise((res) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question(q, (a) => { rl.close(); res(a); });
  });
}

async function interactive() {
  console.log("\n" + WALKTHROUGH + "\n");
  let url = "", events = null;
  while (true) {
    try { url = (await question("  Paste your secret iCal link (Enter to cancel): ")).trim(); }
    catch { console.log("\n  Cancelled."); return 1; }
    if (!url) { console.log("  Cancelled — no calendar connected."); return 1; }
    console.log("  Checking that link…");
    const r = await validate(url);
    if (r.error) { console.log(`  ✗ ${r.error}`); console.log("  Let's try again (or press Enter to cancel)."); continue; }
    events = r.events;
    break;
  }
  saveIcsUrl(url);
  setAutoJoin(true);
  console.log();
  summarize(events);

  let ans = "n";
  try { ans = (await question("\n  Start auto-join automatically when you log in? (Y/n): ")).trim().toLowerCase(); } catch {}
  if (ans === "" || ans === "y" || ans === "yes") {
    try { require("./autostart.js").enable(); }
    catch (e) { console.log(`  (couldn't set up start-on-login: ${e.message})`); console.log("  Start it yourself any time with:  node autojoin.js start"); }
  } else {
    console.log("  Starting it for now (won't survive a reboot — run `node autojoin.js enable` for that):");
    spawnSync(process.execPath, [path.join(HERE, "autojoin.js"), "start"], { stdio: "inherit" });
  }
  console.log("\n  Done — auto-join is connected. Check it any time with: node autojoin.js status");
  return 0;
}

module.exports = { WALKTHROUGH, validate, saveIcsUrl, setAutoJoin, summarize, clean, interactive };

if (require.main === module) {
  interactive().then((c) => process.exit(c || 0)).catch((e) => { console.error(e.message); process.exit(1); });
}
