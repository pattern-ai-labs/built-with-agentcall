#!/usr/bin/env node
/**
 * Build your notetaker — the one-time setup that assembles YOUR config.
 *
 * In a terminal (cmd, PowerShell, VS Code, Cursor, Replit, bash...), just run it and answer:
 *     npm run build           (or:  node build.js)
 *
 * No terminal (an AI agent, CI, a script)? Pass the answers as flags instead — key first:
 *     node build.js --key ak_ac_... --name Juno --display ring --format md
 *     node build.js --key ak_ac_... --image ./logo.png        # your own avatar
 *
 * Either way it writes .env (your key, gitignored) + config.jsonc into the project folder.
 * After this one-time build you can just edit config.jsonc directly. Powered by AgentCall.
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const PROJECT_ROOT = path.dirname(__dirname);
const AVATARS = path.join(PROJECT_ROOT, "avatars");
const IMG_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"];

// ── terminal styling (ANSI on a TTY; plain text otherwise) ─────────────────────
const ANSI = !!process.stdout.isTTY;
const BOLD = "1", DIM = "2";
const col = (code, s) => (ANSI ? `\x1b[${code}m${s}\x1b[0m` : s);

// AgentCall brand (truecolor). Unsupported terminals ignore these and fall back
// to default text — readable everywhere, never garbled.
const E = "\x1b[", R = E + "0m";
const INK = E + "38;2;28;29;26m";       // ink text on the cream card
const MUTE = E + "38;2;120;118;108m";   // muted text on the cream card
const CREAM = E + "48;2;243;240;232m";  // #F3F0E8 paper — the card surface
const LIMEBG = E + "48;2;200;255;58m";  // #C8FF3A lime — the badge surface
const ONLIME = E + "38;2;12;13;10m";    // near-black text on lime
const CARD_W = 46;

function emitCard(rows) {
  // Brand colors live only in these non-interactive cards (no input echo to fight);
  // plain text is the graceful fallback.
  if (!ANSI) {
    console.log();
    for (const [, plain] of rows) if (plain) console.log("  " + plain);
    console.log();
    return;
  }
  const blank = "  " + CREAM + " ".repeat(CARD_W) + R;
  console.log();
  console.log(blank);
  for (const [styled, plain] of rows) {
    const pad = " ".repeat(Math.max(0, CARD_W - 2 - plain.length));
    console.log("  " + CREAM + INK + "  " + styled + pad + R);
  }
  console.log(blank);
  console.log();
}

function pill(text) {
  // lime badge: lime background, near-black bold text, then back to the cream card
  return [LIMEBG + ONLIME + E + "1m" + " " + text + " " + R + CREAM + INK, " " + text + " "];
}

function banner() {
  emitCard([
    pill("ONE-TIME SETUP"),
    ["", ""],
    [E + "1m" + "▣  N O T E T A K E R" + R + CREAM + INK, "▣  N O T E T A K E R"],
    [MUTE + "build it once · powered by agentcall.dev" + R + CREAM, "build it once · powered by agentcall.dev"],
  ]);
}

function doneCard(name) {
  emitCard([
    pill("BUILT"),
    ["", ""],
    [E + "1m" + "✓  " + name + " is ready" + R + CREAM + INK, "✓  " + name + " is ready"],
    [MUTE + "config.jsonc + .env are written" + R + CREAM, "config.jsonc + .env are written"],
  ]);
}

function parseFlags(argv) {
  const a = {};
  const rest = argv.slice(2);
  for (let i = 0; i < rest.length; i++) {
    const x = rest[i];
    if (x === "--key") a.key = rest[++i];
    else if (x === "--name") a.name = rest[++i];
    else if (x === "--display") a.display = rest[++i];
    else if (x === "--format") a.format = rest[++i];
    else if (x === "--image") a.image = rest[++i];
  }
  return a;
}

function slug(name) {
  return name.toLowerCase().replace(/[^a-z0-9]/g, "") || "brand";
}

function copyImage(imgPath, name) {
  // Strip invisible bidi/zero-width marks that Windows' "Copy as path" prepends,
  // plus surrounding quotes/space, so a pasted path actually resolves.
  let p = (imgPath || "").replace(/[\u200b-\u200f\u202a-\u202e\ufeff]/g, "").trim().replace(/^["']|["']$/g, "").trim();
  if (p.startsWith("~")) p = path.join(os.homedir(), p.slice(1));
  const ext = path.extname(p).toLowerCase();
  if (!p) { console.log("   (no image given — using the Pattern mark)"); return "pattern"; }
  if (!fs.existsSync(p)) { console.log("   (couldn't find that file — using the Pattern mark)\n     " + p); return "pattern"; }
  if (!IMG_EXTS.includes(ext)) {
    console.log(`   (that's a '${ext || "?"}' file — use an image: png, jpg, jpeg, gif, webp, or svg. Using the Pattern mark.)`);
    return "pattern";
  }
  const dest = slug(name) + ext;
  try {
    fs.copyFileSync(p, path.join(AVATARS, dest));
    console.log("   " + col(BOLD, "✓") + ` copied your image to avatars/${dest}`);
    return slug(name);
  } catch (e) {
    console.log(`   (couldn't copy: ${e.message} — using the Pattern mark)`);
    return "pattern";
  }
}

function setValue(text, key, value) {
  // JSON.stringify escapes quotes/backslashes so any name stays valid JSON; the value
  // pattern allows backslash-escapes so a re-run matches a previously-escaped value.
  return text.replace(new RegExp(`("${key}"\\s*:\\s*)"(?:[^"\\\\]|\\\\.)*"`),
                      (m, p1) => p1 + JSON.stringify(String(value)));
}

function writeConfig(name, display, fmt) {
  const p = path.join(PROJECT_ROOT, "config.jsonc");
  let text = fs.readFileSync(p, "utf-8");
  text = setValue(text, "BOT_NAME", name);
  text = setValue(text, "DISPLAY", display);
  text = setValue(text, "OUTPUT_FORMAT", fmt);
  fs.writeFileSync(p, text);
}

function existingKey() {
  // A key the notetaker could already use — env var, an existing .env, or
  // ~/.agentcall/config.json. Lets the build skip re-asking when one's set.
  const env = (process.env.AGENTCALL_API_KEY || "").trim();
  if (env) return env;
  try {
    for (let line of fs.readFileSync(path.join(PROJECT_ROOT, ".env"), "utf-8").split("\n")) {
      line = line.trim();
      if (line.startsWith("AGENTCALL_API_KEY=")) {
        const v = line.slice(line.indexOf("=") + 1).trim().replace(/^["']|["']$/g, "");
        if (v) return v;
      }
    }
  } catch { /* no .env */ }
  try {
    const v = JSON.parse(fs.readFileSync(path.join(os.homedir(), ".agentcall", "config.json"), "utf-8")).api_key || "";
    if (v) return v;
  } catch { /* no config.json */ }
  return "";
}

