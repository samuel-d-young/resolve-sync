// First-run setup wizard — gets an editor from "just installed" to "ready to
// push" without typing a path or opening a terminal.
let wiz = { step: 1, provider: null, roots: new Set(), detected: null };

function showWizard(force = false) {
  $("wizard").classList.remove("hidden");
  gotoStep(1);
  runChecks();
  loadDetection();
}
function hideWizard() { $("wizard").classList.add("hidden"); }

function gotoStep(n) {
  wiz.step = n;
  for (const p of document.querySelectorAll(".wpane")) {
    p.classList.toggle("hidden", Number(p.dataset.pane) !== n);
  }
  for (const s of document.querySelectorAll(".step")) {
    const i = Number(s.dataset.step);
    s.classList.toggle("active", i === n);
    s.classList.toggle("done", i < n);
  }
  $("wizBack").classList.toggle("hidden", n === 1);
  $("wizNext").textContent = n === 4 ? "Finish" : "Continue";
}

function checkRow(state, title, sub, action) {
  const d = document.createElement("div");
  d.className = "check " + state;
  const ic = state === "ok" ? "\u2713" : state === "bad" ? "\u2715" : "!";
  d.innerHTML = `<span class="ic">${ic}</span><span class="txt">${esc(title)}` +
    (sub ? `<span class="sub">${esc(sub)}</span>` : "") + `</span>`;
  if (action) {
    const b = document.createElement("button");
    b.className = "btn small";
    b.textContent = action.label;
    b.onclick = action.fn;
    d.appendChild(b);
  }
  return d;
}

async function runChecks() {
  const box = $("checkList");
  box.innerHTML = '<p class="muted">Checking\u2026</p>';
  try {
    const d = await api("/api/detect");
    wiz.detected = d;
    box.innerHTML = "";

    box.appendChild(d.resolve_connected
      ? checkRow("ok", "DaVinci Resolve is connected",
          d.current_project ? "Currently open: " + d.current_project : "")
      : checkRow("bad", "Can't talk to DaVinci Resolve",
          "Resolve must be running, must be the Studio edition, and Preferences > " +
          "System > General > External scripting must be set to Local."));

    box.appendChild(d.providers.length
      ? checkRow("ok", "Found " + d.providers.length + " place" +
          (d.providers.length > 1 ? "s" : "") + " to keep your projects",
          d.providers.map((p) => p.name).join(", "))
      : checkRow("warn", "No Dropbox, Google Drive or OneDrive found",
          "You can still pick a folder or a private GitHub repo on the next step."));

    box.appendChild(d.media_suggestions.length
      ? checkRow("ok", "Found " + d.media_suggestions.length + " likely footage folders",
          "Read from your existing Resolve projects")
      : checkRow("warn", "Couldn't work out where your footage lives",
          "You can add folders by hand on step 3."));
  } catch (e) {
    box.innerHTML = "";
    box.appendChild(checkRow("bad", "Setup check failed", e.message));
  }
}

async function loadDetection() {
  if (!wiz.detected) {
    try { wiz.detected = await api("/api/detect"); } catch { return; }
  }
  renderProviders();
  renderMediaSuggestions();
}

// Measured on a real ~18 KB project file, not estimated:
//   synced folder 0.012s · GitHub 2.7s · Google Drive 20.9s per backup.
// Shown so the choice is informed — Drive is convenient but ~8x slower than
// GitHub, and GitHub is the only one that can refuse a clashing backup.
const PROVIDER_SPEED = {
  dropbox:  { secs: 0.5,  label: "Instant",     note: "Saves locally, then Dropbox uploads in the background." },
  gdrive:   { secs: 0.5,  label: "Instant",     note: "Saves locally, then Google Drive uploads in the background." },
  onedrive: { secs: 0.5,  label: "Instant",     note: "Saves locally, then OneDrive uploads in the background." },
  custom:   { secs: 0.5,  label: "Instant",     note: "Saved straight to the folder or NAS." },
  github:   { secs: 2.7,  label: "~3 seconds",  note: "Waits until it is safely stored. Blocks two computers overwriting each other." },
  google:   { secs: 20.9, label: "~20 seconds", note: "Uploads to Drive and waits for confirmation. Slower, but nothing to install." },
};

