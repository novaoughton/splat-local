# Where heavy, generated data lives (Python env, Brush build, projects). Sourced by
# setup.sh and run.sh. Defaults to ~/SplatPipelineData, outside the repo, so a repo
# in a synced folder (Google Drive, iCloud) never syncs gigabytes of build output.
# Set SPLAT_DATA_DIR before running either script to put it somewhere else.

export SPLAT_DATA_DIR="${SPLAT_DATA_DIR:-$HOME/SplatPipelineData}"
export UV_PROJECT_ENVIRONMENT="$SPLAT_DATA_DIR/venv"
export SPLAT_JOBS_DIR="$SPLAT_DATA_DIR/jobs"
export BRUSH_SRC_DIR="$SPLAT_DATA_DIR/brush_src"
export BRUSH_PREBUILT="$SPLAT_DATA_DIR/brush"
export OBJCAP_BIN="$SPLAT_DATA_DIR/bin/objcap"  # Object Capture helper for meshes (tools/objcap)
mkdir -p "$SPLAT_DATA_DIR" "$SPLAT_JOBS_DIR"

# Homebrew's rustup is keg-only, so put it (and cargo) on PATH here.
export PATH="/opt/homebrew/opt/rustup/bin:$HOME/.cargo/bin:$PATH"

# Same preference order as server/stages/train_brush.py: source build, then prebuilt.
if [[ -x "$BRUSH_SRC_DIR/target/release/brush" ]]; then
  export BRUSH_BIN="$BRUSH_SRC_DIR/target/release/brush"
elif [[ -x "$BRUSH_PREBUILT" ]]; then
  export BRUSH_BIN="$BRUSH_PREBUILT"
fi
