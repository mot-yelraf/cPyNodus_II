#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKIN_SOURCE="$ROOT_DIR/Nodus"
USER_SOURCE="$ROOT_DIR/bin/user"
WEEWX_CONFIG="/etc/weewx/weewx.conf"
WEEWX_USER_ROOT="/etc/weewx/bin/user"
WEEWX_SKIN_ROOT="/etc/weewx/skins/Nodus"
WEEWX_PYTHON_ROOT="/usr/share/weewx"
MQTTSUBSCRIBE_URL="https://github.com/weewx-mqtt/subscribe/archive/refs/tags/v3.1.1.zip"
DRY_RUN=0
INSPECT_CONFIG=""
INSTALL_TEMP_CONFIG=""
INSTALL_TARGET_CONFIG=""
INSTALL_BACKUP_CONFIG=""
INSTALL_TARGET_SERVICE=""
INSTALL_SERVICE_WAS_ACTIVE=0
INSTALL_ROLLBACK_NEEDED=0
DEVICE_ID_ARG=""
UPDATE_PROFILE=0

if [[ $EUID -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

usage() {
  cat <<'EOF'
Install the Nodus MQTT/WeeWX integration on a Debian or Raspberry Pi OS host.

Usage:
  ./integrations/weewx/install_nodus_weewx.sh [--dry-run]
  ./integrations/weewx/install_nodus_weewx.sh [--device-id ID] [--update-profile]
  ./integrations/weewx/install_nodus_weewx.sh --inspect-config PATH
  ./integrations/weewx/install_nodus_weewx.sh --help

Behavior:
  * Offers to install the WeeWX 5 Debian package when WeeWX is absent.
  * Preserves the primary WeeWX instance and creates
    /etc/weewx/nodus.conf, managed by weewx@nodus.service.
  * Installs MQTTSubscribe 3.1.1 only when it is missing.
  * Installs the Nodus skin, identity, switch status, units, schema, and automation services.
  * Restores the prior Nodus configuration if validation or startup fails.

Options:
  --dry-run             Inspect and prompt, but do not modify the system.
  --device-id ID        Select integrations/weewx/ID.toml explicitly.
  --update-profile      Prompt using saved values, then update ID.toml.
  --inspect-config PATH Print the detected installation mode and exit.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  printf '%s\n' "$*"
}

cleanup_install() {
  local status=$?
  set +e
  if [[ $status -ne 0 && $INSTALL_ROLLBACK_NEEDED -eq 1 ]]; then
    echo "ERROR: installation failed; restoring the prior Nodus configuration." >&2
    if [[ -n "$INSTALL_BACKUP_CONFIG" && -e "$INSTALL_BACKUP_CONFIG" ]]; then
      run_root cp -a "$INSTALL_BACKUP_CONFIG" "$INSTALL_TARGET_CONFIG"
    elif [[ -n "$INSTALL_TARGET_CONFIG" ]]; then
      run_root rm -f "$INSTALL_TARGET_CONFIG"
    fi
    if [[ $INSTALL_SERVICE_WAS_ACTIVE -eq 1 && -n "$INSTALL_TARGET_SERVICE" ]]; then
      run_root systemctl start "$INSTALL_TARGET_SERVICE"
    fi
  fi
  if [[ -n "$INSTALL_TEMP_CONFIG" ]]; then
    rm -f "$INSTALL_TEMP_CONFIG"
  fi
  exit "$status"
}

trap cleanup_install EXIT

run_root() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf 'DRY-RUN:'
    printf ' %q' "${SUDO[@]}" "$@"
    printf '\n'
    return 0
  fi
  "${SUDO[@]}" "$@"
}

run_weewx() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf 'DRY-RUN: run as weewx:'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  if [[ $EUID -eq 0 ]]; then
    runuser -u weewx -- "$@"
  else
    sudo -u weewx "$@"
  fi
}

validate_mqttsubscribe_driver() {
  local config="$1"
  local python_path="$WEEWX_PYTHON_ROOT"
  if [[ -n "${PYTHONPATH:-}" ]]; then
    python_path="$python_path:$PYTHONPATH"
  fi
  [[ -d "$WEEWX_PYTHON_ROOT/weeutil" ]] || \
    die "WeeWX Python modules not found under $WEEWX_PYTHON_ROOT."
  run_weewx env "PYTHONPATH=$python_path" \
    python3 "$WEEWX_USER_ROOT/MQTTSubscribe.py" \
    configure driver --validate --conf "$config"
}