function speedBadge(key) {
  const s = PROVIDER_SPEED[key];
  if (!s) return "";
  const cls = s.secs <= 1 ? "sp-fast" : s.secs <= 5 ? "sp-ok" : "sp-slow";
  return `<span class="speed ${cls}">${esc(s.label)} per backup</span>` +
         `<span class="os">${esc(s.note)}</span>`;
}

const PROVIDER_ICONS = {
  dropbox: "\u{1F4E6}", gdrive: "\u{1F7E1}", onedrive: "\u2601\uFE0F",
  google: "\u{1F310}", github: "\u{1F512}", custom: "\u{1F4C1}",
};

function renderProviders() {
  const box = $("providerList");
  box.innerHTML = "";
  const opts = [...(wiz.detected?.providers || [])];
  opts.push({ key: "google", name: "Cloud (Google Drive)", path: "",
    note: "Sign in with Google. Nothing to install." });
  opts.push({ key: "github", name: "Cloud (Git) — private repo", path: "",
    note: "Advanced. Stops two computers from overwriting each other." });
  opts.push({ key: "custom", name: "Network folder (NAS or external drive)", path: "", note: "" });

  for (const p of opts) {
    const b = document.createElement("button");
    b.className = "opt" + (wiz.provider?.key === p.key ? " active" : "");
    b.innerHTML = `<span class="on">${PROVIDER_ICONS[p.key] || "\u{1F4C1}"}</span>` +
      `<span style="flex:1"><span class="ot">${esc(p.name)}</span>` +
      (p.path ? `<span class="os">${esc(p.path)}</span>` : "") +
      speedBadge(p.key) +
      (p.note ? `<span class="ow">${esc(p.note)}</span>` : "") + `</span>`;
    b.onclick = () => { wiz.provider = p; renderProviders(); };
    box.appendChild(b);
  }
  const gp = document.getElementById("googlePanel");
  if (gp) {
    gp.classList.toggle("hidden", wiz.provider?.key !== "google");
    if (wiz.provider?.key === "google" && typeof loadGoogle === "function") loadGoogle();
  }
  renderProviderExtra();
}

function renderProviderExtra() {
  let extra = $("providerExtra");
  if (!extra) {
    extra = document.createElement("div");
    extra.id = "providerExtra";
    $("providerList").after(extra);
  }
  extra.innerHTML = "";
  const p = wiz.provider;
  if (!p) return;
  if (p.key === "github") {
    extra.innerHTML =
      '<label class="fld">Your private repo address' +
      '<input id="wizGitRemote" type="text" placeholder="git@github.com:you/resolve-sync-projects.git" /></label>' +
      '<p class="hint">Needs Git installed and an SSH key set up on this computer.</p>';
  } else if (p.key === "custom") {
    extra.innerHTML =
      '<label class="fld">A folder both computers can see' +
      '<input id="wizCustomPath" type="text" placeholder="\\\\NAS\\Projects\\ResolveSync" /></label>';
  }
}

