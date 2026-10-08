#!/usr/bin/env bash
# One-time setup for Splat Local on an Apple Silicon Mac. Safe to re-run: every step
# skips what is already in place. See README.md for what each piece is for.
set -euo pipefail
cd "$(dirname "$0")"
[[ -f env.sh ]] && source env.sh

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1; }

[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || die "Splat Local needs an Apple Silicon Mac (M1 or later)."
need brew || die "Homebrew is required: install it from https://brew.sh, then run ./setup.sh again."
xcode-select -p >/dev/null 2>&1 || die "The Xcode Command Line Tools are required: run  xcode-select --install  then ./setup.sh again."

need ffmpeg || { say "Installing ffmpeg"; brew install ffmpeg; }
need uv || { say "Installing uv (Python package manager)"; brew install uv; }
need node || { say "Installing Node.js (for splat-transform exports)"; brew install node; }
if ! need cargo; then
  say "Installing Rust (to build the Brush trainer)"
  brew list rustup >/dev/null 2>&1 || brew install rustup
  rustup default stable
fi

say "Syncing the Python environment into $UV_PROJECT_ENVIRONMENT (fastapi, pycolmap, sharp-frames...)"
uv sync

# Brush, the splat trainer, built from a pinned commit: the app relies on its CLI flags
# (training steps, mip render mode, LPIPS loss, image cache size), which older releases lack.
BRUSH_REPO="https://github.com/ArthurBrussee/brush"
BRUSH_COMMIT="6378a76add3b93501abb55c2dc08d71688537679"  # 2026-09-26, brush 1.0.0
BRUSH_SRC_DIR="${BRUSH_SRC_DIR:-vendor/brush_src}"
if [[ ! -x "$BRUSH_SRC_DIR/target/release/brush" ]]; then
  if [[ ! -d "$BRUSH_SRC_DIR/.git" ]]; then
    say "Fetching Brush ${BRUSH_COMMIT:0:7}"
    git init -q "$BRUSH_SRC_DIR"
    git -C "$BRUSH_SRC_DIR" remote add origin "$BRUSH_REPO"
  fi
  git -C "$BRUSH_SRC_DIR" fetch -q --depth 1 origin "$BRUSH_COMMIT"
  git -C "$BRUSH_SRC_DIR" checkout -q FETCH_HEAD
  say "Building Brush (one-time, roughly 5-15 minutes)"
  (cd "$BRUSH_SRC_DIR" && cargo build --release -p brush-app)
  [[ -x "$BRUSH_SRC_DIR/target/release/brush" ]] || die "Brush did not build; see the cargo output above."
fi

# Mesh output: tools/objcap wraps Apple's Object Capture (needs swiftc, from the
# Command Line Tools checked above). The mesh stage also builds it on first use.
OBJCAP_BIN="${OBJCAP_BIN:-vendor/objcap}"
if [[ ! -x "$OBJCAP_BIN" || tools/objcap/main.swift -nt "$OBJCAP_BIN" ]]; then
  say "Building the Object Capture helper (mesh output)"
  mkdir -p "$(dirname "$OBJCAP_BIN")"
  swiftc -O tools/objcap/main.swift -o "$OBJCAP_BIN" || say "objcap build failed: splats still work, mesh output won't"
fi

# splat-transform: .spz/.sog exports, the viewer's scene and the Unity export.
# Same version as SPLAT_TRANSFORM_VERSION in server/stages/export.py.
say "Fetching splat-transform"
npx --yes @playcanvas/splat-transform@3.9.0 --version >/dev/null

say "Done. Start the app with  ./run.sh  and open http://127.0.0.1:8000"
