// Resolve Sync — Project Manager view.
//
// Deliberately mirrors DaVinci Resolve's own Project Manager: a Project
// Libraries sidebar, a sortable list with Resolve's native columns, a
// thumbnail/list toggle, and Resolve's collaboration signal (initials chip +
// refresh glyph on the row) instead of a generic banner.

const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || res.statusText);
    err.status = res.status;
    throw err;
  }
  return data;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg, kind = "") {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = "toast"), 4200);
}

// ---------------------------------------------------------------- state --
let view = { rows: [], selected: new Set(), sort: "name", asc: true,
             mode: "list", overview: null, current: null, panelProject: null,
             folder: null };
let busy = new Set();   // operations THIS window started, shown instantly
let configHydrated = false;   // wizard.js reads this

// -------------------------------------------------------------- helpers --
const STATUS_GLYPH = { synced: "●", syncing: "◐", incoming: "⟳",
                       none: "○", attn: "▲", active: "⟳" };

function initials(name) {
  return String(name || "?").trim().split(/\s+/).slice(0, 2)
    .map((w) => w[0]).join("").toUpperCase() || "?";
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso.includes("T") ? iso : iso.replace(" ", "T"));
  if (isNaN(d)) return iso;
  const days = (Date.now() - d.getTime()) / 86400000;
  if (days < 1) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (days < 7) return d.toLocaleDateString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString();
}

// ----------------------------------------------------------- the loader --
async function loadOverview() {
  try {
    const o = await api("/api/overview");
    view.overview = o;
    view.rows = o.projects;
    view.current = o.current_project;
    renderAll();
  } catch (e) {
    $("content").innerHTML =
      `<p class="muted" style="padding:16px">${esc(e.message)}</p>`;
    setStatus(false, e.message);
  }
}

function renderAll() {
  const o = view.overview;
  if (!o) return;
  setStatus(o.resolve_connected,
    o.resolve_connected
      ? "Resolve connected" + (o.current_project ? ` · ${o.current_project}` : "")
      : "Resolve not connected");

  const a = o.auto_sync || {};
  $("autoText").textContent = a.enabled
    ? `Auto backup on · ${(a.watching || []).length} watched`
    : "Auto backup off";
  const c = o.counts || {};
  const activeNames = [...new Set([...(o.busy || []), ...busy])];
  $("countText").textContent = activeNames.length
    ? `Syncing: ${activeNames.slice(0, 3).join(", ")}` +
      (activeNames.length > 3 ? ` +${activeNames.length - 3}` : "")
    : `${c.backed_up || 0} of ${c.total || 0} backed up` +
      (c.incoming ? ` · ${c.incoming} incoming` : "");
  // While work is in flight, poll fast so the state clears promptly. Re-arms on
  // every render that still shows activity, and stops as soon as it is quiet.
  clearTimeout(renderAll._fast);
  renderAll._fast = null;
  if (activeNames.length) {
    renderAll._fast = setTimeout(loadOverview, 1500);
  }

  renderLibs();
  renderProjects();
  updateSelInfo();
}

function setStatus(ok, text) {
  $("dot").className = "dot " + (ok ? "ok" : "err");
  $("statusText").textContent = text;
}

// ------------------------------------------------------------- sidebar --
const LIB_LABEL = {
  git: ["Cloud (Git)", "☁"], drive: ["Cloud (Google Drive)", "☁"],
  folder: ["Network / synced folder", "▤"],
};

function folderLabel(path) {
  return path === "" ? "Top level" : path;
}