function renderMediaSuggestions() {
  const box = $("mediaSuggestions");
  const sugg = wiz.detected?.media_suggestions || [];
  box.innerHTML = "";
  if (!sugg.length) {
    box.innerHTML = '<p class="muted">Nothing detected \u2014 add a folder below.</p>';
    return;
  }
  // Preselect the drives that are actually connected right now.
  if (!wiz.roots.size) sugg.filter((s) => s.online).forEach((s) => wiz.roots.add(s.path));
  for (const s of sugg) {
    const row = document.createElement("label");
    row.className = "pick" + (s.online ? "" : " off");
    row.innerHTML =
      `<input type="checkbox" ${wiz.roots.has(s.path) ? "checked" : ""}>` +
      `<span class="pp">${esc(s.path)}</span>` +
      `<span class="pc">${s.clips} clips${s.online ? "" : " \u00b7 drive not connected"}</span>`;
    row.querySelector("input").onchange = (ev) =>
      ev.target.checked ? wiz.roots.add(s.path) : wiz.roots.delete(s.path);
    box.appendChild(row);
  }
}

async function finishWizard() {
  const p = wiz.provider;
  const roots = [...wiz.roots];
  const extra = ($("extraRoot")?.value || "").trim();
  if (extra) roots.push(extra);

  const body = { media_roots: roots, author: $("wizAuthor").value.trim() };
  if (p?.key === "google") {
    if (!googleState?.signed_in) return toast("Sign in with Google first", "err");
    body.backend = "drive";
    body.store_path = "";
  } else if (p?.key === "github") {
    body.backend = "git";
    body.git_remote = ($("wizGitRemote")?.value || "").trim();
    if (!body.git_remote) return toast("Enter your repo address", "err");
    body.store_path = (wiz.detected?.repo_default || "") ||
      "C:\\Users\\Public\\ResolveSync\\repo";
  } else {
    body.backend = "folder";
    body.store_path = p?.key === "custom"
      ? ($("wizCustomPath")?.value || "").trim()
      : (p?.path || "");
    if (!body.store_path) return toast("Choose where your projects should sync", "err");
  }

  try {
    await api("/api/config", { method: "POST", body: JSON.stringify(body) });
    hideWizard();
    // Force the main settings form to re-read what the wizard just saved.
    configHydrated = false;
    ["storePathGit", "storePathFolder", "gitRemote", "mediaRoots", "author"]
      .forEach((id) => { if ($(id)) $(id).value = ""; });
    await loadOverview();
    toast("Setup complete \u2014 tick projects and press Push", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

// ---- wiring ---------------------------------------------------------------
async function wizNext() {
  if (wiz.step === 1) { await loadDetection(); return gotoStep(2); }
  if (wiz.step === 2) {
    if (!wiz.provider) return toast("Choose where your projects should be kept", "err");
    renderMediaSuggestions();
    return gotoStep(3);
  }
  if (wiz.step === 3) {
    const p = wiz.provider;
    $("wizAuthor").value = wiz.detected?.author || "";
    const sum = $("wizSummary");
    sum.innerHTML = "";
    sum.appendChild(checkRow("ok", "Project Library: " + (p?.name || "?"),
      p?.key === "github" ? ($("wizGitRemote")?.value || "")
        : p?.key === "custom" ? ($("wizCustomPath")?.value || "") : (p?.path || "")));
    sum.appendChild(checkRow("ok",
      wiz.roots.size + " footage folder" + (wiz.roots.size === 1 ? "" : "s") + " selected", ""));
    const sp = PROVIDER_SPEED[p?.key];
    if (sp) {
      sum.appendChild(checkRow(sp.secs > 5 ? "warn" : "ok",
        "Each backup takes " + sp.label.toLowerCase(), sp.note));
    }
    return gotoStep(4);
  }
  return finishWizard();
}

$("wizNext").onclick = wizNext;
$("wizBack").onclick = () => gotoStep(Math.max(1, wiz.step - 1));
$("wizCancel").onclick = hideWizard;
if ($("btnWizard")) $("btnWizard").onclick = () => showWizard(true);
if ($("btnWizard2")) $("btnWizard2").onclick = () => {
  $("settings").classList.add("hidden");
  showWizard(true);
};

(async () => {
  try {
    const d = await api("/api/detect");
    wiz.detected = d;
    if (!d.configured) showWizard();
  } catch { /* agent still starting */ }
})();
