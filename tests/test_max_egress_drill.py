"""The production drill must not touch host INPUT or block administrative access."""
import json
import sys

import pytest

from scripts import max_egress_drill as drill


@pytest.mark.parametrize("gateway", ["192.0.2.1", "192.0.2.99"])
def test_drill_scope_and_timer_before_injection(tmp_path, monkeypatch, gateway):
    calls = []
    container = [{"Config": {"Env": ["MAX_EGRESS_PROXY_URL=http://proxy.example.invalid:8080"]},
        "State": {"Pid": 12345},
        "NetworkSettings": {"Networks": {"test": {"Gateway": "192.0.2.1", "IPAddress": "192.0.2.2"}}}}]
    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            return json.dumps(container)
        if args[:2] == ["docker", "exec"]:
            return gateway + " STREAM test"
        return "active"
    monkeypatch.setattr(drill, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(drill, "run", run)
    monkeypatch.setattr(sys, "argv", ["drill", "start", "--container", "test", "--seconds", "300"])
    if gateway != "192.0.2.1":
        with pytest.raises(SystemExit, match="Refusing"):
            drill.main()
        assert not drill.STATE.exists()
        assert not any(c[0] == "systemd-run" for c in calls)
        return
    drill.main()
    timer = next(i for i, c in enumerate(calls) if c[0] == "systemd-run")
    injection = next(i for i, c in enumerate(calls) if "-I" in c)
    assert timer < injection
    assert "nsenter" in calls[timer] and calls[injection][0] == "nsenter"
    assert "INPUT" not in calls[injection]
    assert "OUTPUT" in calls[injection]
    assert "--dport" in calls[injection]
    assert "--target" in calls[injection] and "12345" in calls[injection]


@pytest.mark.parametrize("seconds", [0, 1201])
def test_drill_rejects_unbounded_duration(monkeypatch, seconds):
    monkeypatch.setattr(sys, "argv", ["drill", "start", "--container", "test", "--seconds", str(seconds)])
    with pytest.raises(SystemExit):
        drill.main()
