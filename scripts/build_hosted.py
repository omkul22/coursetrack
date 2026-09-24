"""Derive the hosted page from the local dashboard.

Both should look identical; only the interactive half differs. Generating one
from the other means the board layout cannot drift between them.
"""
import pathlib
import re

local = pathlib.Path("src/coursetrack/dashboard/static/index.html").read_text()
out = pathlib.Path("docs/index.html")

style = re.search(r"<style>(.*?)</style>", local, re.S).group(1)
script = re.search(r"<script>(.*?)</script>", local, re.S).group(1)

# --- strip the interactive half -------------------------------------------
# The hosted page is a static file: there is no server to accept a click, so a
# checkbox there is decorative and misleading. Show state, not a control.
script = script.replace(
    '''      <button class="chk ${row.done ? "on" : ""}" aria-pressed="${row.done}"
        title="${row.done ? "Mark not done" : "Mark done"}">${row.done ? TICK : ""}</button>
''', "")
script = script.replace(
    '''    <div class="acts">
      ${row.editable ? `<button class="ghost" data-act="move">Move</button>` : ""}
      <button class="ghost" data-act="dismiss">${row.dismissed ? "Restore" : "Dismiss"}</button>
      ${row.editable ? `<button class="ghost" data-act="delete">Delete</button>` : ""}
    </div>''', "")

# Drop every handler block that needs the local API.
for block in [
    re.compile(r"\n  el\.querySelector\(\"\.chk\"\)\.onclick.*?\n  \};\n", re.S),
    re.compile(r"\n  const move = el\.querySelector.*?\n  \};\n", re.S),
    re.compile(r"\n  el\.querySelector\('\[data-act=\"dismiss\"\]'\)\.onclick.*?\n  \};\n", re.S),
    re.compile(r"\n  const del = el\.querySelector.*?\n  \};\n", re.S),
    re.compile(r"\nfunction renderCourses\(\) \{.*?\n\}\n", re.S),
    re.compile(r"\n\$\(\"syncBtn\"\)\.onclick.*?\n\};\n", re.S),
    re.compile(r"\n\$\(\"addForm\"\)\.onsubmit.*?\n\};\n", re.S),
    re.compile(r"\n\$\(\"courseForm\"\)\.onsubmit.*?\n\};\n", re.S),
    re.compile(r"\nasync function api\(path.*?\n\}\n", re.S),
    re.compile(r"\nfunction toast\(message.*?\n\}\n", re.S),
    re.compile(r"\nasync function load\(\) \{.*?\n\}\n", re.S),
]:
    script = block.sub("\n", script)

script = script.replace("  renderCourses();\n", "")
script = script.replace("load();\nsetInterval(render, 60000);\nsetInterval(load, 300000);\n", "")

# The hosted page reads a decrypted payload, not the local API.
script = script.replace("  const rows = STATE.deadlines.filter((r) => r.course_enabled !== false);",
                        "  const rows = STATE.deadlines;")
script = script.replace("""  assignColors([
    ...STATE.courses.map((c) => c.id),
    ...rows.map((r) => r.course_id || r.course || "__none__"),
  ]);""",
                        """  assignColors(rows.map((r) => r.course || "__none__"));""")
script = script.replace('''    const key = row.course_id || row.course || "__none__";
    if (!groups.has(key)) groups.set(key, { key, name: row.course || "No course", rows: [] });''',
                        '''    const key = row.course || "__none__";
    if (!groups.has(key)) groups.set(key, { key, name: row.course || "No course", rows: [] });''')
script = script.replace('''    const key = row.course_id || row.course || "__none__";
    if (!byCourse.has(key)) {''', '''    const key = row.course || "__none__";
    if (!byCourse.has(key)) {''')
script = script.replace("""  $("health").className = "badge " + (STATE.health.ok ? "ok" : "bad");
  $("healthText").textContent = STATE.health.detail;""",
"""  const age = (now - new Date(STATE.generated_at)) / HOUR;
  $("health").className = "badge " + (age < 26 ? "ok" : "bad");
  $("healthText").textContent = age < 1 ? "updated just now" : `updated ${Math.round(age)}h ago`;""")
script = script.replace("""    (STATE.recipient ? ` · ${STATE.recipient}` : "");""", """ "";""")
script = script.replace("""  const el = document.createElement("div");
  el.className = "card" + (row.done ? " finished" : "");
""", """  const el = document.createElement("div");
  el.className = "card" + (row.done ? " finished" : "");
""")

