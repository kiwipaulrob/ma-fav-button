import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "relay", Path(__file__).resolve().parents[1] / "ma-fav-button-relay.py")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def test_announce_target_rejects_synced_group_with_single_live_member():
    players = [
        {"player_id": "moode", "name": "moode-sendspin", "available": True,
         "active_group": "grp1"},
        {"player_id": "ghost", "name": "browser-sendspin", "available": False},
        {"player_id": "grp1", "name": "SendSpin", "available": True,
         "group_members": ["moode", "ghost"]},
    ]
    assert relay.announce_target(players, players[0])["player_id"] == "moode"


def test_announce_target_returns_group_when_two_members_live():
    players = [
        {"player_id": "moode", "name": "moode-sendspin", "available": True,
         "active_group": "grp1"},
        {"player_id": "other", "name": "kitchen-sendspin", "available": True},
        {"player_id": "grp1", "name": "SendSpin", "available": True,
         "group_members": ["moode", "other"]},
    ]
    assert relay.announce_target(players, players[0])["player_id"] == "grp1"


def test_resolve_active_prefers_paused_device_over_idle_moode(monkeypatch,
                                                              tmp_path):
    monkeypatch.setattr(relay, "LAST_FILE", str(tmp_path / "last.json"))
    monkeypatch.setattr(relay, "_LAST_MEM", None)
    players = [
        {"player_id": "moode", "name": "moode-sendspin", "available": True,
         "state": "idle"},
        {"player_id": "office", "name": "Office Speaker", "available": True,
         "state": "paused"},
    ]
    picked = relay.resolve_active(players, record_last=False)
    assert picked["player_id"] == "office"
    assert not (tmp_path / "last.json").exists()


def test_queue_cmd_handles_none_active_queue(monkeypatch):
    def fake_ma(cmd, args=None, mid="fav", timeout=25):
        if cmd == "player_queues/next":
            raise RuntimeError("QueueEmpty")
        if cmd == "player_queues/get_active_queue":
            return None
        return {}

    monkeypatch.setattr(relay, "ma", fake_ma)
    try:
        relay._queue_cmd("player_queues/next", "p1", "double", "next")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "QueueEmpty" in str(exc)


def test_dry_run_resolves_player_without_mutating_last(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "LAST_FILE", str(tmp_path / "last.json"))
    monkeypatch.setattr(relay, "_LAST_MEM", None)
    monkeypatch.setattr(
        relay, "ma",
        lambda cmd, args=None, mid="fav", timeout=25: [
            {"player_id": "moode", "name": "moode-sendspin", "available": True,
             "state": "playing", "volume_level": 25}])
    res = relay.handle_press("single", dry_run=True)
    assert res == {"toggled": False, "dry_run": True,
                   "would_action": "paused", "player": "moode-sendspin"}
    assert not (tmp_path / "last.json").exists()