function renderLibs() {
  const o = view.overview;
  const box = $("libs");
  const [label, ico] = LIB_LABEL[o.backend] || ["Project Library", "▤"];
  $("crumbLib").textContent =
    label + (view.folder !== null ? " › " + folderLabel(view.folder) : "");
  box.innerHTML = "";

  // The library itself, with a link to where the backups actually live.
  const el = document.createElement("div");
  el.className = "lib active lib-main";
  el.innerHTML =
    `<span class="ico">${ico}</span>` +
    `<span class="nm">${esc(label)}` +
    `<span class="lib-sub">${esc(o.store_info?.label || "")}</span></span>`;
  const open = document.createElement("button");
  open.className = "mini";
  open.title = "Open the backup location";
  open.textContent = "↗";
  open.onclick = (e) => { e.stopPropagation(); openStoreLocation(); };
  el.appendChild(open);
  const dot = document.createElement("span");
  dot.className = "dot " + (o.resolve_connected ? "ok" : "");
  el.appendChild(dot);
  el.onclick = () => openSettings();
  box.appendChild(el);

  // Resolve's folder tree, with per-folder backup.
  const head = document.createElement("div");
  head.className = "side-head";
  head.textContent = "Folders";
  box.appendChild(head);

  const mkRow = (name, path, count) => {
    const row = document.createElement("div");
    row.className = "lib" + (view.folder === path ? " active" : "");
    row.innerHTML =
      `<span class="ico">${path === null ? "▥" : "▣"}</span>` +
      `<span class="nm">${esc(name)}</span><span class="cnt">${count}</span>`;
    if (path !== null) {
      const up = document.createElement("button");
      up.className = "mini";
      up.title = "Back up every project in this folder";
      up.textContent = "⬆";
      up.onclick = (e) => { e.stopPropagation(); backupFolder(path); };
      row.appendChild(up);
    }
    row.onclick = () => { view.folder = path; renderLibs(); renderProjects(); };
    box.appendChild(row);
  };

  mkRow("All projects", null, view.rows.length);
  for (const f of o.folders || []) mkRow(folderLabel(f.path), f.path, f.count);
}

async function openStoreLocation() {
  try {
    const r = await api("/api/store/open", { method: "POST", body: "{}" });
    toast("Opened " + (r.target || "backup location"), "ok");
  } catch (e) { toast(e.message, "err"); }
}

function backupFolder(path) {
  const names = view.rows
    .filter((r) => r.folder === path && r.in_resolve)
    .map((r) => r.name);
  if (!names.length) return toast("Nothing to back up in this folder", "err");
  const what = `${names.length} project${names.length > 1 ? "s" : ""}`;
  if (!confirm(`Back up ${what} in “${folderLabel(path)}”?`)) return;
  backup(names, $("note")?.value.trim() || "");
}

// ------------------------------------------------------------ projects --
const COLUMNS = [
  { key: "sel", label: "", cls: "tick" },
  { key: "name", label: "Name" },
  { key: "modified", label: "Last Modified" },
  { key: "timelines", label: "Timelines" },
  { key: "format", label: "Format" },
  { key: "fps", label: "Frame Rate" },
  { key: "status", label: "Sync", cls: "state" },
  { key: "act", label: "", cls: "act" },
];

function visibleRows() {
  const q = $("filter").value.trim().toLowerCase();
  let rows = view.rows.filter((r) => !q || r.name.toLowerCase().includes(q));
  if (view.folder !== null) rows = rows.filter((r) => r.folder === view.folder);
  const dir = view.asc ? 1 : -1;
  const val = (r) => {
    switch (view.sort) {
      case "modified": return r.meta?.modified || "";
      case "timelines": return r.meta?.timelines || 0;
      case "format": return r.meta?.format || "";
      case "fps": return r.meta?.fps || 0;
      case "status": return r.status;
      default: return r.name.toLowerCase();
    }
  };
  return rows.sort((a, b) => (val(a) > val(b) ? dir : val(a) < val(b) ? -dir : 0));
}

function renderProjects() {
  const rows = visibleRows();
  const box = $("content");
  if (!rows.length) {
    box.innerHTML = '<p class="muted" style="padding:16px">No projects match.</p>';
    return;
  }
  box.innerHTML = "";
  box.appendChild(view.mode === "grid" ? gridOf(rows) : tableOf(rows));
}

