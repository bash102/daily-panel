# Build Plan and Sequence

The source of truth for behavior is [SPEC.md](../SPEC.md). This file covers *order of work* and *module contracts*.

## Principles
- **Hardware at the edges.** Every hardware or cloud dependency sits behind a small backend class with a `Fake*` twin, so all logic is unit-tested off-Pi. Backends that need real hardware or the network live in `zozobox/hw/` or are imported lazily.
- **Inject time.** Anything timing-based takes a `clock` callable (and, where it sleeps, a way to advance it in tests).
- **Never crash the box.** Background threads log and continue. Hardware and cloud errors degrade gracefully, as in SPEC §7.4.
- **Python 3.11+** (Raspberry Pi OS Bookworm). Package `zozobox/`, tests in `tests/`, run with `python -m pytest`. Cloud code lives in `cloud/`.

## Milestones

| # | Milestone | Depends on | Status |
|---|---|---|---|
| M0 | Repo skeleton, config, test tooling | — | done |
| M1 | Buttons (debounce, lockout, chord) and LEDs (PCA9685, states, gamma) + bench tool | M0 | done |
| M2 | Audio: dual-output playback, chimes, recording with cap, stop and silence detection, loudness | M0 | done |
| M3 | Printer: card rendering (letter, weather, mood faces, coloring page) + Brother QL-800 backend | M0 | done |
| M4 | Local store: SQLite index, settings, content cache, daily state, event and print log, upload outbox | M0 | done |
| M5 | Interaction engine: the button state machine from SPEC §2–§6 | M1–M4 | done (see docs/ENGINE.md) |
| M6 | Nightly content pipeline: Claude, OpenAI images and TTS, safety review, approval gate, evergreen fallback, spend tracking | M4 | done |
| M7 | Mood speech-to-text + classification, with offline queue | M4 | done |
| M8 | Cloud: Cloudflare Worker (Twilio IVR + PIN, R2, review timer, SMS, Pi sync API) + Pi sync client | M4 | done |
| M9 | Parent dashboard (FastAPI): settings, approvals, archive, mood log, stats, reprint, review/delete, spend | M4, M5 | done |
| M10 | App wiring (`zozobox.app`), systemd units, ALSA config, install script, read-only root, Tailscale notes | all | done (see docs/DEPLOY.md) |
| M11 | Hardware bring-up and end-to-end test on the real box (owner) | M10 | pending (owner; checklist in docs/BRINGUP.md) |

**Sequence:**
1. M0 → M1.
2. **M2, M3 and M4 in parallel.**
3. M5.
4. **M6, M7, M8 and M9 in parallel.**
5. M10, then M11.

## Module contracts
These signatures are what parallel milestones build against. Implementations may add helpers, but must not change these without updating this file.

### M2 `zozobox/audio/`
```python
class Output(Enum): BOTH, SPEAKER          # BOTH = handset earpiece + box speaker
class Playback:                             # handle for one sound
    def stop(self) -> None; def wait(self, timeout=None) -> bool; done: bool
class AudioOut(Protocol):
    def play(self, path: Path, output: Output = Output.BOTH) -> Playback  # non-blocking
    def stop_all(self) -> None
    def set_volume(self, level: float) -> None   # 0..1, always under the fixed ceiling
@dataclass class Recording: path: Path; duration_s: float; silent: bool
class AudioIn(Protocol):
    def record(self, path: Path, max_s: float, stop: threading.Event) -> Recording  # blocking
```
- While `record` runs, the box speaker is muted (echo and feedback).
- `silent` = no speech above the threshold, so the caller discards the recording (SPEC §2.2).
- Provide `FakeAudioOut` and `FakeAudioIn`.

### M3 `zozobox/printer/`
```python
WIDTH_PX = 696  # 62 mm continuous paper on the QL-800 at 300 dpi
def render_letter_card(letter: str, words: list[str], drawing: Image | None) -> Image
def render_weather_card(summary: str, icon: str, hi_c: float, lo_c: float) -> Image
def render_mood_card(mood: str | None) -> Image      # None = all four faces
def render_coloring_page(art: Image) -> Image
class Printer(Protocol): def print_image(self, img: Image) -> None   # raises PrinterError
```
- Output is 1-bit and WIDTH_PX wide.
- `FakePrinter` saves PNGs.

### M4 `zozobox/store/`
```python
class Store:  # SQLite at <data_dir>/zozobox.db; media under <data_dir>/media/
    settings: Settings        # quiet_hours, location, rec_cap_s (<=300), volume, spend_cap_usd, review_delay_s, ...
    content: ContentRepo      # items by kind (story/letter/weather/coloring/phrase), served/approved/safety state
    daily: DailyState         # printed-today per item, rotation index, heard flags; resets at morning time
    events: EventLog          # every press, play, print (with a PNG copy), recording, mood
    outbox: Outbox            # files and records waiting to upload to R2
```

### M5 `zozobox/engine/`
- Consumes `buttons.Event`s and drives `AudioOut`, `AudioIn`, `Printer`, `LedController` and `Store`.
- Each experience is a sequence of steps.
- Implements skip, switch, the print-only-after-completion rule, the Print re-press rule, LED rules, quiet hours and the record cap.
- Fully tested with fakes and a fake clock.

### M6–M9
The M6 content contract and the M7 classifier contract are in [ENGINE.md](ENGINE.md).

