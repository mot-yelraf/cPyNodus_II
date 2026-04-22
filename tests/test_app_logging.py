from cpynodus_ii import app


def test_log_stamp_falls_back_to_elapsed_seconds_when_rtc_unset(monkeypatch):
    monkeypatch.setattr(app.time, "localtime", lambda *_args: (2000, 1, 1, 0, 0, 0, 5, 1, -1))
    monkeypatch.setattr(app.time, "monotonic", lambda: 12.7)

    assert app._log_stamp(10.0) == "2s"


def test_log_stamp_uses_local_datetime_when_rtc_is_valid(monkeypatch):
    monkeypatch.setattr(app.time, "localtime", lambda *_args: (2026, 4, 22, 8, 18, 23, 2, 112, -1))

    assert app._log_stamp(10.0) == "2026-04-22 08:18:23"
