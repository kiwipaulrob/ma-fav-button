#!/usr/bin/env python3
"""Favourite-this-song relay for HA dashboard / Zigbee button.

POST /fav-button {"source": "dashboard"|"single"|"hold"|"double", "dry_run": bool}
  1. Resolves the ACTIVE device by state each press (moode-sendspin while
     in use, else whichever non-TV device is playing/paused - TVs excluded).
  2. single->toggle play/pause, double->next track (mute-bridged),
     else favourite + voice announce on the device (or its sync group).
GET /health -> {"status": "ok"}

Secrets: MA_MCP_TOKEN read from /root/.hermes/.env at runtime (never logged).
Log: /root/.hermes/logs/ma-fav-button.log
"""
import json
import logging
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

ENV_FILE = "/root/.hermes/.env"
MA_API = "http://192.168.214.159:8095/api"
PORT = 8699
FAV_NAME = "moode-sendspin"
GROUP_NAME = "SendSpin"
ANN_DB = 5  # fallback/idle announce level (no music level to scale to)
ANN_RATIO = 0.8  # voice at 80% of the current music level (26 Oct 2026:
# user asked for proportional voice - fixed levels shout over quiet
# late-night music and vanish under loud daytime playback)
ANN_FLOOR = 3  # voice never below this (whispers stay audible)
LEAN_ENABLE = False  # lean-volume parked 26 Oct 2026 (round-puck gestures
# unreliable in real handling); poll loop stays dormant until re-enabled

LAST_FILE = "/root/.hermes/logs/ma-fav-button-last.json"

logging.basicConfig(filename="/root/.hermes/logs/ma-fav-button.log",
                    level=logging.INFO,
                    format="%(asctime)s %(message)s")


