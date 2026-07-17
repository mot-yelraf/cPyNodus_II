"""Structural checks for the host-side Nodus WeeWX skin."""

from pathlib import Path

SKIN_ROOT = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "weewx"
    / "Nodus"
)
IDENTITY_EXTENSION = SKIN_ROOT.parent / "bin" / "user" / "nodus_identity.py"
UNITS_EXTENSION = SKIN_ROOT.parent / "bin" / "user" / "nodus_units.py"
SCHEMA_EXTENSION = SKIN_ROOT.parent / "bin" / "user" / "nodus_schema.py"


def test_nodus_refreshes_and_hides_unavailable_observations():
    template = (SKIN_ROOT / "index.html.tmpl").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")

    assert '<meta http-equiv="refresh" content="60" />' in template
    assert '<div class="brand-title">Nodus Sensor</div>' in template
    assert "font-size: clamp(1.7rem, 3vw, 2.4rem);" in stylesheet
    assert template.index('class="brand-title"') < template.index(
        'class="device-identity"'
    )
    assert template.index('class="device-identity"') < template.index(
        'class="brand-sub"'
    )
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
    skin_conf = (SKIN_ROOT / "skin.conf").read_text(encoding="utf-8")
    stylesheet = (SKIN_ROOT / "style.css").read_text(encoding="utf-8")

    assert "copy_once = style.css" in skin_conf
    assert ".tile-grid" in stylesheet
    assert "@media (prefers-color-scheme: dark)" in stylesheet


def test_nodus_skin_uses_current_name_everywhere():
    readme = (SKIN_ROOT / "README.md").read_text(encoding="utf-8")
    skin_conf = (SKIN_ROOT / "skin.conf").read_text(encoding="utf-8")

    assert SKIN_ROOT.name == "Nodus"
    assert "    skin = Nodus" in skin_conf
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
    assert '$nodus_automation.enabled' in template
    assert 'class="automation-grid"' in template
    assert "$rule.last_action" in template
    assert ".automation-card" in stylesheet


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