function tableOf(rows) {
  const t = document.createElement("table");
  t.className = "projects";
  const head = document.createElement("thead");
  head.innerHTML = "<tr>" + COLUMNS.map((c) =>
    `<th class="${c.cls || ""}" data-k="${c.key}">${esc(c.label)}` +
    (view.sort === c.key ? (view.asc ? " ▲" : " ▼") : "") + "</th>").join("") + "</tr>";
  head.querySelectorAll("th").forEach((th) => {
    const k = th.dataset.k;
    if (k === "sel" || k === "act") return;
    th.onclick = () => {
      view.asc = view.sort === k ? !view.asc : true;
      view.sort = k;
      renderProjects();
    };
  });
  t.appendChild(head);

  const body = document.createElement("tbody");
  for (const r of rows) body.appendChild(rowOf(r));
  t.appendChild(body);
  return t;
}

function rowOf(r) {
  const tr = document.createElement("tr");
  if (view.selected.has(r.name)) tr.className = "sel";
  const m = r.meta || {};

  const tick = document.createElement("td");
  tick.className = "tick";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = view.selected.has(r.name);
  cb.onclick = (e) => {
    e.stopPropagation();
    if (cb.checked) view.selected.add(r.name); else view.selected.delete(r.name);
    tr.classList.toggle("sel", cb.checked);
    updateSelInfo();
  };
  tick.appendChild(cb);

  const name = document.createElement("td");
  name.className = "name";
  name.textContent = r.name;
  if (r.is_open) {
    const b = document.createElement("span");
    b.className = "tag mine";
    b.textContent = "open";
    b.style.marginLeft = "7px";
    name.appendChild(b);
  }

  const cell = (txt, cls = "dim") => {
    const td = document.createElement("td");
    td.className = cls;
    td.textContent = txt === 0 ? "" : (txt || "");
    return td;
  };

  const state = document.createElement("td");
  state.className = "state";
  state.appendChild(statusChip(r));

  const act = document.createElement("td");
  act.className = "act";
  const btn = document.createElement("button");
  btn.className = "btn sm";
  btn.textContent = "Versions";
  btn.onclick = (e) => { e.stopPropagation(); openPanel(r.name); };
  act.appendChild(btn);

  tr.append(tick, name, cell(when(m.modified)), cell(m.timelines),
            cell(m.format), cell(m.frame_rate), state, act);
  tr.onclick = () => openPanel(r.name);
  return tr;
}

function statusChip(r) {
  const st = busy.has(r.name) ? "active" : r.status;
  const label = busy.has(r.name) ? "Backing up…" : r.status_label;
  const wrap = document.createElement("span");
  wrap.className = "state-chip s-" + st;
  wrap.innerHTML =
    `<span class="glyph">${STATUS_GLYPH[st] || "○"}</span>` +
    `<span>${esc(label)}</span>`;
  // Resolve's own idiom: when someone else has changes, a refresh glyph next to
  // their initials pulls them. The same gesture editors already know.
  if (r.status === "incoming") {
    const chip = document.createElement("span");
    chip.className = "who";
    chip.style.marginLeft = "6px";
    chip.textContent = initials(view.overview?.author);
    const pull = document.createElement("button");
    pull.className = "pullglyph";
    pull.title = "Get the newer version";
    pull.textContent = "⟳";
    pull.onclick = (e) => { e.stopPropagation(); quickPull(r.name, r.staged_version); };
    wrap.append(chip, pull);
  }
  return wrap;
}

function posterHue(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return h % 360;
}

function gridOf(rows) {
  const g = document.createElement("div");
  g.className = "grid";
  for (const r of rows) {
    const m = r.meta || {};
    const c = document.createElement("div");
    c.className = "card" + (view.selected.has(r.name) ? " sel" : "");
    const hue = posterHue(r.name);
    // The poster tile is the immediate, always-correct background. A real
    // frame fades in over it if one can be produced — so the grid is never
    // blank, never janky, and degrades gracefully without ffmpeg.
    const poster =
      `<div class="thumb" style="background:` +
      `linear-gradient(140deg, hsl(${hue} 32% 24%), hsl(${hue} 40% 12%))">` +
      `<span class="poster-init">${esc(initials(r.name))}</span>` +
      (m.format ? `<span class="poster-fmt">${esc(m.format.split("  ")[1] || m.format)}` +
                  `${m.fps ? " · " + esc(String(m.fps)) : ""}</span>` : "") +
      `</div>`;
    c.innerHTML = poster + `<div class="meta">` +
      `<div class="cn">${esc(r.name)}</div>` +
      `<div class="cs">${esc(m.format || "")}${m.frame_rate ? " · " + esc(m.frame_rate) : ""}</div>` +
      `<div class="cs">${esc(r.status_label)}</div></div>`;
    c.onclick = () => openPanel(r.name);
    g.appendChild(c);
    if (view.overview?.thumbs_available && r.in_resolve) queueThumb(c, r.name);
  }
  return g;
}

