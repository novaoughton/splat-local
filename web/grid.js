// The viewer's ground grid: lines on its parent's XZ plane (the anchor), with
// every 10th line brighter, and the anchor's axes — X red, Y green, Z blue —
// so it's clear which way the exported scene will face.
//
// Cell sizes run 1 cm to 10 m in 1-2-5 steps. Lines stop at the scene's reach
// or MAX_HALF_LINES cells from the anchor, whichever is closer, so a 1 cm grid
// covers 4 m around the anchor instead of drawing a million lines.
import * as THREE from "three";

export const CELL_STEPS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10];
const MAX_HALF_LINES = 200;

export function formatCell(cell, scaled) {
  if (!scaled) return `${cell} units`;
  return cell < 1 ? `${Math.round(cell * 100)} cm` : `${cell} m`;
}

export function createGrid() {
  const group = new THREE.Group();
  const lineMaterial = (color, opacity) =>
    new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
  const minor = new THREE.LineSegments(new THREE.BufferGeometry(), lineMaterial(0xffffff, 0.12));
  const major = new THREE.LineSegments(new THREE.BufferGeometry(), lineMaterial(0xffffff, 0.3));
  const axes = new THREE.LineSegments(
    new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.9, depthWrite: false }),
  );
  for (const o of [minor, major, axes]) { o.renderOrder = 10; group.add(o); }

  // cell and reach in the parent's units.
  function build(cell, reach) {
    const half = Math.max(1, Math.min(MAX_HALF_LINES, Math.ceil(reach / cell)));
    const extent = half * cell;
    const minorPos = [], majorPos = [];
    for (let i = -half; i <= half; i++) {
      if (i === 0) continue; // the axes draw these
      const out = i % 10 === 0 ? majorPos : minorPos, v = i * cell;
      out.push(v, 0, -extent, v, 0, extent, -extent, 0, v, extent, 0, v);
    }
    minor.geometry.dispose();
    major.geometry.dispose();
    minor.geometry = new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(minorPos, 3));
    major.geometry = new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(majorPos, 3));

    const up = extent * 0.25;
    axes.geometry.dispose();
    axes.geometry = new THREE.BufferGeometry()
      .setAttribute("position", new THREE.Float32BufferAttribute([
        -extent, 0, 0, extent, 0, 0, // X
        0, 0, 0, 0, up, 0, // Y
        0, 0, -extent, 0, 0, extent, // Z
      ], 3))
      .setAttribute("color", new THREE.Float32BufferAttribute([
        0.9, 0.3, 0.27, 0.9, 0.3, 0.27,
        0.4, 0.8, 0.4, 0.4, 0.8, 0.4,
        0.3, 0.5, 0.95, 0.3, 0.5, 0.95,
      ], 3));
  }

  return { object: group, build };
}
