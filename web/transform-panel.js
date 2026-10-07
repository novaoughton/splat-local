// The transform panel: number fields and drag-handle modes for the assets (the
// splat and mesh, moved together) and the anchor (the grid: the frame the scene
// will be exported in, Y up and Z forward). Rotations are degrees in Unity's
// order. Measure sets the asset scale from a known length, after which the
// scene's units are metres.
const AXES = ["x", "y", "z"];

const vecRow = (field, label) => `
  <div class="tp-row"><span>${label}</span>${AXES.map((a, i) =>
    `<input type="number" step="any" class="axis-${a}" data-field="${field}" data-i="${i}" aria-label="${label} ${a}" />`).join("")}</div>`;

export function createTransformPanel(root, viewer, { onChange, onAnchorTab }) {
  let tab = "assets", mode = "translate", open = false, scaled = false;
  let measuring = false, measuredDistance = null;

  root.innerHTML = `
    <div class="tp-seg" role="tablist">
      <button data-tab="assets" class="on">assets</button><button data-tab="anchor">anchor</button>
    </div>
    ${vecRow("position", "position")}
    ${vecRow("rotation_deg", "rotation°")}
    <div class="tp-row single" data-only="assets"><span>scale</span>
      <input type="number" step="any" min="0" data-field="scale" aria-label="scale" /><span></span></div>
    <div class="tp-seg">
      <button data-mode="translate" class="on">move</button><button data-mode="rotate">rotate</button><button data-mode="scale" data-only="assets">scale</button>
    </div>
    <div class="tp-actions">
      <button class="view-btn" data-act="measure" data-only="assets">measure</button>
      <button class="view-btn" data-act="place" data-only="anchor">place</button>
      <button class="view-btn" data-act="find">find</button>
      <button class="view-btn" data-act="reset">reset</button>
    </div>
    <div class="tp-measure" hidden>
      real length <input type="number" step="any" min="0" aria-label="Real length in metres" /> m
      <button class="view-btn" data-act="apply-scale">set</button>
    </div>
    <div class="tp-note"></div>
  `;
  const $$ = (sel) => [...root.querySelectorAll(sel)];
  const note = root.querySelector(".tp-note");
  const measureRow = root.querySelector(".tp-measure");
  const measureInput = measureRow.querySelector("input");

  function unitsNote() {
    return tab === "anchor"
      ? "Move and turn the grid so <b>Y points up</b> and <b>Z faces the front</b>. Exports will use this as their origin."
      : scaled ? "Units are <b>metres</b> (set by measuring)." : "Units are the reconstruction's own, <b>not metres</b>. Measure a known length to fix that.";
  }

  function fieldsFrom(t) {
    const pose = t[tab];
    for (const input of $$("input[data-field]")) {
      if (input === document.activeElement) continue; // don't fight the typist
      const v = input.dataset.field === "scale" ? pose.scale : pose[input.dataset.field]?.[+input.dataset.i];
      if (v !== undefined) input.value = String(Math.round(v * 1000) / 1000);
    }
  }

  function refresh() {
    $$("[data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    $$("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
    $$("[data-only]").forEach((el) => {
      if (el.tagName === "BUTTON" && el.closest(".tp-seg")) el.disabled = el.dataset.only !== tab;
      else el.hidden = el.dataset.only !== tab;
    });
    fieldsFrom(viewer.getTransform());
    if (!measuring) note.innerHTML = unitsNote();
  }

  function attachGizmo() {
    viewer.setGizmo(open && !measuring ? tab : null, mode); // off while measuring, so clicks land on the scene
  }

  function changed() {
    refresh();
    onChange();
  }

  // Typing in a field moves the scene at once; the gizmo follows.
  root.addEventListener("input", (e) => {
    const input = e.target.closest("input[data-field]");
    if (!input) return;
    const v = parseFloat(input.value);
    if (!Number.isFinite(v)) return;
    const t = viewer.getTransform();
    if (input.dataset.field === "scale") {
      if (v <= 0) return;
      t.assets.scale = v;
    } else {
      t[tab][input.dataset.field][+input.dataset.i] = v;
    }
    viewer.setTransform(t);
    onChange();
  });
  root.addEventListener("change", () => fieldsFrom(viewer.getTransform())); // tidy what was typed

  root.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.tab) {
      tab = b.dataset.tab;
      if (tab === "anchor") { if (mode === "scale") mode = "translate"; onAnchorTab(); }
      cancelMeasure();
      attachGizmo();
      refresh();
    } else if (b.dataset.mode) {
      mode = b.dataset.mode;
      attachGizmo();
      refresh();
    } else if (b.dataset.act === "reset") {
      const t = viewer.getTransform();
      t[tab] = tab === "assets" ? { position: [0, 0, 0], rotation_deg: [0, 0, 0], scale: 1 } : { position: [0, 0, 0], rotation_deg: [0, 0, 0] };
      if (tab === "assets") scaled = false;
      viewer.setTransform(t);
      cancelMeasure();
      changed();
    } else if (b.dataset.act === "measure") {
      startMeasure();
    } else if (b.dataset.act === "place") {
      startPlace();
    } else if (b.dataset.act === "find") {
      viewer.focus(tab);
    } else if (b.dataset.act === "apply-scale") {
      applyScale();
    }
  });

  function startMeasure() {
    measuring = true;
    measuredDistance = null;
    attachGizmo();
    measureRow.hidden = true;
    note.innerHTML = "Click the <b>first point</b> of something you know the size of. Esc cancels.";
    viewer.startMeasure({
      onPoint(i, hit) {
        if (!hit) note.innerHTML = "Missed. Click <b>on the scene</b>.";
        else if (i === 0) note.innerHTML = "Now click the <b>second point</b>.";
      },
      onDone([a, b]) {
        const d = a.distanceTo(b);
        measuredDistance = d;
        note.innerHTML = `That's <b>${d.toPrecision(3)}</b> ${scaled ? "m" : "units"} in the scene. Enter its real length:`;
        measureRow.hidden = false;
        measureInput.value = "";
        measureInput.focus();
      },
    });
  }

  function startPlace() {
    measuring = true;
    attachGizmo();
    note.innerHTML = "Click where the anchor should go, e.g. <b>the floor</b> at the room's centre. Esc cancels.";
    viewer.startMeasure({
      count: 1,
      onPoint(i, hit) { if (!hit) note.innerHTML = "Missed. Click <b>on the scene</b>."; },
      onDone([point]) {
        viewer.placeAnchor(point);
        cancelMeasure();
        changed();
      },
    });
  }

  function applyScale() {
    const metres = parseFloat(measureInput.value);
    if (!(metres > 0) || !measuredDistance) return;
    const t = viewer.getTransform();
    t.assets.scale *= metres / measuredDistance;
    viewer.setTransform(t);
    scaled = true;
    cancelMeasure();
    changed();
  }
  measureInput.addEventListener("keydown", (e) => { if (e.key === "Enter") applyScale(); });

  function cancelMeasure() {
    if (!measuring) return;
    measuring = false;
    measuredDistance = null;
    measureRow.hidden = true;
    viewer.clearMeasure();
    attachGizmo();
    refresh();
  }
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && open) cancelMeasure(); });

  viewer.onTransformChange = changed; // gizmo drags

  return {
    setOpen(v) {
      open = v;
      root.hidden = !v;
      if (!v) cancelMeasure();
      attachGizmo();
      refresh();
    },
    refresh,
    get scaled() { return scaled; },
    set scaled(v) { scaled = !!v; refresh(); },
  };
}
