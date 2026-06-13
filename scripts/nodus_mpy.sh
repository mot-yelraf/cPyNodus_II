#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_ROOT="$ROOT_DIR/cpynodus_ii"
BUILD_ROOT="$ROOT_DIR/build/firmware"
REQUIRED_MPY_ABI="mpy v6.3"
PICO2W_LIB_SOURCE="$ROOT_DIR/../mcu_libs/circuitPython_9.2.8/libs/adafruit-circuitpython-bundle-9.x-mpy-20250314/lib"
XESP32S3_LIB_SOURCE="$ROOT_DIR/../mcu_libs/circuitPython_10.x.x/adafruit-circuitpython-bundle-10.x-mpy-20260606/lib"

COMPILER="${MPY_CROSS:-}"
TARGET="pico2w"
TARGET_CIRCUITPY_VERSION="9.2.8"
TARGET_LIB_SOURCE=""
TARGET_BUILD_ROOT=""
PACKAGE_OUT=""
LIB_OUT=""
DRY_RUN=0
CLEAN=0
PRUNE_STALE=1
STAGE_LIBS=1

LIB_MANIFEST=(
  "adafruit_minimqtt"
  "adafruit_scd30.mpy"
  "adafruit_connection_manager.mpy"
  "adafruit_ahtx0.mpy"
  "adafruit_bme280"
  "adafruit_ticks.mpy"
  "adafruit_scd4x.mpy"
  "adafruit_ntp.mpy"
  "adafruit_httpserver"
  "adafruit_register"
  "adafruit_veml7700.mpy"
  "asyncio"
  "adafruit_bme680.mpy"
)

usage() {
  cat <<'EOF'
Compile cPyNodus_II package modules to CircuitPython-compatible .mpy files.

Usage:
  scripts/nodus_mpy.sh [options]

Options:
  --target VALUE      Build target: pico2w or xesp32s3.
                      Aliases: pico2w-mpy, xesp32s3-mpy.
                      Default: pico2w.
  --compiler VALUE    Path to a CircuitPython-compatible mpy-cross.
                      Defaults to $MPY_CROSS when set, then known local
                      candidates that emit MPY v6.3.
  --lib-source VALUE  Override the Adafruit bundle lib/ source for the target.
  --dry-run           Show compile actions without writing artifacts.
  --clean             Remove build/firmware/<target>/ before compiling.
  --no-prune-stale    Keep existing .mpy files that no longer have source .py.
  --no-stage-libs     Do not stage target lib/ dependencies.
  --help              Show this help.

Output:
  cpynodus_ii/foo.py -> build/firmware/<target>/cpynodus_ii/foo.mpy
  bundle lib entries -> build/firmware/<target>/lib/
EOF
}

normalize_target() {
  case "$1" in
    ""|pico|pico2|pico2w|pico2w-cp928|pico2w-mpy)
      printf '%s\n' "pico2w"
      ;;
    xiao|xiao-esp32s3|xesp32s3|xesp32s3-cp1021|xesp32s3-mpy)
      printf '%s\n' "xesp32s3"
      ;;
    *)
      return 1
      ;;
  esac
}

configure_target() {
  local normalized=""
  if ! normalized="$(normalize_target "$TARGET")"; then
    echo "Invalid --target '$TARGET'. Use pico2w or xesp32s3." >&2
    exit 2
  fi

  TARGET="$normalized"
  case "$TARGET" in
    pico2w)
      TARGET_CIRCUITPY_VERSION="9.2.8"
      TARGET_LIB_SOURCE="${TARGET_LIB_SOURCE:-$PICO2W_LIB_SOURCE}"
      ;;
    xesp32s3)
      TARGET_CIRCUITPY_VERSION="10.2.1"
      TARGET_LIB_SOURCE="${TARGET_LIB_SOURCE:-$XESP32S3_LIB_SOURCE}"
      ;;
  esac

  TARGET_BUILD_ROOT="$BUILD_ROOT/$TARGET"
  PACKAGE_OUT="$TARGET_BUILD_ROOT/cpynodus_ii"
  LIB_OUT="$TARGET_BUILD_ROOT/lib"
}

find_compiler() {
  local candidate=""
  local candidates=(
    "$ROOT_DIR/../mcu_libs/mpy-cross/mpy-cross"
    "$ROOT_DIR/../mcu_libs/circuitpython/mpy-cross/build/mpy-cross"
    "$ROOT_DIR/tools/mpy-cross/mpy-cross"
    "$ROOT_DIR/.venv/bin/mpy-cross"
    "$ROOT_DIR/.venv/bin/mpy-cross-v6.3"
  )

  if [[ -n "$COMPILER" ]]; then
    printf '%s\n' "$COMPILER"
    return 0
  fi

  for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  for candidate in mpy-cross-v6.3 circuitpython-mpy-cross mpy-cross; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done

  return 1
}