// Each thumbnail costs one ffmpeg run server-side, so they load a few at a
// time, nearest-the-viewport first. Deliberately NOT IntersectionObserver:
// that requires the page to be compositing, which fails in a background or
// headless window — and silently loading nothing is the worst outcome.
const thumbSeen = new Set();      // generated OK at least once
const thumbMissing = new Set();   // no clip on this machine; don't retry
let thumbQueue = [];
let thumbActive = 0;
const THUMB_PARALLEL = 3;

function queueThumb(card, name) {
  const thumb = card.querySelector(".thumb");
  if (!thumb || thumbMissing.has(name)) return;
  if (thumbSeen.has(name)) return applyThumb(thumb, name);   // cached: instant
  thumbQueue.push([thumb, name, card]);
  if (!queueThumb._pump) {
    // Collect one render's worth of cards, then order by what's on screen.
    queueThumb._pump = setTimeout(() => { queueThumb._pump = null; pumpThumbs(); }, 60);
  }
}

function pumpThumbs() {
  if (thumbQueue.length > 1) {
    thumbQueue.sort((a, b) => {
      const ay = a[2].getBoundingClientRect().top;
      const by = b[2].getBoundingClientRect().top;
      return Math.abs(ay) - Math.abs(by);       // nearest the viewport first
    });
  }
  while (thumbActive < THUMB_PARALLEL && thumbQueue.length) {
    const [thumb, name] = thumbQueue.shift();
    thumbActive++;
    loadThumb(thumb, name).finally(() => { thumbActive--; pumpThumbs(); });
  }
}

function applyThumb(thumb, name) {
  const url = "/api/thumb?project=" + encodeURIComponent(name);
  thumb.style.backgroundImage = `url("${url}")`;
  thumb.style.backgroundSize = "cover";
  thumb.style.backgroundPosition = "center";
  thumb.classList.add("has-frame");
}

function loadThumb(thumb, name) {
  const url = "/api/thumb?project=" + encodeURIComponent(name);
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => { thumbSeen.add(name); applyThumb(thumb, name); resolve(); };
    img.onerror = () => {
      // 404 means no usable clip is on this machine (offline drive, audio-only
      // project). Remember it so we don't re-request on every re-render.
      thumbMissing.add(name);
      resolve();
    };
    img.src = url;
  });
}

function updateSelInfo() {
  const n = view.selected.size;
  $("selInfo").textContent = n ? `${n} selected` : "";
  $("btnBackup").disabled = n === 0;
  $("btnBackup").textContent = n > 1 ? `Back up ${n} projects` : "Back up selected";
}

// --------------------------------------------------------------- panel --
async function openPanel(name) {
  view.panelProject = name;
  $("panel").classList.remove("hidden");
  $("panelTitle").textContent = name;
  $("relinkSection").classList.add("hidden");
  const row = view.rows.find((r) => r.name === name);
  const m = row?.meta || {};
  const row_folder = row?.folder;
  const facts = [["Folder", row_folder == null ? null : folderLabel(row_folder)],
                 ["Format", m.format], ["Frame rate", m.frame_rate],
                 ["Timelines", m.timelines], ["Last modified", when(m.modified)],
                 ["Created", when(m.created)], ["Status", row?.status_label]]
    .filter(([, v]) => v !== undefined && v !== "" && v !== null && v !== 0);
  $("panelMeta").innerHTML =
    `<h3>Project</h3><div class="muted" style="font-size:12px;line-height:1.7">` +
    facts.map(([k, v]) => `${esc(k)}: <span style="color:var(--text)">${esc(v)}</span>`)
         .join("<br>") + "</div>";
  $("versions").innerHTML = '<p class="muted">Loading…</p>';
  try {
    const d = await api("/api/versions?project=" + encodeURIComponent(name));
    renderVersions(d);
  } catch (e) {
    $("versions").innerHTML = `<p class="muted">${esc(e.message)}</p>`;
  }
}

