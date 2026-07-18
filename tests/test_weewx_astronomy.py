"""Host-side tests for the WeeWX astronomy report extension."""

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "weewx"
    / "bin"
    / "user"
    / "nodus_astronomy.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("nodus_astronomy", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase_labels_cover_the_eight_lunar_octants():
    module = _module()

    assert module._phase_label(0) == "New Moon"
    assert module._phase_label(45) == "Waxing Crescent"
    assert module._phase_label(90) == "First Quarter"
    assert module._phase_label(180) == "Full Moon"
    assert module._phase_label(315) == "Waning Crescent"
    assert module._phase_label(360) == "New Moon"


def test_station_number_accepts_weewx_altitude_units():
    module = _module()

    assert module._station_number("1782, meter") == 1782.0
    assert module._station_number("-108.27") == -108.27
    assert module._station_number("invalid", 4.0) == 4.0


def test_local_canvas_angle_uses_screen_xy_orientation():
    module = _module()

    angle = module._local_canvas_angle(
        97.6625859,
        11.6091865,
        118.9809424,
        68.7473271,
    )

    assert angle == pytest.approx(278.8643, abs=0.001)


def test_search_list_base64_encodes_static_astronomy_payload(monkeypatch):
    module = _module()
    expected = {
        "ok": True,
        "source": "skyfield",
        "moon_phase_label": "Waxing Crescent",
        "points": [[0, -20.0, 10.0]],
    }
    captured = {}

    def fake_build(latitude, longitude, altitude, tzinfo, ephemeris):
        captured.update(
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            ephemeris=ephemeris,
        )
        return expected

    monkeypatch.setattr(module, "build_astronomy_payload", fake_build)
    generator = SimpleNamespace(
        config_dict={
            "Station": {
                "latitude": "32.79",
                "longitude": "-108.27",
                "altitude": "1782, meter",
            }
        },
        skin_dict={"NodusAstronomy": {"ephemeris": "/data/de421.bsp"}},
    )

    extension = module.NodusAstronomy(generator)
    encoded = extension.nodus_astronomy["payload_b64"]

    assert json.loads(base64.b64decode(encoded)) == expected
    assert captured == {
        "latitude": 32.79,
        "longitude": -108.27,
        "altitude": 1782.0,
        "ephemeris": "/data/de421.bsp",
    }