validate_compiler() {
  local compiler="$1"
  local version=""

  if [[ "$compiler" != */* ]]; then
    if ! compiler="$(command -v "$compiler")"; then
      echo "MPY compiler command not found: $1" >&2
      exit 1
    fi
    COMPILER="$compiler"
  fi

  if [[ ! -x "$compiler" ]]; then
    echo "MPY compiler is not executable: $compiler" >&2
    exit 1
  fi

  version="$("$compiler" --version 2>&1 || true)"
  if [[ "$version" != *"$REQUIRED_MPY_ABI"* ]]; then
    echo "MPY compiler must emit $REQUIRED_MPY_ABI for CircuitPython $TARGET_CIRCUITPY_VERSION." >&2
    echo "Compiler: $compiler" >&2
    echo "Version: ${version:-unknown}" >&2
    exit 1
  fi

  echo "Build target: $TARGET (CircuitPython $TARGET_CIRCUITPY_VERSION)"
  echo "Using MPY compiler: $compiler"
  echo "Compiler version: $version"
}

compile_package() {
  local compiler="$1"
  local src=""
  local rel_path=""
  local out_path=""
  local count=0

  if [[ ! -d "$SOURCE_ROOT" ]]; then
    echo "Source package not found: $SOURCE_ROOT" >&2
    exit 1
  fi

  if [[ $CLEAN -eq 1 ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "Would remove $TARGET_BUILD_ROOT"
    else
      rm -rf "$TARGET_BUILD_ROOT"
    fi
  fi

  while IFS= read -r -d '' src; do
    rel_path="${src#$ROOT_DIR/}"
    out_path="$TARGET_BUILD_ROOT/${rel_path%.py}.mpy"
    count=$((count + 1))

    if [[ $DRY_RUN -eq 1 ]]; then
      echo "Would compile $rel_path -> ${out_path#$ROOT_DIR/}"
      continue
    fi

    mkdir -p "$(dirname "$out_path")"
    "$compiler" -s "$rel_path" -o "$out_path" "$src"
  done < <(find "$SOURCE_ROOT" -type f -name '*.py' -print0)

  if [[ $count -eq 0 ]]; then
    echo "No source modules found under $SOURCE_ROOT" >&2
    exit 1
  fi

  if [[ $PRUNE_STALE -eq 1 ]]; then
    prune_stale_artifacts
  fi

  echo "Compiled $count modules into ${PACKAGE_OUT#$ROOT_DIR/}"

  if [[ $STAGE_LIBS -eq 1 ]]; then
    stage_target_libs
  fi
}

prune_stale_artifacts() {
  local artifact=""
  local rel_path=""
  local source_path=""

  if [[ ! -d "$PACKAGE_OUT" ]]; then
    return 0
  fi

  while IFS= read -r -d '' artifact; do
    rel_path="${artifact#$TARGET_BUILD_ROOT/}"
    source_path="$ROOT_DIR/${rel_path%.mpy}.py"

    if [[ -f "$source_path" ]]; then
      continue
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
      echo "Would remove stale artifact ${artifact#$ROOT_DIR/}"
    else
      rm -f "$artifact"
      echo "Removed stale artifact ${artifact#$ROOT_DIR/}"
    fi
  done < <(find "$PACKAGE_OUT" -type f -name '*.mpy' -print0)
}

stage_target_libs() {
  local entry=""
  local source_path=""

  if [[ ! -d "$TARGET_LIB_SOURCE" ]]; then
    echo "Target library source not found: $TARGET_LIB_SOURCE" >&2
    exit 1
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    echo "Would stage libraries from $TARGET_LIB_SOURCE -> ${LIB_OUT#$ROOT_DIR/}"
  else
    rm -rf "$LIB_OUT"
    mkdir -p "$LIB_OUT"
  fi

  for entry in "${LIB_MANIFEST[@]}"; do
    source_path="$TARGET_LIB_SOURCE/$entry"
    if [[ ! -e "$source_path" ]]; then
      echo "Missing target library dependency: $source_path" >&2
      exit 1
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
      echo "Would stage lib/$entry from $source_path"
    else
      cp -R "$source_path" "$LIB_OUT/"
    fi
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      TARGET="${2:-}"
      shift 2
      ;;
    --compiler)
      COMPILER="${2:-}"
      shift 2
      ;;
    --lib-source)
      TARGET_LIB_SOURCE="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --clean)
      CLEAN=1
      shift
      ;;
    --no-prune-stale)
      PRUNE_STALE=0
      shift
      ;;
    --no-stage-libs)
      STAGE_LIBS=0
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

configure_target

if [[ -z "$COMPILER" ]]; then
  if ! COMPILER="$(find_compiler)"; then
    echo "Could not find an MPY compiler." >&2
    echo "Set MPY_CROSS=/path/to/mpy-cross or pass --compiler." >&2
    exit 1
  fi
fi

validate_compiler "$COMPILER"
compile_package "$COMPILER"
