# Local paths for heavy, generated data. Sourced by setup.sh and run.sh.
# The repo lives on Google Drive, so the venv, Brush build and job folders
# are kept on local disk instead, where Drive won't try to sync them.

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
