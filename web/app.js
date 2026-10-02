import { createViewer } from "./viewer.js";

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

const STAGES = ["frames", "poses", "train", "export"];
const STAGE_LABEL = { frames: "Frames", poses: "Poses", train: "Train", export: "Export" };

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
    STAGES.includes(stage) ? " active" : ""
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
    <div class="field">
      <label for="nameInput">Project name</label>
      <input type="text" id="nameInput" maxlength="80" placeholder="named after the video" />
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
    nameInput: $("nameInput"),
    projectsSlot: $("projectsSlot"),
    presetSelect: $("presetSelect"),
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
  if (p.gaussians) parts.push(`${(p.gaussians / 1e6).toFixed(2)}M splats`);
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
  $("clearBtn").addEventListener("click", () => {
    if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
    selectedFile = null;
    els.previewSlot.innerHTML = "";
    els.nameInput.placeholder = "named after the video";
    els.startBtn.disabled = true;
  });
  els.startBtn.disabled = false;
}

function mountRunning() {
  sidebar.innerHTML = `
    <div class="eyebrow">reconstruction in progress</div>
    <div id="sourceSlot"></div>
    <div class="stage-tracker" id="stageTracker">
      ${STAGES.map((s) => `
        <div class="stage-row" data-stage="${s}">
          <span class="dot"></span>
          <span class="label">${STAGE_LABEL[s]}</span>
          <span class="detail" data-detail="${s}"></span>
        </div>`).join("")}
    </div>
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
    <hr class="hr" />
    ${projectButtonsHTML()}
  `;
  els = { artifactList: $("artifactList"), title: $("doneTitle") };
  wireProjectButtons();
}

function mountError() {
  sidebar.innerHTML = `
    <div class="eyebrow" id="errorEyebrow">error</div>
    <h2 class="panel-title" id="errorTitle">Something went wrong</h2>
    <div class="failure" id="errorMsg"></div>
    <hr class="hr" />
    ${projectButtonsHTML()}
  `;
  els = { title: $("errorTitle"), msg: $("errorMsg"), eyebrow: $("errorEyebrow") };
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

function updateRunning(state) {
  renderSource();

  const idx = STAGES.indexOf(state.stage);
  els.tracker.querySelectorAll(".stage-row").forEach((row) => {
    const s = row.dataset.stage;
    const i = STAGES.indexOf(s);
    row.classList.toggle("done", idx > i || state.stage === "done");
    row.classList.toggle("active", idx === i);
  });
  const detail = els.tracker.querySelector('[data-detail="frames"]');
  if (state.frames?.count) detail.textContent = `${state.frames.count}`;
  const trainDetail = els.tracker.querySelector('[data-detail="train"]');
  if (state.checkpoint) trainDetail.textContent = `${state.checkpoint.step.toLocaleString()} / ${state.checkpoint.total_steps.toLocaleString()}`;

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

function updateDone(state) {
  if (state.name) els.title.textContent = state.name;
  els.artifactList.innerHTML = (state.artifacts || []).map((a) => `
    <div class="artifact">
      <div><div class="name">${a.name}</div><div class="size">${a.gaussians ? `${(a.gaussians / 1e6).toFixed(2)}M splats · ` : ""}${humanSize(a.bytes)}</div></div>
      <a class="btn" href="${a.url}" download>Download</a>
    </div>
  `).join("") || `<div class="center-note">No artifacts listed.</div>`;
}

const STAGE_NAME = { frames: "frames", poses: "camera positions", train: "training", export: "export" };

function updateError(state) {
  const cancelled = state.stage === "cancelled";
  const f = state.failure;
  const stage = STAGE_NAME[state.failed_stage] || state.failed_stage;
  els.eyebrow.textContent = cancelled ? "cancelled"
    : !stage ? "reconstruction failed"
    : (state.error || "").startsWith("interrupted") ? `interrupted during ${stage}`
    : `failed at ${stage}`;
  els.title.textContent = state.name || (cancelled ? "Job cancelled" : "Reconstruction failed");

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
  const c = hudCheckpoint;
  viewportHud.innerHTML = [
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
  els.startBtn.disabled = true;
  els.startMsg.textContent = "";
  const form = new FormData();
  form.append("video", selectedFile);
  form.append("preset", els.presetSelect.value);
  form.append("pose_backend", els.poseSelect.value);
  form.append("name", els.nameInput.value);
  fetch("/api/jobs", { method: "POST", body: form })
    .then(async (r) => {
      if (r.status === 409) throw new Error("A job is already running.");
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
