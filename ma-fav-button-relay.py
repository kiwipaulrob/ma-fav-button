#!/usr/bin/env python3
"""Favourite-this-song relay for HA dashboard / Zigbee button.

POST /fav-button {"source": "dashboard"|"single"|"hold"|"double", "dry_run": bool}
  1. Resolves the ACTIVE device by state each press (moode-sendspin while
     in use, else whichever non-TV device is playing, else last-used, else
     any paused device, else idle moode-sendspin - TVs excluded).
  2. single->toggle play/pause, double->next track,
     else favourite + voice announce on the device (or its sync group).
GET /health -> {"status": "ok"}

Secrets: MA_MCP_TOKEN read from /root/.hermes/.env at runtime (never logged).
Log: /root/.hermes/logs/ma-fav-button.log
"""
import json
import logging
import os
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV_FILE = "/root/.hermes/.env"
LOG_FILE = "/root/.hermes/logs/ma-fav-button.log"
LAST_FILE = "/root/.hermes/logs/ma-fav-button-last.json"

MA_API = "http://192.168.214.159:8095/api"
HASS_API = "http://192.168.214.159:8123/api"
PORT = 8699
FAV_NAME = "moode-sendspin"
GROUP_NAME = "SendSpin"
ANN_DB = 5       # fallback/idle announce level (no music level to scale to)
ANN_RATIO = 0.8  # voice at 80% of the current music level
ANN_FLOOR = 3    # voice never below this (whispers stay audible)

LEAN_ENABLE = False  # parked 26 Oct 2026; poll loop stays dormant until True
LEAN_STEP = 5
LEAN_POLL_SECONDS = 2
DIM_STEP = 2  # MA volume points per dimmer rotary step (27 Oct 2026)
def _dim_skip(which, dry_run=False):
    """Twist-while-pressed: next/previous track on the active queue
    (never paused first, so no resume dance is needed)."""
    players = ma("players/all", {}, mid="players")
    fav = resolve_active(players, record_last=not dry_run)
    if fav is None:
        logging.info("source=dimmer %s: no player", which)
        return {"skipped": False, "reason": "no player", "dry_run": dry_run}
    if dry_run:
        return {"skipped": False, "dry_run": True,
                "would": which, "player": fav.get("name")}
    try:
        _queue_cmd("player_queues/" + which, fav["player_id"], "dimmer",
                   mid="dimskip")
    except Exception as ex:
        err = str(ex)[:100]
        logging.info("source=dimmer %s FAILED %s", which, err)
        return {"skipped": False, "reason": err, "player": fav.get("name")}
    logging.info("source=dimmer %s player=%s", which, fav.get("name"))
    return {"skipped": True, "direction": which, "player": fav.get("name")}

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(message)s")

_PRESS_LOCK = threading.Lock()
_ENV_CACHE = {"mtime": 0.0, "data": {}}
_LAST_MEM = None


def _env(key):
    """Read key from ENV_FILE, cached by file mtime to avoid per-RPC disk I/O."""
    try:
        mtime = os.stat(ENV_FILE).st_mtime
        if mtime != _ENV_CACHE["mtime"]:
            data = {}
            with open(ENV_FILE) as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        data[k.strip()] = v.strip().strip('"').strip("'")
            _ENV_CACHE["mtime"] = mtime
            _ENV_CACHE["data"] = data
    except OSError:
        pass
    val = os.environ.get(key) or _ENV_CACHE["data"].get(key)
    if not val:
        raise RuntimeError(f"{key} missing")
    return val


def token():
    return _env("MA_MCP_TOKEN")


def hass_token():
    return _env("HASS_TOKEN")


