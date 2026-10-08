# Splat Local

Turn a phone video of a room into a **3D Gaussian splat**, and optionally a **textured mesh**, entirely on your Mac. Then fly through it, level it, scale it to real metres, and export it ready for Unity.

You use it in your web browser, but nothing leaves your computer: the app runs locally and the browser just shows its interface at `http://127.0.0.1:8000`.

---

## Requirements

| | |
|---|---|
| **Computer** | An **Apple Silicon Mac** (M1 or later). Intel Macs, Windows and Linux are not supported. |
| **Memory** | 24 GB runs *Preview* and *High* (High peaked at 16 GB on a 7-minute 4K video). *Max* needs much more; see [Presets](#presets). |
| **Disk** | About **15 GB** for the tools, plus **1–10 GB per project**. |
| **Software** | [Homebrew](https://brew.sh) and the Xcode Command Line Tools. `setup.sh` installs everything else: ffmpeg, uv/Python, Node.js, Rust and the Brush trainer. |

## Set up (once)

```bash
# 1. If you don't have them yet:
#    Homebrew:  see https://brew.sh
xcode-select --install

# 2. Get the app and run the setup script
git clone https://github.com/novaoughton/splat-local.git
cd splat-local
./setup.sh
```

`setup.sh` takes **10–20 minutes** the first time. Most of that is compiling the Brush splat trainer. It is safe to re-run; it skips anything already done.

Generated data (the Python environment, the Brush build and every project you make) goes in **`~/SplatPipelineData`**, outside the repo. To put it somewhere else, set `SPLAT_DATA_DIR`, e.g. `SPLAT_DATA_DIR=/Volumes/Fast/splat ./setup.sh`, and use the same setting with `./run.sh`.

## Run

```bash
./run.sh
```

Open **http://127.0.0.1:8000** in Safari or Chrome. Leave the terminal open while you use the app, and press **Ctrl+C** in it to stop. To use another port: `PORT=8765 ./run.sh`.

## Use it

1. **Drop a video** on the start screen. A 2–3 minute walk around a room works best; see the in-app **capture guide**.
2. Choose:
   - **Output**: *Gaussian splat* (photoreal), *Mesh* (textured, for collisions/geometry) or *Both*, which are already lined up with each other.
   - **Preset**: *Preview*, *High* (default) or *Max*. The start screen shows how many frames your video will give.
   - **Pose backend**: leave it on *COLMAP*.
3. **Start reconstruction** and watch it build. Stages: frames → camera positions → (mesh) → training → export. The viewer streams the splat as it trains.
4. When it's done, the sidebar lists the **downloads**:
   - `scene.ply`: the full splat, the archive copy.
   - `scene.spz`: the same splat compressed.
   - `scene-view.sog`: a lighter version for viewers.
   - `mesh.zip`: OBJ and textures.
   - `debug-report.md`: timings, memory, settings and quality metrics for the run.

### Viewer controls

| | |
|---|---|
| drag | orbit |
| scroll | zoom |
| **W A S D** | move |
| **Q / E** | down / up |
| **← →** | turn |
| **↑ ↓** | look up / down |

Top right:
- **camera frusta**: show where each frame was taken.
- **reset view**
- **splat / mesh** switch
- **grid**, with a cell-size slider from 1 cm to 10 m
- **transform**: the panel below

### Line it up and export for Unity

Open **transform**:

1. **anchor → floor**: click 3 spots on open floor, spread well apart. The grid snaps to the floor, which becomes y = 0 in the export.
2. **anchor → rotate**: turn the grid so its blue **Z axis faces the front** of the room.
3. **assets → measure**: click both ends of something whose size you know, such as a door's height, and type the real length in metres. The scene is then in real metres.
4. Optionally **assets → move / rotate / scale** to adjust by hand. You can type values or drag the handles; **find** brings the handles into view.

Transforms save automatically with the project. Then click **Export for Unity** in the sidebar. It writes three new downloads and leaves the originals untouched:
- `scene-unity.ply`: the splat
- `mesh-unity.zip`: the mesh
- `unity-transform.json`: the exact transform applied

They're right-handed with +Y up and +Z forward, the anchor at the origin, in metres. If you change the transform afterwards, the sidebar says **out of date** until you export again.

## Presets

| | Preview | High | Max |
|---|---|---|---|
| Frame every | 1.0 s | 0.8 s | 0.7 s |
| Resolution (long edge) | 1536 px | 2048 px | 2560 px |
| Training steps | 10k | 18k | 45k |
| Splat cap | 3M | 4M | 6M |
| LPIPS perceptual loss | – | – | on |
| Longest video | – | 8 min | – |
| Mesh detail | reduced | medium | full |

Longer videos get more frames rather than sparser ones. **Measured** on an M4 Pro with 24 GB, a 7-minute 4K room on High took **52 min** end to end and peaked at **16 GB**.

**Max is for big machines.** Its LPIPS loss runs an image network on every full-size frame Brush renders. On a 24 GB Mac it took training past 25 GB within 90 seconds and stalled. On a smaller machine, run Max without LPIPS:

```bash
SPLAT_LPIPS=0 ./run.sh
```

## Running a Max test

For whoever is testing Max on a bigger Mac:

1. **Prepare the Mac:**
   - Plug in power.
   - Keep the lid open and the Mac awake. For example, run `caffeinate -dimsu` in a second terminal while it trains.
   - Quit other heavy apps.
2. **Start** `./run.sh`, open the app, and drop the test video. Choose **Output: Both**, **Preset: Max**, then **Start reconstruction**.
3. **Close the browser tab while it trains.** The live viewer holds a few GB of memory. The job keeps running, and reopening `http://127.0.0.1:8000` reattaches to it.
4. **Watch Activity Monitor → Memory.** If memory pressure goes red, swap keeps climbing, and the progress bar hasn't moved for 20+ minutes:
   - cancel the job
   - stop the app
   - run it again with `SPLAT_LPIPS=0 ./run.sh`
   - note that you did
5. **When it finishes, fails or is cancelled,** download **`debug-report.md`**: it's in the downloads list, or behind the link on the failure screen. It's also on disk at `~/SplatPipelineData/jobs/<project id>/exports/debug-report.md`. Send it back, ideally with a couple of viewer screenshots.

The report records each stage's time, peak memory (including GPU memory), CPU and swap, plus the machine, settings and tool versions. That's everything needed to tune Max.

## Troubleshooting

| Problem | Fix |
|---|---|
| `setup.sh` stops with a message | Do what it says (install Homebrew or the Command Line Tools), then re-run it. |
| Brush fails to build | `rustup update`, then `./setup.sh` again. |
| "Address already in use" | Another app has port 8000: `PORT=8765 ./run.sh`. |
| The page looks out of date after an update | Reload. If that doesn't do it, in Safari choose Develop → Empty Caches, then reload. |
| No mesh option, or the mesh fails | Install the Command Line Tools (`xcode-select --install`), then `./setup.sh`. |
| Only some frames placed / patchy splat | Usually the footage. Follow the capture guide: move slowly, lock exposure, avoid pointing straight at windows. |
| Disk filling up | **Clean up working files** on a finished project frees most of it; deleting a project removes its folder. Projects live in `~/SplatPipelineData/jobs/`. |

## How it works

```
video ─▶ sharpest frames ─▶ camera positions ─▶ (mesh) ─▶ splat training ─▶ export
         sharp-frames       COLMAP/GLOMAP        Object    Brush (Metal)     splat-transform
                            (pycolmap)           Capture
```

- **Frames:** the sharpest frame in every 0.7–1.0 s window ([sharp-frames](https://github.com/Reflct/sharp-frames-python)).
- **Camera positions:** [COLMAP](https://colmap.github.io) via `pycolmap`, global mapping (GLOMAP) with an incremental fallback. Cameras placed off the walking path or outside the room are dropped.
- **Mesh:** Apple's Object Capture (`tools/objcap`), lined up with the splat through its camera positions.
- **Training:** [Brush](https://github.com/ArthurBrussee/brush), a Rust/Metal Gaussian-splat trainer.
- **Export:** [splat-transform](https://github.com/playcanvas/splat-transform).
- **Viewer:** [Spark](https://sparkjs.dev) on [three.js](https://threejs.org).

More detail: [docs/api.md](docs/api.md) (HTTP API, job states, the debug report, the Unity export frame), [docs/pose-mapper.md](docs/pose-mapper.md), [docs/step-count.md](docs/step-count.md) and [docs/viewer-cost.md](docs/viewer-cost.md).

## Layout

| Path | What |
|---|---|
| `server/` | FastAPI app and pipeline stages (frames, poses, mesh, train, export), the Unity export (`bake.py`) and the debug report (`report.py`) |
| `web/` | the app's interface: upload, progress, viewer, transform panel |
| `viewer/` | the viewer engine (Spark + three.js) |
| `tools/objcap/` | the Object Capture command-line helper (Swift) |
| `vendor/` | Spark and three.js builds served to the browser |
| `tests/` | unit tests: `~/SplatPipelineData/venv/bin/python -m unittest discover -s tests` |
| `setup.sh`, `run.sh`, `env.sh` | setup, start, and where generated data lives |

## Credits and licence

A fork of [splat-local](https://github.com/michael-L-i/splat-local) by Michael Li (MIT, see [LICENSE](LICENSE)). This fork adds:
- frame spacing presets
- misplaced-camera handling
- mesh output
- the transform tools and Unity export
- the debug report

It builds on Brush, COLMAP/GLOMAP, sharp-frames, splat-transform, Spark, three.js and Apple's Object Capture, each under its own licence.