BOOT = '''
/* ---------- decrypt ---------- */
const STORAGE_KEY = "coursetrack.pass";

function b64ToBytes(value) {
  const binary = atob(value);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

async function decrypt(envelope, passphrase) {
  const material = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    { name: "PBKDF2", salt: b64ToBytes(envelope.salt),
      iterations: envelope.iterations, hash: "SHA-256" },
    material, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  // GCM authenticates, so a wrong key throws instead of yielding garbage.
  const plain = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: b64ToBytes(envelope.iv) }, key, b64ToBytes(envelope.ct));
  return JSON.parse(new TextDecoder().decode(plain));
}

const diag = [];
function note(line, bad) {
  diag.push(bad ? `<b>${line}</b>` : line);
  $("diag").innerHTML = diag.join("\\n");
}

async function unlock(passphrase, remember) {
  const response = await fetch(`data.enc?t=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Could not load data.enc (HTTP ${response.status})`);
  STATE = await decrypt(await response.json(), passphrase);
  if (remember) { try { localStorage.setItem(STORAGE_KEY, passphrase); } catch (e) {} }
  $("lock").hidden = true;
  $("app").hidden = false;
  render();
  setInterval(render, 60000);
}

$("unlockForm").onsubmit = async (event) => {
  event.preventDefault();
  const btn = $("unlockBtn");
  btn.disabled = true;
  btn.textContent = "Decrypting…";
  $("err").textContent = "";
  diag.length = 0;
  const raw = $("pass").value;
  const typed = raw.trim();
  note(`passphrase: ${typed.length} chars` +
       (raw !== typed ? ` (trimmed ${raw.length - typed.length} whitespace char(s))` : ""));
  try {
    await unlock(typed, $("remember").checked);
  } catch (error) {
    note(`FAILED: ${error.name}: ${error.message || "(no message)"}`, true);
    $("err").textContent = error.name === "OperationError"
      ? "Wrong passphrase — check for a stray space or newline."
      : error.message;
    $("pass").select();
    btn.disabled = false;
    btn.textContent = "Unlock";
  }
};

$("lockBtn").onclick = () => {
  try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
  location.href = location.pathname;
};

(async function boot() {
  const build = document.querySelector('meta[name=coursetrack-build]')?.content || "?";
  $("build").textContent = "build " + build;
  note(`build ${build}`);
  note(`secure context: ${window.isSecureContext}`, !window.isSecureContext);
  note(`crypto.subtle: ${typeof crypto?.subtle}`, typeof crypto?.subtle !== "object");

  // A passphrase in the URL fragment wins. Fragments are never sent to the
  // server, so a bookmarked link is as private as typing it.
  const fromHash = decodeURIComponent(location.hash.replace(/^#/, "")).trim();
  if (fromHash) {
    try {
      await unlock(fromHash, true);
      history.replaceState(null, "", location.pathname + location.search);
      return;
    } catch (error) {
      $("err").textContent = "The passphrase in this link did not work.";
    }
  }

  let saved = null;
  try { saved = (localStorage.getItem(STORAGE_KEY) || "").trim(); } catch (e) {}
  if (!saved) return;
  try { await unlock(saved, false); }
  catch (error) { try { localStorage.removeItem(STORAGE_KEY); } catch (e) {} }
})();
'''

LOCK_CSS = '''
#lock{min-height:100dvh;display:grid;place-items:center;padding:16px}
#lock .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:26px 22px;box-shadow:var(--shadow);width:100%;max-width:370px;text-align:center;margin:0}
#lock .card:hover{border-color:var(--border)}
#lock h1{font-size:18px;margin:0 0 6px;font-weight:600}
#lock p{color:var(--muted);font-size:13px;margin:0 0 18px}
input[type=password]{font:inherit;font-size:16px;width:100%;padding:10px 12px;border-radius:6px;
  border:1px solid var(--border);background:var(--bg);color:var(--text);text-align:center}
#lock button{width:100%;margin-top:12px}
.err{color:var(--danger);font-size:12.5px;margin-top:12px;min-height:16px}
.remember{display:flex;gap:8px;align-items:center;justify-content:center;
  font-size:12.5px;color:var(--muted);margin-top:12px}
.remember input{width:auto}
#diag{font:11px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--faint);
  text-align:left;margin:14px 0 0;white-space:pre-wrap;word-break:break-all}
#diag b{color:var(--danger);font-weight:600}
.linkbtn{background:none;border:none;color:var(--muted);padding:4px 8px;
  font-size:12px;text-decoration:underline}
.foot{margin-top:26px;text-align:center;color:var(--faint);font-size:11.5px;line-height:1.7}
'''

BODY = '''
<div id="lock">
  <div class="card">
    <h1>CourseTrack</h1>
    <p>This page holds encrypted data. Enter your passphrase to decrypt it in your browser.</p>
    <form id="unlockForm">
      <input type="password" id="pass" placeholder="Passphrase" autocomplete="current-password"
             autofocus spellcheck="false">
      <label class="remember"><input type="checkbox" id="remember" checked>
        Stay unlocked on this device</label>
      <button type="submit" id="unlockBtn" class="primary">Unlock</button>
    </form>
    <div class="err" id="err"></div>
    <pre id="diag"></pre>
  </div>
</div>

<div class="wrap" id="app" hidden>
  <header>
    <div class="grow">
      <h1>CourseTrack</h1>
      <div class="sub" id="sub"></div>
    </div>
    <span class="badge" id="health"><span class="dot"></span><span id="healthText"></span></span>
    <button class="linkbtn" id="lockBtn">Lock</button>
  </header>

  <div class="summary">
    <div id="donut"></div>
    <div style="min-width:0">
      <div class="label">This week by course</div>
      <div class="bars" id="bars"></div>
    </div>
  </div>

  <div class="upnext">
    <h2 id="upTitle">Coming up</h2>
    <div id="up"></div>
  </div>

  <div id="boards"></div>

  <div class="foot">
    Read-only. Tick things off in the local dashboard.<br>
    Decrypted in your browser — the server never sees your passphrase.<br>
    <span id="build"></span>
  </div>
</div>
'''

page = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="coursetrack-build" content="7">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#16191d">
<title>CourseTrack</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>📋</text></svg>">
<style>
/* Generated from the local dashboard by scripts/build_hosted.py -- edit that
   dashboard, not this file, so the two cannot drift apart. */
{style}
{LOCK_CSS}
</style>
</head>
<body>
{BODY}
<script>
"use strict";
{script}
{BOOT}
</script>
</body>
</html>
'''

out.write_text(page)
print(f"  wrote docs/index.html ({len(page)} bytes)")
