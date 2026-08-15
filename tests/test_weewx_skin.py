"""Test the structure of the host-side Nodus WeeWX skin.

The assertions inspect templates, assets, configuration, and generated-data
hooks so packaging changes preserve the intended dashboard contract.
"""

from pathlib import Path

SKIN_ROOT = Path(__file__).resolve().parents[1] / "integrations" / "weewx" / "Nodus"
IDENTITY_EXTENSION = SKIN_ROOT.parent / "bin" / "user" / "nodus_identity.py"
UNITS_EXTENSION = SKIN_ROOT.parent / "bin" / "user" / "nodus_units.py"
SCHEMA_EXTENSION = SKIN_ROOT.parent / "bin" / "user" / "nodus_schema.py"
SWITCH_EXTENSION = SKIN_ROOT.parent / "bin" / "user" / "nodus_switch.py"


def test_nodus_refreshes_and_hides_unavailable_observations():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")

    assert '<meta http-equiv="refresh"' not in template
    assert (
        '<div class="brand-title">\n      <button class="setup-gear graph-launch"'
        in template
    )
    header = template.split('<div class="brand-title">', 1)[1].split("</div>", 1)[0]
    assert header.index('id="nodusGraphumOpen"') < header.index("Nodus AI")
    assert header.index("Nodus AI") < header.index("system-setup-gear")
    assert 'class="setup-gear system-setup-gear"' in template
    assert 'id="nodusGraphumOpen"' in template
    assert '<h1>Nodus AI Graphum</h1>' in template
    assert 'id="nodusGraphumClose" class="circle-close"' in template
    assert '<span class="data-updated-label">Data Updated:</span>' in template
    assert "As of:" not in template
    assert "Indoor Metrics" not in template
    assert "font-size: clamp(1.7rem, 3vw, 2.4rem);" in stylesheet
    assert "text-align: center;" in stylesheet
    assert template.index('class="brand-title"') < template.index(
        'class="data-updated"'
    )
    assert template.index('class="data-updated"') < template.index('class="astro-grid"')
    assert template.index('class="astro-grid"') < template.index(
        'class="metric-heading"'
    )
    assert template.index('class="metric-heading"') < template.index(
        'class="device-identity"'
    )
    assert template.index('class="device-identity"') < template.index(
        'class="brand-sub"'
    )
    assert template.index('class="brand-sub"') < template.index('class="tile-grid"')
    assert '<div class="brand-sub">$nodus.description</div>' in template
    for observation in (
        "absoluteHumidity",
        "dewpoint",
        "dewpointDepression",
        "dewVpdRisk",
        "inHumidity",
        "inTemp",
        "pressure",
        "vpd",
    ):
        assert "#if $current.{}.raw is not None".format(observation) in template


def test_nodus_graphum_uses_live_archive_and_switch_selectors():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    script = (SKIN_ROOT / "dashboard.js").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")

    assert 'id="graphumMetricOptions"' in template
    assert 'id="graphumSwitchOptions"' in template
    assert 'id="graphumSelectionCount">0 of 4' in template
    assert 'fetch(`${nodusApiOrigin}/api/history`' in script
    assert '"Content-Type":"text/plain;charset=UTF-8"' in script
    assert "selected.length > 4" in script
    assert "window.setInterval(nodusLoadGraphum, 15000)" in script
    assert (
        "const start=timestamps.length?Math.min(...timestamps):requestedStart"
        in script
    )
    assert "const end=start+duration" in script
    assert "context.arc(x,y,2.5,0,Math.PI*2)" in script
    assert ".graphum.active { display:flex; }" in stylesheet
    assert ".graphum-controls" in stylesheet


def test_nodus_metric_cards_are_alphabetical():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    labels = (
        "Absolute Humidity",
        "Dew Point",
        "Dewpoint Depression",
        "DewVPD Risk",
        "Inside Humidity",
        "Inside Temp",
        "Station Pressure",
        "VPD",
    )

    positions = [
        template.index("<header>{}</header>".format(label)) for label in labels
    ]
    assert positions == sorted(positions)


