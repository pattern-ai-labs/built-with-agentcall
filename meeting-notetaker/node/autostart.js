"use strict";
/*
 * Start auto-join automatically when you log in — OS-specific bit (Node mirror of
 * python/autostart.py). Native, no-admin mechanism per platform, and we print the
 * exact file created so nothing happens behind your back:
 *
 *   Windows   a hidden launcher in your Startup folder
 *   macOS     a LaunchAgent (~/Library/LaunchAgents)
 *   Linux     a systemd --user service (~/.config/systemd/user)
 *
 * enable() also starts it now; disable() also stops it now.
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync, execFileSync } = require("child_process");

const HERE = __dirname;
const AUTOJOIN = path.join(HERE, "autojoin.js");
const NODE = process.execPath;
const APP_NAME = "MeetingNotetaker-AutoJoin";
const LABEL = "dev.agentcall.notetaker-autojoin";
const BOOT_LOG = path.join(path.dirname(HERE), ".notetaker", "autojoin.boot.log");

// ── Windows: a hidden .vbs in the Startup folder ────────────────────────────
function winStartupDir() {
  return path.join(process.env.APPDATA || "", "Microsoft", "Windows", "Start Menu", "Programs", "Startup");
}
function winVbs() { return path.join(winStartupDir(), APP_NAME + ".vbs"); }
function winEnable() {
  const vbs = winVbs();
  const runArg = `"""${NODE}"" ""${AUTOJOIN}"" start"`;   // VBS doubles internal quotes
  const content = 'Set sh = CreateObject("WScript.Shell")\r\n' +
    `sh.CurrentDirectory = "${HERE}"\r\n` +
    `sh.Run ${runArg}, 0, False\r\n`;
  fs.mkdirSync(winStartupDir(), { recursive: true });
  fs.writeFileSync(vbs, content);
  console.log("  ✓ added a startup launcher so auto-join starts when you log in:");
  console.log(`    ${vbs}`);
  return 0;
}
function winDisable() {
  try { fs.unlinkSync(winVbs()); console.log("  removed the startup launcher — auto-join won't start on login anymore."); }
  catch (e) { if (e.code === "ENOENT") console.log("  (it wasn't set to start on login.)"); else { console.log(`  couldn't remove ${winVbs()}: ${e.message}`); return 1; } }
  return 0;
}
function winIsEnabled() { try { return fs.existsSync(winVbs()); } catch { return false; } }

// ── macOS: a LaunchAgent plist ──────────────────────────────────────────────
function macPlist() { return path.join(os.homedir(), "Library", "LaunchAgents", LABEL + ".plist"); }
function macEnable() {
  const plist = macPlist();
  fs.mkdirSync(path.dirname(plist), { recursive: true });
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>${NODE}</string><string>${AUTOJOIN}</string><string>run</string></array>
  <key>WorkingDirectory</key><string>${HERE}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>${BOOT_LOG}</string>
  <key>StandardErrorPath</key><string>${BOOT_LOG}</string>
</dict>
</plist>
`;
  fs.writeFileSync(plist, xml);
  spawnSync("launchctl", ["unload", plist]);
  const r = spawnSync("launchctl", ["load", "-w", plist], { encoding: "utf-8" });
  console.log(`  ✓ installed a LaunchAgent: ${plist}`);
  if (r.status !== 0) {
    console.log(`  (launchctl load said: ${(r.stderr || r.stdout || "").trim()})`);
    console.log(`  You can load it yourself with:  launchctl load -w "${plist}"`);
  }
  return 0;
}
function macDisable() {
  const plist = macPlist();
  spawnSync("launchctl", ["unload", "-w", plist]);
  try { fs.unlinkSync(plist); } catch (e) { if (e.code === "ENOENT") { console.log("  (it wasn't set to start on login.)"); return 0; } console.log(`  couldn't remove ${plist}: ${e.message}`); return 1; }
  console.log("  removed the LaunchAgent — auto-join won't start on login anymore.");
  return 0;
}
function macIsEnabled() { try { return fs.existsSync(macPlist()); } catch { return false; } }

// ── Linux: a systemd --user service ─────────────────────────────────────────
function linuxUnit() { return path.join(os.homedir(), ".config", "systemd", "user", LABEL + ".service"); }
function hasSystemctl() { try { execFileSync("systemctl", ["--version"], { stdio: "ignore" }); return true; } catch { return false; } }
function linuxEnable() {
  if (!hasSystemctl()) {
    console.log("  systemd isn't available here. Add this to your login startup by hand:");
    console.log(`    ${NODE} ${AUTOJOIN} start`);
    return 1;
  }
  const unit = linuxUnit();
  fs.mkdirSync(path.dirname(unit), { recursive: true });
  fs.writeFileSync(unit,
    "[Unit]\nDescription=Meeting Notetaker auto-join (watches your calendar)\n\n" +
    `[Service]\nWorkingDirectory=${HERE}\nExecStart=${NODE} ${AUTOJOIN} run\nRestart=on-failure\n\n` +
    "[Install]\nWantedBy=default.target\n");
  spawnSync("systemctl", ["--user", "daemon-reload"]);
  const r = spawnSync("systemctl", ["--user", "enable", "--now", LABEL + ".service"], { encoding: "utf-8" });
  console.log(`  ✓ installed a systemd --user service: ${unit}`);
  if (r.status !== 0) console.log(`  (systemctl said: ${(r.stderr || r.stdout || "").trim()})`);
  console.log("  Tip: to run it even before you log in, enable lingering once:");
  console.log("    loginctl enable-linger $USER");
  return 0;
}
function linuxDisable() {
  if (hasSystemctl()) spawnSync("systemctl", ["--user", "disable", "--now", LABEL + ".service"]);
  try { fs.unlinkSync(linuxUnit()); } catch (e) { if (e.code === "ENOENT") { console.log("  (it wasn't set to start on login.)"); return 0; } console.log(`  couldn't remove the unit: ${e.message}`); return 1; }
  if (hasSystemctl()) spawnSync("systemctl", ["--user", "daemon-reload"]);
  console.log("  removed the systemd service — auto-join won't start on login anymore.");
  return 0;
}
function linuxIsEnabled() { try { return fs.existsSync(linuxUnit()); } catch { return false; } }

// ── dispatch ─────────────────────────────────────────────────────────────────
function startNow() { spawnSync(NODE, [AUTOJOIN, "start"], { stdio: "inherit" }); }
function stopNow() { spawnSync(NODE, [AUTOJOIN, "stop"], { stdio: "inherit" }); }

function enable() {
  if (process.platform === "win32") { const rc = winEnable(); startNow(); return rc; }
  if (process.platform === "darwin") return macEnable();
  return linuxEnable();
}
function disable() {
  if (process.platform === "win32") { stopNow(); return winDisable(); }
  if (process.platform === "darwin") return macDisable();
  return linuxDisable();
}
function isEnabled() {
  if (process.platform === "win32") return winIsEnabled();
  if (process.platform === "darwin") return macIsEnabled();
  return linuxIsEnabled();
}

module.exports = { enable, disable, isEnabled };

if (require.main === module) {
  const cmd = process.argv[2];
  if (cmd === "enable") process.exit(enable());
  else if (cmd === "disable") process.exit(disable());
  else if (cmd === "status") { console.log("start-on-login:", isEnabled() ? "yes" : "no"); process.exit(0); }
  else { console.log("usage: node autostart.js [enable|disable|status]"); process.exit(0); }
}