function writeEnv(key) {
  // Write/refresh the gitignored .env: set AGENTCALL_API_KEY, keep any other lines.
  const p = path.join(PROJECT_ROOT, ".env");
  let keep = [];
  try {
    keep = fs.readFileSync(p, "utf-8").split("\n").map((l) => l.replace(/\r$/, ""))
      .filter((l) => l.trim() && !l.trim().startsWith("AGENTCALL_API_KEY="));
  } catch { keep = []; }
  fs.writeFileSync(p, [`AGENTCALL_API_KEY=${key}`, ...keep].join("\n") + "\n");
  if (process.platform !== "win32") {           // keep the secret owner-only on POSIX
    try { fs.chmodSync(p, 0o600); } catch { /* best effort */ }
  }
}

function nextSteps() {
  const b = (s) => col(BOLD, s), d = (s) => col(DIM, s);
  console.log("  " + b("Run it"));
  console.log("    node notetaker.js " + d('"https://meet.google.com/your-link"'));
  console.log();
  console.log("  " + b("Change anything later"));
  console.log("    " + d("·") + " name, face, or notes format  " + d("→") + "  edit " + b("config.jsonc"));
  console.log("    " + d("·") + " your AgentCall key  " + d("→") + "  edit " + b(".env"));
  console.log("    " + d("·") + " your own camera tile  " + d("→") + "  drop an image in " + b("avatars/") + " and set " + b("DISPLAY"));
  console.log();
}

