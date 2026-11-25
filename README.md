# Zozo's button box

Zozo's button box is a wall-mounted toddler box running on a Raspberry Pi 3. It has four big illuminated arcade buttons, a retro phone handset, a speaker and a label printer:

- **Print** prints something new each press: the letter of the day, the weather, a mood card ("How are you feeling?") or a coloring page.
- **Grandma** works like voicemail. It plays grandma's latest message, then records Zozo's reply. Grandma calls a Twilio number to listen and to leave new messages.
- **Story** plays a short story, generated each night, with Zozo as the hero.
- **Soundboard** plays silly sounds.

The box is built to survive slapping: every interaction is a single press, and presses are debounced and locked out. A parent dashboard on the Pi handles approvals, settings, the archive and reprints. The archive lives in Cloudflare R2.

The behavior spec is [SPEC.md](SPEC.md). The build plan and module contracts are in [docs/PLAN.md](docs/PLAN.md).

## Repo layout

| Path | What |
|---|---|
| `zozobox/app.py` | The engine process (`python -m zozobox.app`). It wires the hardware to the engine. |
| `zozobox/boxconfig.py` | Loads `/etc/zozobox/box.toml`: pins, LED channels, roles, ALSA cards, volume ceilings, printer. |
| `zozobox/buttons.py`, `leds.py`, `hw/pi.py` | Slap-proof button input, PCA9685 LED control, and the Pi backends. |
| `zozobox/audio/` | ALSA playback on two outputs, recording, loudness normalization. |
| `zozobox/printer/` | Card rendering, the Brother QL-800 backend, and the cross-process printer lock. |
| `zozobox/store/` | SQLite store: settings, content cache, daily state, event log, upload outbox. |
| `zozobox/engine/` | The button state machine (see [docs/ENGINE.md](docs/ENGINE.md)). |
| `zozobox/content/` | The nightly content pipeline (Claude, OpenAI images and TTS, safety gate) and the soundboard starter pack. |
| `zozobox/mood/` | Mood speech-to-text and classification, with an offline queue. |
| `zozobox/sync/` | The Pi side of cloud sync (see [docs/SYNC.md](docs/SYNC.md)). |
| `zozobox/dashboard/` | The parent dashboard (FastAPI). |
| `zozobox/assets.py`, `import_clip.py` | Generated click and beep chimes, and the `zozobox-import-clip` command. |
| `cloud/worker/` | The Cloudflare Worker: Twilio IVR, PIN, review timer, SMS, R2 and the sync API ([README](cloud/worker/README.md)). |
| `deploy/` | `install.sh`, systemd units, `box.toml` and `secrets.env` templates, `asound.conf`, the udev rule and the sudoers drop-in. |
| `tests/` | pytest (fakes for all hardware and cloud). `tests/integration/` runs a whole simulated day. |

## Developer quickstart

You need Python 3.11 or later. ffmpeg is optional and only used by the loudness tests.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
python -m pytest -q                       # the whole suite; no network, no hardware

python -m zozobox.app --fake              # the engine with fake hardware; type r/g/b/y + Enter
python -m zozobox.bench leds --fake --hold 0.05
python -m zozobox.dashboard --data-dir ~/.zozobox-fake --printer fake:/tmp/prints --host 127.0.0.1

cd cloud/worker && npm install && npm test   # the worker (vitest, Node 22+)
```

`--fake` keeps its data in `~/.zozobox-fake/`. To give the soundboard some clips there, copy any WAVs into `~/.zozobox-fake/soundboard/`. (`python -m zozobox.content bootstrap --data-dir ~/.zozobox-fake` makes the TTS starter clips, but it needs real API keys and the clips play only once approved on the dashboard.)

## Deploying

The full steps are in [docs/DEPLOY.md](docs/DEPLOY.md). In short:

1. Flash Raspberry Pi OS Bookworm Lite. Clone this repo to `/opt/zozobox/src` and run `sudo deploy/install.sh`.
2. Fill in `/etc/zozobox/secrets.env` (API keys, worker URL and token). Adjust `/etc/zozobox/box.toml`.
3. Deploy the Cloudflare Worker and set up the Twilio number ([cloud/worker/README.md](cloud/worker/README.md)).
4. Run `sudo systemctl start zozobox-bootstrap`, then approve the phrases on the dashboard at `http://<pi>:8080`.
5. Bring up the hardware with the checklist in [docs/BRINGUP.md](docs/BRINGUP.md).
6. Install Tailscale. Set up the read-only root with its persistent data partition.

The services are `zozobox-engine`, `zozobox-dashboard`, `zozobox-sync`, `zozobox-nightly.timer` (about 02:30) and `zozobox-mood-reprocess.timer` (hourly). Their logs are in the journal: `journalctl -u 'zozobox-*' -f`.

## Docs

- [SPEC.md](SPEC.md): the behavior specification.
- [docs/PLAN.md](docs/PLAN.md): milestones, module contracts and hardware notes.
- [docs/ENGINE.md](docs/ENGINE.md): the interaction engine, and the content and mood contracts.
- [docs/SYNC.md](docs/SYNC.md): the Pi to cloud protocol.
- [docs/DEPLOY.md](docs/DEPLOY.md): installing on the Pi, the cloud, Twilio, Tailscale and the read-only root.
- [docs/BRINGUP.md](docs/BRINGUP.md): the hardware bring-up checklist (M11).
- [docs/hardware-research.md](docs/hardware-research.md): notes on the handset and LED driver choices.
