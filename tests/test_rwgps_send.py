"""Offline tests: sending a planned route to Ride with GPS.

No network: requests in windroute.rwgps is stubbed. Covers the API client's
upload -> poll-task -> make-public flow, and the web endpoint's gating (off unless
the host is configured, passphrase only when one is set, only real generated GPX files).
Run:  python tests/test_rwgps_send.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webapp
from windroute import rwgps


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, str(payload)

    def json(self):
        return self._payload


def _stub(post_resp, task_states):
    """Patch requests in rwgps; returns (restore, calls)."""
    calls = {"post": [], "get": [], "put": []}
    states = list(task_states)
    saved = (rwgps.requests.post, rwgps.requests.get, rwgps.requests.put)

    def fake_post(url, **k):
        calls["post"].append((url, k))
        return post_resp

    def fake_get(url, **k):
        calls["get"].append(url)
        return _Resp(200, {"task": states.pop(0)})
    def fake_put(url, **k):
        calls["put"].append((url, k))
        return _Resp(200, {"route": {"id": 555}})
    rwgps.requests.post, rwgps.requests.get, rwgps.requests.put = fake_post, fake_get, fake_put

    def restore():
        rwgps.requests.post, rwgps.requests.get, rwgps.requests.put = saved
    return restore, calls


def test_upload_polls_task_until_route_is_ready():
    done = {"id": 7, "status": "completed",
            "items": [{"item_type": "route", "item_id": 555, "item_url": "x"}]}
    restore, calls = _stub(_Resp(202, {"task": {"id": 7, "status": "pending"}}),
                           [{"id": 7, "status": "pending"}, done])
    try:
        task = rwgps.upload_route("K", "T", b"<gpx/>", "My ride", "desc", "a.gpx")
        route = rwgps.wait_for_task("K", "T", task, sleep=lambda s: None)
    finally:
        restore()
    assert route == {"id": 555, "url": "https://ridewithgps.com/routes/555"}
    url, kw = calls["post"][0]
    assert url.endswith("/routes.json")
    assert kw["files"]["file"][0] == "a.gpx" and kw["data"]["name"] == "My ride"
    assert kw["headers"]["x-rwgps-auth-token"] == "T"
    assert len(calls["get"]) == 2 and calls["get"][0].endswith("/tasks/7.json")


def test_send_route_makes_the_new_route_public():
    done = {"id": 7, "status": "completed", "items": [{"item_type": "route", "item_id": 555}]}
    restore, calls = _stub(_Resp(202, {"task": {"id": 7, "status": "pending"}}), [done])
    saved_sleep = rwgps.time.sleep
    rwgps.time.sleep = lambda s: None
    try:
        route = rwgps.send_route("K", "T", b"<gpx/>", "n")
    finally:
        rwgps.time.sleep = saved_sleep
        restore()
    url, kw = calls["put"][0]
    assert url.endswith("/routes/555.json") and kw["json"] == {"route": {"visibility": "public"}}
    assert route["id"] == 555 and "note" not in route


def test_failed_import_raises_with_codes():
    restore, _ = _stub(_Resp(202, {"task": {"id": 8, "status": "pending"}}),
                       [{"id": 8, "status": "failed", "errors": [{"code": "invalid_file"}]}])
    try:
        rwgps.send_route("K", "T", b"x", "n")
    except rwgps.RwgpsError as exc:
        assert "invalid_file" in str(exc)
    else:
        raise AssertionError("expected RwgpsError")
    finally:
        restore()


def test_slow_import_times_out():
    restore, _ = _stub(_Resp(202, {"task": {"id": 9, "status": "pending"}}),
                       [{"id": 9, "status": "pending"}] * 5)
    try:
        task = rwgps.upload_route("K", "T", b"x", "n")
        rwgps.wait_for_task("K", "T", task, max_wait_s=3, sleep=lambda s: None)
    except rwgps.RwgpsError as exc:
        assert "still importing" in str(exc)
    else:
        raise AssertionError("expected RwgpsError")
    finally:
        restore()


def _with_env(env):
    keys = ("RWGPS_API_KEY", "RWGPS_AUTH_TOKEN", "RWGPS_SEND_PASSPHRASE")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(env)

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


def test_endpoint_off_unless_configured():
    restore = _with_env({})
    saved = rwgps.default_creds_path
    rwgps.default_creds_path = lambda: webapp.OUT_DIR / "no-such-creds.json"
    try:
        r = webapp.app.test_client().post("/rwgps/send", json={"gpx": "abcd1234-0.gpx"})
        assert r.status_code == 404
    finally:
        rwgps.default_creds_path = saved
        restore()


def test_endpoint_without_passphrase_just_sends():
    restore = _with_env({"RWGPS_API_KEY": "K", "RWGPS_AUTH_TOKEN": "T"})
    gpx = webapp.OUT_DIR / "feedc0de-3.gpx"
    gpx.write_bytes(b"<gpx/>")
    saved = rwgps.send_route
    rwgps.send_route = lambda *a, **k: {"id": 2, "url": "https://ridewithgps.com/routes/2"}
    webapp._rwgps_sent.clear()
    webapp._rl_hits.clear()
    try:
        r = webapp.app.test_client().post("/rwgps/send", json={"gpx": gpx.name})
        assert r.status_code == 200 and r.get_json()["url"].endswith("/routes/2")
    finally:
        rwgps.send_route = saved
        gpx.unlink()
        restore()


def test_endpoint_checks_passphrase_and_sends_once():
    restore = _with_env({"RWGPS_API_KEY": "K", "RWGPS_AUTH_TOKEN": "T",
                         "RWGPS_SEND_PASSPHRASE": "open sesame"})
    gpx = webapp.OUT_DIR / "feedc0de-1.gpx"
    gpx.write_bytes(b"<gpx/>")
    sent = []
    saved = rwgps.send_route
    rwgps.send_route = lambda *a, **k: sent.append(a) or {"id": 1, "url": "https://ridewithgps.com/routes/1"}
    webapp._rwgps_sent.clear()
    webapp._rl_hits.clear()
    try:
        c = webapp.app.test_client()
        r = c.post("/rwgps/send", json={"gpx": gpx.name, "passphrase": "nope"})
        assert r.status_code == 403 and r.get_json()["passphrase"] is True
        r = c.post("/rwgps/send", json={"gpx": "../webapp.py", "passphrase": "open sesame"})
        assert r.status_code == 400
        r = c.post("/rwgps/send", json={"gpx": "feedc0de-2.gpx", "passphrase": "open sesame"})
        assert r.status_code == 410
        body = {"gpx": gpx.name, "passphrase": "open sesame", "name": "Oct 8 ride"}
        r = c.post("/rwgps/send", json=body)
        assert r.status_code == 200 and r.get_json()["url"].endswith("/routes/1")
        assert sent[0][2] == b"<gpx/>" and sent[0][3] == "Oct 8 ride"
        r = c.post("/rwgps/send", json=body)            # a second click: no duplicate
        assert r.status_code == 200 and len(sent) == 1
    finally:
        rwgps.send_route = saved
        gpx.unlink()
        restore()


def test_endpoint_uploads_the_tcx_twin_when_there_is_one():
    restore = _with_env({"RWGPS_API_KEY": "K", "RWGPS_AUTH_TOKEN": "T"})
    gpx = webapp.OUT_DIR / "feedc0de-4.gpx"
    tcx = gpx.with_suffix(".tcx")
    gpx.write_bytes(b"<gpx/>")
    tcx.write_bytes(b"<tcx/>")
    sent = []
    saved = rwgps.send_route
    rwgps.send_route = lambda *a, **k: sent.append((a, k)) or {"id": 3, "url": "u"}
    webapp._rwgps_sent.clear()
    webapp._rl_hits.clear()
    try:
        r = webapp.app.test_client().post(
            "/rwgps/send", json={"gpx": gpx.name, "filename": "oct08-25mi-loop-Swind.gpx"})
        assert r.status_code == 200
        (args, kw), = sent
        assert args[2] == b"<tcx/>" and kw["filename"] == "oct08-25mi-loop-Swind.tcx"
    finally:
        rwgps.send_route = saved
        gpx.unlink()
        tcx.unlink()
        restore()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  PASS ", name)