function assemble(name, display, fmt, key, reused) {
  console.log();
  console.log("  " + col(BOLD, `Building ${name}`) + col(DIM, " …"));
  console.log("   " + col(BOLD, "✓") + " wired the AgentCall listener");
  console.log("   " + col(BOLD, "✓") + ` set the "${display}" face`);
  writeConfig(name, display, fmt);
  console.log("   " + col(BOLD, "✓") + " wrote config.jsonc");
  writeEnv(key);
  console.log("   " + col(BOLD, "✓") + (reused ? " copied your AgentCall key into .env" : " saved your key to .env"));

  doneCard(name);
  nextSteps();
}

function askText(question, hint, def) {
  // One text question on a fresh readline, so the arrow picker can own raw stdin between asks.
  return new Promise((resolve) => {
    const rl = require("readline").createInterface({ input: process.stdin, output: process.stdout });
    rl.on("SIGINT", () => { console.log("\n  Build cancelled."); process.exit(1); });
    console.log(`\n  ${col(BOLD, question)}` + (hint ? `  ${col(DIM, hint)}` : ""));
    rl.question("  " + col(BOLD, "›") + " ", (ans) => { rl.close(); resolve(((ans || "").trim()) || def); });
  });
}

function chooseTyped(question, options, def) {
  return new Promise((resolve) => {
    const rl = require("readline").createInterface({ input: process.stdin, output: process.stdout });
    rl.on("SIGINT", () => { console.log("\n  Build cancelled."); process.exit(1); });
    console.log(`\n  ${col(BOLD, question)}`);
    options.forEach(([k, d], i) =>
      console.log(`    ${col(BOLD, String(i + 1))}  ${k.padEnd(11)}${col(DIM, d)}${k === def ? col(DIM, "  (default)") : ""}`));
    rl.question("  " + col(BOLD, "›") + " ", (raw) => {
      rl.close();
      raw = (raw || "").trim();
      const n = parseInt(raw, 10);
      if (Number.isInteger(n) && n >= 1 && n <= options.length) return resolve(options[n - 1][0]);
      const m = options.find(([k]) => k.toLowerCase() === raw.toLowerCase());
      resolve(m ? m[0] : def);
    });
  });
}

function chooseArrows(question, options, def) {
  return new Promise((resolve, reject) => {
    const stdin = process.stdin;
    if (!stdin.isTTY || !stdin.setRawMode) return reject(new Error("no raw tty"));
    let idx = Math.max(0, options.findIndex(([k]) => k === def));
    const n = options.length;
    const width = 15 + Math.max(...options.map(([, d]) => d.length));
    const rows = () => options.map(([k, d], i) => {
      if (i !== idx) return "  " + "   " + k.padEnd(11) + col(DIM, d);
      const s = " ▸ " + k.padEnd(11) + d;
      return "  " + LIMEBG + ONLIME + s + " ".repeat(Math.max(0, width - s.length)) + R;
    });
    process.stdout.write(`\n  ${col(BOLD, question)}\n  ${col(DIM, "↑/↓ move · enter select")}\n`);
    process.stdout.write(rows().join("\n") + "\n\x1b[?25l");
    const prevRaw = stdin.isRaw;
    stdin.setRawMode(true); stdin.resume();
    const onData = (buf) => {
      const s = buf.toString();
      let act = null;
      if (s === "\x1b[A" || s === "k") { idx = (idx - 1 + n) % n; act = "move"; }
      else if (s === "\x1b[B" || s === "j") { idx = (idx + 1) % n; act = "move"; }
      else if (/^[1-9]$/.test(s) && +s <= n) { idx = +s - 1; act = "move"; }
      else if (s === "\r" || s === "\n") act = "enter";
      else if (s === "\x03" || s === "\x1b") act = "cancel";
      if (!act) return;
      if (act === "move") {
        process.stdout.write(`\x1b[${n}A`);
        for (const r of rows()) process.stdout.write("\x1b[K" + r + "\n");
        return;
      }
      stdin.removeListener("data", onData);
      stdin.setRawMode(prevRaw || false); stdin.pause();
      process.stdout.write(`\x1b[?25h\x1b[${n + 1}A\x1b[J`);
      if (act === "cancel") { console.log("\n  Build cancelled."); process.exit(1); }
      console.log(`    ${col(DIM, "›")} ${col(BOLD, options[idx][0])}`);
      resolve(options[idx][0]);
    };
    stdin.on("data", onData);
  });
}

