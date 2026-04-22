from cpynodus_ii import app


def test_log_stamp_falls_back_to_elapsed_seconds_when_rtc_unset(monkeypatch):
    monkeypatch.setattr(app.time, "localtime", lambda *_args: (2000, 1, 1, 0, 0, 0, 5, 1, -1))
    monkeypatch.setattr(app.time, "monotonic", lambda: 12.7)

    assert app._log_stamp(10.0) == "2s"


def test_log_stamp_uses_local_datetime_when_rtc_is_valid(monkeypatch):
    monkeypatch.setattr(app.time, "localtime", lambda *_args: (2026, 4, 22, 8, 18, 23, 2, 112, -1))

    assert app._log_stamp(10.0) == "2026-04-22 08:18:23"


def test_collect_garbage_returns_true_when_collect_succeeds(monkeypatch):
    called = {"count": 0}

    def _collect():
        called["count"] += 1

    monkeypatch.setattr(app.gc, "collect", _collect)

    assert app._collect_garbage() is True
    assert called["count"] == 1


def test_collect_garbage_with_log_emits_gc_line(monkeypatch):
    lines = []

    monkeypatch.setattr(app, "_collect_garbage", lambda: True)
    monkeypatch.setattr(app, "_memory_summary", lambda: "free_mem=111 mem_alloc=222")
    monkeypatch.setattr(app, "_print_log", lambda prefix, message, *, start_monotonic: lines.append((prefix, message, start_monotonic)))

    assert app._collect_garbage_with_log(10.0, "post_web_start") is True
    assert lines == [("gc", "phase=post_web_start collected=True free_mem=111 mem_alloc=222", 10.0)]


def test_log_memory_checkpoint_emits_phase_and_memory(monkeypatch):
    lines = []

    monkeypatch.setattr(app, "_memory_summary", lambda: "free_mem=123 mem_alloc=456")
    monkeypatch.setattr(app, "_print_log", lambda prefix, message, *, start_monotonic: lines.append((prefix, message, start_monotonic)))

    app._log_memory_checkpoint(10.0, "post_web_start")

    assert lines == [("memory", "phase=post_web_start free_mem=123 mem_alloc=456", 10.0)]
