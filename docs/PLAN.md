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
| M2 | Audio: dual-output playback, chimes, recording with cap, stop and silence detection, loudness | M0 | |
| M3 | Printer: card rendering (letter, weather, mood faces, coloring page) + Brother QL-800 backend | M0 | |
| M4 | Local store: SQLite index, settings, content cache, daily state, event and print log, upload outbox | M0 | |
| M5 | Interaction engine: the button state machine from SPEC §2–§6 | M1–M4 | |
| M6 | Nightly content pipeline: Claude, OpenAI images and TTS, safety review, approval gate, evergreen fallback, spend tracking | M4 | |
| M7 | Mood speech-to-text + classification, with offline queue | M4 | |
| M8 | Cloud: Cloudflare Worker (Twilio IVR + PIN, R2, review timer, SMS, Pi sync API) + Pi sync client | M4 | |
| M9 | Parent dashboard (FastAPI): settings, approvals, archive, mood log, stats, reprint, review/delete, spend | M4, M5 | |
| M10 | App wiring (`zozobox.app`), systemd units, ALSA config, install script, read-only root, Tailscale notes | all | |
| M11 | Hardware bring-up and end-to-end test on the real box (owner) | M10 | |

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
Contracts are written in this file when M5 lands.

## Testing this repo
```bash
python -m pytest
python -m zozobox.bench leds --fake --hold 0.05
```
On the Pi: `pip install -e .[pi]`, then `zozobox-bench leds` and `zozobox-bench buttons`.
