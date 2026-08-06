#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/cpynodus_ii/__init__.py"
MPY_BUILD_SCRIPT="$ROOT_DIR/scripts/nodus_mpy.sh"
MPY_TARGET="pico2w"
MPY_TARGET_ROOT="$ROOT_DIR/build/firmware/$MPY_TARGET"
MPY_BUILD_ROOT="$MPY_TARGET_ROOT/cpynodus_ii"
MPY_LIB_ROOT="$MPY_TARGET_ROOT/lib"
MPY_VERSION_ARTIFACT="$MPY_BUILD_ROOT/__init__.mpy"
MPY_BUILD_INFO="$MPY_TARGET_ROOT/BUILD_INFO"
MPY_TARGET_CIRCUITPY_VERSION="9.2.8"
MPY_REQUIRED_ABI="mpy v6.3"
DEPRECATED_MANIFEST="$ROOT_DIR/scripts/deprecated_target_files.txt"
MPY_REBUILD_DEFERRED=0

# Prevent macOS from creating AppleDouble sidecar files (._*) on FAT/CIRCUITPY.
export COPYFILE_DISABLE=1
export COPY_EXTENDED_ATTRIBUTES_DISABLE=1

get_project_version() {
  local version="unknown-version"
  if [[ -f "$VERSION_FILE" ]]; then
    version="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$VERSION_FILE" | head -n 1)"
  fi
  if [[ -z "$version" ]]; then
    version="unknown-version"
  fi
  printf '%s\n' "$version"
}

get_mpy_artifact_version() {
  local artifact="$1"
  local line=""
  local saw_version_name=0

  if [[ ! -f "$artifact" ]] || ! command -v strings >/dev/null 2>&1; then
    return 0
  fi

  while IFS= read -r line; do
    if [[ $saw_version_name -eq 1 ]]; then
      printf '%s\n' "$line"
      return 0
    fi
    if [[ "$line" == "__version__" ]]; then
      saw_version_name=1
    fi
  done < <(strings "$artifact")
}

set_mpy_target_paths() {
  case "$MPY_TARGET" in
    pico2w)
      MPY_TARGET_CIRCUITPY_VERSION="9.2.8"
      ;;
    xesp32s3)
      MPY_TARGET_CIRCUITPY_VERSION="10.2.1"
      ;;
    *)
      echo "Invalid MPY target '$MPY_TARGET'." >&2
      exit 2
      ;;
  esac
  MPY_TARGET_ROOT="$ROOT_DIR/build/firmware/$MPY_TARGET"
  MPY_BUILD_ROOT="$MPY_TARGET_ROOT/cpynodus_ii"
  MPY_LIB_ROOT="$MPY_TARGET_ROOT/lib"
  MPY_VERSION_ARTIFACT="$MPY_BUILD_ROOT/__init__.mpy"
  MPY_BUILD_INFO="$MPY_TARGET_ROOT/BUILD_INFO"
}

build_info_value() {
  local key="$1"
  if [[ ! -f "$MPY_BUILD_INFO" ]]; then
    return 0
  fi
  sed -n "s/^${key}=//p" "$MPY_BUILD_INFO" | head -n 1
}

