"""The interaction engine (PLAN M5, SPEC sections 2 - 6).

A deterministic, tick-driven state machine:

    eng = Engine(cfg, store, audio_out, audio_in, printer, leds, phrases,
                 mood_classifier, soundboard, clock=time.monotonic)
    eng.handle(event)   # a buttons.Press or buttons.ParentChord
    eng.tick()          # drive the running experience, periodic checks
    eng.start(button_input.events)   # or: a thread running both, every ~20 ms

At most one experience runs at a time. A press of another button cancels it
(no print) and starts that button's experience; a press of the same button is
the role's skip rule. Nothing in here may crash the box: every failure is
logged, the experience in progress ends, and the engine carries on.
"""

from __future__ import annotations

import collections
import logging
import math
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..audio import AudioIn, AudioOut, Output
from ..buttons import ParentChord, Press
from ..config import BoxConfig, Btn, Role
from ..leds import LedState
from ..store import Kind, Slot
from ..store.events import EventType
from . import builders
from .experience import Experience
from .sources import MoodClassifier, PhraseBook, Soundboard

log = logging.getLogger(__name__)


@dataclass
class EngineSettings:
    """Engine tunables. Runtime settings (volume, cap, quiet hours) come from
    the store; these are fixed per install."""

    click: Path | None = None  # short click on every accepted press (box speaker)
    beep: Path | None = None  # "start talking" beep before the grandma reply
    repeat_pause_s: float = 3.0  # letter: time for Zozo to repeat each word
    mood_record_s: float = 8.0
    classify_timeout_s: float = 20.0  # longer = treated as offline
    sleepy_interval_s: float = 300.0  # quiet hours: one "shh" per 5 min at most
    recent_clips: int = 5  # soundboard: don't repeat any of the last N clips
    praise_phrases: tuple[str, ...] = ("praise_great_job", "praise_well_done", "praise_you_did_it")
    record_join_s: float = 3.0  # wait this long for the recorder to stop
    print_timeout_s: float = 60.0  # a print job running longer counts as failed
    close_join_s: float = 15.0  # close(): wait this long for print jobs / late saves
    click_retry_s: float = 2.0  # a click suppressed by the mute is retried this long
    max_play_s: float = 900.0  # a sound still "playing" after this is stopped
    state_poll_s: float = 1.0  # quiet hours / day rollover / volume checks
    led_refresh_s: float = 5.0  # re-read "something new" state (sync may add messages)
    tick_s: float = 0.02