def token():
    for line in open(ENV_FILE):
        line = line.strip()
        if line.startswith("MA_MCP_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("MA_MCP_TOKEN missing")


def ma(cmd, args=None, mid="fav", timeout=25):
    body = json.dumps(
        {"message_id": mid, "command": cmd, "args": args or {}}).encode()
    req = urllib.request.Request(
        MA_API, data=body,
        headers={"Authorization": "Bearer " + token(),
                 "Content-Type": "application/json"})
    res = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    if isinstance(res, dict) and "error" in res:
        raise RuntimeError(str(res["error"])[:200])
    return res.get("result", res) if isinstance(res, dict) else res


def title_of(item):
    if item is None:
        return None
    if isinstance(item, str):
        return item or None
    name = item.get("name") or item.get("title")
    artists = item.get("artists") or item.get("artist")
    if isinstance(artists, list):
        artists = ", ".join(
            a if isinstance(a, str) else a.get("name", "") for a in artists)
    if name and artists:
        return f"{name} by {artists}"
    return name


def is_tv(p):
    name = str(p.get("name", "")).lower()
    return ("tv" in name or "oled" in name
            or p.get("player_id") in ("upb8a0b8a166f7", "upcc6ea4169ef0"))


def get_last():
    try:
        return json.load(open(LAST_FILE)).get("player_id")
    except Exception:
        return None


def set_last(player_id):
    try:
        json.dump({"player_id": player_id}, open(LAST_FILE, "w"))
    except Exception:
        pass


def resolve_active(players):
    """Which device the button controls: moode-sendspin while it is in use,
    else whichever non-TV device is playing (or paused), else the last-used
    device (a paused web client reports idle, not paused), else None."""
    avail = [p for p in players
             if p.get("available", True) and not is_tv(p)]
    fav = next((p for p in avail if p.get("name") == FAV_NAME), None)
    if fav is not None and fav.get("state") in ("playing", "paused"):
        set_last(fav["player_id"])
        return fav
    playing = [p for p in avail if p.get("state") == "playing"]
    if playing:
        set_last(playing[0]["player_id"])
        return playing[0]
    last = get_last()
    if last:
        prev = next((p for p in avail if p.get("player_id") == last), None)
        if prev is not None:
            return prev
    if fav is not None:
        set_last(fav["player_id"])
        return fav
    paused = [p for p in avail if p.get("state") == "paused"]
    if paused:
        set_last(paused[0]["player_id"])
        return paused[0]
    return None


def announce_level(fav):
    """Proportional voice level: 80% of the music's current level on
    the resolved device, never below the floor. Falls back to the
    fixed level when the device reports no volume."""
    try:
        music = float(fav.get("volume_level"))
    except (TypeError, ValueError):
        return ANN_DB
    return max(ANN_FLOOR, min(100, round(music * ANN_RATIO)))


def announce_target(players, target):
    """Where the voice goes: the target's sync group if it has one,
    the SendSpin group for moode, else the target itself.
    Never returns a group the target is not actually a member of
    (announcing into a dissolved/ghost group plays silence). And never
    a group with fewer than 2 LIVE members: a group announce forces
    MA's slow ungroup/regroup dance (worse with a dead ghost member
    timing out), while a direct announce restores in a fraction of the
    time. Adapts automatically when rooms join later."""
    by_id = {p.get("player_id"): p for p in players}
    gid = target.get("synced_to") or target.get("active_group")
    if gid:
        grp = next((p for p in players if p.get("player_id") == gid), None)
        if grp is not None:
            return grp
    if target.get("name") == FAV_NAME:
        grp = next((p for p in players if p.get("name") == GROUP_NAME),
                   None)
        members = (grp.get("group_members") or []) if grp is not None else []
        if target["player_id"] in members:
            live = [m for m in members
                    if (by_id.get(m) or {}).get("available", False)]
            if len(live) >= 2:
                return grp
    return target


def resume_queue(source, player_id, mid="resume"):
    """Resume playback for a player, own queue first.

    get_active_queue(member) follows the CONTROLLING (group) queue, which
    may be empty while the member's own queue holds the playback position
    (QueueEmpty 500, seen 26 Oct 2026: group SendSpin empty, moode at
    index 34). Player queue_ids equal their player_id, so try that
    first and fall back to whatever get_active_queue resolves.
    """
    try:
        ma("player_queues/resume", {"queue_id": player_id}, mid=mid)
        return
    except Exception as ex:
        if "QueueEmpty" not in str(ex):
            raise
        logging.info("source=%s own-queue resume empty, trying active",
                     source)
    q = ma("player_queues/get_active_queue", {"player_id": player_id},
           mid=mid + "Q")
    ma("player_queues/resume", {"queue_id": q["queue_id"]}, mid=mid + "2")


def handle_toggle(source):
    players = ma("players/all", {}, mid="players")
    fav = resolve_active(players)
    if fav is None:
        logging.info("source=%s toggle: nothing playing", source)
        return {"toggled": False, "reason": "nothing playing"}
    # Explicit pause/play, never the toggle: play_pause would not restart
    # a paused Sendspin player (2nd click did nothing).
    # NOTE: no mute-bridge here (tried 26 Oct 2026): the pause/resume click
    # survives digital silence, i.e. it is the SMSL USB DAC's relay on
    # stream stop/start, not a signal pop - muting only added lag.
    if fav.get("state") == "playing":
        ma("players/cmd/pause", {"player_id": fav["player_id"]},
           mid="pause")
        action = "paused"
    else:
        # Resume the PLAYER'S OWN queue: MA's players/cmd/play resumes the
        # server-global active queue instead (QueueEmpty 500s when that
        # queue - e.g. moode's, cleared - is empty while another device
        # plays). Queue-scoped resume targets the right device.
        try:
            resume_queue(source, fav["player_id"], mid="resumeT")
            action = "playing"
        except Exception as ex:
            logging.info("source=%s resume FAILED %s", source,
                         str(ex)[:120])
            return {"toggled": False,
                    "reason": str(ex)[:100], "player": fav.get("name")}
    logging.info("source=%s toggle->%s player=%s", source, action,
                 fav.get("name"))
    return {"toggled": True, "action": action, "player": fav.get("name")}


def handle_next(source):
    players = ma("players/all", {}, mid="players")
    fav = resolve_active(players)
    if fav is None:
        logging.info("source=%s next: no player", source)
        return {"skipped": False, "reason": "no player"}
    grp = announce_target(players, fav)
    try:
        # NOTE: no mute-bridge (removed 26 Oct 2026, same verdict as the
        # toggle path): the skip click is the SMSL USB DAC's hardware relay
        # on stream rebuild, not a signal pop - muting only added lag.
        # Own queue first: get_active_queue(member) follows the group queue
        # (see resume_queue).
        try:
            ma("player_queues/next", {"queue_id": fav["player_id"]},
               mid="next")
        except Exception as ex:
            if "QueueEmpty" not in str(ex):
                raise
            q = ma("player_queues/get_active_queue",
                   {"player_id": fav["player_id"]}, mid="queue")
            ma("player_queues/next", {"queue_id": q["queue_id"]},
               mid="next2")
    except Exception as ex:
        err = str(ex)[:100]
        logging.info("source=%s next FAILED %s", source, err)
        return {"skipped": False, "reason": err}
    logging.info("source=%s skipped to next on player=%s", source,
                 fav.get("name"))
    return {"skipped": True, "player": fav.get("name")}


HASS_API = "http://192.168.214.159:8123/api"
LEAN_STEP = 5
# Settle-and-classify (TS0601 round puck tumbles through every threshold
# mid-gesture, so threshold crossings are unusable): act only when the
# pose sits still LEAN_SETTLE_SAMPLES polls in a row, then classify.
LEAN_POLL_SECONDS = 2
LEAN_SETTLE_SAMPLES = 3  # ~6 s still hold
LEAN_JITTER = 12


def hass_token():
    for line in open(ENV_FILE):
        line = line.strip()
        if line.startswith("HASS_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("HASS_TOKEN missing")


def hass_number(entity_id):
    req = urllib.request.Request(
        HASS_API + "/states/" + entity_id,
        headers={"Authorization": "Bearer " + hass_token()})
    s = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    return float(s.get("state"))


def lean_poll_loop():
    """Poll volume X/Y; step once per sustained excursion. Each axis is
    judged on its own sustain (a hand-held round puck rocks the other
    axis while settling, so cross-axis stillness must not veto).
    Re-arm only on return to rest. Threshold crossings alone never act."""
    if not LEAN_ENABLE:
        return
    down_n, up_n, down_miss, armed = 0, 0, 0, True
    while True:
        try:
            x = hass_number("sensor.volume_x")
            y = hass_number("sensor.volume_y")
            # Re-arm: X home + Y broadly home (Y's rest drifts 2->10-35
            # with seating, so the band is wide), but never while an UP
            # hold is in progress - a mild sustained UP (y 15-60) must
            # still count up_n toward firing.
            in_down = x < 100
            in_up = y > 15 and x > 200
            if x > 200 and y < 60 and not in_up:  # rest
                if not armed:
                    logging.info("lean re-armed X=%s Y=%s", x, y)
                armed, down_n, up_n, down_miss = True, 0, 0, 0
            elif armed:
                if in_down:
                    down_n, down_miss = down_n + 1, 0
                elif x < 160:
                    # near-zone wobble: tolerate a single miss
                    down_miss += 1
                    if down_miss >= 2:
                        down_n, down_miss = 0, 0
                else:
                    down_n, down_miss = 0, 0
                up_n = up_n + 1 if in_up else 0
                logging.info("lean poll X=%s Y=%s down_n=%s up_n=%s",
                             x, y, down_n, up_n)
                if down_n >= 4:  # X-low ~8 s -> DOWN
                    armed = False
                    handle_lean("lean_down")
                elif up_n >= 3:  # Y-high ~6 s with X home -> UP
                    armed = False
                    handle_lean("lean_up")
        except Exception as ex:
            logging.info("lean poll %s", str(ex)[:100])
        time.sleep(LEAN_POLL_SECONDS)


def handle_lean(source):
    """Tilt/lean gesture from the TS0601 vibration sensor ("volume"):
    lean_down (X excursion 253->18) steps -5, lean_up (Y excursion
    2->34) steps +5, on the active device (same resolve as the button)."""
    delta = -LEAN_STEP if source == "lean_down" else LEAN_STEP
    players = ma("players/all", {}, mid="players")
    target = resolve_active(players)
    if target is None:
        logging.info("source=%s lean: no device", source)
        return {"volume": None, "target": None}
    try:
        cur = float(target.get("volume_level"))
    except (TypeError, ValueError):
        cur = None
    if cur is None:
        logging.info("source=%s lean: no volume on %s", source,
                     target.get("name"))
        return {"volume": None, "target": target.get("name")}
    new = min(100, max(0, round(cur + delta)))
    ma("players/cmd/volume_set",
       {"player_id": target["player_id"], "volume_level": new}, mid="vol")
    logging.info("source=%s lean %s->%s player=%s", source, cur, new,
                 target.get("name"))
    return {"volume": new, "target": target.get("name")}


def handle_press(source, dry_run=False, action=None):
    if action is None:
        action = {"single": "toggle", "double": "next",
                  "lean_down": "lean", "lean_up": "lean"}.get(source, "fav")
    if action == "lean":
        if dry_run:
            return {"volume": None, "dry_run": True}
        return handle_lean(source)
    if action == "toggle":
        if dry_run:
            return {"toggled": False, "dry_run": True}
        return handle_toggle(source)
    if action == "next":
        if dry_run:
            return {"skipped": False, "dry_run": True}
        return handle_next(source)
    players = ma("players/all", {}, mid="players")
    fav = resolve_active(players)
    if fav is None:
        msg = "Nothing is playing right now"
        if not dry_run:
            grp = next((p for p in players if p.get("name") == GROUP_NAME),
                       None)
            if grp is not None:
                ma("players/cmd/play_announcement",
                   {"player_id": grp["player_id"], "message": msg,
                    "volume_level": ANN_DB}, mid="ann0")
        logging.info("source=%s nothing playing", source)
        return {"favourited": False, "announced": not dry_run,
                "message": msg}
    title = title_of(fav.get("current_item"))
    if not title:
        grp0 = announce_target(players, fav)
        title = title_of(grp0.get("current_item"))
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
    msg = (f"Added {title} to favourites" if ok
           else f"Could not favourite {title}")
    grp = announce_target(players, fav)
    was_playing = fav.get("state") == "playing"
    idx_before = None
    try:
        q0 = ma("player_queues/get_active_queue",
                {"player_id": fav["player_id"]}, mid="queue0")
        idx_before = q0.get("current_index")
    except Exception:
        idx_before = None
    alevel = announce_level(fav)
    ma("players/cmd/play_announcement",
       {"player_id": grp["player_id"], "message": msg,
        "volume_level": alevel, "pre_announce": False}, mid="ann")
    logging.info("source=%s announce level=%s (music was %s) player=%s",
                 source, alevel, fav.get("volume_level"),
                 fav.get("name"))
    resumed = False
    if was_playing:
        # MA restores by itself after most announcements; give it a short
        # grace, then resume without the old 20 s wait. (Previous shape:
        # 4 s + 8x2 s polls, compounded by MA's slow regroup around the
        # dead ghost member - the felt delay.)
        time.sleep(2)
        for _ in range(3):
            try:
                st = ma("player_queues/get_active_queue",
                        {"player_id": fav["player_id"]},
                        mid="queue3").get("state")
            except Exception:
                st = None
            if st == "playing":
                break
            time.sleep(2)
        else:
            st = None
        if st != "playing":
            try:
                resume_queue(source, fav["player_id"], mid="resume")
                resumed = True
            except Exception as ex:
                logging.info("source=%s resume FAILED %s", source,
                             str(ex)[:120])
    logging.info("source=%s favourited=%s resumed=%s player=%s title=%s",
                 source, ok, resumed, fav.get("name"), title)
    try:
        q1 = ma("player_queues/get_active_queue",
                {"player_id": fav["player_id"]}, mid="queue4")
        logging.info("source=%s queue index before=%s after=%s state=%s",
                     source, idx_before, q1.get("current_index"),
                     q1.get("state"))
    except Exception as ex:
        logging.info("source=%s index check FAILED %s", source,
                     str(ex)[:100])
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
            self._send(200, handle_press(payload.get("source", "?"),
                                         bool(payload.get("dry_run")),
                                         payload.get("action")))
        except Exception as ex:
            logging.info("press FAILED %s", str(ex)[:200])
            self._send(502, {"error": str(ex)[:150]})


if __name__ == "__main__":
    threading.Thread(target=lean_poll_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
