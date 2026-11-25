# Interaction engine (M5)

`zozobox/engine/` turns button events into experiences (SPEC sections 2–6). It drives `AudioOut`, `AudioIn`, `Printer`, `LedController` and `Store`. Everything is injected, so the whole state machine is tested off-Pi with fakes and a fake clock (`tests/engine/`).

## API

```python
from zozobox.engine import Engine, EngineSettings, StorePhraseBook, DirPhraseBook, DirSoundboard

eng = Engine(
    cfg,                 # zozobox.config.BoxConfig (cfg.roles maps Btn -> Role)
    store,               # zozobox.store.Store
    audio_out, audio_in, # zozobox.audio (create_alsa_audio() in production)
    printer,             # zozobox.printer.Printer
    leds,                # zozobox.leds.LedController
    phrases,             # PhraseBook: path_for(name) -> Path | None
    mood_classifier,     # MoodClassifier: classify(path) -> MoodResult   (M7)
    soundboard,          # Soundboard: clips() -> list[Path]
    clock=time.monotonic,          # seconds, used for pauses/timeouts/rate limits
    rng=random.Random(),
    settings=EngineSettings(click=..., beep=...),
    on_parent_chord=callback,      # (ParentChord) -> None; the app decides what to do
)
eng.handle(event)        # buttons.Press / buttons.ParentChord
eng.tick()               # drive the running experience + periodic checks (~every 20 ms)
eng.start(button_input.events)  # thread: run(queue) = handle + tick loop
eng.close()             # cancel, then wait (<= close_join_s) for print jobs / late saves
eng.busy                 # an experience is running
```

Production wiring is `zozobox/app.py` (`python -m zozobox.app`, PLAN M10). It builds the real backends from `/etc/zozobox/box.toml`, uses `StorePhraseBook` with a `DirPhraseBook(<data dir>/assets/phrases)` fallback and `StoreSoundboard(store, <data dir>/soundboard)` (approved TTS starter clips from the content store plus the owner's imported clips; the legacy `starter/` subdirectory is ignored), and passes the generated chimes as `click` and `beep`.

`handle()` and `tick()` must be called from one thread (the `run` loop does this, and they also hold a lock). They never raise.

`close()` stops the loop and cancels the running experience (a grandma reply is saved). It then waits up to `close_join_s` for helper threads that still write to the store: print jobs that kept running after a switch-away, and late grandma saves. Call it before `store.close()`.

`EngineSettings` holds per-install tunables: `click` and `beep` sound files (bundled assets on the box speaker), `repeat_pause_s=3`, `mood_record_s=8`, `classify_timeout_s=20`, `sleepy_interval_s=300`, `recent_clips=5`, `praise_phrases`, `record_join_s=3`, `print_timeout_s=60`, `close_join_s=15`, `click_retry_s=2`, `max_play_s=900`, `state_poll_s=1`, `led_refresh_s=5` and `tick_s=0.02`. Runtime settings (volume, `rec_cap_s`, quiet hours, day start) are read from the store.

## Model

- **Experience** (`experience.py`): one button's sequence of steps, plus hooks: `on_complete` (every step finished), `on_same_press` (the role's skip rule) and `on_abandon`. Steps can insert follow-up steps after themselves (used by the mood flow).
- **Steps** (`steps.py`): `start(eng)`, `poll(eng) -> done`, `cancel(eng)`.
  - `PlayStep`: plays a file or a named phrase. A missing sound is skipped with a log line. A failed playback (device error) is logged as an `error` event, and the sequence carries on. `on_played` runs only when the sound played to its end, including when it ended just before a press cancelled the step (not yet polled). `audible` says whether the sound came out at all (started, not failed, not suppressed).
  - `PauseStep`: waits on the engine clock.
  - `CallStep`: runs a function on the engine thread.
  - `WorkerStep`: runs blocking work in a helper thread. The result is delivered on the engine thread by `poll`, with an optional timeout on the engine clock.
    - `RecordStep`: `AudioIn.record` with a `threading.Event` stop. The cap is also enforced on the engine clock. `cancel()` always stops the take and waits (up to `record_join_s`) for the recorder to return, so the speaker mute is released before anything else plays. Then the take is saved (`save_on_cancel`, grandma) or deleted (mood).
    - `ClassifyStep`: an error, a timeout or a bad label counts as offline or unknown. Cancelled (switch-away) before the answer: `on_abandon` logs the take as `mood_pending` (reason `abandoned`), and the late answer is dropped.
    - `PrintStep`: renders, prints, then `daily.mark_printed`, then `events.record_print`, all inside the worker. A print in progress is never interrupted: once paper is moving, switching away cannot lose the bookkeeping. A failure is logged (`print_failed`) from the worker, so it is recorded even after a switch-away. While the job runs the item is in flight on the engine (`print_in_flight`), so no other run prints it, and jobs are serialized on `eng.printer_lock`. After `print_timeout_s` the step stops waiting (`print_failed`, reason `timeout`, then `printer_sleepy`); the job may still finish and record the print. It refuses to print (reason in `exp.data["print_skipped"]`) in quiet hours (`quiet`), after a box-day rollover since the sequence was built (`day_rolled_over`), when already printed (`already_printed`) or while the same item's job is in flight (`in_progress`).