mpy_build_info_rebuild_reason() {
  local value=""
  local source_version=""

  if [[ ! -f "$MPY_BUILD_INFO" ]]; then
    echo "missing MPY build metadata: ${MPY_BUILD_INFO#$ROOT_DIR/}"
    return 0
  fi

  value="$(build_info_value "format")"
  if [[ "$value" != "cpynodus-mpy-build-v1" ]]; then
    echo "invalid MPY build metadata format: ${value:-missing}"
    return 0
  fi

  value="$(build_info_value "target")"
  if [[ "$value" != "$MPY_TARGET" ]]; then
    echo "MPY build target $value does not match requested target $MPY_TARGET"
    return 0
  fi

  value="$(build_info_value "content")"
  if [[ "$value" != "$CONTENT" ]]; then
    echo "MPY build content $value does not match requested content $CONTENT"
    return 0
  fi

  value="$(build_info_value "circuitpython")"
  if [[ "$value" != "$MPY_TARGET_CIRCUITPY_VERSION" ]]; then
    echo "MPY build CircuitPython $value does not match target $MPY_TARGET_CIRCUITPY_VERSION"
    return 0
  fi

  value="$(build_info_value "mpy_abi")"
  if [[ "$value" != "$MPY_REQUIRED_ABI" ]]; then
    echo "MPY build ABI $value does not match required $MPY_REQUIRED_ABI"
    return 0
  fi

  value="$(build_info_value "compiler_version")"
  if [[ "$value" != *"CircuitPython $MPY_TARGET_CIRCUITPY_VERSION"* ]]; then
    echo "MPY compiler version does not match target CircuitPython $MPY_TARGET_CIRCUITPY_VERSION"
    return 0
  fi
  if [[ "$value" != *"$MPY_REQUIRED_ABI"* ]]; then
    echo "MPY compiler version does not report required ABI $MPY_REQUIRED_ABI"
    return 0
  fi

  value="$(build_info_value "stage_libs")"
  if [[ "$value" != "1" ]]; then
    echo "MPY build metadata reports unstaged libraries"
    return 0
  fi

  source_version="$(get_project_version)"
  value="$(build_info_value "project_version")"
  if [[ "$source_version" != "unknown-version" && "$value" != "$source_version" ]]; then
    echo "MPY build metadata version $value does not match source version $source_version"
    return 0
  fi
}

