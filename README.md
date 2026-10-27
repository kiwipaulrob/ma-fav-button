# ma-fav-button — a music button for Music Assistant

One physical (or dashboard) button that follows you around the house:
**single-press** toggles play/pause, **double-press** skips to the next
track, **hold** favourites the current song and has a voice confirm it —
all on *whichever Music Assistant player is currently playing*. No
rewiring when you switch rooms; no dead buttons when players rename.

Built against Music Assistant 2.10.x + Home Assistant, with a Zigbee
(Tuya TS0041 via Zigbee2MQTT) button and an HA dashboard button as the
two trigger sources. A small Python relay does the thinking; HA just
forwards presses to it.

> **Status:** daily-driver code, running in production since Oct 2026.
> The lean/tilt volume-gesture experiment in this repo is **parked**
> (documented below) — everything else is live.

---

## How it works

```
Zigbee button ──MQTT device trigger──┐
                                     ├─→ HA automation ──rest_command──→  relay:8699 ──MA JSON-RPC──→ Music Assistant
HA dashboard button ──state trigger──┘        (dumb pipe)                (the brains)
```

**HA stays dumb on purpose.** The automation forwards `{source}` and
nothing else, so routing changes never need an HA restart — only the
relay restarts (2 s). The Music Assistant token lives in a root-only
`.env` on the relay host, never in HA.

**The relay resolves the *active* device on every press:**

1. `moode-sendspin` while playing/paused (the quality endpoint wins),
2. else whichever non-TV device is playing (follows you to e.g. a
   browser player with zero reconfiguration),
3. else the last-used device (a paused client reports `idle`, not
   `paused` — memory covers that),
4. else any paused device. TVs are excluded by name and player ID.

It also remembers the last-used device across presses, because a
paused web/Sendspin client looks identical to an idle one.

## Features

| Press | Action |
|---|---|
| Single | Play/pause toggle on the active device's **own queue** (see lesson 2) |
| Double | Next track on the active queue |
| Hold / dashboard | Favourite the song + spoken confirmation, then resume the track |
| Dimmer rotate | Volume ±(steps × 2) on the active device (YLKG08YL event) |
| Dimmer press / long-press | Single press = toggle (800 ms double window), double-press = next track, long-press = favourite + voice |

- **Proportional voice:** the confirmation speaks at 80% of the
  music's current level (floor of 3/100) instead of a fixed level —
  no more shouting over quiet late-night listening. Ratio and floor
  are constants (`ANN_RATIO`, `ANN_FLOOR`).
- **Group-aware announcements:** speaks on the device's sync group
  when the group has 2+ live members, direct otherwise (skips MA's
  slow ungroup/regroup dance around dead members).
- **Dry-run mode:** every action accepts `"dry_run": true` and resolves
  without touching anything — safe to test wiring end to end.
- **Health endpoint:** `GET /health` → `{"status": "ok"}`.

## Requirements

- Music Assistant 2.10.x (JSON-RPC at `http://<ha-host>:8095/api`)
- Home Assistant with the Music Assistant integration (for the
  dashboard button; the relay itself only needs MA's API)
- A host for the relay: any always-on Linux box with Python 3.11+
  (no dependencies beyond the standard library)
- A long-lived MA access token (`MA_MCP_TOKEN` or any valid bearer
  token) and, for the dormant lean poll only, an HA token
- Optional: Zigbee2MQTT button (any model exposing single/hold/double)

## Installation

1. Copy `ma-fav-button-relay.py` to the relay host, e.g.
   `/opt/ma-fav-button/`.
2. Create `/root/.hermes/.env` (mode 600) with your token:
   `MA_MCP_TOKEN=<token>`. (Any path works — update `ENV_FILE`.)
3. Adjust constants at the top of the file: `MA_API`, `FAV_NAME`
   (your quality endpoint), `GROUP_NAME`, `ANN_RATIO`, `ANN_FLOOR`.
4. Install and start the service (`examples/ma-fav-button.service`):
   ```bash
   sudo cp examples/ma-fav-button.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now ma-fav-button.service
   curl localhost:8699/health
   ```
5. Add the `rest_command` (`examples/configuration-rest-command.yaml`)
   to HA's `configuration.yaml` — **this needs one full HA restart**
   (`rest_command` has no reload service; stage everything first).
6. Add the automation (`examples/ha-automations.yaml`) and hit
   **Automations → Reload** (no restart needed for automations).
7. Dry-run test:
   ```bash
   curl -X POST localhost:8699/fav-button \
     -d '{"source":"single","dry_run":true}'
   ```

## Lessons encoded here (read before simplifying)

1. **Announcing into a dead group plays silence with no error.**
   Daemon restarts can drop a player from its sync group, leaving only
   a ghost member; MA accepts the announcement and plays it to nobody,
   which reads as "the button died". The relay never announces into a
   group the target isn't a member of, and regrouping is a normal
   post-restart chore.
2. **Resume the player's OWN queue, never the global one.**
   `players/cmd/play` resumes the server-global active queue and 500s
   with `QueueEmpty` when that queue is empty (e.g. group queue empty
   while the member's own queue holds the position). Always resolve the
   queue scoped to the player first, `get_active_queue` fallback second.
3. **Mute/fade cannot fix a lock-loss relay click.** Proven: the pause/
   resume click on a USB DAC (SMSL/XMOS muting relay) sounds identical
   through full digital silence — it's hardware, not signal. Fixed
   separately in the Sendspin client (keep the stream open on pause);
   see [sendspin-python-cli PR #304](https://github.com/Sendspin/sendspin-python-cli/pull/304).
4. **Announcement clicks are genuine format switches.** TTS arrives as
   FLAC 48 kHz/16-bit, music as 44.1 kHz/24-bit — two real stream
   rebuilds per voice message, correct behaviour. Silencing them needs
   MA to announce at the current stream format (upstream work, not here).

## The parked lean experiment

A Tuya TS0601 vibration sensor ("volume") was trialled as a
tip-to-adjust volume remote: X-axis excursion = down, Y-axis = up,
settle-and-classify in a 2 s poll loop (threshold crossings alone are
unusable — a round puck tumbles through every threshold mid-gesture).
Findings: UP worked first try; DOWN needed per-axis sustain (cross-axis
stillness vetoes real holds) plus a wide re-arm band (rest Y drifts
2→35 with seating). Parked because hand-held holds rock too much for
daily reliability — a real rotary knob is the right hardware. The code
path remains (`LEAN_ENABLE = False`, automation off); the HA
automation file documents the trigger shapes tried. If you revive it,
start with the calibration table in the relay comments.

## Files

- `ma-fav-button-relay.py` — the relay (stdlib only, ~340 lines)
- `tests/test_relay.py` — unit tests (group guard, paused priority,
  empty-queue guard, dry-run purity); run `python3 -m pytest tests/ -q`
- `examples/ma-fav-button.service` — systemd unit
- `examples/configuration-rest-command.yaml` — HA `rest_command`
- `examples/ha-automations.yaml` — button automation (+ parked lean)

## License

MIT — do what you like, no warranty. Not affiliated with Music
Assistant, Nabu Casa, Tuya, or SMSL.