class Engine:
    def __init__(
        self,
        cfg: BoxConfig,
        store,
        audio_out: AudioOut,
        audio_in: AudioIn,
        printer,
        leds,
        phrases: PhraseBook,
        mood_classifier: MoodClassifier,
        soundboard: Soundboard,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        *,
        settings: EngineSettings | None = None,
        on_parent_chord: Callable[[ParentChord], None] | None = None,
    ):
        self.cfg = cfg
        self.roles: dict[Btn, Role] = dict(cfg.roles)
        self.store = store
        self.audio_out = audio_out
        self.audio_in = audio_in
        self.printer = printer
        self.leds = leds
        self.phrases = phrases
        self.mood_classifier = mood_classifier
        self.soundboard = soundboard
        self.clock = clock
        self.rng = rng or random.Random()
        self.settings = settings or EngineSettings()
        self.on_parent_chord = on_parent_chord

        self.current: Experience | None = None
        self._lock = threading.RLock()
        self._recent_clips: collections.deque[Path] = collections.deque(maxlen=max(self.settings.recent_clips, 1))
        self._last_sleepy = -math.inf
        self._volume: float | None = None
        self._base: dict[Btn, LedState] = {b: LedState.OFF for b in self.roles}
        self._day = None
        self._quiet: bool | None = None
        self._next_state_poll = -math.inf
        self._next_led_refresh = -math.inf
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._click_retry_until: float | None = None
        # Print jobs: items whose job is running (never print one twice), and
        # one job at a time on the printer.
        self._printing: set[str] = set()
        self._printing_lock = threading.Lock()
        self.printer_lock = threading.Lock()
        # Helper threads that write to the store after their step is gone
        # (print jobs, late grandma saves); close() waits for them.
        self._helpers: set[threading.Thread] = set()
        self._helpers_lock = threading.Lock()

        self._check_volume()
        try:
            self._periodic(force=True)
        except Exception:
            log.exception("engine: initial state check failed")

    # ------------------------------------------------------------ public ---
    def handle(self, event: Any) -> None:
        with self._lock:
            try:
                if isinstance(event, Press):
                    self._on_press(event)
                elif isinstance(event, ParentChord):
                    self._on_chord(event)
                else:
                    log.warning("engine: unknown event %r", event)
            except Exception:
                log.exception("engine: handling %r failed", event)
                self.log_error("engine_error", where="handle", event=repr(event))
                if self.current is not None:
                    self._abort(self.current)

    def tick(self) -> None:
        with self._lock:
            self._retry_click()
            self._drive()
            try:
                self._periodic()
            except Exception:
                log.exception("engine: periodic checks failed")

    def run(self, events: "queue.Queue", stop: threading.Event | None = None) -> None:
        """Loop: handle queued events, tick every settings.tick_s."""
        stop = stop or self._stop
        while not stop.is_set():
            try:
                ev = events.get(timeout=self.settings.tick_s)
            except queue.Empty:
                ev = None
            except Exception:
                log.exception("engine: event queue failed")
                ev = None
                time.sleep(self.settings.tick_s)
            if ev is not None:
                self.handle(ev)
            self.tick()

    def start(self, events: "queue.Queue") -> threading.Thread:
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, args=(events,), name="engine", daemon=True)
        self._thread.start()
        return self._thread

    def close(self) -> None:
        """Stop the loop, cancel the running experience (saving a reply being
        recorded) and wait up to settings.close_join_s for print jobs and late
        saves, so their store writes land before the caller closes the store."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        with self._lock:
            if self.current is not None:
                self._cancel(self.current, "close")
        deadline = time.monotonic() + max(self.settings.close_join_s, 0.0)
        with self._helpers_lock:
            helpers = list(self._helpers)
        for t in helpers:
            if t is threading.current_thread():
                continue
            t.join(timeout=max(deadline - time.monotonic(), 0.0))
            if t.is_alive():
                log.warning("engine close: %s still running", t.name)

    # ------------------------------------------- print jobs / helper threads ---
    def track_thread(self, t: threading.Thread) -> None:
        """Have close() wait for `t` (it must be started right after)."""
        with self._helpers_lock:
            self._helpers = {h for h in self._helpers if h.is_alive() or not h.ident}
            self._helpers.add(t)

    def print_in_flight(self, item: str) -> bool:
        with self._printing_lock:
            return item in self._printing

    def begin_print(self, item: str) -> bool:
        """Reserve `item` for a print job; False if one is already running."""
        with self._printing_lock:
            if item in self._printing:
                return False
            self._printing.add(item)
            return True

    def end_print(self, item: str) -> None:
        with self._printing_lock:
            self._printing.discard(item)

    @property
    def busy(self) -> bool:
        return self.current is not None

    # ------------------------------------------------- helpers for steps ---
    def phrase_path(self, name: str) -> Path | None:
        try:
            p = self.phrases.path_for(name)
        except Exception:
            log.exception("phrase lookup failed: %s", name)
            return None
        if p is None:
            log.info("phrase %r not available", name)
        return p

    def log_event(self, type: str, payload: dict | None = None, *, media: str | None = None) -> None:
        try:
            self.store.events.append(type, payload or {}, media=media)
        except Exception:
            log.exception("could not log %s event", type)

    def log_play(self, label: str, path: Path, output: Output) -> None:
        self.log_event(EventType.PLAY, {
            "what": label, "path": str(path), "output": output.value,
            "experience": self.current.name if self.current else None,
        })

    def log_error(self, kind: str, **info: Any) -> None:
        self.log_event(EventType.ERROR, {"kind": kind, **{k: _jsonable(v) for k, v in info.items()}})

    def enqueue_upload(self, rel: str, path: Path) -> None:
        ctype = {".wav": "audio/wav", ".opus": "audio/opus", ".ogg": "audio/ogg"}.get(
            Path(path).suffix.lower(), "application/octet-stream")
        try:
            self.store.outbox.enqueue(rel, path=path, content_type=ctype)
        except Exception:
            log.exception("could not queue %s for upload", rel)

    def on_recording(self, active: bool) -> None:
        self._apply_leds()

    def pick_praise(self) -> str | None:
        names = [n for n in self.settings.praise_phrases if self.phrase_path(n) is not None]
        return self.rng.choice(names) if names else None

    def pick_clip(self) -> Path | None:
        try:
            clips = list(self.soundboard.clips())
        except Exception:
            log.exception("soundboard listing failed")
            return None
        if not clips:
            return None
        avoid_n = min(self.settings.recent_clips, len(clips) - 1)
        recent = list(self._recent_clips)[-avoid_n:] if avoid_n > 0 else []
        candidates = [c for c in clips if c not in recent] or clips
        clip = self.rng.choice(candidates)
        self._recent_clips.append(clip)
        return clip

    # ------------------------------------------------------------ presses ---
    def _on_press(self, ev: Press) -> None:
        btn = ev.button
        role = self.roles.get(btn)
        if role is None:
            log.warning("press on unmapped button %s", btn)
            return
        cur = self.current
        if self._is_quiet():
            self._quiet_press(btn, role, cur)
            return
        if cur is not None and cur.button == btn:
            if role is Role.PRINT:
                self._log_press(btn, role, "ignored")  # SPEC 2.1: does nothing
                return
            self._log_press(btn, role, "skip")
            if cur.on_same_press is not None:
                cur.on_same_press(self, cur)
            self._click()
            self._drive()
            return
        self._log_press(btn, role, "switch" if cur is not None else "start")
        if cur is not None:
            self._cancel(cur, "switch")
        self._click()
        try:
            exp = builders.BUILDERS[role](self, btn)
        except Exception:
            log.exception("could not build %s experience", role.value)
            self.log_error("build_failed", role=role.value)
            exp = None
        if exp is not None:
            self._begin(exp)
        else:
            self._refresh_leds()

    def _quiet_press(self, btn: Btn, role: Role, cur: Experience | None) -> None:
        now = self.clock()
        sleepy = now - self._last_sleepy >= self.settings.sleepy_interval_s
        self.log_event(EventType.QUIET_PRESS, {"button": btn.value, "role": role.value, "sleepy": sleepy})
        if cur is not None and cur.recording:
            self._cancel(cur, "quiet")  # a grandma reply is saved, not lost
        if not sleepy:
            return
        self._last_sleepy = now
        if self.current is not None:
            self._cancel(self.current, "quiet")
        self._begin(builders.build_sleepy())

    def _on_chord(self, ev: ParentChord) -> None:
        log.info("parent chord")
        self.log_event("parent_chord", {})
        if self.current is not None:
            self._cancel(self.current, "parent_chord")
        try:
            self.audio_out.stop_all()
        except Exception:
            log.exception("stop_all failed")
        self._refresh_leds()
        if self.on_parent_chord is not None:
            try:
                self.on_parent_chord(ev)
            except Exception:
                log.exception("on_parent_chord callback failed")

    def _log_press(self, btn: Btn, role: Role, action: str) -> None:
        self.log_event(EventType.PRESS, {"button": btn.value, "role": role.value, "action": action})

    def _click(self) -> None:
        self._click_retry_until = None
        if self._play_click():
            self._click_retry_until = self.clock() + self.settings.click_retry_s

    def _play_click(self) -> bool:
        """Play the click; True if it was suppressed (speaker muted)."""
        click = self.settings.click
        if click is None:
            return False
        try:
            if Path(click).is_file():
                pb = self.audio_out.play(Path(click), Output.SPEAKER)
                return bool(getattr(pb, "suppressed", False))
        except Exception:
            log.exception("click failed")
        return False

    def _retry_click(self) -> None:
        """Backstop: a click lost to the recording mute (a recorder that did
        not stop within record_join_s) is played once the mute is gone."""
        until = self._click_retry_until
        if until is None:
            return
        if self.clock() > until or (self.current is not None and self.current.recording):
            self._click_retry_until = None  # too late, or muted on purpose
            return
        if not self._play_click():
            self._click_retry_until = None

    # -------------------------------------------------------- experiences ---
    def _begin(self, exp: Experience) -> None:
        log.info("start %s", exp.name)
        self.current = exp
        self._refresh_leds()
        self._drive()

    def _finish(self, exp: Experience) -> None:
        exp.finished = exp.completed = True
        if self.current is exp:
            self.current = None
        log.info("finished %s", exp.name)
        if exp.on_complete is not None:
            try:
                exp.on_complete(self)
            except Exception:
                log.exception("completing %s failed", exp.name)
                self.log_error("complete_failed", experience=exp.name)
        self._refresh_leds()

    def _cancel(self, exp: Experience, reason: str) -> None:
        log.info("cancel %s (%s)", exp.name, reason)
        if self.current is exp:
            self.current = None
        exp.finished = True
        try:
            if exp.step is not None:
                exp.step.cancel(self)
        except Exception:
            log.exception("cancelling %s failed", exp.name)
        if exp.on_abandon is not None:
            try:
                exp.on_abandon(self)
            except Exception:
                log.exception("abandon hook of %s failed", exp.name)
        self._refresh_leds()

    def _abort(self, exp: Experience) -> None:
        self.log_error("experience_failed", experience=exp.name)
        self._cancel(exp, "error")

    def _drive(self) -> None:
        exp = self.current
        guard = 0
        try:
            while exp is not None and self.current is exp:
                guard += 1
                if guard > 500:  # a runaway sequence of instant steps
                    raise RuntimeError(f"{exp.name}: too many steps in one tick")
                step = exp.step
                if step is None:
                    step = exp.next()
                    if step is None:
                        self._finish(exp)
                        return
                    log.debug("%s: %r", exp.name, step)
                    step.start(self)
                    if step.recording:
                        self._apply_leds()
                if not step.poll(self):
                    return
                if exp.step is step:
                    exp.step = None
                if step.recording:
                    self._apply_leds()
        except Exception:
            log.exception("experience %s failed", exp.name if exp else None)
            if exp is not None and not exp.finished:
                self._abort(exp)

    # -------------------------------------------------- periodic / state ---
    def _is_quiet(self) -> bool:
        try:
            return bool(self.store.is_quiet())
        except Exception:
            log.exception("quiet-hours check failed")
            return False

    def _check_volume(self) -> None:
        try:
            v = float(self.store.settings.volume)
        except Exception:
            log.exception("could not read volume")
            return
        if v != self._volume:
            try:
                self.audio_out.set_volume(v)
                self._volume = v
                log.info("volume set to %.2f", v)
            except Exception:
                log.exception("set_volume failed")

    def _periodic(self, force: bool = False) -> None:
        now = self.clock()
        if force or now >= self._next_state_poll:
            self._next_state_poll = now + self.settings.state_poll_s
            self._check_volume()
            day = self.store.box_day()
            quiet = self._is_quiet()
            if day != self._day or quiet != self._quiet:
                if self._day is not None and day != self._day:
                    log.info("box day rolled over to %s", day)
                self._day, self._quiet = day, quiet
                force = True
        if force or now >= self._next_led_refresh:
            self._refresh_leds()

    def _refresh_leds(self) -> None:
        """Recompute the resting state of every button (SPEC 4), then overlay
        the running experience."""
        self._next_led_refresh = self.clock() + self.settings.led_refresh_s
        try:
            quiet = self._is_quiet()
            base: dict[Btn, LedState] = {}
            for btn, role in self.roles.items():
                if quiet:
                    base[btn] = LedState.OFF
                else:
                    base[btn] = LedState.BRIGHT if self._has_new(role) else LedState.DIM
            self._base = base
        except Exception:
            log.exception("LED state computation failed")
        self._apply_leds()

    def _has_new(self, role: Role) -> bool:
        store = self.store
        if role is Role.PRINT:
            printed = store.daily.printed_items()
            return any(i not in printed for i in builders.printable_items(self))
        if role is Role.GRANDMA:
            return store.recordings.has_unheard(Slot.GRANDMA)
        if role is Role.STORY:
            return store.content.current_for_today(Kind.STORY) is not None and not store.daily.story_heard
        return False  # soundboard: always just available

    def _apply_leds(self) -> None:
        cur = self.current
        for btn in self.roles:
            state = self._base.get(btn, LedState.OFF)
            if cur is not None and cur.button == btn and not cur.finished:
                state = LedState.PULSE_FAST if cur.recording else LedState.PULSE_SLOW
            try:
                self.leds.set(btn, state)
            except Exception:
                log.exception("LED set failed for %s", btn)


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return repr(v)