usage() {
  cat <<'EOF'
Deploy cPyNodus_II files to a CIRCUITPY drive (local or remote over SSH).

Usage:
  scripts/deploy_nodus.sh --target <path|host:path> [options]

Target formats:
  /Volumes/CIRCUITPY
  /media/pi/CIRCUITPY
  pi@raspberrypi:/media/pi/CIRCUITPY

Options:
  --target VALUE      Required. Destination path or host:path.
  --mode VALUE        `auto` (default), `drive`, or `staging`.
  --content VALUE     `full` (default), `runtime`, `pico2w-mpy`, or
                      `xesp32s3-mpy`. `mpy` remains a `pico2w-mpy` alias.
                      `runtime` syncs boot/code, package files, board
                      TOML templates, removing target cpynodus_ii/*.mpy first.
                      Target MPY content syncs root `*.py`, board TOML
                      templates, staged target `lib/`, and
                      compiled build/firmware/<target>/cpynodus_ii/*.mpy,
                      removing matching target cpynodus_ii/*.py first. It runs
                      scripts/nodus_mpy.sh first when artifacts are stale.
  --dry-run           Show what would be copied, do not write anything.
  --force             Skip CIRCUITPY path safety guard.
  --delete            Delete files on destination not present in source set.
                      Supported only with `--content full`.
  --prune-deprecated  Remove target paths listed in scripts/deprecated_target_files.txt.
  --ota-public-key PATH
                      Copy a trusted OTA public-key JSON to
                      /ota-public-key.json. Existing keys are preserved when
                      this option is omitted.
  --clear-reboot-log  Remove target postmortem logs after deploy (default).
  --keep-reboot-log   Preserve target postmortem logs.
  --help              Show this help.

Examples:
  scripts/deploy_nodus.sh --target /Volumes/CIRCUITPY
  scripts/deploy_nodus.sh --target /Volumes/CIRCUITPY --content runtime
  scripts/deploy_nodus.sh --target /Volumes/CIRCUITPY --content pico2w-mpy
  scripts/deploy_nodus.sh --target /Volumes/CIRCUITPY --content xesp32s3-mpy
  scripts/deploy_nodus.sh --target pi@raspberrypi:/media/pi/CIRCUITPY --dry-run
  scripts/deploy_nodus.sh --target pi@raspberrypi:/home/pi/cPyNodus_II-release --mode staging
EOF
}

TARGET=""
MODE="auto"
CONTENT="full"
DRY_RUN=0
FORCE=0
DELETE_MODE=0
PRUNE_DEPRECATED=0
CLEAR_REBOOT_LOG=1
OTA_PUBLIC_KEY_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      TARGET="${2:-}"
      shift 2
      ;;
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --content)
      CONTENT="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --delete)
      DELETE_MODE=1
      shift
      ;;
    --prune-deprecated)
      PRUNE_DEPRECATED=1
      shift
      ;;
    --ota-public-key)
      OTA_PUBLIC_KEY_PATH="${2:-}"
      shift 2
      ;;
    --clear-reboot-log)
      CLEAR_REBOOT_LOG=1
      shift
      ;;
    --keep-reboot-log)
      CLEAR_REBOOT_LOG=0
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

if [[ -z "$TARGET" ]]; then
  echo "Missing required argument: --target" >&2
  usage
  exit 2
fi

if [[ "$MODE" != "auto" && "$MODE" != "drive" && "$MODE" != "staging" ]]; then
  echo "Invalid --mode '$MODE'. Use: auto, drive, staging." >&2
  exit 2
fi

case "$CONTENT" in
  full|runtime)
    ;;
  mpy|pico2w-mpy)
    MPY_TARGET="pico2w"
    CONTENT="pico2w-mpy"
    ;;
  xesp32s3-mpy)
    MPY_TARGET="xesp32s3"
    ;;
  *)
    echo "Invalid --content '$CONTENT'. Use: full, runtime, pico2w-mpy, or xesp32s3-mpy." >&2
    exit 2
    ;;
esac

if [[ "$CONTENT" == *"-mpy" ]]; then
  set_mpy_target_paths
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required but not installed." >&2
  exit 1
fi

require_circuitpy_path() {
  local p="$1"
  if [[ $FORCE -eq 1 ]]; then
    return 0
  fi
  if [[ "$p" != *"CIRCUITPY"* ]]; then
    echo "Refusing deploy: target path does not contain 'CIRCUITPY': $p" >&2
    echo "Use --force to override." >&2
    exit 1
  fi
}

RSYNC_ARGS=(
  -av
  --human-readable
  --exclude=".git/"
  --exclude=".pytest_cache/"
  --exclude="__pycache__/"
  --exclude=".DS_Store"
  --exclude="._*"
  --exclude=".vscode/"
  --exclude=".gitignore"
  --exclude="tests/"
  --exclude="docs/"
  --exclude="specs/"
  --exclude="scripts/"
  --exclude="build/"
  --exclude="*.md"
  --exclude="*.txt"
  --exclude="*.pyc"
  --exclude="*.pyo"
  --exclude=".codex_write_test_*.tmp"
  --exclude="/ota-public-key.json"
  --exclude="pyproject.toml"
)

if [[ $DRY_RUN -eq 1 ]]; then
  RSYNC_ARGS+=(--dry-run --itemize-changes)
fi

if [[ $DELETE_MODE -eq 1 ]]; then
  RSYNC_ARGS+=(--delete)
fi

SRC="$ROOT_DIR/"
TARGET_PATH_ONLY="$TARGET"
if [[ "$TARGET" == *:* ]]; then
  TARGET_PATH_ONLY="${TARGET#*:}"
fi

DEPLOY_MODE="$MODE"
if [[ "$DEPLOY_MODE" == "auto" ]]; then
  if [[ "$TARGET_PATH_ONLY" == *"CIRCUITPY"* ]]; then
    DEPLOY_MODE="drive"
  else
    DEPLOY_MODE="staging"
  fi
fi

if [[ "$DEPLOY_MODE" == "drive" ]]; then
  require_circuitpy_path "$TARGET_PATH_ONLY"
  # CIRCUITPY mass-storage can fail rsync temp-file rename operations.
  RSYNC_ARGS+=(--inplace --no-perms --no-owner --no-group --omit-dir-times)
fi

if [[ "$CONTENT" != "full" && $DELETE_MODE -eq 1 ]]; then
  echo "--delete is not supported with --content $CONTENT (ambiguous scope)." >&2
  exit 2
fi

run_full_sync() {
  local destination="$1"
  rsync "${RSYNC_ARGS[@]}" "$SRC" "$destination"
}

sync_ota_public_key() {
  local destination="$1"
  local key_rsync_args=(-av --checksum --human-readable)
  if [[ -z "$OTA_PUBLIC_KEY_PATH" ]]; then
    return
  fi
  if [[ ! -f "$OTA_PUBLIC_KEY_PATH" ]]; then
    echo "OTA public key file not found: $OTA_PUBLIC_KEY_PATH" >&2
    exit 2
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    key_rsync_args+=(--dry-run --itemize-changes)
  fi
  if [[ "$DEPLOY_MODE" == "drive" ]]; then
    key_rsync_args+=(--inplace --no-perms --no-owner --no-group --omit-dir-times)
  fi
  rsync "${key_rsync_args[@]}" "$OTA_PUBLIC_KEY_PATH" \
    "${destination%/}/ota-public-key.json"
}

find_mpy_rebuild_reason() {
  local src=""
  local rel_path=""
  local artifact=""
  local source_version=""
  local artifact_version=""
  local metadata_reason=""

  if [[ ! -d "$MPY_BUILD_ROOT" ]]; then
    echo "missing MPY build directory: ${MPY_BUILD_ROOT#$ROOT_DIR/}"
    return 0
  fi

  if [[ ! -f "$MPY_VERSION_ARTIFACT" ]]; then
    echo "missing MPY version artifact: ${MPY_VERSION_ARTIFACT#$ROOT_DIR/}"
    return 0
  fi

  if [[ ! -d "$MPY_LIB_ROOT" ]]; then
    echo "missing staged target lib directory: ${MPY_LIB_ROOT#$ROOT_DIR/}"
    return 0
  fi

  metadata_reason="$(mpy_build_info_rebuild_reason)"
  if [[ -n "$metadata_reason" ]]; then
    echo "$metadata_reason"
    return 0
  fi

  source_version="$(get_project_version)"
  artifact_version="$(get_mpy_artifact_version "$MPY_VERSION_ARTIFACT")"
  if [[ "$source_version" != "unknown-version" && -z "$artifact_version" ]]; then
    echo "could not read MPY artifact version from ${MPY_VERSION_ARTIFACT#$ROOT_DIR/}"
    return 0
  fi
  if [[ "$source_version" != "unknown-version" && "$source_version" != "$artifact_version" ]]; then
    echo "source version $source_version does not match MPY artifact version $artifact_version"
    return 0
  fi

  while IFS= read -r -d '' src; do
    rel_path="${src#$ROOT_DIR/}"
    artifact="$MPY_TARGET_ROOT/${rel_path%.py}.mpy"

    if [[ ! -f "$artifact" ]]; then
      echo "missing MPY artifact for $rel_path"
      return 0
    fi

    if [[ "$src" -nt "$artifact" ]]; then
      echo "source newer than MPY artifact for $rel_path"
      return 0
    fi
  done < <(find "$ROOT_DIR/cpynodus_ii" -type f -name '*.py' -print0)
}

run_mpy_build_if_needed() {
  local reason=""

  reason="$(find_mpy_rebuild_reason)"
  if [[ -z "$reason" ]]; then
    return 0
  fi

  if [[ ! -x "$MPY_BUILD_SCRIPT" ]]; then
    echo "MPY build is stale, but build script is not executable: ${MPY_BUILD_SCRIPT#$ROOT_DIR/}" >&2
    echo "Reason: $reason" >&2
    exit 1
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    echo "Would run ${MPY_BUILD_SCRIPT#$ROOT_DIR/} --target $MPY_TARGET before --content $CONTENT deploy: $reason"
    MPY_REBUILD_DEFERRED=1
    return 0
  fi

  echo "Running ${MPY_BUILD_SCRIPT#$ROOT_DIR/} --target $MPY_TARGET before --content $CONTENT deploy: $reason"
  "$MPY_BUILD_SCRIPT" --target "$MPY_TARGET"
}

validate_mpy_build() {
  local src=""
  local rel_path=""
  local artifact=""
  local metadata_reason=""
  local missing=0
  local stale=0
  local count=0

  if [[ ! -d "$MPY_BUILD_ROOT" ]]; then
    echo "Missing MPY build directory: $MPY_BUILD_ROOT" >&2
    echo "Compile cpynodus_ii modules before using --content $CONTENT." >&2
    exit 1
  fi

  metadata_reason="$(mpy_build_info_rebuild_reason)"
  if [[ -n "$metadata_reason" ]]; then
    echo "MPY build metadata does not match --content $CONTENT: $metadata_reason" >&2
    echo "Run ${MPY_BUILD_SCRIPT#$ROOT_DIR/} --target $MPY_TARGET first." >&2
    exit 1
  fi

  while IFS= read -r -d '' src; do
    rel_path="${src#$ROOT_DIR/}"
    artifact="$MPY_TARGET_ROOT/${rel_path%.py}.mpy"
    count=$((count + 1))

    if [[ ! -f "$artifact" ]]; then
      echo "Missing MPY artifact for $rel_path: ${artifact#$ROOT_DIR/}" >&2
      missing=1
      continue
    fi

    if [[ "$src" -nt "$artifact" ]]; then
      echo "Stale MPY artifact for $rel_path: ${artifact#$ROOT_DIR/}" >&2
      stale=1
    fi
  done < <(find "$ROOT_DIR/cpynodus_ii" -type f -name '*.py' -print0)

  if [[ $count -eq 0 ]]; then
    echo "No cpynodus_ii source modules found to validate for --content mpy." >&2
    exit 1
  fi

  if [[ $missing -ne 0 || $stale -ne 0 ]]; then
    echo "MPY build is incomplete or stale; refusing --content $CONTENT deploy." >&2
    exit 1
  fi

  if [[ ! -d "$MPY_LIB_ROOT" ]]; then
    echo "Missing staged target lib directory: $MPY_LIB_ROOT" >&2
    echo "Run ${MPY_BUILD_SCRIPT#$ROOT_DIR/} --target $MPY_TARGET first." >&2
    exit 1
  fi
}

validate_manifest_entry() {
  local rel_path="$1"
  [[ -n "$rel_path" ]] || return 1
  [[ "$rel_path" != /* ]] || return 1
  [[ "$rel_path" != *".."* ]] || return 1
  return 0
}

remote_shell_quote() {
  local value="$1"
  printf "'%s'" "${value//\'/\'\\\'\'}"
}

prune_deprecated_targets() {
  local destination="$1"
  local rel_path=""
  local target_ref=""

  if [[ ! -f "$DEPRECATED_MANIFEST" ]]; then
    echo "Deprecated manifest not found: $DEPRECATED_MANIFEST" >&2
    exit 1
  fi

  while IFS= read -r rel_path || [[ -n "$rel_path" ]]; do
    rel_path="${rel_path%%#*}"
    rel_path="${rel_path%"${rel_path##*[![:space:]]}"}"
    rel_path="${rel_path#"${rel_path%%[![:space:]]*}"}"
    [[ -n "$rel_path" ]] || continue

    if ! validate_manifest_entry "$rel_path"; then
      echo "Skipping invalid deprecated target entry: $rel_path" >&2
      continue
    fi

    target_ref="${destination%/}/$rel_path"
    if [[ "$TARGET" == *:* ]]; then
      if [[ $DRY_RUN -eq 1 ]]; then
        echo "Would remove deprecated remote target path: $target_ref"
      else
        ssh "${TARGET%%:*}" "TARGET_PATH=$(remote_shell_quote "${TARGET_PATH_ONLY%/}/$rel_path"); if [ -e \"\$TARGET_PATH\" ]; then rm -rf -- \"\$TARGET_PATH\" && echo \"Removed deprecated remote target path: \$TARGET_PATH\"; fi"
      fi
    else
      if [[ $DRY_RUN -eq 1 ]]; then
        if [[ -e "$target_ref" ]]; then
          echo "Would remove deprecated local target path: $target_ref"
        fi
      else
        if [[ -e "$target_ref" ]]; then
          rm -rf -- "$target_ref"
          echo "Removed deprecated local target path: $target_ref"
        fi
      fi
    fi
  done < "$DEPRECATED_MANIFEST"
}

clear_postmortem_logs() {
  local destination="$1"
  local log_name
  local target_ref

  for log_name in "_reboot.log" "_recovery.log"; do
    target_ref="${destination%/}/$log_name"

    if [[ "$TARGET" == *:* ]]; then
      if [[ $DRY_RUN -eq 1 ]]; then
        echo "Would remove remote postmortem log: $target_ref"
      else
        ssh "${TARGET%%:*}" "TARGET_PATH=$(remote_shell_quote "${TARGET_PATH_ONLY%/}/$log_name"); if [ -e \"\$TARGET_PATH\" ]; then rm -f -- \"\$TARGET_PATH\" && echo \"Removed remote postmortem log: \$TARGET_PATH\"; fi"
      fi
    else
      if [[ $DRY_RUN -eq 1 ]]; then
        if [[ -e "$target_ref" ]]; then
          echo "Would remove local postmortem log: $target_ref"
        else
          echo "Would remove local postmortem log if present: $target_ref"
        fi
      else
        if [[ -e "$target_ref" ]]; then
          rm -f -- "$target_ref"
          echo "Removed local postmortem log: $target_ref"
        fi
      fi
    fi
  done
}

remove_mpy_shadow_py_targets() {
  local destination="$1"
  local artifact=""
  local rel_path=""
  local py_rel_path=""
  local target_ref=""

  while IFS= read -r -d '' artifact; do
    rel_path="${artifact#$MPY_TARGET_ROOT/}"
    py_rel_path="${rel_path%.mpy}.py"

    if ! validate_manifest_entry "$py_rel_path"; then
      echo "Skipping invalid MPY shadow target path: $py_rel_path" >&2
      continue
    fi

    target_ref="${destination%/}/$py_rel_path"

    if [[ "$TARGET" == *:* ]]; then
      if [[ $DRY_RUN -eq 1 ]]; then
        echo "Would remove remote MPY-shadowed source if present: $target_ref"
      else
        ssh "${TARGET%%:*}" "TARGET_PATH=$(remote_shell_quote "${TARGET_PATH_ONLY%/}/$py_rel_path"); if [ -f \"\$TARGET_PATH\" ]; then rm -f -- \"\$TARGET_PATH\" && echo \"Removed remote MPY-shadowed source: \$TARGET_PATH\"; fi"
      fi
    else
      if [[ $DRY_RUN -eq 1 ]]; then
        if [[ -f "$target_ref" ]]; then
          echo "Would remove local MPY-shadowed source: $target_ref"
        else
          echo "Would remove local MPY-shadowed source if present: $target_ref"
        fi
      else
        if [[ -f "$target_ref" ]]; then
          rm -f -- "$target_ref"
          echo "Removed local MPY-shadowed source: $target_ref"
        fi
      fi
    fi
  done < <(find "$MPY_BUILD_ROOT" -type f -name '*.mpy' -print0)
}

remove_runtime_shadow_mpy_targets() {
  local destination="$1"
  local package_ref="${destination%/}/cpynodus_ii"
  local found=0
  local target_ref=""

  if [[ "$TARGET" == *:* ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "Would remove remote runtime MPY files if present under: $package_ref"
    else
      ssh "${TARGET%%:*}" "TARGET_PATH=$(remote_shell_quote "${TARGET_PATH_ONLY%/}/cpynodus_ii"); if [ -d \"\$TARGET_PATH\" ]; then find \"\$TARGET_PATH\" -type f -name '*.mpy' -print -exec rm -f -- {} \\; | while IFS= read -r path; do echo \"Removed remote runtime MPY: \$path\"; done; fi"
    fi
    return 0
  fi

  if [[ ! -d "$package_ref" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "Would remove local runtime MPY files if present under: $package_ref"
    fi
    return 0
  fi

  while IFS= read -r -d '' target_ref; do
    found=1
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "Would remove local runtime MPY: $target_ref"
    else
      rm -f -- "$target_ref"
      echo "Removed local runtime MPY: $target_ref"
    fi
  done < <(find "$package_ref" -type f -name '*.mpy' -print0)

  if [[ $DRY_RUN -eq 1 && $found -eq 0 ]]; then
    echo "No local runtime MPY files found under: $package_ref"
  fi
}

run_runtime_sync() {
  local destination="$1"
  local root_runtime_files=()
  local f

  for f in \
    "$ROOT_DIR/boot.py" \
    "$ROOT_DIR/code.py" \
    "$ROOT_DIR/safemode.py" \
    "$ROOT_DIR/dataclasses.py"; do
    if [[ -f "$f" ]]; then
      root_runtime_files+=("$f")
    fi
  done

  if [[ ${#root_runtime_files[@]} -eq 0 && ! -d "$ROOT_DIR/boards" && ! -d "$ROOT_DIR/cpynodus_ii" ]]; then
    echo "No runtime deployable files found in $ROOT_DIR" >&2
    exit 1
  fi

  if [[ ${#root_runtime_files[@]} -gt 0 ]]; then
    rsync "${RSYNC_ARGS[@]}" "${root_runtime_files[@]}" "$destination"
  fi

  if [[ -d "$ROOT_DIR/boards" ]]; then
    rsync "${RSYNC_ARGS[@]}" "$ROOT_DIR/boards/" "$destination/boards/"
  fi

  if [[ -d "$ROOT_DIR/cpynodus_ii" ]]; then
    remove_runtime_shadow_mpy_targets "$destination"
    rsync "${RSYNC_ARGS[@]}" "$ROOT_DIR/cpynodus_ii/" "$destination/cpynodus_ii/"
  fi

}

run_mpy_sync() {
  local destination="$1"
  local root_runtime_files=()
  local f

  run_mpy_build_if_needed
  if [[ $MPY_REBUILD_DEFERRED -eq 0 ]]; then
    validate_mpy_build
  else
    echo "Skipping strict MPY validation because dry-run did not rebuild artifacts."
  fi

  for f in \
    "$ROOT_DIR/boot.py" \
    "$ROOT_DIR/code.py" \
    "$ROOT_DIR/safemode.py" \
    "$ROOT_DIR/dataclasses.py"; do
    if [[ -f "$f" ]]; then
      root_runtime_files+=("$f")
    fi
  done

  if [[ ${#root_runtime_files[@]} -eq 0 && ! -d "$ROOT_DIR/boards" && ! -d "$MPY_BUILD_ROOT" && ! -d "$MPY_LIB_ROOT" ]]; then
    echo "No MPY deployable files found in $ROOT_DIR" >&2
    exit 1
  fi

  if [[ ${#root_runtime_files[@]} -gt 0 ]]; then
    rsync "${RSYNC_ARGS[@]}" "${root_runtime_files[@]}" "$destination"
  fi

  if [[ -d "$ROOT_DIR/boards" ]]; then
    rsync "${RSYNC_ARGS[@]}" "$ROOT_DIR/boards/" "$destination/boards/"
  fi

  if [[ -d "$MPY_BUILD_ROOT" && $MPY_REBUILD_DEFERRED -eq 0 ]]; then
    remove_mpy_shadow_py_targets "$destination"

    rsync "${RSYNC_ARGS[@]}" "$MPY_BUILD_ROOT/" "$destination/cpynodus_ii/"
  elif [[ $MPY_REBUILD_DEFERRED -eq 1 ]]; then
    echo "Would sync compiled package after MPY build: ${MPY_BUILD_ROOT#$ROOT_DIR/}/ -> $destination/cpynodus_ii/"
  fi

  if [[ -d "$MPY_LIB_ROOT" && $MPY_REBUILD_DEFERRED -eq 0 ]]; then
    rsync "${RSYNC_ARGS[@]}" "$MPY_LIB_ROOT/" "$destination/lib/"
  elif [[ $MPY_REBUILD_DEFERRED -eq 1 ]]; then
    echo "Would sync staged target libraries after MPY build: ${MPY_LIB_ROOT#$ROOT_DIR/}/ -> $destination/lib/"
  fi
}

if [[ "$TARGET" == *:* ]]; then
  DEST="$TARGET/"
  echo "Deploying to remote target ($DEPLOY_MODE, content=$CONTENT): $TARGET"
  case "$CONTENT" in
    runtime) run_runtime_sync "$DEST" ;;
    *-mpy) run_mpy_sync "$DEST" ;;
    *) run_full_sync "$DEST" ;;
  esac
else
  if [[ ! -d "$TARGET" ]]; then
    mkdir -p "$TARGET"
  fi
  DEST="$TARGET/"
  echo "Deploying to local target ($DEPLOY_MODE, content=$CONTENT): $TARGET"
  case "$CONTENT" in
    runtime) run_runtime_sync "$DEST" ;;
    *-mpy) run_mpy_sync "$DEST" ;;
    *) run_full_sync "$DEST" ;;
  esac
fi

sync_ota_public_key "$DEST"

if [[ $PRUNE_DEPRECATED -eq 1 ]]; then
  prune_deprecated_targets "$DEST"
fi

if [[ $CLEAR_REBOOT_LOG -eq 1 ]]; then
  clear_postmortem_logs "$DEST"
fi

PROJECT_VERSION="$(get_project_version)"
echo "$PROJECT_VERSION deployment complete..."