ask_yes_no() {
  local prompt="$1"
  local default="${2:-no}"
  local suffix="[y/N]"
  local answer=""
  if [[ "$default" == "yes" ]]; then
    suffix="[Y/n]"
  fi
  while true; do
    read -r -p "$prompt $suffix " answer
    answer="${answer:-$default}"
    case "${answer,,}" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *) echo "Please answer yes or no." ;;
    esac
  done
}

prompt_value() {
  local prompt="$1"
  local default="${2:-}"
  local value=""
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " value
    PROMPT_VALUE="${value:-$default}"
  else
    read -r -p "$prompt: " value
    PROMPT_VALUE="$value"
  fi
}

prompt_secret() {
  local prompt="$1"
  local value=""
  read -r -s -p "$prompt (leave empty for none): " value
  printf '\n'
  PROMPT_VALUE="$value"
}

prompt_saved_secret() {
  local prompt="$1"
  local value=""
  read -r -s -p "$prompt (leave empty to keep saved value): " value
  printf '\n'
  PROMPT_VALUE="$value"
}

profile_value() {
  local path="$1"
  local section="$2"
  local key="$3"
  python3 - "$path" "$section" "$key" <<'PY'
import sys
import tomllib

path, section, key = sys.argv[1:]
with open(path, "rb") as handle:
    data = tomllib.load(handle)
value = data.get(section, {}) if section else data
value = value.get(key, "") if isinstance(value, dict) else ""
if isinstance(value, bool):
    print("true" if value else "false")
elif value is not None:
    print(value)
PY
}

write_device_profile() {
  local path="$1"
  local device_id="$2"
  local family="$3"
  local location="$4"
  local latitude="$5"
  local longitude="$6"
  local altitude="$7"
  local broker="$8"
  local port="$9"
  shift 9
  local base_topic="$1"
  local username="$2"
  local password="$3"
  local tls_enable="$4"
  local ca_certs="$5"
  local temporary=""

  local q_device_id q_family q_location q_altitude q_broker q_base_topic
  local q_username q_password q_ca_certs
  conf_escape "$device_id"; q_device_id="$CONF_ESCAPED"
  conf_escape "$family"; q_family="$CONF_ESCAPED"
  conf_escape "$location"; q_location="$CONF_ESCAPED"
  conf_escape "$altitude"; q_altitude="$CONF_ESCAPED"
  conf_escape "$broker"; q_broker="$CONF_ESCAPED"
  conf_escape "$base_topic"; q_base_topic="$CONF_ESCAPED"
  conf_escape "$username"; q_username="$CONF_ESCAPED"
  conf_escape "$password"; q_password="$CONF_ESCAPED"
  conf_escape "$ca_certs"; q_ca_certs="$CONF_ESCAPED"

  temporary="$(mktemp "${path}.tmp.XXXXXX")"
  chmod 600 "$temporary"
  cat >"$temporary" <<EOF
schema = 1
device_id = "$q_device_id"
sensor_family = "$q_family"

[station]
description = "$q_location"
latitude = $latitude
longitude = $longitude
altitude = "$q_altitude"

[mqtt]
broker = "$q_broker"
port = $port
base_topic = "$q_base_topic"
username = "$q_username"
password = "$q_password"
use_tls = $tls_enable
ca_certs = "$q_ca_certs"
EOF
  mv "$temporary" "$path"
  chmod 600 "$path"
}

configured_device_id() {
  local config="$1"
  [[ -r "$config" ]] || return 0
  python3 - "$config" <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
matches = re.findall(r"\[\[\[([^]\n]+)/data\]\]\]", text)
if len(matches) == 1:
    print(matches[0].rsplit("/", 1)[-1])
PY
}

