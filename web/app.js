import { createViewer } from "./viewer.js";
import { CELL_STEPS, formatCell } from "./grid.js";
import { createTransformPanel } from "./transform-panel.js";

const $ = (id) => document.getElementById(id);
const sidebar = $("sidebar");
const statusDot = $("statusDot");
const statusText = $("statusText");
const viewportHud = $("viewportHud");
const frustaToggle = $("frustaToggle");
const frustaCheckbox = $("frustaCheckbox");
const fileInput = $("fileInput");

const resetViewBtn = $("resetViewBtn");
const viewer = createViewer($("canvas"));
resetViewBtn.addEventListener("click", () => viewer.resetView());
// Splat or mesh, when a project has both.
const layerToggle = $("layerToggle");
let layerShown = "splat";
layerToggle.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-layer]");
  if (!b) return;
  layerShown = b.dataset.layer;
  viewer.setLayer(layerShown);
  layerToggle.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  renderHud();
});

const ALL_STAGES = ["frames", "poses", "mesh", "train", "export"];
const STAGE_LABEL = { frames: "Frames", poses: "Poses", mesh: "Mesh", train: "Train", export: "Export" };
// Mirrors server/pipeline.py stage_names(): poses always run; the mesh goes
// before training in "both".
function stagesFor(outputs) {
  return ALL_STAGES.filter((s) =>
    (s !== "mesh" || outputs === "mesh" || outputs === "both") &&
    ((s !== "train" && s !== "export") || outputs !== "mesh"));
}
const OUTPUT_LABEL = { splat: "splat", mesh: "mesh", both: "splat + mesh" };

let jobId = null;
let projectName = null; // the open project's name, for the delete confirmation
let es = null;
let selectedFile = null;
let objectUrl = null; // blob URL for selectedFile; revoked when it is replaced or cleared
let inputUrl = null; // the server's copy of the upload, for tabs that did not do the upload
let currentPanel = null; // 'idle' | 'running' | 'done' | 'error'
let els = {}; // cached refs into the currently-mounted panel
let seenFrames = new Set();
let lastSparseUrl = null;
let lastCheckpointUrl = null;
let checkpointCount = 0;
let lastCamerasCount = 0;
let hudCheckpoint = null; // the latest state.checkpoint, for the HUD
let splatCount = null; // splats in the scene the viewer is showing
let splatCountUrl = null; // the checkpoint URL splatCount belongs to
let splatCountKey = null; // last lookup made, so state events don't repeat it

frustaCheckbox.addEventListener("change", () => viewer.setFrustaVisible(frustaCheckbox.checked));
viewer.onLoadingChange = (on) => { $("sceneLoading").hidden = !on; };

// Grid and transforms, for finished projects. Edits save with the project a
// moment after the last change; nothing is baked into the downloads yet.
const viewStack = $("viewStack");
const gridCheckbox = $("gridCheckbox");
const gridSlider = $("gridSlider");
const gridValue = $("gridValue");
const transformBtn = $("transformBtn");
let transformApplied = false; // the open project's saved transform is on screen

function updateGrid() {
  const cell = CELL_STEPS[+gridSlider.value];
  gridValue.textContent = formatCell(cell, transformPanel.scaled);
  viewer.setGrid({ visible: gridCheckbox.checked, cell });
}
gridCheckbox.addEventListener("change", () => { updateGrid(); gridCheckbox.blur(); });
gridSlider.addEventListener("input", updateGrid);
gridSlider.addEventListener("change", () => gridSlider.blur()); // hand WASD back to the viewer

let saveTimer = null;
function currentTransform() {
  return { ...viewer.getTransform(), scaled: transformPanel.scaled };
}
function putTransform(id) {
  return fetch(`/api/jobs/${encodeURIComponent(id)}/transform`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(currentTransform()),
  }).then((r) => { if (!r.ok) throw new Error(`saving the transform failed (${r.status})`); });
}
function saveTransform() {
  clearTimeout(saveTimer);
  const id = jobId;
  saveTimer = setTimeout(() => {
    if (!id || id !== jobId) return;
    putTransform(id).catch((e) => console.error(e));
  }, 500);
}

const transformPanel = createTransformPanel($("transformPanel"), viewer, {
  onChange() { saveTransform(); updateGrid(); if (lastDoneState) updateUnityExport(lastDoneState); },
  onAnchorTab() { if (!gridCheckbox.checked) { gridCheckbox.checked = true; updateGrid(); } },
});
transformBtn.addEventListener("click", () => setTransformOpen(transformBtn.getAttribute("aria-expanded") !== "true"));

function setTransformOpen(open) {
  transformBtn.setAttribute("aria-expanded", String(open));
  transformBtn.classList.toggle("on", open);
  transformPanel.setOpen(open);
}