- **Builders** (`builders.py`): one per role. They read the store and return the experience.
- **Never crash**: an exception in a step, a builder or a hook is logged (as an `error` event too when the store works). The running experience ends without printing, and the engine carries on. LED, store-logging and click failures are contained individually.

## Behavior summary

| Situation | What happens |
|---|---|
| Accepted press (not quiet) | A click plays on `Output.SPEAKER`. When a recording was running (grandma or mood), the click and the next experience play only after the recorder has stopped (up to `record_join_s`), so neither is lost to the speaker mute. Backstop: a click that still comes back `suppressed` (a recorder stuck past `record_join_s`) is retried every tick for up to `click_retry_s`. |
| Press of another button | Cancel the current experience (no print, no completion hooks). A grandma reply being recorded is stopped and **saved** first; a mood take being recorded is stopped and discarded; a mood take being classified is logged as `mood_pending`. Then the new button's experience starts. |
| Press of the same button | Print: ignored entirely (no click, no skip; logged `action=ignored`). Grandma: during her message or "say hi", skip to the beep; during recording, stop and save, and the experience ends. Soundboard: stop the clip and play another. Story: next chunk (skipping the last chunk finishes the story). |
| ParentChord | Cancel the experience (saving a reply being recorded), `audio_out.stop_all()`, log `parent_chord`, call `on_parent_chord`. |
| Quiet hours | LEDs off. No click. Every press is logged as `quiet_press`. The `sleepy` phrase plays at most once per `sleepy_interval_s` (it replaces a running experience). A grandma recording is stopped and saved on any press. Otherwise a running experience that started before quiet hours finishes, and its LED keeps pulsing. `PrintStep` refuses to print in quiet hours. |
| Volume | `store.settings.volume` is applied at construction and re-checked every `state_poll_s`. |
| Day rollover / quiet start-end | Checked every `state_poll_s`. LEDs are recomputed. Rotation and printed state reset by themselves (DailyState is per box day). |

**Print rotation** (`daily.print_item`): letter → weather → mood → coloring.
- The rotation advances when an item's sequence completes, including audio-only replays, printer failures and print timeouts. It does not advance when the item is abandoned (the next press restarts the same item), when the sequence finishes after a box-day rollover, or when `PrintStep` refused the print for quiet hours or a day rollover (treated like an abandoned run: same item next time, content not marked completed).
- An item whose print job is still running from an abandoned run counts as printed: the next Print press replays its audio only.
- The print happens only as the final step, so it needs every earlier step to have completed.
- An item already printed today plays its audio only. If every item that can print today (content present, plus mood) has printed, it adds `all_printed_today`.
- A missing or unservable item plays `print_not_ready` and the rotation moves on.
- Printer failure (`PrinterError` or any render or print exception): the item is not marked printed, an `error` event `print_failed` is logged, `printer_sleepy` plays, and nothing is archived.

**Per item:**
- Letter: intro → for each word, its prompt + `repeat_pause_s` pause → a random available `praise_*` → `here_is_your_card` → `render_letter_card(letter, words, drawing)`.
- Weather: summary audio → `here_is_your_card` → `render_weather_card(...)`.
- Coloring: intro → `here_is_your_card` → `render_coloring_page(art, title)`.
- Mood: `ask_mood` → record (≤ `mood_record_s`), then branch on the result:

| Result | Logged | Then |
|---|---|---|
| silent | `mood` label `silent`, source `box`, not classified | print all four faces |
| offline (or error, or timeout) | `mood_pending` event | print all four faces |
| switched away while classifying | `mood_pending` event, payload `reason: "abandoned"` | (experience gone) |
| recognized | `mood` label, source `online` | `you_feel_<mood>` + print that face |
| unknown, 1st attempt | `mood` `unknown`, source `online` | `ask_mood_again` + record + classify once more |
| unknown, 2nd attempt | `mood` `unknown`, source `online` | print all four faces |

Every take's audio is kept under `media/mood/…` and queued in the outbox. Mood events carry `extra={"attempt": n}`. Switching away mid-recording discards the take, and nothing is logged.

**Grandma:**
- Plays the live `Slot.GRANDMA` recording, if any. When it plays to the end (also when a press lands right after it ended, before the next tick): `recordings.mark_heard` + `daily.mark_grandma_heard`.
- `say_hi_grandma`, then the beep (`Output.SPEAKER`), then a record with cap = `min(store.settings.rec_cap_s, 300)`.
- `create_alsa_audio()` mutes the speaker while recording. The engine plays nothing until the take ends.
- A silent or empty take is deleted, and a `recording` event is logged with `saved: false`. The previous message stays.
- Otherwise the take is saved as the new `Slot.ZOZO` (superseding the old one), queued in the outbox, and a `recording` event is logged with `saved: true`.

**Soundboard:** a random clip, never one of the last `min(recent_clips, n-1)` played.

**Story:** `content.current_for_today('story')`, `mark_served` at start. It plays the chunks in order. When finished, and at least one chunk was audible (played fully or until skipped; not every playback failed): `mark_completed` + `daily.mark_story_heard`. With no story, or no playable chunk: `no_story_yet`. Replays are unlimited.

**LEDs** (recomputed after each experience start/end, each recording start/end, on quiet or day change, and every `led_refresh_s` so synced messages and content show up):

| Button | Bright | Otherwise |
|---|---|---|
| Print | any printable rotation item not printed today | dim |
| Grandma | `recordings.has_unheard(GRANDMA)` | dim |
| Story | a story is available today and `daily.story_heard` is false | dim |
| Soundboard | never | dim |
| Active button | `PULSE_FAST` while recording, else `PULSE_SLOW` | |
| Quiet hours | all `OFF`, except a still-running experience's pulse | |

## Events written (store.events)

| type | payload | media |
|---|---|---|
| `press` | `button`, `role`, `action` (`start`, `switch`, `skip` or `ignored`) | |
| `quiet_press` | `button`, `role`, `sleepy` (bool: the phrase played) | |
| `play` | `what` (label), `path`, `output`, `experience` | |
| `print` | via `record_print(item, img, content_id=…, extra=…)`; `extra` has `letter` for letter and `mood` (`happy`… or `all`) for mood | the PNG copy |
| `recording` | `slot: zozo_message`, `saved`, `rec_id` or `reason`, `duration_s` | the take, if saved |
| `mood` | via `record_mood(label, transcript, source, audio, ts=take start, extra={attempt})` | the take |
| `mood_pending` | `attempt`, `audio`, `transcript`, optional `reason` (`abandoned`) (ts = take start) | the take |
| `error` | `kind` (`play_failed`, `print_failed`, `record_failed`, `experience_failed`, `build_failed`, `complete_failed` or `engine_error`) + details; `print_failed` has `item`, `error` and, for a hung printer, `reason: "timeout"` | |
| `parent_chord` | | |

**Media written by the engine (relative to the media dir):**
- `recordings/zozo/YYYY/MM/DD/HHMMSSmmm-<id8>.wav`
- `mood/YYYY/MM/DD/HHMMSSmmm-<id8>.wav`

Both are queued in the outbox with the relative path as `remote_key` and `content_type` `audio/wav`. Print PNGs are queued by the store itself.

## Content contract for M6

All items must be servable (`safety=passed`, approval `approved` or `auto`). The engine reads `content.current_for_today(kind)` and calls `mark_served` when it starts using an item, and `mark_completed` when the full sequence finishes. Media values are paths relative to the media dir. Audio is a WAV (or another `aplay`-playable format) normalized to -16 LUFS. A missing media key or file only skips that one step, except where noted below.