resolve_profile_device_id() {
  if [[ -n "$DEVICE_ID_ARG" ]]; then
    printf '%s\n' "$DEVICE_ID_ARG"
    return
  fi
  local configured=""
  configured="$(configured_device_id /etc/weewx/nodus.conf)"
  if [[ -n "$configured" && -f "$ROOT_DIR/$configured.toml" ]]; then
    printf '%s\n' "$configured"
    return
  fi
  local profiles=()
  shopt -s nullglob
  profiles=("$ROOT_DIR"/*.toml)
  shopt -u nullglob
  if (( ${#profiles[@]} == 1 )); then
    basename "${profiles[0]}" .toml
  fi
}

prompt_coordinate() {
  local kind="$1"
  local current="${2:-}"
  while true; do
    prompt_value "${kind^} in decimal degrees" "$current"
    current="$PROMPT_VALUE"
    if valid_coordinate "$current" "$kind"; then
      PROMPT_VALUE="$current"
      return 0
    fi
    echo "Invalid $kind: $current"
    current=""
  done
}

ini_value() {
  local path="$1"
  local section="$2"
  local key="$3"
  awk -v wanted_section="$section" -v wanted_key="$key" '
    function trim(value) {
      sub(/^[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      return value
    }
    /^[[:space:]]*\[[^[][^]]*\][[:space:]]*$/ {
      current = $0
      gsub(/^[[:space:]]*\[|\][[:space:]]*$/, "", current)
      next
    }
    current == wanted_section {
      line = $0
      if (line ~ "^[[:space:]]*" wanted_key "[[:space:]]*=") {
        sub("^[[:space:]]*" wanted_key "[[:space:]]*=[[:space:]]*", "", line)
        sub(/[[:space:]]+#.*$/, "", line)
        line = trim(line)
        if (line ~ /^\".*\"$/) {
          line = substr(line, 2, length(line) - 2)
        }
        print line
        exit
      }
    }
  ' "$path"
}

valid_coordinate() {
  local value="$1"
  local kind="$2"
  [[ "$value" =~ ^-?[0-9]+([.][0-9]+)?$ ]] || return 1
  if [[ "$kind" == "latitude" ]]; then
    awk -v value="$value" 'BEGIN { exit !(value >= -90 && value <= 90) }'
  else
    awk -v value="$value" 'BEGIN { exit !(value >= -180 && value <= 180) }'
  fi
}

detect_install_mode() {
  local config="$1"
  local station_type=""
  local driver=""
  local latitude=""
  local longitude=""
  [[ -r "$config" ]] || die "WeeWX configuration is not readable: $config"

  station_type="$(ini_value "$config" Station station_type)"
  latitude="$(ini_value "$config" Station latitude)"
  longitude="$(ini_value "$config" Station longitude)"
  if [[ -n "$station_type" ]]; then
    driver="$(ini_value "$config" "$station_type" driver)"
  fi

  DETECTED_STATION_TYPE="$station_type"
  DETECTED_DRIVER="$driver"
  DETECTED_LATITUDE="$latitude"
  DETECTED_LONGITUDE="$longitude"
  DETECTED_MODE="separate"
}

print_detection() {
  local config="$1"
  detect_install_mode "$config"
  cat <<EOF
config=$config
mode=$DETECTED_MODE
station_type=${DETECTED_STATION_TYPE:-none}
driver=${DETECTED_DRIVER:-none}
latitude=${DETECTED_LATITUDE:-unset}
longitude=${DETECTED_LONGITUDE:-unset}
EOF
}

ensure_debian_host() {
  [[ -r /etc/os-release ]] || die "This installer requires Debian or Raspberry Pi OS."
  # shellcheck disable=SC1091
  source /etc/os-release
  local family="${ID:-} ${ID_LIKE:-}"
  [[ "$family" == *debian* ]] || die "This installer supports Debian-family package installations only."
  command -v systemctl >/dev/null 2>&1 || die "systemd is required."
}

install_weewx_package() {
  if command -v weectl >/dev/null 2>&1 && [[ -r "$WEEWX_CONFIG" ]]; then
    return 0
  fi
  log "WeeWX is not installed as a standard Debian package."
  if ! ask_yes_no "Install WeeWX from the official WeeWX apt repository?" no; then
    log "WeeWX installation declined. Nothing was changed."
    exit 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY-RUN: would configure the official WeeWX apt repository and install weewx."
    exit 0
  fi

  run_root apt-get update
  run_root apt-get install -y wget gnupg ca-certificates
  if [[ ! -f /etc/apt/trusted.gpg.d/weewx.gpg ]]; then
    local key_file=""
    key_file="$(mktemp)"
    wget -qO "$key_file" https://weewx.com/keys.html
    "${SUDO[@]}" gpg --dearmor --yes \
      --output /etc/apt/trusted.gpg.d/weewx.gpg "$key_file"
    rm -f "$key_file"
  fi
  if [[ ! -f /etc/apt/sources.list.d/weewx.list ]]; then
    printf '%s\n' 'deb [arch=all] https://weewx.com/apt/python3 buster main' | \
      "${SUDO[@]}" tee /etc/apt/sources.list.d/weewx.list >/dev/null
  fi
  run_root apt-get update
  log "The WeeWX package installer will request station information."
  run_root apt-get install weewx

  command -v weectl >/dev/null 2>&1 || die "weectl was not installed."
  [[ -r "$WEEWX_CONFIG" ]] || die "Expected configuration not found: $WEEWX_CONFIG"
}

weewx_major_version() {
  local output=""
  output="$(weectl --version 2>&1 || true)"
  if [[ "$output" =~ ([0-9]+)\.[0-9]+ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    printf '0\n'
  fi
}

conf_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  CONF_ESCAPED="$value"
}

add_field() {
  local source_name="$1"
  local observation_name="$2"
  FIELD_STANZAS+="
            [[[[values_${source_name}]]]]
                ignore = false
                name = ${observation_name}
                conversion_type = float
"
}

build_field_stanzas() {
  local family="$1"
  FIELD_STANZAS=""
  if [[ "$family" != "soil" ]]; then
    add_field "Temperature" "inTemp"
    add_field "Rel-Humidity" "inHumidity"
    add_field "Dew Point" "dewpoint"
    add_field "Ambient VPD" "vpd"
    add_field "Humidity" "absoluteHumidity"
    add_field "Dew Point Deficit" "dewpointDepression"
    add_field "DewVPD Risk" "dewVpdRisk"
  fi
  case "$family" in
    avpd)
      add_field "Baro-Pressure" "pressure"
      ;;
    aqi)
      add_field "Baro-Pressure" "pressure"
      add_field "Gas" "gasResistance"
      add_field "Air Quality" "airQuality"
      ;;
    co2)
      add_field "CO2" "co2"
      ;;
    soil)
      add_field "Soil Temp_C" "soilTemperature"
      add_field "Soil Moisture" "soilMoisturePct"
      add_field "Soil Moisture Deficit" "soilMoistureDeficit"
      add_field "Soil Stress Index" "soilStressIndex"
      add_field "Soil pH" "soilPh"
      add_field "Soil EC" "soilEc"
      add_field "Soil Nitrogen" "soilNitrogen"
      add_field "Soil Phosphorus" "soilPhosphorus"
      add_field "Soil Potassium" "soilPotassium"
      add_field "Soil Fertility Index" "soilFertilityIndex"
      ;;
  esac
}

infer_family() {
  local device_id=""
  device_id="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$device_id" in
    aqi-*) printf 'aqi\n' ;;
    co2-*) printf 'co2\n' ;;
    soil-*) printf 'soil\n' ;;
    lux-*) printf 'lux\n' ;;
    aht-*|avpd-*|apvpd-*|apvpd_aht-*) printf 'avpd\n' ;;
    *) printf 'avpd\n' ;;
  esac
}

render_config() {
  local output="$1"
  local version="$2"
  local location="$3"
  local latitude="$4"
  local longitude="$5"
  local altitude="$6"
  local database_name="$7"
  local html_root="$8"
  local broker="$9"
  shift 9
  local port="$1"
  local username="$2"
  local password="$3"
  local tls_enable="$4"
  local ca_certs="$5"
  local topic="$6"
  local mqtt_module="$7"
  local admin_username="${8:-admin}"
  local admin_password="${9:-change-me}"
  local admin_port="${10:-8767}"

  conf_escape "$location"; location="$CONF_ESCAPED"
  conf_escape "$broker"; broker="$CONF_ESCAPED"
  conf_escape "$username"; username="$CONF_ESCAPED"
  conf_escape "$password"; password="$CONF_ESCAPED"
  conf_escape "$ca_certs"; ca_certs="$CONF_ESCAPED"
  conf_escape "$admin_username"; admin_username="$CONF_ESCAPED"
  conf_escape "$admin_password"; admin_password="$CONF_ESCAPED"

  cat >"$output" <<EOF
# Nodus WeeWX configuration generated by integrations/weewx/install_nodus_weewx.sh
debug = 0
version = $version

[Station]
    location = "$location"
    latitude = $latitude
    longitude = $longitude
    altitude = $altitude
    station_type = MQTTSubscribeDriver
    rain_year_start = 1
    week_start = 6

[StdRESTful]
    [[StationRegistry]]
        register_this_station = false

[StdReport]
    SKIN_ROOT = /etc/weewx/skins
    HTML_ROOT = $html_root
    data_binding = wx_binding
    log_success = true
    log_failure = true

    [[Nodus]]
        skin = Nodus
        HTML_ROOT = $html_root
        enable = true

    [[Defaults]]
        lang = en
        unit_system = metric

[StdConvert]
    target_unit = METRICWX

[StdCalibrate]
    [[Corrections]]

[StdQC]
    [[MinMax]]
        pressure = 700, 1100, hPa
        inTemp = -50, 80, degree_C
        inHumidity = 0, 100

[StdWXCalculate]
    [[Calculations]]
        pressure = prefer_hardware
        dewpoint = prefer_hardware
        inDewpoint = prefer_hardware

[StdTimeSynch]
    clock_check = 14400
    max_drift = 5

[StdArchive]
    archive_interval = 300
    record_generation = software
    loop_hilo = true
    data_binding = wx_binding

[DataBindings]
    [[wx_binding]]
        database = archive_sqlite
        table_name = archive
        manager = weewx.manager.DaySummaryManager
        schema = user.nodus_schema.schema

[Databases]
    [[archive_sqlite]]
        database_name = $database_name
        database_type = SQLite

[DatabaseTypes]
    [[SQLite]]
        driver = weedb.sqlite
        SQLITE_ROOT = /var/lib/weewx

[Engine]
    [[Services]]
        prep_services = weewx.engine.StdTimeSynch, user.nodus_units.NodusUnits
        data_services = $mqtt_module.MQTTSubscribeService, user.nodus_switch.NodusSwitchStatus, user.nodus_automation.NodusAutomation
        process_services = weewx.engine.StdConvert, weewx.engine.StdCalibrate, weewx.engine.StdQC, weewx.wxservices.StdWXCalculate
        xtype_services = weewx.wxxtypes.StdWXXTypes, weewx.wxxtypes.StdPressureCooker, weewx.wxxtypes.StdRainRater, weewx.wxxtypes.StdDelta
        archive_services = weewx.engine.StdArchive
        restful_services = weewx.restx.StdStationRegistry
        report_services = weewx.engine.StdPrint, weewx.engine.StdReport

[MQTTSubscribeService]
    enable = false
    stop_on_validation_errors = true

[MQTTSubscribeDriver]
    driver = $mqtt_module
    stop_on_validation_errors = true
    host = "$broker"
    port = $port
    username = "$username"
    password = "$password"

    [[tls]]
        enable = $tls_enable
        ca_certs = "$ca_certs"

    [[topics]]
        unit_system = METRICWX

        [[[message]]]
            type = json
            flatten_delimiter = _

        [[[$topic]]]
            subscribe = true
            ignore = true
$FIELD_STANZAS

[NodusAutomation]
    enabled = true
    status_file = /var/lib/weewx/nodus_automation.json
    rules_file = /var/lib/weewx/nodus_automation_rules.json
    runtime_file = /var/lib/weewx/nodus_automation_runtime.json
    stale_after = 180
    command_timeout = 15

[NodusSwitchStatus]
    enabled = true
    status_file = /var/lib/weewx/nodus_switch.json
    control_file = /var/lib/weewx/nodus_switch_control.json
    rules_file = /var/lib/weewx/nodus_automation_rules.json
    max_events = 20
    command_timeout = 15
    admin_enabled = true
    admin_require_auth = false
    admin_host = 0.0.0.0
    admin_port = $admin_port
    admin_username = "$admin_username"
    admin_password = "$admin_password"
    admin_web_root = /etc/weewx/skins/Nodus/admin
    dashboard_web_root = $html_root
EOF
  chmod 600 "$output"
}

ensure_mqttsubscribe() {
  local config="$1"
  if [[ -f "$WEEWX_USER_ROOT/MQTTSubscribe.py" ]]; then
    log "MQTTSubscribe is already installed."
    return 0
  fi
  log "Installing MQTTSubscribe 3.1.1."
  if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY-RUN: would install $MQTTSUBSCRIBE_URL"
    return 0
  fi
  run_weewx weectl extension install "$MQTTSUBSCRIBE_URL" \
    --config="$config" --yes
}

ensure_paho() {
  if python3 -c 'import paho.mqtt.client' >/dev/null 2>&1; then
    return 0
  fi
  log "Installing the Paho MQTT Python client."
  run_root apt-get update
  run_root apt-get install -y python3-paho-mqtt
}

ensure_astral() {
  if python3 -c 'from astral import Observer; from astral.sun import sun' \
    >/dev/null 2>&1; then
    return 0
  fi
  log "Installing the Astral Python library."
  run_root apt-get update
  run_root apt-get install -y python3-astral
}

validate_integration_sources() {
  for path in \
    "$SKIN_SOURCE/index.html.tmpl" \
    "$SKIN_SOURCE/skin.conf" \
    "$SKIN_SOURCE/style.css" \
    "$SKIN_SOURCE/dashboard.js" \
    "$SKIN_SOURCE/admin/index.html" \
    "$SKIN_SOURCE/admin/admin.css" \
    "$SKIN_SOURCE/admin/admin.js" \
    "$USER_SOURCE/nodus_identity.py" \
    "$USER_SOURCE/nodus_admin.py" \
    "$USER_SOURCE/nodus_switch.py" \
    "$USER_SOURCE/nodus_automation.py" \
    "$USER_SOURCE/nodus_units.py" \
    "$USER_SOURCE/nodus_schema.py"; do
    [[ -r "$path" ]] || die "Required repository file is missing: $path"
  done
}

install_integration_files() {
  local html_root="$1"

  run_root install -d -o root -g weewx -m 0775 \
    "$WEEWX_SKIN_ROOT" "$WEEWX_SKIN_ROOT/admin" "$WEEWX_USER_ROOT" \
    /var/lib/weewx "$html_root" "$html_root/setup"
  run_root install -o root -g weewx -m 0644 \
    "$SKIN_SOURCE/index.html.tmpl" "$WEEWX_SKIN_ROOT/index.html.tmpl"
  run_root install -o root -g weewx -m 0644 \
    "$SKIN_SOURCE/skin.conf" "$WEEWX_SKIN_ROOT/skin.conf"
  run_root install -o root -g weewx -m 0644 \
    "$SKIN_SOURCE/style.css" "$WEEWX_SKIN_ROOT/style.css"
  run_root install -o root -g weewx -m 0644 \
    "$SKIN_SOURCE/dashboard.js" "$WEEWX_SKIN_ROOT/dashboard.js"
  run_root install -o root -g weewx -m 0644 \
    "$SKIN_SOURCE/admin/index.html" "$WEEWX_SKIN_ROOT/admin/index.html"
  run_root install -o root -g weewx -m 0644 \
    "$SKIN_SOURCE/admin/admin.css" "$WEEWX_SKIN_ROOT/admin/admin.css"
  run_root install -o root -g weewx -m 0644 \
    "$SKIN_SOURCE/admin/admin.js" "$WEEWX_SKIN_ROOT/admin/admin.js"
  run_root install -o weewx -g weewx -m 0644 \
    "$SKIN_SOURCE/admin/index.html" "$html_root/setup/index.html"
  run_root install -o weewx -g weewx -m 0644 \
    "$SKIN_SOURCE/admin/admin.css" "$html_root/setup/admin.css"
  run_root install -o weewx -g weewx -m 0644 \
    "$SKIN_SOURCE/admin/admin.js" "$html_root/setup/admin.js"
  run_root install -o root -g weewx -m 0644 \
    "$USER_SOURCE/nodus_identity.py" "$WEEWX_USER_ROOT/nodus_identity.py"
  run_root install -o root -g weewx -m 0644 \
    "$USER_SOURCE/nodus_admin.py" "$WEEWX_USER_ROOT/nodus_admin.py"
  run_root install -o root -g weewx -m 0644 \
    "$USER_SOURCE/nodus_switch.py" "$WEEWX_USER_ROOT/nodus_switch.py"
  run_root install -o root -g weewx -m 0644 \
    "$USER_SOURCE/nodus_automation.py" "$WEEWX_USER_ROOT/nodus_automation.py"
  run_root install -o root -g weewx -m 0644 \
    "$USER_SOURCE/nodus_units.py" "$WEEWX_USER_ROOT/nodus_units.py"
  run_root install -o root -g weewx -m 0644 \
    "$USER_SOURCE/nodus_schema.py" "$WEEWX_USER_ROOT/nodus_schema.py"
  run_root install -o weewx -g weewx -m 0664 \
    "$SKIN_SOURCE/style.css" "$html_root/style.css"
  run_root install -o weewx -g weewx -m 0664 \
    "$SKIN_SOURCE/dashboard.js" "$html_root/dashboard.js"
}

main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      --device-id)
        [[ $# -ge 2 ]] || die "--device-id requires an ID."
        DEVICE_ID_ARG="$2"
        shift 2
        ;;
      --update-profile) UPDATE_PROFILE=1; shift ;;
      --inspect-config)
        [[ $# -ge 2 ]] || die "--inspect-config requires a path."
        INSPECT_CONFIG="$2"
        shift 2
        ;;
      --help|-h) usage; exit 0 ;;
      *) die "Unknown argument: $1" ;;
    esac
  done

  if [[ -n "$INSPECT_CONFIG" ]]; then
    print_detection "$INSPECT_CONFIG"
    exit 0
  fi

  validate_integration_sources
  ensure_debian_host
  install_weewx_package

  local major=""
  major="$(weewx_major_version)"
  (( major >= 5 )) || die "WeeWX 5 or later is required."
  detect_install_mode "$WEEWX_CONFIG"

  local mode="separate"
  local target_config="/etc/weewx/nodus.conf"
  local target_service="weewx@nodus.service"
  local database_name="nodus.sdb"
  local html_root="/var/www/html/weewx/nodus"
  systemctl cat weewx@.service >/dev/null 2>&1 || \
    die "The WeeWX package does not provide weewx@.service."
  log "Primary station detected: ${DETECTED_STATION_TYPE:-none} (${DETECTED_DRIVER:-none})"
  log "The primary weewx.service and $WEEWX_CONFIG will not be modified."

  local location=""
  local latitude="$DETECTED_LATITUDE"
  local longitude="$DETECTED_LONGITUDE"
  local altitude=""
  location="$(ini_value "$WEEWX_CONFIG" Station location)"
  altitude="$(ini_value "$WEEWX_CONFIG" Station altitude)"

  local broker=""
  local port="1883"
  local base_topic="nodus"
  local device_id=""
  local family=""
  local username=""
  local password=""
  local tls_enable="false"
  local ca_certs=""
  local mqtt_module="user.MQTTSubscribe"
  local admin_username="admin"
  local admin_password=""
  local admin_port="8767"
  local profile_path=""
  local profile_exists=0

  device_id="$(resolve_profile_device_id)"
  while [[ ! "$device_id" =~ ^[A-Za-z0-9_.-]+$ ]]; do
    prompt_value "Nodus device ID, for example aht-va41ka"
    device_id="$PROMPT_VALUE"
    if [[ ! "$device_id" =~ ^[A-Za-z0-9_.-]+$ ]]; then
      echo "Use only letters, digits, dot, underscore, and hyphen."
    fi
  done
  profile_path="$ROOT_DIR/$device_id.toml"
  if [[ -f "$profile_path" ]]; then
    profile_exists=1
    local stored_device_id=""
    stored_device_id="$(profile_value "$profile_path" "" device_id)"
    [[ "$stored_device_id" == "$device_id" ]] || \
      die "Profile device_id does not match its filename: $profile_path"
    family="$(profile_value "$profile_path" "" sensor_family)"
    location="$(profile_value "$profile_path" station description)"
    latitude="$(profile_value "$profile_path" station latitude)"
    longitude="$(profile_value "$profile_path" station longitude)"
    altitude="$(profile_value "$profile_path" station altitude)"
    broker="$(profile_value "$profile_path" mqtt broker)"
    port="$(profile_value "$profile_path" mqtt port)"
    base_topic="$(profile_value "$profile_path" mqtt base_topic)"
    username="$(profile_value "$profile_path" mqtt username)"
    password="$(profile_value "$profile_path" mqtt password)"
    tls_enable="$(profile_value "$profile_path" mqtt use_tls)"
    ca_certs="$(profile_value "$profile_path" mqtt ca_certs)"
  fi

  if (( profile_exists == 1 && UPDATE_PROFILE == 0 )); then
    log "Using saved device profile: $profile_path"
  else
    prompt_value "Station description" "${location:-Nodus Sensor}"
    location="$PROMPT_VALUE"
    prompt_coordinate latitude "$latitude"
    latitude="$PROMPT_VALUE"
    prompt_coordinate longitude "$longitude"
    longitude="$PROMPT_VALUE"
    prompt_value "Altitude with unit" "${altitude:-0, meter}"
    altitude="$PROMPT_VALUE"
    prompt_value "MQTT broker hostname or address" "${broker:-localhost}"
    broker="$PROMPT_VALUE"
    prompt_value "MQTT broker port" "${port:-1883}"
    port="$PROMPT_VALUE"
    prompt_value "MQTT base topic" "${base_topic:-nodus}"
    base_topic="${PROMPT_VALUE%/}"
    family="${family:-$(infer_family "$device_id")}"
    prompt_value "Sensor family (avpd, co2, aqi, or soil; lux requires manual setup)" "$family"
    family="${PROMPT_VALUE,,}"
    prompt_value "MQTT subscriber username" "$username"
    username="$PROMPT_VALUE"
    if [[ -n "$password" ]]; then
      prompt_saved_secret "MQTT subscriber password"
    else
      prompt_secret "MQTT subscriber password"
    fi
    if [[ -n "$PROMPT_VALUE" || -z "$password" ]]; then
      password="$PROMPT_VALUE"
    fi
    if [[ "$tls_enable" == "true" ]]; then
      if ! ask_yes_no "Use MQTT TLS?" yes; then
        tls_enable="false"
        ca_certs=""
      fi
    elif ask_yes_no "Use MQTT TLS?" no; then
      tls_enable="true"
    fi
    if [[ "$tls_enable" == "true" ]]; then
      prompt_value "CA certificate path" "$ca_certs"
      ca_certs="$PROMPT_VALUE"
    fi
  fi
  [[ "$family" =~ ^(avpd|co2|aqi|soil)$ ]] || die "Unsupported sensor family: $family"
  valid_coordinate "$latitude" latitude || die "Invalid latitude: $latitude"
  valid_coordinate "$longitude" longitude || die "Invalid longitude: $longitude"
  [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || \
    die "Invalid MQTT port: $port"
  base_topic="${base_topic%/}"
  [[ -n "$broker" && -n "$base_topic" ]] || die "Profile MQTT settings are incomplete."
  if [[ "$tls_enable" == "true" && -z "$ca_certs" ]]; then
    die "A CA certificate path is required for TLS."
  fi
  build_field_stanzas "$family"
  if [[ -f "$WEEWX_USER_ROOT/mqttsubscribe.py" ]]; then
    mqtt_module="user.mqttsubscribe"
  fi
  local topic="${base_topic}/${device_id}/data"
  local version=""
  version="$(ini_value "$WEEWX_CONFIG" "" version || true)"
  if [[ -z "$version" ]]; then
    version="$(weectl --version 2>&1 | grep -Eo '[0-9]+([.][0-9]+)+' | head -n 1)"
  fi
  version="${version:-5.0.0}"

  INSTALL_TEMP_CONFIG="$(mktemp)"
  render_config "$INSTALL_TEMP_CONFIG" "$version" "$location" "$latitude" \
    "$longitude" "$altitude" "$database_name" "$html_root" "$broker" \
    "$port" "$username" "$password" "$tls_enable" "$ca_certs" "$topic" \
    "$mqtt_module" "$admin_username" "$admin_password" "$admin_port"

  log ""
  log "Installation plan:"
  log "  mode:       $mode"
  log "  config:     $target_config"
  log "  service:    $target_service"
  log "  database:   /var/lib/weewx/$database_name"
  log "  report:     $html_root"
  log "  MQTT topic: $topic"
  log "  dashboard:  http://<this-host>/weewx/nodus/"
  log "  setup UI:   http://<this-host>/weewx/nodus/setup/"
  if ! ask_yes_no "Apply this installation?" no; then
    log "Installation cancelled. Nothing was changed."
    exit 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY-RUN: configuration rendered successfully; no changes applied."
    exit 0
  fi

  if (( profile_exists == 0 || UPDATE_PROFILE == 1 )); then
    write_device_profile "$profile_path" "$device_id" "$family" "$location" \
      "$latitude" "$longitude" "$altitude" "$broker" "$port" "$base_topic" \
      "$username" "$password" "$tls_enable" "$ca_certs"
    log "Saved device profile: $profile_path"
  fi

  local stamp=""
  stamp="$(date +%Y%m%d-%H%M%S)"
  INSTALL_TARGET_CONFIG="$target_config"
  INSTALL_TARGET_SERVICE="$target_service"
  if systemctl is-active --quiet "$target_service"; then
    INSTALL_SERVICE_WAS_ACTIVE=1
  fi
  if [[ -e "$target_config" ]]; then
    INSTALL_BACKUP_CONFIG="${target_config}.${stamp}.bak"
    run_root cp -a "$target_config" "$INSTALL_BACKUP_CONFIG"
  fi
  run_root systemctl stop "$target_service" || true
  INSTALL_ROLLBACK_NEEDED=1
  run_root install -d -o root -g weewx -m 0775 \
    /etc/weewx "$WEEWX_USER_ROOT"
  run_root install -o root -g weewx -m 0660 "$INSTALL_TEMP_CONFIG" "$target_config"

  ensure_paho
  ensure_astral
  install_integration_files "$html_root"
  ensure_mqttsubscribe "$target_config"
  # The extension installer may merge defaults into the target. Reinstall the
  # deterministic Nodus configuration after the extension files are present.
  run_root install -o root -g weewx -m 0660 "$INSTALL_TEMP_CONFIG" "$target_config"

  log "Validating MQTTSubscribe driver configuration."
  validate_mqttsubscribe_driver "$target_config"

  log "Generating the Nodus dashboard with the installed skin."
  if ! (cd /tmp && run_weewx weectl report run Nodus --config="$target_config"); then
    log "WARNING: initial dashboard generation failed; WeeWX will retry at the next archive interval."
  fi
  run_root systemctl enable --now "$target_service"
  run_root systemctl status "$target_service" --no-pager -l || true
  INSTALL_ROLLBACK_NEEDED=0

  log ""
  log "Nodus WeeWX installation complete."
  log "Configuration: $target_config"
  log "Service log:  sudo journalctl -u $target_service -f"
  log "Report output: $html_root/"
  log "Dashboard:    http://<this-host>/weewx/nodus/"
  log "Setup UI:     http://<this-host>/weewx/nodus/setup/"
  log "If no archive record exists yet, the first report appears after WeeWX archives one."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
