"""FAN_BRIDGE_TOKEN: writes need X-Fan-Token once it is set; reads stay open."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SETTINGS_PATH", os.path.join(tempfile.mkdtemp(), "settings.json"))
os.environ["FAN_BRIDGE_NO_LOOP"] = "1"

import app as fan_app  # noqa: E402
import power           # noqa: E402


def client(monkeypatch, token="", power_token=""):
    monkeypatch.setattr(fan_app, "TOKEN", token)
    monkeypatch.setattr(power, "TOKEN", power_token)
    return fan_app.app.test_client()


def test_no_token_set_keeps_writes_open(monkeypatch):
    c = client(monkeypatch)
    assert c.post("/api/settings", json={"load_floor_pct": 40}).status_code == 200


def test_writes_are_refused_without_the_token(monkeypatch):
    c = client(monkeypatch, token="s3cret")
    for path, body in (("/api/settings", {"jbod_host": "evil.example"}), ("/api/presets/apply", {"name": "Quiet"}),
                       ("/api/jbod/selftest", {}), ("/api/power", {"cards": {}})):
        r = c.post(path, json=body)
        assert r.status_code == 401 and r.get_json()["token"] is True, path
        assert c.post(path, json=body, headers={"X-Fan-Token": "wrong"}).status_code == 401, path
    assert fan_app.config.load()["jbod_host"] != "evil.example"


def test_reads_stay_open_and_the_token_lets_writes_through(monkeypatch):
    c = client(monkeypatch, token="s3cret")
    assert c.get("/api/state").status_code == 200
    assert c.get("/api/power").status_code == 200
    r = c.post("/api/settings", json={"load_floor_pct": 45}, headers={"X-Fan-Token": "s3cret"})
    assert r.status_code == 200 and r.get_json()["settings"]["load_floor_pct"] == 45


def test_power_token_still_opens_power_only(monkeypatch):
    c = client(monkeypatch, token="s3cret", power_token="pw")
    assert c.post("/api/power", json={"cards": {}}, headers={"X-Power-Token": "pw"}).status_code == 200
    assert c.post("/api/settings", json={"load_floor_pct": 40}, headers={"X-Power-Token": "pw"}).status_code == 401
    # the main token works for power too
    assert c.post("/api/power", json={"cards": {}}, headers={"X-Fan-Token": "s3cret"}).status_code == 200


def test_power_token_alone_behaves_as_before(monkeypatch):
    c = client(monkeypatch, power_token="pw")
    assert c.post("/api/power", json={"cards": {}}).status_code == 403
    assert c.post("/api/power", json={"cards": {}}, headers={"X-Power-Token": "pw"}).status_code == 200