function hideViewTools() {
  setTransformOpen(false);
  viewStack.hidden = true;
  gridCheckbox.checked = false;
  transformPanel.scaled = false;
  transformApplied = false;
  updateGrid();
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function humanSize(bytes) {
  if (bytes == null) return "";
  const units = ["B", "KB", "MB", "GB"];
  let n = bytes, u = 0;
  while (n >= 1024 && u < units.length - 1) { n /= 1024; u++; }
  return `${n.toFixed(u === 0 ? 0 : 1)} ${units[u]}`;
}

// Prefer the local blob: the tab that uploaded already holds the bytes, so there
// is no reason to pull them back off the server. A reload or a second tab has
// only the job id, and falls back to the copy the job wrote to disk.
function videoSrc() {
  return objectUrl || inputUrl;
}

function videoPlayerHTML(src, meta) {
  return `
    <div class="video-preview">
      <video src="${src}" muted loop autoplay playsinline></video>
    </div>
    <div class="video-meta">${meta}</div>
  `;
}

function setStatusPill(stage) {
  statusText.textContent = (stage || "idle").toUpperCase();
  statusDot.className = "dot" + (
    stage === "done" ? " good" :
    stage === "error" || stage === "cancelled" ? " bad" :
    ALL_STAGES.includes(stage) ? " active" : ""
  );
}

// ---------------------------------------------------------------- mounting --
function mount(panel) {
  currentPanel = panel;
  if (panel === "idle") mountIdle();
  else if (panel === "running") mountRunning();
  else if (panel === "done") mountDone();
  else if (panel === "error") mountError();
}

function mountIdle() {
  sidebar.innerHTML = `
    <div class="eyebrow">new reconstruction</div>
    <div class="dropzone" id="dropzone" tabindex="0">
      <div class="glyph">⌁</div>
      <div class="hint">Drop a video, or click to browse</div>
      <div class="sub">MP4 · MOV · WEBM</div>
    </div>
    <a class="guide-link" href="/capture.html" target="_blank" rel="noopener">How to film a room that reconstructs well →</a>
    <div id="videoPreviewSlot"></div>
    <div class="length-warn" id="lengthWarn"></div>
    <div class="field">
      <label for="nameInput">Project name</label>
      <input type="text" id="nameInput" maxlength="80" placeholder="named after the video" />
    </div>
    <div class="field">
      <label for="outputSelect">Output</label>
      <select id="outputSelect">
        <option value="splat" selected>Gaussian splat: photoreal</option>
        <option value="mesh">Mesh: textured, ~3 min</option>
        <option value="both">Both: splat + aligned mesh</option>
      </select>
    </div>
    <div class="field">
      <label>Preset</label>
      <select id="presetSelect">
        <option value="preview">Preview — ~8 min</option>
        <option value="high" selected>High — ~14 min</option>
        <option value="max">Max — ~1 h</option>
      </select>
    </div>
    <div class="field">
      <label>Pose backend</label>
      <select id="poseSelect">
        <option value="colmap" selected>COLMAP — best quality</option>
        <option value="da3">Depth Anything 3 — fast, experimental</option>
      </select>
    </div>
    <button class="btn btn-primary" id="startBtn" disabled>Start reconstruction</button>
    <div class="inline-msg" id="startMsg"></div>
    <div id="projectsSlot"></div>
  `;
  els = {
    dropzone: $("dropzone"),
    previewSlot: $("videoPreviewSlot"),
    lengthWarn: $("lengthWarn"),
    nameInput: $("nameInput"),
    projectsSlot: $("projectsSlot"),
    presetSelect: $("presetSelect"),
    outputSelect: $("outputSelect"),
    poseSelect: $("poseSelect"),
    startBtn: $("startBtn"),
    startMsg: $("startMsg"),
  };
  els.dropzone.addEventListener("click", () => fileInput.click());
  els.dropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") fileInput.click(); });
  els.dropzone.addEventListener("dragover", (e) => { e.preventDefault(); els.dropzone.classList.add("drag"); });
  els.dropzone.addEventListener("dragleave", () => els.dropzone.classList.remove("drag"));
  els.dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    els.dropzone.classList.remove("drag");
    if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
  });
  fileInput.onchange = () => { if (fileInput.files[0]) handleFile(fileInput.files[0]); };
  els.startBtn.addEventListener("click", startJob);
  els.presetSelect.addEventListener("change", updateLengthWarning);
  if (selectedFile) renderVideoPreview();
  loadProjects();
}

// Saved projects, newest first. Finished ones reopen in the viewer with their
// downloads; a running one reattaches to its live progress.
function projectStatus(p) {
  if (p.stage === "done") return "";
  if (p.stage === "cancelled") return "cancelled";
  if (p.stage !== "error") return "running";
  if (p.preset === "unknown") return "no project file";
  if ((p.error || "").startsWith("interrupted")) return "interrupted";
  return p.failed_stage ? `failed at ${STAGE_NAME[p.failed_stage] || p.failed_stage}` : "failed";
}