def test_nodus_preserves_requested_metric_precision():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    skin_conf = (SKIN_ROOT / "skin.conf").read_text(encoding="utf-8")

    assert '$current.vpd.format("%.3f")' in template
    assert "        kPa = %.3f" in skin_conf
    assert "        vpd = %.3f" in skin_conf


def test_nodus_bundle_includes_the_copied_stylesheet():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    skin_conf = (SKIN_ROOT / "skin.conf").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")

    copy_once = (
        "copy_once = pico.min.css, style.css, dashboard.js, "
        "moon-surface.png, nodus-favicon.svg"
    )
    assert copy_once in skin_conf
    assert 'href="pico.min.css?v=2.1.1"' in template
    assert "cdn.jsdelivr.net" not in template
    assert (SKIN_ROOT / "pico.min.css").is_file()
    assert 'href="style.css?v=20260814-3"' in template
    assert 'src="dashboard.js?v=20260814-3"' in template
    assert (SKIN_ROOT / "moon-surface.png").is_file()
    assert ".tile-grid" in stylesheet
    assert "@media (prefers-color-scheme: dark)" in stylesheet


def test_nodus_uses_shared_n_svg_favicon_on_every_html_surface():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    admin = (SKIN_ROOT / "admin" / "index.html").read_text(encoding="utf-8")
    system = (SKIN_ROOT / "system" / "index.html").read_text(encoding="utf-8")
    favicon = (SKIN_ROOT / "nodus-favicon.svg").read_text(encoding="utf-8")

    assert 'href="nodus-favicon.svg" type="image/svg+xml"' in template
    assert 'href="../nodus-favicon.svg" type="image/svg+xml"' in admin
    assert 'href="nodus-favicon.svg" type="image/svg+xml"' in system
    assert '<title id="title">Nodus favicon</title>' in favicon
    assert 'id="nodus-gradient"' in favicon


def test_system_info_uses_requested_five_five_four_grid():
    page = (SKIN_ROOT / "system" / "index.html").read_text(encoding="utf-8")
    script = (SKIN_ROOT / "system" / "system.js").read_text(encoding="utf-8")
    version = (SKIN_ROOT / "system" / "nodus-ai-version.js").read_text(
        encoding="utf-8"
    )
    stylesheet = (SKIN_ROOT / "system" / "system.css").read_text(encoding="utf-8")
    labels = (
        "Hostname",
        "HTTP Port",
        "MQTT Broker",
        "MQTT Port",
        "TLS",
        "Station",
        "Lat",
        "Long",
        "Alt",
        "Time Zone",
        "WeeWX Service",
        "WeeWX Config",
        "Database",
        "Dashboard",
    )

    positions = [script.index('["{}"'.format(label)) for label in labels]
    assert positions == sorted(positions)
    assert '<details id="weewx-preferences" class="setup-group">' in page
    assert '<summary>WeeWX Preferences</summary>' in page
    assert '<summary>Station &amp; MQTT</summary>' in page
    assert '<summary>WeeWX Runtime &amp; Files</summary>' in page
    assert 'id="station-info" class="info-grid"' in page
    assert 'id="weewx-info" class="info-grid runtime-grid"' in page
    assert "grid-template-columns:repeat(5,minmax(0,1fr));" in stylesheet
    assert "grid-template-columns:repeat(4,minmax(0,1fr));" in stylesheet
    assert ".runtime-grid .info-card strong { font-size:70%; }" in stylesheet
    assert 'data-view="install-device"' in page
    assert 'id="install-form"' in page
    assert 'id="install-device-list"' in page
    assert 'api("/api/install"' in script
    assert 'src="nodus-ai-version.js"' in page
    assert 'window.NODUS_AI_VERSION = "v0.26.226.1"' in version


def test_nodus_skin_uses_current_name_everywhere():
    readme = (SKIN_ROOT / "README.md").read_text(encoding="utf-8")
    skin_conf = (SKIN_ROOT / "skin.conf").read_text(encoding="utf-8")

    assert SKIN_ROOT.name == "Nodus"
    assert "    skin = Nodus" in skin_conf
    assert "    site_title = Nodus Automatio Instrumentorum" in skin_conf
    assert "    nodus_title = Nodus Automatio Instrumentorum" in skin_conf
    assert "Nodus WeeWX skin files" in readme