function renderVersions(d) {
  const box = $("versions");
  box.innerHTML = "";
  if (d.forked) {
    const w = document.createElement("div");
    w.className = "check warn";
    w.innerHTML = `<span class="txt">This project has ${d.heads.length} different
      latest versions.<span class="sub">Two computers saved from the same point.
      Choose which one to continue from.</span></span>`;
    box.appendChild(w);
  }
  if (!d.versions.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No versions yet. Press Back up to save one.";
    box.appendChild(p);
    return;
  }
  for (const v of d.versions) {
    const el = document.createElement("div");
    el.className = "ver";
    const tags = [];
    if ((d.heads || []).includes(v.version_id)) tags.push('<span class="tag head">latest</span>');
    if (v.version_id === d.parent) tags.push('<span class="tag mine">you have this</span>');
    if (v.media?.length) tags.push(`<span class="tag">${v.media.length} media</span>`);
    el.innerHTML =
      `<div class="vm">${esc(v.message || "(no note)")}</div>` +
      `<div class="vs">${esc(v.author || "?")} · ${when(v.created)}</div>` +
      `<div class="vb">${tags.join("")}</div>`;
    const b = document.createElement("button");
    b.className = "btn sm";
    b.textContent = "Restore";
    b.style.marginLeft = "auto";
    b.onclick = () => doPull(d.project, v.version_id);
    el.querySelector(".vb").appendChild(b);
    box.appendChild(el);
  }
}

// -------------------------------------------------------------- actions --
async function backup(names, note, allowFork = false) {
  if (!names.length) return;
  $("btnBackup").disabled = true;
  names.forEach((n) => busy.add(n));
  renderProjects();
  renderAll();
  try {
    const r = await api("/api/push", {
      method: "POST",
      body: JSON.stringify({ projects: names, message: note || "", allow_fork: allowFork }),
    });
    if (r.results) {
      const bad = r.results.filter((x) => !x.ok);
      toast(`${r.pushed} of ${r.total} backed up` +
        (bad.length ? ` · ${bad.length} need attention` : ""), bad.length ? "" : "ok");
    } else {
      toast("Backed up", "ok");
    }
    $("note").value = "";
    await loadOverview();
    if (view.panelProject) openPanel(view.panelProject);
  } catch (e) {
    if (e.status === 409) {
      const keep = confirm(e.message + "\n\nKeep your version on a separate branch instead?");
      if (keep) return backup(names, note, true);
      toast("Nothing was overwritten", "");
    } else {
      toast(e.message, "err");
    }
  } finally {
    names.forEach((n) => busy.delete(n));
    renderProjects();
    updateSelInfo();
  }
}