def ma(cmd, args=None, mid="fav", timeout=25):
    body = json.dumps(
        {"message_id": mid, "command": cmd, "args": args or {}}).encode()
    req = urllib.request.Request(
        MA_API, data=body,
        headers={"Authorization": "Bearer " + token(),
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        res = json.loads(resp.read())
    if isinstance(res, dict) and "error" in res:
        raise RuntimeError(str(res["error"])[:200])
    return res.get("result", res) if isinstance(res, dict) else res


def title_of(item):
    if not item:
        return None
    if isinstance(item, str):
        return item
    name = item.get("name") or item.get("title")
    artists = item.get("artists") or item.get("artist")
    if isinstance(artists, list):
        artists = ", ".join(
            a if isinstance(a, str) else a.get("name", "") for a in artists)
    return f"{name} by {artists}" if (name and artists) else name


def is_tv(p):
    name = str(p.get("name", "")).lower()
    return ("tv" in name or "oled" in name
            or p.get("player_id") in ("upb8a0b8a166f7", "upcc6ea4169ef0"))


def get_last():
    global _LAST_MEM
    if _LAST_MEM:
        return _LAST_MEM
    try:
        with open(LAST_FILE) as fh:
            _LAST_MEM = json.load(fh).get("player_id")
            return _LAST_MEM
    except Exception:
        return None


def set_last(player_id):
    global _LAST_MEM
    if not player_id or player_id == _LAST_MEM:
        return
    _LAST_MEM = player_id
    try:
        d = os.path.dirname(LAST_FILE)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".last-", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump({"player_id": player_id}, fh)
        os.replace(tmp, LAST_FILE)
    except Exception:
        pass


def resolve_active(players, record_last=True):
    """Which device the button controls:
    1. moode-sendspin while playing/paused,
    2. else whichever non-TV device is playing,
    3. else the last-used device (a paused web/Sendspin client reports idle),
    4. else any paused device,
    5. else idle moode-sendspin if available.
    """
    avail = [p for p in players if p.get("available", True) and not is_tv(p)]
    fav = next((p for p in avail if p.get("name") == FAV_NAME), None)

    def _pick(p):
        if p is not None and record_last:
            set_last(p["player_id"])
        return p

    if fav is not None and fav.get("state") in ("playing", "paused"):
        return _pick(fav)
    playing = [p for p in avail if p.get("state") == "playing"]
    if playing:
        return _pick(playing[0])
    last = get_last()
    if last:
        prev = next((p for p in avail if p.get("player_id") == last), None)
        if prev is not None:
            return prev
    paused = [p for p in avail if p.get("state") == "paused"]
    if paused:
        return _pick(paused[0])
    if fav is not None:
        return _pick(fav)
    return None


def announce_level(fav):
    """Proportional voice level: 80% of the music's current level on
    the resolved device, never below ANN_FLOOR."""
    try:
        music = float(fav.get("volume_level"))
    except (TypeError, ValueError):
        return ANN_DB
    return max(ANN_FLOOR, min(100, round(music * ANN_RATIO)))


def announce_target(players, target):
    """Where the voice goes: the target's sync group if it has >=2 LIVE
    members AND includes target, else the target itself (Lesson 1)."""
    by_id = {p.get("player_id"): p for p in players}
    gid = target.get("synced_to") or target.get("active_group")
    grp = by_id.get(gid) if gid else None
    if grp is None and target.get("name") == FAV_NAME:
        grp = next((p for p in players if p.get("name") == GROUP_NAME), None)
    if grp is not None:
        members = grp.get("group_members") or []
        if target.get("player_id") in members:
            live = [m for m in members
                    if (by_id.get(m) or {}).get("available", False)]
            if len(live) >= 2:
                return grp
    return target


def _queue_cmd(cmd, player_id, source, mid):
    """Execute a player_queues command on the player's own queue first,
    falling back to get_active_queue on QueueEmpty (Lesson 2)."""
    try:
        ma(cmd, {"queue_id": player_id}, mid=mid)
        return
    except Exception as ex:
        if "QueueEmpty" not in str(ex):
            raise
        logging.info("source=%s own-queue %s empty, trying active", source, cmd)
    q = ma("player_queues/get_active_queue", {"player_id": player_id},
           mid=mid + "Q") or {}
    qid = q.get("queue_id")
    if not qid:
        raise RuntimeError("QueueEmpty: no active queue for " + str(player_id))
    ma(cmd, {"queue_id": qid}, mid=mid + "2")


def resume_queue(source, player_id, mid="resume"):
    _queue_cmd("player_queues/resume", player_id, source, mid)


def handle_toggle(source, dry_run=False):
    players = ma("players/all", {}, mid="players")
    fav = resolve_active(players, record_last=not dry_run)
    if fav is None:
        logging.info("source=%s toggle: nothing playing", source)
        return {"toggled": False, "reason": "nothing playing", "dry_run": dry_run}
    action = "paused" if fav.get("state") == "playing" else "playing"
    if dry_run:
        return {"toggled": False, "dry_run": True, "would_action": action,
                "player": fav.get("name")}
    if action == "paused":
        ma("players/cmd/pause", {"player_id": fav["player_id"]}, mid="pause")
    else:
        try:
            resume_queue(source, fav["player_id"], mid="resumeT")
        except Exception as ex:
            logging.info("source=%s resume FAILED %s", source, str(ex)[:120])
            return {"toggled": False, "reason": str(ex)[:100],
                    "player": fav.get("name")}
    logging.info("source=%s toggle->%s player=%s", source, action, fav.get("name"))
    return {"toggled": True, "action": action, "player": fav.get("name")}


def handle_next(source, dry_run=False):
    players = ma("players/all", {}, mid="players")
    fav = resolve_active(players, record_last=not dry_run)
    if fav is None:
        logging.info("source=%s next: no player", source)
        return {"skipped": False, "reason": "no player", "dry_run": dry_run}
    if dry_run:
        return {"skipped": False, "dry_run": True, "player": fav.get("name")}
    try:
        _queue_cmd("player_queues/next", fav["player_id"], source, mid="next")
    except Exception as ex:
        err = str(ex)[:100]
        logging.info("source=%s next FAILED %s", source, err)
        return {"skipped": False, "reason": err, "player": fav.get("name")}
    logging.info("source=%s skipped to next on player=%s", source, fav.get("name"))
    return {"skipped": True, "player": fav.get("name")}


def hass_number(entity_id):
    req = urllib.request.Request(
        HASS_API + "/states/" + entity_id,
        headers={"Authorization": "Bearer " + hass_token()})
    with urllib.request.urlopen(req, timeout=15) as resp:
        s = json.loads(resp.read().decode())
    return float(s.get("state"))


def lean_poll_loop():
    """Poll volume X/Y; step once per sustained excursion."""
    if not LEAN_ENABLE:
        return
    down_n, up_n, down_miss, armed = 0, 0, 0, True
    while True:
        try:
            x, y = hass_number("sensor.volume_x"), hass_number("sensor.volume_y")
            in_down = x < 100
            in_up = y > 15 and x > 200
            if x > 200 and y < 60 and not in_up:
                if not armed:
                    logging.info("lean re-armed X=%s Y=%s", x, y)
                armed, down_n, up_n, down_miss = True, 0, 0, 0
            elif armed:
                if in_down:
                    down_n, down_miss = down_n + 1, 0
                elif x < 160:
                    down_miss += 1
                    if down_miss >= 2:
                        down_n, down_miss = 0, 0
                else:
                    down_n, down_miss = 0, 0
                up_n = up_n + 1 if in_up else 0
                logging.info("lean poll X=%s Y=%s down_n=%s up_n=%s",
                             x, y, down_n, up_n)
                if down_n >= 4:
                    armed = False
                    handle_lean("lean_down")
                elif up_n >= 3:
                    armed = False
                    handle_lean("lean_up")
        except Exception as ex:
            logging.info("lean poll %s", str(ex)[:100])
        time.sleep(LEAN_POLL_SECONDS)


def handle_lean(source, dry_run=False):
    delta = -LEAN_STEP if source == "lean_down" else LEAN_STEP
    players = ma("players/all", {}, mid="players")
    target = resolve_active(players, record_last=not dry_run)
    if target is None:
        logging.info("source=%s lean: no device", source)
        return {"volume": None, "target": None, "dry_run": dry_run}
    try:
        cur = float(target.get("volume_level"))
    except (TypeError, ValueError):
        return {"volume": None, "target": target.get("name"), "dry_run": dry_run}
    new = min(100, max(0, round(cur + delta)))
    if dry_run:
        return {"volume": new, "target": target.get("name"), "dry_run": True}
    ma("players/cmd/volume_set",
       {"player_id": target["player_id"], "volume_level": new}, mid="vol")
    logging.info("source=%s lean %s->%s player=%s", source, cur, new,
                 target.get("name"))
    return {"volume": new, "target": target.get("name")}


def handle_dimmer(action, steps, dry_run=False):
    """YLKG08YL rotary: volume step scaled by event steps
    (delta = steps * DIM_STEP) on the active device."""
    try:
        steps = max(1, int(steps))
    except (TypeError, ValueError):
        steps = 1
    delta = steps * DIM_STEP if action == "dim_up" else -steps * DIM_STEP
    players = ma("players/all", {}, mid="players")
    target = resolve_active(players, record_last=not dry_run)
    if target is None:
        logging.info("source=dimmer %s: no device", action)
        return {"volume": None, "target": None, "dry_run": dry_run}
    try:
        cur = float(target.get("volume_level"))
    except (TypeError, ValueError):
        cur = None
    if cur is None:
        logging.info("source=dimmer %s: no volume on %s", action,
                     target.get("name"))
        return {"volume": None, "target": target.get("name"),
                "dry_run": dry_run}
    new = min(100, max(0, round(cur + delta)))
    if dry_run:
        return {"volume": new, "target": target.get("name"), "dry_run": True}
    ma("players/cmd/volume_set",
       {"player_id": target["player_id"], "volume_level": new}, mid="dimvol")
    logging.info("source=dimmer %s steps=%s %s->%s player=%s", action, steps,
                 cur, new, target.get("name"))
    return {"volume": new, "target": target.get("name")}


def handle_announce(source, dry_run=False):
    """Announce the current song without favouriting: same voice rules
    (proportional level, group-aware target) and resume handling as the
    favourite path, minus the library write."""
    players = ma("players/all", {}, mid="players")
    fav = resolve_active(players, record_last=not dry_run)
    if fav is None:
        return {"announced": False, "reason": "nothing playing",
                "dry_run": dry_run}
    grp = announce_target(players, fav)
    title = title_of(fav.get("current_item")) or title_of(grp.get("current_item"))
    if not title:
        try:
            q = ma("player_queues/get_active_queue",
                   {"player_id": fav["player_id"]}, mid="queue")
            title = title_of((q or {}).get("current_item"))
        except Exception:
            title = None
    title = title or "this song"
    if len(title) > 90:
        title = title[:87] + "..."
    msg = f"Now playing: {title}"
    if dry_run:
        return {"announced": False, "dry_run": True,
                "would_announce": msg, "player": fav.get("name")}
    was_playing = fav.get("state") == "playing"
    alevel = announce_level(fav)
    ma("players/cmd/play_announcement",
       {"player_id": grp["player_id"], "message": msg,
        "volume_level": alevel, "pre_announce": False}, mid="annA")
    logging.info("source=%s announce level=%s (music was %s) player=%s",
                 source, alevel, fav.get("volume_level"), fav.get("name"))
    resumed = False
    if was_playing:
        time.sleep(2)
        st = None
        for _ in range(3):
            try:
                q_poll = ma("player_queues/get_active_queue",
                            {"player_id": fav["player_id"]}, mid="queue3") or {}
                st = q_poll.get("state")
            except Exception:
                st = None
            if st == "playing":
                break
            time.sleep(2)
        if st != "playing":
            try:
                resume_queue(source, fav["player_id"], mid="resume")
                resumed = True
            except Exception as ex:
                logging.info("source=%s resume FAILED %s", source, str(ex)[:120])
    return {"announced": True, "resumed": resumed, "message": msg,
            "player": fav.get("name")}


def handle_press(source, dry_run=False, action=None, event_type=None,
                   steps=1):
    if source == "dimmer":
        # YLKG08YL rotary/button events (27 Oct 2026). press reuses the
        # button toggle; long_press favourites+announces (announce-only
        # stays on the dashboard `announce` source).
        action = {"rotate_right": "dim_up", "rotate_left": "dim_down",
                  "rotate_right_pressed": "dim_next",
                  "rotate_left_pressed": "dim_prev",
                  "press": "toggle", "long_press": "fav"}.get(event_type)
        if action is None:
            logging.info("source=dimmer unknown event %s", event_type)
            return {"dimmer": False, "reason": "unknown event"}
        if action in ("dim_up", "dim_down"):
            return handle_dimmer(action, steps, dry_run)
        if action in ("dim_next", "dim_prev"):
            return _dim_skip("next" if action == "dim_next" else "previous",
                             dry_run)
        if action == "toggle":
            return handle_toggle(source, dry_run=dry_run)
        if action != "announce":
            action = "fav"
    if action is None:
        action = {"single": "toggle", "double": "next",
                  "lean_down": "lean", "lean_up": "lean",
                  "announce": "announce"}.get(source, "fav")
    if action == "announce":
        return handle_announce(source, dry_run=dry_run)
    if action == "lean":
        return handle_lean(source, dry_run=dry_run)
    if action == "toggle":
        return handle_toggle(source, dry_run=dry_run)
    if action == "next":
        return handle_next(source, dry_run=dry_run)

    players = ma("players/all", {}, mid="players")
    fav = resolve_active(players, record_last=not dry_run)
    if fav is None:
        msg = "Nothing is playing right now"
        announced = False
        if not dry_run:
            # Lesson 1: never announce to a dead group.
            by_id = {p.get("player_id"): p for p in players}
            grp = next((p for p in players if p.get("name") == GROUP_NAME), None)
            live = [m for m in (grp.get("group_members") or [])
                    if (by_id.get(m) or {}).get("available", False)] if grp else []
            ann_p = grp if len(live) >= 2 else (by_id.get(live[0]) if live else None)
            if ann_p is not None:
                try:
                    ma("players/cmd/play_announcement",
                       {"player_id": ann_p["player_id"], "message": msg,
                        "volume_level": ANN_DB}, mid="ann0")
                    announced = True
                except Exception as ex:
                    logging.info("source=%s idle announce FAILED %s", source, str(ex)[:100])
        logging.info("source=%s nothing playing", source)
        return {"favourited": False, "announced": announced, "message": msg}

    grp = announce_target(players, fav)
    title = title_of(fav.get("current_item")) or title_of(grp.get("current_item"))
    if not title:
        try:
            q = ma("player_queues/get_active_queue",
                   {"player_id": fav["player_id"]}, mid="queue")
            title = title_of((q or {}).get("current_item"))
        except Exception:
            title = None
    title = title or "this song"
    if len(title) > 90:
        title = title[:87] + "..."
    if dry_run:
        return {"favourited": False, "announced": False, "dry_run": True,
                "would_favourite": title, "player": fav.get("name")}

    try:
        ma("players/add_currently_playing_to_favorites",
           {"player_id": fav["player_id"]}, mid="fav")
        ok = True
    except Exception as ex:
        logging.info("source=%s favourite FAILED %s", source, str(ex)[:120])
        ok = False
    msg = f"Added {title} to favourites" if ok else f"Could not favourite {title}"
    was_playing = fav.get("state") == "playing"
    alevel = announce_level(fav)
    ma("players/cmd/play_announcement",
       {"player_id": grp["player_id"], "message": msg,
        "volume_level": alevel, "pre_announce": False}, mid="ann")
    logging.info("source=%s announce level=%s (music was %s) player=%s",
                 source, alevel, fav.get("volume_level"), fav.get("name"))

    resumed = False
    if was_playing:
        time.sleep(2)
        st = None
        for _ in range(3):
            try:
                q_poll = ma("player_queues/get_active_queue",
                            {"player_id": fav["player_id"]}, mid="queue3") or {}
                st = q_poll.get("state")
            except Exception:
                st = None
            if st == "playing":
                break
            time.sleep(2)
        if st != "playing":
            try:
                resume_queue(source, fav["player_id"], mid="resume")
                resumed = True
            except Exception as ex:
                logging.info("source=%s resume FAILED %s", source, str(ex)[:120])

    logging.info("source=%s favourited=%s resumed=%s player=%s title=%s",
                 source, ok, resumed, fav.get("name"), title)
    return {"favourited": ok, "announced": True, "resumed": resumed,
            "message": msg, "player": fav.get("name")}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200 if self.path == "/health" else 404,
                   {"status": "ok"} if self.path == "/health" else {})

    def do_POST(self):
        if self.path != "/fav-button":
            return self._send(404, {})
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        try:
            with _PRESS_LOCK:
                res = handle_press(payload.get("source", "?"),
                                   bool(payload.get("dry_run")),
                                   payload.get("action"),
                                   payload.get("event_type"),
                                   payload.get("steps", 1))
            self._send(200, res)
        except Exception as ex:
            logging.info("press FAILED %s", str(ex)[:200])
            self._send(502, {"error": str(ex)[:150]})


if __name__ == "__main__":
    if LEAN_ENABLE:
        threading.Thread(target=lean_poll_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