function projectMeta(p) {
  const parts = [];
  if (p.created) parts.push(new Date(p.created * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" }));
  if (p.preset !== "unknown") parts.push(p.preset);
  if (p.outputs && p.outputs !== "splat") parts.push(OUTPUT_LABEL[p.outputs] || p.outputs);
  if (p.gaussians) parts.push(`${(p.gaussians / 1e6).toFixed(2)}M splats`);
  else if (p.triangles) parts.push(`${(p.triangles / 1000).toFixed(0)}k triangles`);
  if (p.bytes != null) parts.push(humanSize(p.bytes));
  const status = projectStatus(p);
  if (status) parts.push(status);
  return parts.join(" · ");
}

function loadProjects() {
  fetch("/api/jobs")
    .then((r) => (r.ok ? r.json() : []))
    .then((list) => {
      if (currentPanel !== "idle") return;
      if (!list.length) { els.projectsSlot.innerHTML = ""; return; }
      els.projectsSlot.innerHTML = `
        <div class="field">
          <label>Past projects</label>
          <div class="project-list">
            ${list.map((p) => `
              <div class="project">
                <button class="project-open" data-id="${escapeHTML(p.id)}">
                  ${p.thumbnail ? `<img src="${escapeHTML(p.thumbnail)}" alt="" loading="lazy" />` : `<span class="thumb-empty"></span>`}
                  <span class="text">
                    <span class="name">${escapeHTML(p.name)}</span>
                    <span class="meta${p.stage === "done" ? "" : " bad"}">${escapeHTML(projectMeta(p))}</span>
                  </span>
                </button>
                <button class="project-delete" data-id="${escapeHTML(p.id)}" title="Delete project" aria-label="Delete ${escapeHTML(p.name)}">✕</button>
              </div>`).join("")}
          </div>
        </div>
      `;
      const byId = Object.fromEntries(list.map((p) => [p.id, p]));
      els.projectsSlot.querySelectorAll(".project-open").forEach((b) => {
        b.addEventListener("click", () => attach(b.dataset.id));
      });
      els.projectsSlot.querySelectorAll(".project-delete").forEach((b) => {
        const p = byId[b.dataset.id];
        b.addEventListener("click", () => deleteProject(p.id, p.name, p.bytes).then((ok) => ok && loadProjects()));
      });
    })
    .catch(() => {});
}

// Deletes the whole project folder — downloads included — after a confirm.
// Resolves true once it is gone.
async function deleteProject(id, name, bytes) {
  const size = bytes != null ? ` frees ${humanSize(bytes)} and` : "";
  if (!confirm(`Delete "${name}"?\n\nThis${size} removes its folder from disk, including the downloadable scene files. It can't be undone.`)) return false;
  const r = await fetch(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => null);
  if (r && r.ok) return true;
  const detail = r ? (await r.json().catch(() => ({}))).detail : null;
  alert(`Couldn't delete "${name}": ${detail || "the app didn't respond"}.`);
  return false;
}

function handleFile(file) {
  if (!file.type.startsWith("video/")) {
    els.startMsg.textContent = "That doesn't look like a video file.";
    return;
  }
  els.startMsg.textContent = "";
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  selectedFile = file;
  objectUrl = URL.createObjectURL(file);
  els.nameInput.placeholder = file.name.replace(/\.[^.]+$/, "");
  renderVideoPreview();
}

function renderVideoPreview() {
  els.previewSlot.innerHTML = videoPlayerHTML(
    videoSrc(),
    `<span>${selectedFile.name} · ${humanSize(selectedFile.size)}</span><button id="clearBtn">remove</button>`,
  );
  // The duration sets how many frames the preset will take.
  els.previewSlot.querySelector("video").addEventListener("loadedmetadata", (e) => {
    selectedDuration = Number.isFinite(e.target.duration) ? e.target.duration : null;
    updateLengthWarning();
  });
  $("clearBtn").addEventListener("click", () => {
    if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
    selectedFile = null;
    selectedDuration = null;
    els.previewSlot.innerHTML = "";
    els.nameInput.placeholder = "named after the video";
    els.startBtn.disabled = true;
    updateLengthWarning();
  });
  els.startBtn.disabled = false;
}

// Presets take one frame per `frame_spacing_s` of video (at least `min_frames`), so a
// longer video gets more frames rather than sparser ones. Mirrors server/presets.py
// frame_plan.
function framePlan(p, duration) {
  const count = Math.max(p.min_frames, Math.round(Math.max(duration, 0.1) / p.frame_spacing_s));
  return { count, spacing: duration / count };
}
let presetInfo = null; // GET /api/presets
let selectedDuration = null; // seconds, once the preview has read the metadata

fetch("/api/presets").then((r) => (r.ok ? r.json() : null)).then((p) => { presetInfo = p; updateLengthWarning(); }).catch(() => {});

function formatDuration(s) {
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return m ? `${m} min ${sec} s` : `${sec} s`;
}

function updateLengthWarning() {
  if (currentPanel !== "idle" || !els.lengthWarn) return;
  const preset = els.presetSelect.value;
  const info = presetInfo?.[preset];
  if (!selectedDuration || !info?.frame_spacing_s) { els.lengthWarn.innerHTML = ""; return; }
  const labelOf = (name) => [...els.presetSelect.options].find((o) => o.value === name)?.textContent.split(" —")[0] || name;
  const label = labelOf(preset);
  const fits = (p) => p.max_video_s == null || selectedDuration <= p.max_video_s;
  els.lengthWarn.classList.toggle("note", fits(info));
  if (!fits(info)) {
    const other = Object.entries(presetInfo).find(([name, p]) => name !== preset && fits(p));
    els.lengthWarn.innerHTML = `
      <p><b>This video is too long for ${escapeHTML(label)}.</b> It runs ${formatDuration(selectedDuration)}, and ${escapeHTML(label)} takes up to ${formatDuration(info.max_video_s)}.</p>
      <p>${other ? `The ${escapeHTML(labelOf(other[0]))} preset takes it. Or t` : "T"}rim it to the best 2–3 minutes, or film one video per area.
      <a class="guide-link" href="/capture.html" target="_blank" rel="noopener">Capture guide →</a></p>
    `;
    return;
  }
  const { count, spacing } = framePlan(info, selectedDuration);
  els.lengthWarn.innerHTML = `<p>${escapeHTML(label)} will use about <b>${count} frames</b> from this ${formatDuration(selectedDuration)} video, one every ${spacing.toFixed(1)} s.</p>`;
}

function mountRunning() {
  sidebar.innerHTML = `
    <div class="eyebrow">reconstruction in progress</div>
    <div id="sourceSlot"></div>
    <div class="stage-tracker" id="stageTracker"></div>
    <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
    <div class="status-msg" id="statusMsg">—</div>
    <div id="filmstripSlot"></div>
    <hr class="hr" />
    <button class="btn" id="closeBtn">Back to projects</button>
    <button class="btn btn-danger" id="cancelBtn">Cancel job</button>
  `;
  // Leaves the job running: it stays in the project list, and reopening it
  // reattaches to its live progress.
  $("closeBtn").addEventListener("click", resetToIdle);
  els = {
    sourceSlot: $("sourceSlot"),
    tracker: $("stageTracker"),
    progressFill: $("progressFill"),
    statusMsg: $("statusMsg"),
    filmstripSlot: $("filmstripSlot"),
    cancelBtn: $("cancelBtn"),
  };
  renderTracker("splat"); // until the job's state says otherwise
  els.cancelBtn.addEventListener("click", () => {
    els.cancelBtn.disabled = true;
    els.cancelBtn.textContent = "Cancelling…";
    fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" }).catch(() => {});
  });
  frustaToggle.hidden = false;
  resetViewBtn.hidden = false;
}

function mountDone() {
  sidebar.innerHTML = `
    <div class="eyebrow">reconstruction complete</div>
    <h2 class="panel-title" id="doneTitle">Scene ready</h2>
    <div class="artifact-list" id="artifactList"></div>
    <div class="mesh-note" id="meshNote"></div>
    <div class="disk-note" id="diskNote"></div>
    <button class="btn" id="cleanBtn" hidden></button>
    <hr class="hr" />
    <div class="unity-export">
      <div class="eyebrow">unity export</div>
      <div class="unity-status" id="unityStatus"></div>
      <button class="btn" id="unityBtn">Export for Unity</button>
    </div>
    <hr class="hr" />
    ${projectButtonsHTML()}
  `;
  els = { artifactList: $("artifactList"), title: $("doneTitle"), meshNote: $("meshNote"), diskNote: $("diskNote"), cleanBtn: $("cleanBtn"),
          unityStatus: $("unityStatus"), unityBtn: $("unityBtn") };
  els.unityBtn.addEventListener("click", startUnityExport);
  wireProjectButtons();
  // A finished room opens from inside, among the capture cameras, where their
  // frusta are clutter; they stay one click away.
  frustaCheckbox.checked = false;
  viewer.setFrustaVisible(false);
  viewStack.hidden = false;
  els.cleanBtn.addEventListener("click", cleanProject);
  loadDisk();
}

// Disk use for the open project, and the clean-up offer when there's
// working data worth removing (training snapshots dominate: ~5 GB a run).
const CLEAN_THRESHOLD = 10 * 1024 * 1024;

async function loadDisk() {
  const id = jobId;
  const disk = await fetch(`/api/jobs/${encodeURIComponent(id)}/disk`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (!disk || id !== jobId || currentPanel !== "done") return;
  els.diskNote.textContent = `Using ${humanSize(disk.bytes)} on disk.`;
  els.cleanBtn.hidden = disk.reclaimable < CLEAN_THRESHOLD;
  els.cleanBtn.textContent = `Clean up working files · frees ${humanSize(disk.reclaimable)}`;
  els.cleanBtn.dataset.reclaimable = disk.reclaimable;
}

async function cleanProject() {
  const freeing = humanSize(Number(els.cleanBtn.dataset.reclaimable));
  const name = projectName || "this project";
  if (!confirm(`Clean up "${name}"?\n\nThis frees ${freeing} by removing training snapshots, the camera-solving data and spare frames. The scene, the downloads and the source video stay, so it opens exactly as before. It can't be retrained without starting again from the video.`)) return;
  els.cleanBtn.disabled = true;
  els.cleanBtn.textContent = "Cleaning up…";
  const r = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/clean`, { method: "POST" }).catch(() => null);
  if (!r || !r.ok) {
    const detail = r ? (await r.json().catch(() => ({}))).detail : null;
    alert(`Couldn't clean up "${name}": ${detail || "the app didn't respond"}.`);
    els.cleanBtn.disabled = false;
    loadDisk();
    return;
  }
  const { freed, bytes } = await r.json();
  els.cleanBtn.hidden = true;
  els.diskNote.textContent = `Cleaned up: freed ${humanSize(freed)}. Now using ${humanSize(bytes)} on disk.`;
}

function mountError() {
  sidebar.innerHTML = `
    <div class="eyebrow" id="errorEyebrow">error</div>
    <h2 class="panel-title" id="errorTitle">Something went wrong</h2>
    <div class="failure" id="errorMsg"></div>
    <a class="guide-link" id="reportLink" download hidden>Download the debug report →</a>
    <hr class="hr" />
    ${projectButtonsHTML()}
  `;
  els = { title: $("errorTitle"), msg: $("errorMsg"), eyebrow: $("errorEyebrow"), reportLink: $("reportLink") };
  wireProjectButtons();
}

// Close and Delete, shared by the finished and failed panels.
function projectButtonsHTML() {
  return `
    <button class="btn" id="closeBtn">Close project</button>
    <button class="btn btn-danger" id="deleteBtn">Delete project</button>
  `;
}

function wireProjectButtons() {
  $("closeBtn").addEventListener("click", resetToIdle);
  $("deleteBtn").addEventListener("click", async () => {
    if (await deleteProject(jobId, projectName || "this project", null)) resetToIdle();
  });
}

// ------------------------------------------------------------- updating ----
// The footage stays on screen for the whole run, so the splat resolving in the
// viewport can be read against what it was reconstructed from. Mounted once and
// then left alone — rebuilding it on every state event would restart playback
// twice a second.
function renderSource() {
  const src = videoSrc();
  if (!src || !els.sourceSlot || els.sourceSlot.querySelector("video")) return;
  els.sourceSlot.innerHTML = videoPlayerHTML(src, "<span>source footage</span>");
}

function renderTracker(outputs) {
  els.tracker.dataset.outputs = outputs;
  els.tracker.innerHTML = stagesFor(outputs).map((s) => `
    <div class="stage-row" data-stage="${s}">
      <span class="dot"></span>
      <span class="label">${STAGE_LABEL[s]}</span>
      <span class="detail" data-detail="${s}"></span>
    </div>`).join("");
}

function updateRunning(state) {
  renderSource();

  const outputs = state.outputs || "splat";
  if (els.tracker.dataset.outputs !== outputs) renderTracker(outputs);
  const stages = stagesFor(outputs);
  const idx = stages.indexOf(state.stage);
  els.tracker.querySelectorAll(".stage-row").forEach((row) => {
    const s = row.dataset.stage;
    const i = stages.indexOf(s);
    row.classList.toggle("done", idx > i || state.stage === "done");
    row.classList.toggle("active", idx === i);
  });
  const detail = els.tracker.querySelector('[data-detail="frames"]');
  if (state.frames?.count) detail.textContent = `${state.frames.count}`;
  const meshDetail = els.tracker.querySelector('[data-detail="mesh"]');
  if (meshDetail && state.mesh) meshDetail.textContent = `${(state.mesh.triangles / 1000).toFixed(0)}k tris`;
  if (meshDetail && state.mesh_error) meshDetail.textContent = "failed";
  const trainDetail = els.tracker.querySelector('[data-detail="train"]');
  if (trainDetail && state.checkpoint) trainDetail.textContent = `${state.checkpoint.step.toLocaleString()} / ${state.checkpoint.total_steps.toLocaleString()}`;

  els.progressFill.style.width = `${Math.round((state.progress || 0) * 100)}%`;
  els.statusMsg.textContent = state.message || "";

  const samples = state.frames?.sample || [];
  if (samples.length) {
    if (!els.filmstripSlot.querySelector(".filmstrip")) {
      els.filmstripSlot.innerHTML = `<div class="filmstrip" id="filmstrip"></div>`;
      els.filmstrip = $("filmstrip");
    }
    for (const src of samples) {
      if (seenFrames.has(src)) continue;
      seenFrames.add(src);
      const img = document.createElement("img");
      img.src = src;
      img.loading = "lazy";
      els.filmstrip.appendChild(img);
    }
  }
}

// --- Unity export ---------------------------------------------------------
// Files baked from the saved transform (server/bake.py). The status compares
// what was baked with what the viewer shows now, so later edits read as
// "out of date".
let lastDoneState = null;
let unityPoll = null;

function sameTransform(a, b) {
  const key = (t) => JSON.stringify(t && ["assets", "anchor", "level_up", "scaled"].map((k) => t[k]),
    (_, v) => (typeof v === "number" ? Math.round(v * 1e5) / 1e5 : v));
  return !!a && !!b && key(a) === key(b);
}

function updateUnityExport(state) {
  if (!els.unityStatus) return;
  const ux = state.unity_export;
  const when = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  let text;
  if (ux?.status === "running") text = "Exporting… a minute or two for a big room.";
  else if (ux?.status === "error") text = `<b>Export failed:</b> ${escapeHTML(ux.error || "unknown error")}`;
  else if (ux?.status === "done") {
    text = sameTransform(ux.transform, currentTransform())
      ? `Exported at ${when(ux.at)}; matches the current transform. Files are in the downloads above.`
      : `<b>Out of date:</b> the transform has changed since the export at ${when(ux.at)}.`;
  } else text = "Writes the splat and mesh upright, with the anchor as the origin. The originals above stay as they are.";
  if (!transformPanel.scaled && ux?.status !== "running") text += " <b>Scale not measured:</b> units aren't metres yet (transform → measure).";
  els.unityStatus.innerHTML = text;
  els.unityBtn.disabled = ux?.status === "running";
  if (ux?.status === "running" && !unityPoll) pollUnityExport();
}

function pollUnityExport() {
  if (unityPoll) return;
  const id = jobId;
  unityPoll = setInterval(async () => {
    if (id !== jobId) { clearInterval(unityPoll); unityPoll = null; return; }
    const state = await fetch(`/api/jobs/${encodeURIComponent(id)}`).then((r) => r.json()).catch(() => null);
    if (!state) return;
    render(state);
    if (state.unity_export?.status !== "running") { clearInterval(unityPoll); unityPoll = null; }
  }, 2000);
}

async function startUnityExport() {
  const id = jobId;
  els.unityBtn.disabled = true;
  clearTimeout(saveTimer);
  try {
    await putTransform(id); // export exactly what's on screen
    const r = await fetch(`/api/jobs/${encodeURIComponent(id)}/unity-export`, { method: "POST" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `export failed (${r.status})`);
  } catch (e) {
    els.unityStatus.innerHTML = `<b>Couldn't export:</b> ${escapeHTML(e.message)}`;
    els.unityBtn.disabled = false;
    return;
  }
  els.unityStatus.textContent = "Exporting… a minute or two for a big room.";
  pollUnityExport();
}

function updateDone(state) {
  lastDoneState = state;
  updateUnityExport(state);
  if (state.name) els.title.textContent = state.name;
  els.artifactList.innerHTML = (state.artifacts || []).map((a) => `
    <div class="artifact">
      <div><div class="name">${a.name}</div><div class="size">${a.gaussians ? `${(a.gaussians / 1e6).toFixed(2)}M splats · ` : ""}${a.triangles ? `${(a.triangles / 1000).toFixed(0)}k triangles · ` : ""}${humanSize(a.bytes)}</div></div>
      <a class="btn" href="${a.url}" download>Download</a>
    </div>
  `).join("") || `<div class="center-note">No artifacts listed.</div>`;
  els.meshNote.innerHTML = meshNoteHTML(state);
}

// A mesh that failed alongside a good splat ("both"), or one built from few of
// the frames (Object Capture skips frames it can't use, e.g. glass rooms).
function meshNoteHTML(state) {
  if (state.mesh_failure) {
    return `<b>The mesh couldn't be built:</b> ${escapeHTML(state.mesh_failure.title)}. The splat is unaffected.
      ${state.mesh_failure.capture_guide ? `<a class="guide-link" href="/capture.html" target="_blank" rel="noopener">Capture guide →</a>` : ""}`;
  }
  const m = state.mesh;
  if (m && m.frames_total && m.frames_used / m.frames_total < 0.7) {
    return `<b>The mesh used ${m.frames_used} of ${m.frames_total} frames,</b> so it may be incomplete.
      Object Capture skips frames it can't place; glass and plain close-ups are the usual cause.`;
  }
  return "";
}

const STAGE_NAME = { frames: "frames", poses: "camera positions", mesh: "mesh", train: "training", export: "export" };

function updateError(state) {
  const cancelled = state.stage === "cancelled";
  const f = state.failure;
  const stage = STAGE_NAME[state.failed_stage] || state.failed_stage;
  els.eyebrow.textContent = cancelled ? "cancelled"
    : !stage ? "reconstruction failed"
    : (state.error || "").startsWith("interrupted") ? `interrupted during ${stage}`
    : `failed at ${stage}`;
  els.title.textContent = state.name || (cancelled ? "Job cancelled" : "Reconstruction failed");
  // Timings, memory and settings up to the point it stopped.
  const reportFile = (state.artifacts || []).find((a) => a.name === "debug-report.md");
  els.reportLink.hidden = !reportFile;
  if (reportFile) els.reportLink.href = reportFile.url;

  // Rebuilt only when the content changes, so an open "technical details" stays open.
  const raw = state.error || state.message || "";
  const key = JSON.stringify([cancelled, f, raw]);
  if (els.msg.dataset.key === key) return;
  els.msg.dataset.key = key;
  if (cancelled) {
    els.msg.innerHTML = `<p class="detail">The job was cancelled.</p>`;
  } else if (f) {
    els.msg.innerHTML = `
      <p class="headline">${escapeHTML(f.title)}</p>
      <p class="detail">${escapeHTML(f.detail)}</p>
      ${f.tips?.length ? `<div class="tips-label">Next time</div><ul class="tips">${f.tips.map((t) => `<li>${escapeHTML(t)}</li>`).join("")}</ul>` : ""}
      ${f.capture_guide ? `<a class="guide-link" href="/capture.html" target="_blank" rel="noopener">Read the capture guide →</a>` : ""}
      ${raw && raw !== f.detail ? `<details><summary>Technical details</summary><pre>${escapeHTML(raw)}</pre></details>` : ""}
    `;
  } else {
    // A project saved before failures were explained: show what there is.
    els.msg.innerHTML = `<p class="detail">${escapeHTML(raw || "An unknown error occurred.")}</p>`;
  }
}

function updateViewer(state) {
  // Before setCameras(): the opening view is framed through the transform.
  if (currentPanel === "done" && !transformApplied) {
    transformApplied = true;
    viewer.setTransform(state.transform || null);
    transformPanel.scaled = !!state.transform?.scaled;
    updateGrid();
  }
  if (state.sparse_url && state.sparse_url !== lastSparseUrl) {
    lastSparseUrl = state.sparse_url;
    viewer.loadSparse(state.sparse_url);
  }
  if (state.cameras && state.cameras.length && state.cameras.length !== lastCamerasCount) {
    lastCamerasCount = state.cameras.length;
    viewer.setCameras(state.cameras);
  }
  if (state.checkpoint && state.checkpoint.url !== lastCheckpointUrl) {
    lastCheckpointUrl = state.checkpoint.url;
    checkpointCount++;
    viewer.loadCheckpoint(state.checkpoint.url);
  }
  if (state.mesh && state.mesh.url) viewer.loadMesh(state.mesh.url).then(renderHud);
  // The splat/mesh switch, once both exist.
  layerToggle.hidden = !(state.mesh && state.checkpoint);
  hudCheckpoint = state.checkpoint || null;
  // At most two lookups per file: when it appears, and again once the export
  // stage's measurements land.
  const countKey = state.checkpoint && `${state.checkpoint.url}|${state.artifacts ? 1 : 0}`;
  if (countKey && countKey !== splatCountKey) {
    splatCountKey = countKey;
    updateSplatCount(state);
  }
  renderHud();
}

// The count of the file on screen. The viewer itself can't say: it streams the
// scene in levels of detail, so its own count starts at 0 and then tracks only
// what is currently drawn. Finished scenes use the export stage's measurement
// (it lands a beat after the viewer file does, hence the retry on later
// states); training checkpoints are PLYs, whose header states the count.
async function updateSplatCount(state) {
  const url = state.checkpoint.url;
  if (splatCountUrl !== url) { splatCountUrl = url; splatCount = null; }
  const name = url.split("/").pop();
  let count = (state.artifacts || []).find((a) => a.name === name)?.gaussians ?? null;
  if (count == null && name.endsWith(".ply")) count = await plyVertexCount(url);
  if (splatCountUrl !== url || count == null) return;
  splatCount = count;
  renderHud();
}

// Reads just the header: the first chunk of the response, then cancels the rest.
async function plyVertexCount(url) {
  try {
    const r = await fetch(url, { headers: { Range: "bytes=0-4095" } });
    const reader = r.body.getReader();
    const { value } = await reader.read();
    reader.cancel().catch(() => {});
    const m = new TextDecoder().decode(value).match(/element vertex (\d+)/);
    return m ? Number(m[1]) : null;
  } catch {
    return null;
  }
}

function renderHud() {
  const triangles = viewer.meshTriangles;
  const meshShown = triangles != null && (layerShown === "mesh" || !hudCheckpoint);
  const c = meshShown ? null : hudCheckpoint;
  viewportHud.innerHTML = [
    meshShown && `<div class="line">mesh <b>${triangles.toLocaleString()}</b> triangles</div>`,
    c && `<div class="line">checkpoint <b>${checkpointCount}</b></div>`,
    c && `<div class="line">step <b>${c.step.toLocaleString()}</b> / ${c.total_steps.toLocaleString()}</div>`,
    c && splatCount != null && `<div class="line">splats <b>${splatCount.toLocaleString()}</b></div>`,
  ].filter(Boolean).join("");
}

// --------------------------------------------------------------- driving ---
function render(state) {
  setStatusPill(state.stage);
  if (state.input_url) inputUrl = state.input_url;
  if (state.name) projectName = state.name;
  const panel = state.stage === "done" ? "done" : state.stage === "error" || state.stage === "cancelled" ? "error" : "running";
  if (panel !== currentPanel) mount(panel);
  if (panel === "running") updateRunning(state);
  else if (panel === "done") updateDone(state);
  else updateError(state);
  updateViewer(state);
  if (["done", "error", "cancelled"].includes(state.stage) && es) { es.close(); es = null; }
}

function connectEvents(id) {
  if (es) es.close();
  es = new EventSource(`/api/jobs/${id}/events`);
  es.addEventListener("state", (e) => render(JSON.parse(e.data)));
  es.addEventListener("open", () => {
    fetch(`/api/jobs/${id}`).then((r) => r.json()).then(render).catch(() => {});
  });
  es.addEventListener("error", () => { /* browser retries automatically */ });
}

function startJob() {
  if (!selectedFile) return;
  // Refuse before uploading: the server would only say no after the whole video arrived.
  const limit = presetInfo?.[els.presetSelect.value]?.max_video_s;
  if (limit != null && selectedDuration > limit) {
    els.startMsg.textContent = "This video is too long for the chosen preset; see above.";
    return;
  }
  els.startBtn.disabled = true;
  els.startMsg.textContent = "";
  const form = new FormData();
  form.append("video", selectedFile);
  form.append("preset", els.presetSelect.value);
  form.append("pose_backend", els.poseSelect.value);
  form.append("outputs", els.outputSelect.value);
  form.append("name", els.nameInput.value);
  fetch("/api/jobs", { method: "POST", body: form })
    .then(async (r) => {
      if (r.status === 409) throw new Error("A job is already running.");
      if (r.status === 400) {
        const detail = (await r.json().catch(() => ({}))).detail;
        if (detail) throw new Error(`Couldn't start: ${detail}.`);
      }
      if (!r.ok) throw new Error(`Failed to start job (${r.status}).`);
      return r.json();
    })
    .then(({ job_id }) => {
      attach(job_id);
      setStatusPill("frames");
    })
    .catch((err) => {
      els.startBtn.disabled = false;
      els.startMsg.textContent = err.message;
    });
}

// Follow a job's state from scratch: a new upload, a saved project, or a run
// already in progress.
function attach(id) {
  jobId = id;
  projectName = null;
  sessionStorage.setItem("vts_job", id);
  seenFrames = new Set();
  lastSparseUrl = null;
  lastCheckpointUrl = null;
  checkpointCount = 0;
  lastCamerasCount = 0;
  mount("running");
  connectEvents(id);
}

function resetToIdle() {
  if (es) { es.close(); es = null; }
  sessionStorage.removeItem("vts_job");
  jobId = null;
  projectName = null;
  if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
  inputUrl = null;
  selectedFile = null;
  seenFrames = new Set();
  lastSparseUrl = null;
  lastCheckpointUrl = null;
  checkpointCount = 0;
  lastCamerasCount = 0;
  frustaToggle.hidden = true;
  resetViewBtn.hidden = true;
  layerToggle.hidden = true;
  hideViewTools();
  if (unityPoll) { clearInterval(unityPoll); unityPoll = null; }
  lastDoneState = null;
  layerShown = "splat";
  layerToggle.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x.dataset.layer === "splat"));
  hudCheckpoint = null;
  splatCount = null;
  splatCountUrl = null;
  splatCountKey = null;
  frustaCheckbox.checked = true;
  viewer.setFrustaVisible(true);
  viewportHud.innerHTML = "";
  viewer.reset();
  mount("idle");
  setStatusPill("idle");
}

// ------------------------------------------------------------------- init --
(async function init() {
  let saved = sessionStorage.getItem("vts_job");
  if (!saved) {
    // attach to a job started elsewhere (another tab, or via the API)
    try {
      const { job_id } = await (await fetch("/api/jobs/active")).json();
      if (job_id) saved = job_id;
    } catch { /* older server without the endpoint */ }
  }
  if (saved) {
    attach(saved);
  } else {
    mount("idle");
  }
})();