async function doPull(project, versionId) {
  try {
    toast("Restoring and reconnecting media…");
    const r = await api("/api/pull", {
      method: "POST",
      body: JSON.stringify({ project, version_id: versionId,
                             import_as: `${project} (restored)` }),
    });
    renderRelink(r.relink);
    const rl = r.relink || {};
    toast(`Restored · ${rl.found || 0} media reconnected` +
      (rl.missing_count ? `, ${rl.missing_count} missing` : "") +
      (rl.unverified_count ? `, ${rl.unverified_count} to confirm` : ""),
      (rl.missing_count || rl.unverified_count) ? "" : "ok");
    loadOverview();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function quickPull(project, versionId) {
  // Auto-pull tells the overview which version it staged; when it has, the
  // /api/versions round-trip (seconds on the git/drive backends) is skipped.
  try {
    if (!versionId) {
      const d = await api("/api/versions?project=" + encodeURIComponent(project));
      versionId = d.head;
    }
    if (versionId) doPull(project, versionId);
  } catch (e) { toast(e.message, "err"); }
}

function renderRelink(rl) {
  if (!rl) return;
  $("relinkSection").classList.remove("hidden");
  const box = $("relinkBody");
  box.innerHTML =
    `<div class="muted" style="font-size:12px;margin-bottom:8px">` +
    `${rl.found} reconnected · ${rl.missing_count} missing · ` +
    `${rl.unverified_count || 0} to confirm</div>`;
  for (const u of rl.unverified || []) {
    const d = document.createElement("div");
    d.className = "check warn";
    d.innerHTML = `<span class="txt">${esc(u.clip)}<span class="sub">${esc(u.reason)}.
      Not linked automatically — a matching name and size alone isn't proof.</span></span>`;
    box.appendChild(d);
  }
  for (const name of rl.missing || []) {
    const d = document.createElement("div");
    d.className = "check bad";
    d.innerHTML = `<span class="txt">${esc(name)}<span class="sub">Not found on this computer</span></span>`;
    box.appendChild(d);
  }
}

// ------------------------------------------------------------ settings --
async function openSettings() {
  try {
    const s = await api("/api/status");
    const a = await api("/api/autosync");
    $("setAuthor").value = s.config.author || "";
    $("setRoots").value = (s.config.media_roots || []).join("\n");
    $("setAuto").checked = !!a.enabled;
    const secs = String(a.interval || 90);
    if (![...$("setInterval").options].some((o) => o.value === secs)) {
      const o = document.createElement("option");
      o.value = secs;
      o.textContent = `${secs} seconds`;
      $("setInterval").insertBefore(o, $("setInterval").firstChild);
    }
    $("setInterval").value = secs;
    $("autoHint").textContent = (a.watching || []).length
      ? `Watching ${a.watching.length} project(s).`
      : "Tick projects in the list and back them up once — they'll be watched after that.";
    const [label] = LIB_LABEL[s.config.backend] || ["Project Library"];
    $("libSummary").textContent =
      `${label} — ${s.config.git_remote || s.config.store_path || "not set"}`;
    $("settings").classList.remove("hidden");
  } catch (e) { toast(e.message, "err"); }
}

async function saveSettings() {
  try {
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify({
        author: $("setAuthor").value.trim(),
        media_roots: $("setRoots").value.split("\n").map((s) => s.trim()).filter(Boolean),
      }),
    });
    await api("/api/autosync", {
      method: "POST",
      body: JSON.stringify({ enabled: $("setAuto").checked,
                             interval: Number($("setInterval").value) }),
    });
    $("settings").classList.add("hidden");
    toast("Settings saved", "ok");
    loadOverview();
  } catch (e) { toast(e.message, "err"); }
}

// --------------------------------------------------------------- wiring --
$("filter").oninput = renderProjects;
$("btnAll").onclick = () => {
  visibleRows().forEach((r) => view.selected.add(r.name));
  renderProjects(); updateSelInfo();
};
$("btnNone").onclick = () => { view.selected.clear(); renderProjects(); updateSelInfo(); };
$("btnOpen").onclick = () => {
  if (!view.current) return toast("No project open in Resolve", "err");
  view.selected = new Set([view.current]);
  renderProjects(); updateSelInfo();
};
$("viewList").onclick = () => {
  view.mode = "list";
  $("viewList").classList.add("active"); $("viewGrid").classList.remove("active");
  renderProjects();
};
$("viewGrid").onclick = () => {
  view.mode = "grid";
  $("viewGrid").classList.add("active"); $("viewList").classList.remove("active");
  renderProjects();
};
$("btnBackup").onclick = () => backup([...view.selected], $("note").value.trim());
$("panelBackup").onclick = () => view.panelProject && backup([view.panelProject], $("note").value.trim());
$("panelClose").onclick = () => { $("panel").classList.add("hidden"); view.panelProject = null; };
$("btnSettings").onclick = openSettings;
$("setClose").onclick = () => $("settings").classList.add("hidden");
$("setSave").onclick = saveSettings;

loadOverview();
setInterval(loadOverview, 12000);