## Testing this repo
```bash
python -m pytest
python -m zozobox.bench leds --fake --hold 0.05
python -m zozobox.app --fake            # engine with fake hardware; r/g/b/y + Enter
```
On the Pi: `deploy/install.sh` (docs/DEPLOY.md), then `zozobox-bench leds`, `buttons`, `audio` and `print` (docs/BRINGUP.md).

## Hardware notes from M2–M4 (check during M11 bring-up)
**Audio**
- ALSA card names and mixer controls are placeholders. The handset is `Phone`, the speaker DAC is `Device`, and the controls are `PCM` and `Mic`. Confirm them with `aplay -l`, `arecord -l` and `amixer -c CARD scontrols`, then set them in `/etc/zozobox/box.toml` (`[audio.*]`, see deploy/box.toml.example).
- The volume ceilings (`max_fraction`) are placeholders: handset 0.5, speaker 0.7. Calibrate them with a sound level meter to about 75–80 dBA. M10 also installs an `asound.conf` softvol cap as the backstop.
- If a ceiling can't be applied, that output refuses to play (`refuse_uncapped=True`). This is deliberate: a wrong control name means silence, not loud audio.
- Production wiring is `create_alsa_audio()`, which mutes the speaker while recording.
- M10 installed `deploy/asound.conf` (softvol hard cap + dmix per output). `box.toml` points `[audio.handset|speaker].alsa_pcm` at it. Card names in both files are still placeholders.

**Printer**
- `brother_ql==0.9.4` (pip) and `libusb-1.0-0` (apt).
- Add a udev rule for USB device `04f9:209b`. The QL-800 product ID comes from Brother's USB IDs and isn't in brother_ql's device list. (M10: `deploy/udev/99-zozobox-ql800.rules`.)
- Turn off the QL-800's Editor Lite mode (hold its button until the light goes out).

**Store**
- The database and media must live on the persistent data partition, not the read-only overlay root.
- Only the sync process opens the store with `recover_outbox=True`.

## Notes from M6–M9
- **First-month approval covers everything**, including the 16 fixed phrases and the evergreen copies. Until a parent approves the phrase items on the dashboard, the box plays no spoken phrases (clicks and beeps still work). Approve them right after bootstrap.
- **Archive audio is WAV** (SPEC 7.3, decided at M10).
- **Content `status()`** reports `phrases_pending`. Weather items may carry `meta.source="template"` when Claude is unavailable twice.
- **The Worker's quiet-hours pause** is mirrored in the dashboard's review-countdown display. The DST edge cases are covered by unit tests only.

## Notes from M10
- **`python -m zozobox.app`** (console script `zozobox-engine`) is the engine process. Static config comes from `/etc/zozobox/box.toml` (`zozobox/boxconfig.py`, template `deploy/box.toml.example`). Unknown keys are errors; check with `--check-config`. `--fake` runs everything with fakes.
- **Hardware is optional at runtime.** A part that fails to start is logged and replaced by an inert stand-in, and an `error` event `hw_init_failed` is recorded. Buttons and LEDs are retried every 30 s (`zozobox/hw/supervised.py`, `[app].hw_retry_s`; a `hw_recovered` event when they come up), the printer every 60 s (`SupervisedPrinter`). The store is the one hard requirement. A `box.toml` error exits 2, and the engine and dashboard units don't restart on it (`RestartPreventExitStatus=2`).
- **Dashboard first-run setup** needs a one-time token (`<data dir>/dashboard-setup-token`, also in the journal), so nobody else on the LAN can choose the parent password. The dashboard reads `box.toml` for reprints (`[printer]`, `[app].printer_*`).
- **Sync refuses placeholder or non-https cloud settings** (`check_cloud_config`), so the box token is never sent to a template host.
- **One printer lock for both processes.** The engine and the dashboard (`--printer brother`) print through `LockedPrinter`, an `fcntl` lock on `<data dir>/.printer.lock`. A lock busy for more than 30 s fails that print with `PrinterError` ("printer busy").
- **Parent chord.** It plays a network-status chime (two beeps if a sync pass reached the cloud in the last 5 minutes, one if not), blinks the LEDs, then runs `sudo -n /usr/bin/systemctl poweroff`. `deploy/sudoers/zozobox-poweroff` allows only that command. As a result the engine unit cannot use `NoNewPrivileges` or any option that implies it. docs/DEPLOY.md describes a polkit alternative.
- **Bundled assets.** The click and beep are generated deterministically (`zozobox/assets.py`) into `<data dir>/chimes/` at start-up. The bootstrap step TTS-generates the soundboard starter pack, 16 hand-reviewed clips in `zozobox/content/soundboard.py`, as content items of kind `soundboard` (media `soundboard/<slug>-<id>.wav`). Like the phrases they need first-month approval; the engine's `StoreSoundboard` plays only servable ones. `zozobox-import-clip` adds owner-chosen CC0 clips to `[app].soundboard_dir` (no approval). The TTS pack stands in for SPEC 2.3's bundled CC0 pack, which can't be redistributed from the repo. Fallback phrase WAVs can be dropped into `<data dir>/assets/phrases/<name>.wav`.
- **Integration tests** (`tests/integration/`) run a whole simulated day against an in-process fake worker over localhost HTTP, and start `python -m zozobox.app --fake` as a real process to check a clean SIGTERM/SIGINT shutdown.
