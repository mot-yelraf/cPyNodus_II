"""Verify host web profiling against complete and interrupted HTTP responses.

Local HTTP fixtures exercise curl's actual transfer accounting without touching
a board or retaining configuration response bodies.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from testApparatus.profile_webui import probe, serial_delta


@pytest.fixture
def web_origin():
    """Serve a complete page and a deliberately truncated response."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "20" if self.path == "/setup" else "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield "http://127.0.0.1:{}".format(server.server_port)
    server.shutdown()
    server.server_close()
    worker.join()


def test_probe_distinguishes_http_success_from_complete_transfer(web_origin):
    complete = probe(web_origin, "/", 2)
    assert complete["complete"]
    assert complete["status"] == 200
    assert complete["bytes"] == complete["content_length"] == 2
    partial = probe(web_origin, "/setup", 2)
    assert partial["status"] == 200
    assert not partial["complete"]
    assert partial["bytes"] == 2
    assert partial["content_length"] == 20
    assert partial["curl_exit"] == 18


def test_serial_delta_preserves_partial_records_and_handles_truncation(tmp_path):
    log = tmp_path / "serial.log"
    log.write_bytes(b"old\nweb health phase=ready\nmemory phase=")
    offset, events = serial_delta(log, 4)
    assert events == ["web health phase=ready"]
    with log.open("ab") as capture:
        capture.write(b"periodic_gc\n")
    offset, events = serial_delta(log, offset)
    assert events == ["memory phase=periodic_gc"]
    log.write_bytes(b"web new\n")
    _, events = serial_delta(log, offset)
    assert events == ["web new"]