def test_nodus_identifies_device_and_firmware():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    skin_conf = (SKIN_ROOT / "skin.conf").read_text(encoding="utf-8")

    assert "$nodus.device_id" in template
    assert "$nodus.version" in template
    assert "user.nodus_identity.NodusIdentity" in skin_conf
    assert "[NodusIdentity]" in skin_conf
    assert "    device_id = unknown" in skin_conf
    assert "    firmware_version = unknown" in skin_conf
    assert IDENTITY_EXTENSION.is_file()
    assert UNITS_EXTENSION.is_file()
    assert SCHEMA_EXTENSION.is_file()


def test_nodus_shows_host_side_switch_automation_status():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    skin_conf = (SKIN_ROOT / "skin.conf").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")

    assert "user.nodus_automation.NodusAutomationStatus" in skin_conf
    assert "[NodusAutomationStatus]" in skin_conf
    assert "$nodus_automation.enabled" in template
    assert 'class="automation-grid"' in template
    assert "<header>$rule.name : $rule.enable_state</header>" in template
    assert "$rule.last_action" in template
    assert ".automation-card" in stylesheet


def test_nodus_shows_discovered_switches_without_automation_rules():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    skin_conf = (SKIN_ROOT / "skin.conf").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")
    script = (SKIN_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert "user.nodus_switch.NodusSwitchStatusSearchList" in skin_conf
    assert "[NodusSwitchStatus]" in skin_conf
    assert "#if $nodus_switch.channels" in template
    assert '<section class="switch-panel">' in template
    assert "$channel.label" in template
    assert "<span>$channel.channel_id</span>" in template
    assert "$channel.state" in template
    assert "$channel.automation" in template
    assert "$event.display" in template
    assert "mode-$channel.mode_class" in template
    assert 'data-channel-id="$channel.channel_id"' in template
    assert 'src="dashboard.js?v=20260814-3"' in template
    assert ".switch-current.mode-automated" in stylesheet
    assert ".switch-current.mode-automated .switch-control-mode" in stylesheet
    assert "grid-template-columns: minmax(150px, 1.05fr)" in stylesheet
    assert "white-space: nowrap;" in stylesheet
    assert "width: calc(50% - 12px);" in stylesheet
    assert "height: calc(5.625rem + 28px);" in stylesheet
    assert "overflow-y: auto;" in stylesheet
    assert "font-size: 75%;" in stylesheet
    assert "white-space: nowrap;" in stylesheet
    assert ".switch-panel" in stylesheet
    assert "const nodusGuardUntil = {};" in script
    assert 'nodusApi("/api/switch/toggle"' in script
    assert "(result.guard_seconds || 5) * 1000" in script
    assert 'element.addEventListener("click", nodusToggle)' in script
    assert SWITCH_EXTENSION.is_file()


def test_nodus_skin_embeds_skyfield_sun_and_moon_cards():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    skin_conf = (SKIN_ROOT / "skin.conf").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")
    script = (SKIN_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert "user.nodus_astronomy.NodusAstronomy" in skin_conf
    assert "ephemeris = /var/lib/weewx/skyfield/de421.bsp" in skin_conf
    assert 'data-astronomy="$nodus_astronomy.payload_b64"' in template
    assert 'data-astronomy-detail="astronomy.txt?ts=$current.dateTime.raw"' in template
    assert (SKIN_ROOT / "astronomy.txt.tmpl").read_text(encoding="utf-8").strip() == (
        "$nodus_astronomy.detail_payload_b64"
    )
    assert "template = astronomy.txt.tmpl" in skin_conf
    assert 'id="moonPhaseCanvas"' in template
    assert 'id="sunMoonPositionCanvas"' in template
    assert 'data-moon-view="local"' in template
    assert 'data-moon-view="reference"' in template
    assert ".astro-grid" in stylesheet
    assert "const horizon = pad + (innerHeight * 0.54);" in script
    assert "function nodusPlaceTimeLabel(id, raw)" in script
    assert "element.style.left" in script
    assert "grid-template-columns: minmax(82px, 1fr) 145px" in stylesheet
    assert "height: 175px;" in stylesheet
    assert "function nodusDrawMoon(data)" in script
    assert 'nodusMoonSurfaceImage.src = "moon-surface.png?v=1"' in script
    assert "if (surfacePixels)" in script
    assert "function nodusDrawPositions(data)" in script
    assert "function nodusSmoothSkyYMapper(" in script
    assert "function nodusOrbitDisplayPoints(" in script
    assert "function nodusSmoothElevationPoints(" in script
    assert "function nodusDrawPositionPath(" in script
    assert "context.bezierCurveTo(" in script
    assert "if (moonDisplay.length < 2)" in script
    assert 'id="sunMoon29Card"' in template
    assert 'id="sunMoon29Canvas"' in template
    assert "function nodusDraw29Days(data)" in script
    assert "function nodusSet29DayOpen(open)" in script
    assert "function nodusLoadAstronomyDetail()" in script
    assert 'grid.classList.toggle("astronomy-expanded", open)' in script
    assert ".astronomy-expanded-card" in stylesheet


def test_nodus_skin_refreshes_only_when_visible_and_report_changes():
    script = (SKIN_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert 'fetch("index.html", {method: "HEAD", cache: "no-cache"})' in script
    assert "Date.parse(document.lastModified)" in script
    assert "validator !== nodusReportValidator" in script
    assert "document.hidden" in script
    assert "function nodusStopPresentationRefresh()" in script
    assert (
        'document.addEventListener("visibilitychange", nodusHandleVisibility)'
        in script
    )


def test_nodus_skin_links_sensor_and_switch_gears_to_limited_admin_ui():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")

    assert 'href="setup/#sensor"' in template
    assert 'href="setup/#switch"' in template
    assert template.count('class="setup-gear-icon"') == 3
    assert "⚙" not in template
    assert ".setup-gear-icon" in stylesheet
    assert "window.location.hostname" not in template
    for name in ("index.html", "admin.css", "admin.js"):
        assert (SKIN_ROOT / "admin" / name).is_file()
    for name in ("index.html", "system.css", "system.js"):
        assert (SKIN_ROOT / "system" / name).is_file()


def test_nodus_dashboard_shows_manager_liveness_and_install_badges():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    script = (SKIN_ROOT / "dashboard.js").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")

    assert 'id="device-manager-state"' in template
    assert template.index('class="device-status-dot"') < template.index(
        "<strong>Device:</strong>"
    )
    assert 'id="device-manager-badges"' in template
    assert "nodusManagerOrigin" in script
    assert 'device?.installed, "Installed"' in script
    assert 'device?.discovered, "Discovered"' in script
    assert ".device-status-dot.online" in stylesheet


def test_nodus_admin_ui_labels_automation_threshold_units():
    script = (SKIN_ROOT / "admin" / "admin.js").read_text(encoding="utf-8")

    assert "state.metric_options" in script
    assert "ON threshold (${unit})" in script
    assert "OFF threshold (${unit})" in script
    assert "pattern=" not in (SKIN_ROOT / "admin" / "index.html").read_text(
        encoding="utf-8"
    )


def test_nodus_admin_ui_uses_sensorius_style_sensor_and_switch_views():
    page = (SKIN_ROOT / "admin" / "index.html").read_text(encoding="utf-8")
    script = (SKIN_ROOT / "admin" / "admin.js").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "admin" / "admin.css").read_text(encoding="utf-8")

    for view in (
        "sensor-settings",
        "sensor-calibration",
        "sensor-info",
        "switch-settings",
        "automations",
        "switch-info",
    ):
        assert 'data-view="{}"'.format(view) in page
    assert "System Calibration" not in page
    assert 'class="circle-close" href="../"' in page
    assert ">Dashboard</a>" not in page
    assert 'id="switch-labels"' in page
    assert 'id="automation-editor"' in page
    assert page.index('class="workspace"') < page.index('id="notice"')
    assert page.index('id="notice"') < page.index('id="sensor-settings"')
    assert 'href="admin.css"' in page
    assert 'src="admin.js"' in page
    assert "location.hostname}:8767`" in script
    assert '"#sensor": "sensor-settings"' in script
    assert '"#switch": "switch-settings"' in script
    assert 'window.addEventListener("hashchange", selectSetupView)' in script
    assert "view.hidden = view.id !== selected" in script
    assert "Channel label for switch_${channel.index}" in script
    assert "Sending switch settings to Nodus over MQTT" in script
    assert "Nodus confirmed the switch settings save" in script
    assert "Nodus save failed" in script
    assert ".view-card[hidden]" in stylesheet
    assert ".settings-dialog" in stylesheet
    assert "#notice.pending" in stylesheet
    assert "width: min(50%, 560px);" in stylesheet
    assert "#switch-setting-fields { align-content: start;" in stylesheet
    assert "max-width: 980px;" in stylesheet
    assert "height: 44px;" in stylesheet


def test_nodus_automations_use_system_navigation_context():
    admin_page = (SKIN_ROOT / "admin" / "index.html").read_text(encoding="utf-8")
    admin_script = (SKIN_ROOT / "admin" / "admin.js").read_text(encoding="utf-8")
    system_page = (SKIN_ROOT / "system" / "index.html").read_text(encoding="utf-8")
    system_script = (SKIN_ROOT / "system" / "system.js").read_text(
        encoding="utf-8"
    )

    switch_nav = admin_page.split('id="switch-nav"', 1)[1].split("</nav>", 1)[0]
    system_nav = admin_page.split('id="system-nav"', 1)[1].split("</nav>", 1)[0]
    assert "Automations" not in switch_nav
    assert "General Settings" in system_nav
    assert 'data-view="automations"' in system_nav
    assert "Install Device" in system_nav
    assert "Remove Device" in system_nav
    assert 'id="automations-link"' in system_page
    assert "const automationView = selected.startsWith(\"automation\")" in admin_script
    assert '$("system-nav").hidden = !automationView' in admin_script
    assert "location.hostname}:8768`" in admin_script
    assert "location.hostname}:8767`" in system_script
    assert "System Settings" not in system_page
    assert 'class="circle-close"' in system_page


def test_nodus_admin_info_groups_are_expandable():
    script = (SKIN_ROOT / "admin" / "admin.js").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "admin" / "admin.css").read_text(encoding="utf-8")

    assert 'document.createElement("details")' in script
    assert "details.open = true" in script
    assert 'document.createElement("summary")' in script
    assert ".info-group[open] summary::before" in stylesheet


def test_nodus_generates_24_hour_card_micrographs():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    skin_conf = (SKIN_ROOT / "skin.conf").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")

    observations = (
        "absoluteHumidity",
        "dewpoint",
        "dewpointDepression",
        "dewVpdRisk",
        "inHumidity",
        "inTemp",
        "pressure",
        "vpd",
    )
    assert "weewx.imagegenerator.ImageGenerator" in skin_conf
    assert "        time_length = 24h" in skin_conf
    assert "    image_height = 190" in skin_conf
    assert "    skip_if_empty = true" in skin_conf
    for color in (
        "#ffffff",
        "#f7f9fa",
        "#d9e4e8",
        "#2864dc",
        "#747b85",
    ):
        assert ' = "{}"'.format(color) in skin_conf
    assert " = #" not in skin_conf
    for observation in observations:
        assert "[[[micro_{}]]]".format(observation) in skin_conf
        image = 'src="micro_{}.png?ts=$current.dateTime.raw"'.format(observation)
        assert image in template
        assert template.index(image) < template.index(
            '<div class="value">', template.index(image)
        )
    assert ".micrograph" in stylesheet
    assert "font-size: clamp(1.8rem, 2.2vw, 2.6rem);" in stylesheet
    assert "  min-height: 0;" in stylesheet