async function choose(question, options, def) {
  // Arrow-key picker on a real terminal; falls back to typed input everywhere else.
  if (ANSI && process.stdin.isTTY && process.stdin.setRawMode) {
    try { return await chooseArrows(question, options, def); }
    catch (e) { if (process.env.NOTETAKER_DEBUG) console.log(col(DIM, `  (arrow picker unavailable: ${e.message} — using typed input)`)); }
  }
  return chooseTyped(question, options, def);
}

async function interactive() {
  console.log(col(DIM, "  A few quick questions and it's yours."));
  let key = "";
  if (existingKey()) {
    console.log("  " + col(BOLD, "✓") + col(DIM, " found your AgentCall key already set — using it."));
  } else {
    while (!key) {
      key = await askText("First — paste your AgentCall key",
                          "free at app.agentcall.dev/api-keys · Ctrl-C to cancel", "");
      if (!key) console.log("  " + col(DIM, "A key is required to run the notetaker — paste it, or Ctrl-C to cancel."));
    }
  }
  const name = await askText("Name your notetaker", "e.g. Juno · enter to keep AgentCall", "AgentCall");
  const face = await choose("How should it show up on camera?", [
    ["audio", "no video — just listens"],
    ["pattern", "the Pattern AI Labs logo"],
    ["ring", "a glowing neon ring"],
    ["transcript", "the live transcript, on screen"],
    ["image", "your own logo or photo"],
  ], "audio");
  let display = face;
  if (face === "image") {
    const ip = await askText("Where's your image?", "png · jpg · gif · svg · webp  (enter to skip)", "");
    display = ip ? copyImage(ip, name) : "pattern";
  }
  const fmt = await choose("How should it save the notes?", [
    ["md", "Markdown"], ["txt", "plain text"], ["json", "JSON"],
  ], "md");
  const found = existingKey();
  assemble(name, display, fmt, key || found, !key && !!found);
}

async function main() {
  const args = parseFlags(process.argv);
  const hasFlags = !!(args.key || args.name || args.display || args.format || args.image);

  if (!hasFlags && !process.stdin.isTTY) {
    console.log("This builder asks you questions, but there's no terminal here");
    console.log("(an AI agent, CI, or piped input). Run it non-interactively with flags:\n");
    console.log("  node build.js --key ak_ac_... --name Juno --display audio --format md");
    console.log("  (--display: audio | pattern | ring | transcript, or --image ./logo.png)");
    console.log("  A key is required: --key, or AGENTCALL_API_KEY / ~/.agentcall/config.json.\n");
    process.exit(0);
  }

  banner();

  if (hasFlags) {
    const name = args.name || "AgentCall";
    const display = args.image ? copyImage(args.image, name) : (args.display || "audio");
    const key = args.key || "";
    const found = existingKey();
    if (!key && !found) {
      console.log("\n  An AgentCall key is required — the notetaker can't run without one.");
      console.log("  Pass --key ak_ac_...  (free at app.agentcall.dev/api-keys),");
      console.log("  or set AGENTCALL_API_KEY, or add it to .env, then build again.");
      process.exit(1);
    }
    assemble(name, display, args.format || "md", key || found, !key && !!found);
  } else {
    await interactive();
  }
}

main().catch((e) => { console.error(e.message); process.exit(1); });
