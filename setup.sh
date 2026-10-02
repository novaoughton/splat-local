#!/usr/bin/env bash
# Setup for Splat Local on Apple Silicon macOS.
set -euo pipefail
cd "$(dirname "$0")"
[[ -f env.sh ]] && source env.sh

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1; }

need brew || { echo "Homebrew required: https://brew.sh"; exit 1; }
need ffmpeg || { say "Installing ffmpeg"; brew install ffmpeg; }
need uv || { say "Installing uv"; brew install uv; }

say "Syncing Python environment (fastapi, pycolmap, sharp-frames...)"
uv sync

# Brush splat trainer: prefer a from-source build (newer quality flags), else prebuilt release.
BRUSH_SRC_DIR="${BRUSH_SRC_DIR:-vendor/brush_src}"
BRUSH_PREBUILT="${BRUSH_PREBUILT:-vendor/brush}"
if [[ ! -x "$BRUSH_SRC_DIR/target/release/brush" && ! -x "$BRUSH_PREBUILT" ]]; then
  if need cargo; then
    say "Building Brush from source (one-time, ~5-10 min)"
    [[ -d "$BRUSH_SRC_DIR" ]] || git clone --depth 1 https://github.com/ArthurBrussee/brush "$BRUSH_SRC_DIR"
    (cd "$BRUSH_SRC_DIR" && cargo build --release -p brush-app) || true
  fi
  if [[ ! -x "$BRUSH_SRC_DIR/target/release/brush" ]]; then
    say "Downloading Brush v0.3.0 prebuilt binary"
    prebuilt_dir="$(dirname "$BRUSH_PREBUILT")"
    curl -sL https://github.com/ArthurBrussee/brush/releases/download/v0.3.0/brush-app-aarch64-apple-darwin.tar.xz |
      tar xJ -C "$prebuilt_dir"
    mv "$prebuilt_dir/brush-app-aarch64-apple-darwin/brush_app" "$BRUSH_PREBUILT"
    rm -rf "$prebuilt_dir/brush-app-aarch64-apple-darwin"
    chmod +x "$BRUSH_PREBUILT"
  fi
fi

# Optional: splat cleanup/compression (.spz/.sog exports)
if need npm; then
  say "Priming splat-transform (optional, for cleanup + .spz/.sog export)"
  npx --yes @playcanvas/splat-transform --version >/dev/null 2>&1 || true
fi

say "Done. Start the app with:  ./run.sh   (then open http://127.0.0.1:8000)"
