"""Generate Skyfield-backed Sun and Moon data for the Nodus WeeWX skin."""

import base64
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from weewx.cheetahgenerator import SearchList
except ImportError:  # pragma: no cover - permits host-side unit tests
    class SearchList:
        """Small stand-in used when WeeWX is not installed."""

        def __init__(self, generator):
            self.generator = generator


log = logging.getLogger(__name__)

_DEFAULT_EPHEMERIS = "/var/lib/weewx/skyfield/de421.bsp"
_SAMPLE_MINUTES = 10


def _station_number(value, default=0.0):
    """Return the leading numeric station-config value."""
    try:
        return float(str(value).split(",", 1)[0].strip())
    except (TypeError, ValueError):
        return default


def _phase_label(degrees):
    """Return an eight-part lunar phase label for a 0..360 angle."""
    names = (
        "New Moon",
        "Waxing Crescent",
        "First Quarter",
        "Waxing Gibbous",
        "Full Moon",
        "Waning Gibbous",
        "Third Quarter",
        "Waning Crescent",
    )
    return names[int(((degrees % 360.0) + 22.5) // 45.0) % 8]


def _event_text(function, observer, day, tzinfo):
    """Return one Astral lunar event as an ISO local time string."""
    try:
        event = function(observer, date=day, tzinfo=tzinfo)
    except (ValueError, TypeError):
        return ""
    return event.astimezone(tzinfo).strftime("%H:%M") if event else ""


def _local_canvas_angle(moon_az, moon_el, sun_az, sun_el):
    """Return the bright-limb angle in HTML canvas screen coordinates."""

    def unit_vector(azimuth, elevation):
        azimuth = math.radians(azimuth)
        elevation = math.radians(elevation)
        return (
            math.cos(elevation) * math.sin(azimuth),
            math.cos(elevation) * math.cos(azimuth),
            math.sin(elevation),
        )

    def dot(left, right):
        return sum(left[index] * right[index] for index in range(3))

    def normalized(vector):
        magnitude = math.sqrt(dot(vector, vector))
        if not math.isfinite(magnitude) or magnitude < 1e-9:
            return None
        return tuple(value / magnitude for value in vector)

    moon_vector = unit_vector(moon_az, moon_el)
    sun_vector = unit_vector(sun_az, sun_el)
    projection = dot(sun_vector, moon_vector)
    bright = normalized(
        tuple(
            sun_vector[index] - (projection * moon_vector[index])
            for index in range(3)
        )
    )
    zenith = (0.0, 0.0, 1.0)
    zenith_projection = dot(zenith, moon_vector)
    screen_up = normalized(
        tuple(
            zenith[index] - (zenith_projection * moon_vector[index])
            for index in range(3)
        )
    )
    if bright is None or screen_up is None:
        return None
    screen_right = normalized(
        (
            (moon_vector[1] * screen_up[2]) - (moon_vector[2] * screen_up[1]),
            (moon_vector[2] * screen_up[0]) - (moon_vector[0] * screen_up[2]),
            (moon_vector[0] * screen_up[1]) - (moon_vector[1] * screen_up[0]),
        )
    )
    if screen_right is None:
        return None
    canvas_x = dot(bright, screen_right)
    canvas_y = -dot(bright, screen_up)
    return math.degrees(math.atan2(canvas_y, canvas_x)) % 360.0


def _next_phase(eph, ts, now):
    """Return the next exact principal lunar phase from Skyfield."""
    from skyfield import almanac

    end = now + timedelta(days=9)
    times, phases = almanac.find_discrete(
        ts.from_datetime(now),
        ts.from_datetime(end),
        almanac.moon_phases(eph),
    )
    if not len(times):
        return "", ""
    labels = ("New Moon", "First Quarter", "Full Moon", "Third Quarter")
    event = times[0].utc_datetime().astimezone(now.tzinfo)
    return labels[int(phases[0])], event.date().isoformat()


def _position_days(eph, ts, site, sun_body, moon_body, day_start):
    """Return compact two-hour Sun/Moon samples for a 29-day plot."""
    from skyfield import almanac

    days = []
    for day_offset in range(29):
        local_start = day_start + timedelta(days=day_offset)
        sun_points = []
        moon_points = []
        for minute in range(0, 1441, 60):
            sample = local_start + timedelta(minutes=minute)
            instant = ts.from_datetime(sample.astimezone(timezone.utc))
            apparent_site = site.at(instant)
            sun_alt, _sun_az, _sun_distance = (
                apparent_site.observe(sun_body).apparent().altaz()
            )
            moon_alt, _moon_az, _moon_distance = (
                apparent_site.observe(moon_body).apparent().altaz()
            )
            sun_points.append([minute, round(float(sun_alt.degrees), 2)])
            moon_points.append([minute, round(float(moon_alt.degrees), 2)])

        noon = local_start + timedelta(hours=12)
        noon_t = ts.from_datetime(noon.astimezone(timezone.utc))
        noon_site = site.at(noon_t)
        sun_apparent = noon_site.observe(sun_body).apparent()
        moon_apparent = noon_site.observe(moon_body).apparent()
        moon_alt, moon_az, _moon_distance = moon_apparent.altaz()
        sun_alt, sun_az, _sun_distance = sun_apparent.altaz()
        visible_angle = _local_canvas_angle(
            float(moon_az.degrees),
            float(moon_alt.degrees),
            float(sun_az.degrees),
            float(sun_alt.degrees),
        )
        phase_degrees = float(almanac.moon_phase(eph, noon_t).degrees) % 360.0
        lit_pct = int(
            round(float(moon_apparent.fraction_illuminated(sun_body)) * 100.0)
        )
        days.append(
            {
                "date": local_start.date().isoformat(),
                "label": local_start.strftime("%b%d"),
                "sun": sun_points,
                "moon": moon_points,
                "phase": round(phase_degrees, 3),
                "lit": lit_pct,
                "angle": (
                    round(visible_angle, 2)
                    if visible_angle is not None and math.isfinite(visible_angle)
                    else None
                ),
            }
        )
    return days


def build_astronomy_payload(
    latitude, longitude, altitude, tzinfo, ephemeris_path, now=None
):
    """Build one day of solar and lunar card data from local ephemerides."""
    from astral import Observer
    from astral import moon as astral_moon
    from astral.sun import sun
    from skyfield import almanac
    from skyfield.api import load, load_file, wgs84
    from skyfield.trigonometry import position_angle_of

    now = now or datetime.now(tzinfo)
    now = now.astimezone(tzinfo)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    observer = Observer(latitude=latitude, longitude=longitude, elevation=altitude)
    solar_events = sun(observer, date=now.date(), tzinfo=tzinfo)

    path = Path(ephemeris_path)
    if not path.is_file():
        raise FileNotFoundError("Skyfield ephemeris is missing: {}".format(path))
    eph = load_file(str(path))
    ts = load.timescale(builtin=True)
    site = eph["earth"] + wgs84.latlon(
        latitude,
        longitude,
        elevation_m=altitude,
    )
    sun_body = eph["sun"]
    moon_body = eph["moon"]

    points = []
    for minute in range(0, 1441, _SAMPLE_MINUTES):
        sample = day_start + timedelta(minutes=minute)
        instant = ts.from_datetime(sample.astimezone(timezone.utc))
        sun_alt, _sun_az, _sun_distance = (
            site.at(instant).observe(sun_body).apparent().altaz()
        )
        moon_alt, _moon_az, _moon_distance = (
            site.at(instant).observe(moon_body).apparent().altaz()
        )
        points.append(
            [
                minute,
                round(float(sun_alt.degrees), 2),
                round(float(moon_alt.degrees), 2),
            ]
        )

    current_t = ts.from_datetime(now.astimezone(timezone.utc))
    current_site = site.at(current_t)
    sun_apparent = current_site.observe(sun_body).apparent()
    moon_apparent = current_site.observe(moon_body).apparent()
    phase_degrees = float(almanac.moon_phase(eph, current_t).degrees) % 360.0
    lit_pct = int(round(float(moon_apparent.fraction_illuminated(sun_body)) * 100.0))
    moon_alt, moon_az, _moon_distance = moon_apparent.altaz()
    sun_alt, sun_az, _sun_distance = sun_apparent.altaz()
    visible_angle = _local_canvas_angle(
        float(moon_az.degrees),
        float(moon_alt.degrees),
        float(sun_az.degrees),
        float(sun_alt.degrees),
    )
    reference_angle = float(
        position_angle_of(moon_apparent.radec(), sun_apparent.radec()).degrees
    )
    next_phase_label, next_phase_date = _next_phase(eph, ts, now)
    position_29d = _position_days(
        eph,
        ts,
        site,
        sun_body,
        moon_body,
        day_start,
    )

    rise = _event_text(astral_moon.moonrise, observer, now.date(), tzinfo)
    setting = _event_text(astral_moon.moonset, observer, now.date(), tzinfo)
    return {
        "ok": True,
        "source": "skyfield",
        "latitude": round(latitude, 6),
        "sunrise": solar_events["sunrise"].strftime("%H:%M"),
        "sun_noon": solar_events["noon"].strftime("%H:%M"),
        "sunset": solar_events["sunset"].strftime("%H:%M"),
        "moonrise": rise,
        "moonset": setting,
        "moon_phase_degrees": round(phase_degrees, 3),
        "moon_phase_label": _phase_label(phase_degrees),
        "moon_lit_pct": lit_pct,
        "moon_visible_angle": (
            round(visible_angle, 2)
            if visible_angle is not None and math.isfinite(visible_angle)
            else None
        ),
        "moon_reference_angle": (
            round(reference_angle, 2) if math.isfinite(reference_angle) else None
        ),
        "moon_next_phase_label": next_phase_label,
        "moon_next_phase_date": next_phase_date,
        "current_minute": (now.hour * 60) + now.minute,
        "points": points,
        "position_29d": position_29d,
    }


def _encoded_payload(payload):
    """Encode JSON so Cheetah HTML entity filtering cannot alter it."""
    document = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return base64.b64encode(document.encode("utf-8")).decode("ascii")


class NodusAstronomy(SearchList):
    """Expose a self-contained astronomy payload to the Nodus skin."""

    def __init__(self, generator):
        super().__init__(generator)
        config = generator.config_dict
        station = config.get("Station", {})
        section = generator.skin_dict.get("NodusAstronomy", {})
        ephemeris = str(section.get("ephemeris") or _DEFAULT_EPHEMERIS)
        payload = {"ok": False, "error": "Astronomy data unavailable"}
        try:
            latitude = _station_number(station.get("latitude"))
            longitude = _station_number(station.get("longitude"))
            altitude = _station_number(station.get("altitude"))
            tzinfo = datetime.now().astimezone().tzinfo
            payload = build_astronomy_payload(
                latitude,
                longitude,
                altitude,
                tzinfo,
                ephemeris,
            )
        except Exception as exc:
            log.warning("Unable to generate Nodus astronomy cards: %s", exc)
            payload["error"] = str(exc)
        self.nodus_astronomy = {"payload_b64": _encoded_payload(payload)}