| kind | `media` keys | `meta` keys | notes |
|---|---|---|---|
| `story` | `chunk_0`, `chunk_1`, … (natural sort), **or** any keys listed in order in `meta.chunks`, **or** a single `audio` | `chunks` (optional list of media keys), `title` (optional) | ~60 s total. One chunk is one skip unit (about 10–20 s each). No playable chunk counts as no story. |
| `letter` | `intro` (announce the letter: "Today's letter is B!"), `word_<i>` for each word, by its index in `meta.words` ("B… ball! Can you say ball?"), `drawing` (optional PNG/JPEG line art for the card) | `letter` (**required**: one alphabetic character), `words` (list; the card shows the first 3) | Without a valid `letter` the item counts as missing. The praise after the words comes from the fixed phrases. |
| `weather` | `audio` (spoken summary) | `summary` (text printed on the card), `icon` (`sunny`, `partly_cloudy`, `cloudy`, `rain`, `snow`, `storm`, `fog`, `wind`, or a synonym `normalize_icon` knows), `hi_c`, `lo_c` (numbers, °C) | `for_date` = the day it is for (the store defaults it). |
| `coloring` | `art` (**required** image; thick black lines on white), `intro` (spoken intro) | `title` (optional, printed above the art) | Unreadable or missing `art` counts as missing. |
| `phrase` | `audio` | `key` = the phrase name | Fixed TTS phrases, cached permanently. Read through `StorePhraseBook` (with a `DirPhraseBook` of bundled files as fallback). |

**Phrase names the engine asks for** (`zozobox.engine.PHRASES`): `say_hi_grandma`, `sleepy`, `printer_sleepy`, `no_story_yet`, `print_not_ready`, `ask_mood`, `ask_mood_again`, `you_feel_happy`, `you_feel_mad`, `you_feel_sad`, `you_feel_neutral`, `all_printed_today`, `here_is_your_card`, and the praise set `praise_great_job`, `praise_well_done`, `praise_you_did_it` (`EngineSettings.praise_phrases`). The click and beep are bundled chime files (`EngineSettings.click` / `.beep`), not phrases.

## Mood classifier contract for M7

```python
@dataclass(frozen=True)
class MoodResult:
    label: str          # "happy" | "mad" | "sad" | "neutral" | "unknown"
    transcript: str = ""
    offline: bool = False

class MoodClassifier(Protocol):
    def classify(self, path: Path) -> MoodResult: ...   # blocking, WAV in
```

- It is called in a worker thread. It may block. After `classify_timeout_s` (default 20 s) the engine gives up and treats the answer as offline. It may raise, which also counts as offline.
- `offline=True` means "couldn't get an answer now". The engine logs a `mood_pending` event (ts = when the take started, `media` = the audio's relative path, payload `attempt`) and prints all four faces.
- A take whose classification was abandoned (Zozo pressed another button) is also logged as `mood_pending`, with payload `reason: "abandoned"`. M7 treats it like any other pending take.
- M7 later re-processes each `mood_pending` event that has no matching `mood` event (match on `media`). It calls `store.events.record_mood(label, transcript=…, source="offline", audio=ev.media, ts=ev.ts)`, so the result lands on the original day and order.
- The audio is already queued for upload.
- Any label outside the five is treated as `unknown`. `transcript` is logged.

## Decisions and deviations

- The engine clock is a float (monotonic seconds). The store keeps its own datetime clock. Tests drive both from one fake clock.
- The rotation advances after a printer failure, since the item's sequence did complete. The item stays unprinted, so its LED stays bright and it prints on its next turn in the loop.
- A switch-away while the print job is already running lets the print finish and be recorded (or its failure logged). The experience still counts as abandoned (rotation not advanced). Because the item is in flight, the next Print press replays that item's audio only, even before the job finishes; if that job then fails, the item stays unprinted and prints on its next turn.
- A print job that hangs past `print_timeout_s` counts as a printer failure for the experience. The item stays in flight until the job returns, so it never prints twice.
- A Print item whose audio all failed (audio device gone) still prints: the card does not depend on the speaker. Only the story requires something audible before it is marked heard.
- No click in quiet hours (the sleepy phrase is the feedback).
- A mood take that is silent is logged as `silent` (a valid `MOOD_LABELS` value), and its audio is kept and uploaded like the others.
- A "first mood of the day" that was `unknown` on attempt 1 is followed by the attempt-2 event. Dashboards wanting the recognized answer use `first_mood(recognized_only=True)`.
- Same-button rate limiting and first-press-wins are left to the button layer (M1 lockout). The engine applies every event it gets.
