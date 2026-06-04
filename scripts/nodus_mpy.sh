#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_ROOT="$ROOT_DIR/cpynodus_ii"
BUILD_ROOT="$ROOT_DIR/build/firmware"
PACKAGE_OUT="$BUILD_ROOT/cpynodus_ii"
REQUIRED_MPY_ABI="mpy v6.3"

COMPILER="${MPY_CROSS:-}"
DRY_RUN=0
CLEAN=0
PRUNE_STALE=1

usage() {
  cat <<'EOF'
Compile cPyNodus_II package modules to CircuitPython-compatible .mpy files.

Usage:
  scripts/nodus_mpy.sh [options]

Options:
  --compiler VALUE    Path to the CircuitPython 9.2.8-compatible mpy-cross.
                      Defaults to $MPY_CROSS when set, then known local
                      candidates that emit MPY v6.3.
  --dry-run           Show compile actions without writing artifacts.
  --clean             Remove build/firmware/cpynodus_ii before compiling.
  --no-prune-stale    Keep existing .mpy files that no longer have source .py.
  --help              Show this help.

Output:
  cpynodus_ii/foo.py -> build/firmware/cpynodus_ii/foo.mpy
EOF
}

find_compiler() {
  local candidate=""
  local candidates=(
    "$ROOT_DIR/../pico_libs/mpy-cross/mpy-cross"
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
    echo "MPY compiler must emit $REQUIRED_MPY_ABI for CircuitPython 9.2.8." >&2
    echo "Compiler: $compiler" >&2
    echo "Version: ${version:-unknown}" >&2
    exit 1
  fi

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
      echo "Would remove $PACKAGE_OUT"
    else
      rm -rf "$PACKAGE_OUT"
    fi
  fi

  while IFS= read -r -d '' src; do
    rel_path="${src#$ROOT_DIR/}"
    out_path="$BUILD_ROOT/${rel_path%.py}.mpy"
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
}

prune_stale_artifacts() {
  local artifact=""
  local rel_path=""
  local source_path=""

  if [[ ! -d "$PACKAGE_OUT" ]]; then
    return 0
  fi

  while IFS= read -r -d '' artifact; do
    rel_path="${artifact#$BUILD_ROOT/}"
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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --compiler)
      COMPILER="${2:-}"
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

if [[ -z "$COMPILER" ]]; then
  if ! COMPILER="$(find_compiler)"; then
    echo "Could not find an MPY compiler." >&2
    echo "Set MPY_CROSS=/path/to/mpy-cross or pass --compiler." >&2
    exit 1
  fi
fi

validate_compiler "$COMPILER"
compile_package "$COMPILER"
