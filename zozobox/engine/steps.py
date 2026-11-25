"""Steps: the building blocks of an experience.

A step is started once, then polled from the engine's tick until it reports
done. `cancel()` abandons it (stopping its sound or asking its worker to stop).
Blocking work (recording, classifying, printing) runs in a helper thread
inside the step; results are delivered back on the engine thread by `poll()`.

Steps get the engine (`eng`) and their experience (`self.exp`, set when the
step is added) so callbacks can insert follow-up steps.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..audio import Output, Playback

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine
    from .experience import Experience

log = logging.getLogger(__name__)


class Step:
    name = "step"
    recording = False  # True while this step holds the mic (LED fast pulse)

    def __init__(self, *, tag: str | None = None):
        self.tag = tag
        self.exp: "Experience | None" = None

    def start(self, eng: "Engine") -> None:
        pass

    def poll(self, eng: "Engine") -> bool:
        """True once the step is finished."""
        return True

    def cancel(self, eng: "Engine") -> None:
        pass

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}{' #' + self.tag if self.tag else ''}>"


class CallStep(Step):
    """Runs a function on the engine thread (decisions, bookkeeping)."""

    def __init__(self, fn: Callable[["Engine", "Experience"], Any], *, name: str = "call",
                 tag: str | None = None):
        super().__init__(tag=tag)
        self.fn = fn
        self.name = name

    def start(self, eng):
        self.fn(eng, self.exp)


class PlayStep(Step):
    """Play one sound and wait for it. A missing sound is skipped with a log
    line. A playback that fails (device error) is logged and the sequence
    carries on. `on_played` runs only if the sound played to its end."""

    def __init__(self, path: Path | str | None = None, *, phrase: str | None = None,
                 output: Output = Output.BOTH, label: str | None = None,
                 on_played: Callable[["Engine"], None] | None = None, tag: str | None = None):
        super().__init__(tag=tag)
        self.path = Path(path) if path is not None else None
        self.phrase = phrase
        self.output = output
        self.label = label or (f"phrase:{phrase}" if phrase else (self.path.name if self.path else "sound"))
        self.name = self.label
        self.on_played = on_played
        self.pb: Playback | None = None
        self.skipped = False
        self._t0 = 0.0
        self._reported = False  # on_played/failure handled

    @property
    def audible(self) -> bool:
        """The sound actually came out (fully or until skipped): it started
        and did not fail and was not suppressed."""
        pb = self.pb
        return pb is not None and pb.done and not pb.failed and not pb.suppressed

    def start(self, eng):
        path = self.path
        if path is None and self.phrase is not None:
            path = eng.phrase_path(self.phrase)
        if path is None or not Path(path).is_file():
            log.info("skipping %s: no audio (%s)", self.label, path)
            self.skipped = True
            return
        self._t0 = eng.clock()
        self.pb = eng.audio_out.play(Path(path), self.output)
        eng.log_play(self.label, path, self.output)

    def poll(self, eng):
        if self.pb is None:
            return True
        if not self.pb.done:
            limit = eng.settings.max_play_s
            if limit and eng.clock() - self._t0 >= limit:
                log.warning("%s still playing after %.0fs; stopping it", self.label, limit)
                self.pb.stop()
                return True
            return False
        self._report(eng)
        return True

    def _report(self, eng) -> None:
        if self._reported or self.pb is None:
            return
        self._reported = True
        if self.pb.failed:
            log.warning("playback of %s failed", self.label)
            eng.log_error("play_failed", what=self.label)
        elif not self.pb.stopped and not self.pb.suppressed and self.on_played is not None:
            self.on_played(eng)

    def cancel(self, eng):
        if self.pb is None:
            return
        if self.pb.done and not self.pb.stopped:
            # It already played to its end but was not polled yet (a press
            # landed between two ticks): it was heard, so report it.
            self._report(eng)
            return
        self.pb.stop()


class PauseStep(Step):
    def __init__(self, seconds: float, *, tag: str | None = None):
        super().__init__(tag=tag)
        self.seconds = seconds
        self.name = f"pause {seconds:g}s"
        self._t0 = 0.0

    def start(self, eng):
        self._t0 = eng.clock()

    def poll(self, eng):
        return eng.clock() - self._t0 >= self.seconds


class WorkerStep(Step):
    """Runs `work(eng, stop)` in a helper thread; `finish(eng, result, error)`
    runs on the engine thread once it is done. If `timeout_s` (engine clock)
    passes first, `on_timeout(eng)` runs instead and the thread is abandoned
    (its result is dropped)."""

    timeout_s: float | None = None
    track = False  # Engine.close() waits for this thread (it writes to the store)

    def __init__(self, *, tag: str | None = None):
        super().__init__(tag=tag)
        self.stop_event = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._result: Any = None
        self._error: BaseException | None = None
        self._finished = False
        self._t0 = 0.0
        self._lock = threading.Lock()

    # subclass hooks
    def work(self, eng: "Engine", stop: threading.Event) -> Any:  # pragma: no cover
        raise NotImplementedError

    def finish(self, eng: "Engine", result: Any, error: BaseException | None) -> None:
        if error is not None:
            raise error

    def on_timeout(self, eng: "Engine") -> None:
        log.warning("%s timed out", self.name)

    # machinery
    def start(self, eng):
        self._t0 = eng.clock()
        self._thread = threading.Thread(target=self._run, args=(eng,), name=f"step-{self.name}",
                                        daemon=True)
        if self.track:
            eng.track_thread(self._thread)
        self._thread.start()

    def _run(self, eng):
        try:
            self._result = self.work(eng, self.stop_event)
        except BaseException as e:  # delivered to finish() on the engine thread
            self._error = e
        finally:
            self._done.set()

    @property
    def worker_done(self) -> bool:
        return self._done.is_set()

    def _deliver(self, eng) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self.finish(eng, self._result, self._error)

    def poll(self, eng):
        if self._thread is None:
            return True
        if self._done.is_set():
            self._deliver(eng)
            return True
        if self.timeout_s is not None and eng.clock() - self._t0 >= self.timeout_s:
            with self._lock:
                if self._finished:
                    return True
                self._finished = True
            self.stop_event.set()
            self.on_timeout(eng)
            return True
        return False

    def cancel(self, eng):
        self.stop_event.set()


class RecordStep(WorkerStep):
    """Record from the mic into `path` for up to `max_s`.

    on_result(eng, step, recording_or_None, error) runs once with the take.
    - `request_stop()` ends the take early; it is still delivered.
    - cancel(): the take is stopped and the recorder joined (up to
      eng.settings.record_join_s) before cancel returns, so the box speaker
      is unmuted again before the next sound (the click, the next
      experience) plays. With save_on_cancel the take is then delivered
      right away, so an interrupted grandma reply is saved before the next
      action starts; without it the take is discarded (file deleted).
    """

    recording = True

    def __init__(self, path: Path, max_s: float, on_result: Callable, *,
                 save_on_cancel: bool = False, name: str = "record", tag: str | None = None):
        super().__init__(tag=tag)
        self.path = Path(path)
        self.max_s = max_s
        self.on_result = on_result
        self.save_on_cancel = save_on_cancel
        self.name = name
        self.started_at = None  # store time when recording began
        self._cancelled = False

    def start(self, eng):
        self.started_at = eng.store.now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        eng.on_recording(True)
        super().start(eng)

    def work(self, eng, stop):
        rec = eng.audio_in.record(self.path, self.max_s, stop)
        if self._cancelled and not self.save_on_cancel:
            _unlink(self.path)
        return rec

    def poll(self, eng):
        # Enforce the cap on the engine clock too (a stuck recorder, fake time).
        if not self._done.is_set() and eng.clock() - self._t0 >= self.max_s:
            self.stop_event.set()
        done = super().poll(eng)
        if done:
            eng.on_recording(False)
        return done

    def request_stop(self) -> None:
        self.stop_event.set()

    def stop_and_wait(self, eng) -> bool:
        """Stop the take and wait for the recorder; deliver it. True if delivered."""
        self.stop_event.set()
        if self._done.wait(eng.settings.record_join_s):
            self._deliver(eng)
            eng.on_recording(False)
            return True
        log.warning("%s: recorder did not stop within %.1fs", self.name, eng.settings.record_join_s)
        return False

    def finish(self, eng, result, error):
        self.on_result(eng, self, result, error)

    def cancel(self, eng):
        self._cancelled = True
        self.stop_event.set()
        if self._thread is None:
            return
        # Always wait for the recorder: it holds the speaker mute until it
        # returns, and anything played before that loses the box speaker.
        stopped = self._done.wait(eng.settings.record_join_s)
        if not stopped:
            log.warning("%s: recorder did not stop within %.1fs", self.name, eng.settings.record_join_s)
        if self.save_on_cancel:
            if stopped:
                self._deliver(eng)
            else:
                # Deliver from the worker when it finally returns.
                t = threading.Thread(target=self._late_deliver, args=(eng,),
                                     name=f"late-{self.name}", daemon=True)
                eng.track_thread(t)
                t.start()
        elif stopped:
            _unlink(self.path)  # else work() deletes it when the recorder returns
        eng.on_recording(False)

    def _late_deliver(self, eng):
        if self._done.wait(60):
            try:
                self._deliver(eng)
            except Exception:
                log.exception("late delivery of %s failed", self.name)


class ClassifyStep(WorkerStep):
    """Classify a mood answer. Errors and timeouts count as offline."""

    name = "classify"

    def __init__(self, path: Path, on_result: Callable, *, timeout_s: float,
                 on_abandon: Callable[["Engine", "ClassifyStep"], None] | None = None,
                 tag: str | None = None):
        super().__init__(tag=tag)
        self.path = Path(path)
        self.on_result = on_result
        self.on_abandon = on_abandon
        self.timeout_s = timeout_s

    def work(self, eng, stop):
        return eng.mood_classifier.classify(self.path)

    def finish(self, eng, result, error):
        from .sources import CLASSIFIER_LABELS, MoodResult

        if error is not None or result is None:
            log.warning("mood classifier failed (%r); treating as offline", error)
            result = MoodResult("unknown", "", offline=True)
        elif getattr(result, "label", None) not in CLASSIFIER_LABELS:
            log.warning("mood classifier returned bad label %r; treating as unknown", result)
            result = MoodResult("unknown", getattr(result, "transcript", "") or "", bool(getattr(result, "offline", False)))
        self.on_result(eng, self, result)

    def on_timeout(self, eng):
        from .sources import MoodResult

        log.warning("mood classifier timed out; treating as offline")
        self.on_result(eng, self, MoodResult("unknown", "", offline=True))

    def cancel(self, eng):
        """Switched away while classifying: the answer is not waited for, but
        the take must not be lost, so `on_abandon` logs it for later (M7)."""
        self.stop_event.set()
        with self._lock:
            if self._finished:
                return
            self._finished = True
        if self.on_abandon is not None:
            self.on_abandon(eng, self)


class PrintStep(WorkerStep):
    """Render and print a card; on success mark it printed and archive it.

    The whole job (render, print, mark printed, record_print) runs in the
    worker, so once paper is moving a switch-away cannot lose the bookkeeping.
    A print failure is logged (`print_failed`) from the worker too, so it is
    recorded even when the experience was abandoned meanwhile.

    Not printed (reason in `skipped_reason` and `exp.data["print_skipped"]`):
    `quiet` (quiet hours), `day_rolled_over` (the sequence was built on an
    earlier box day), `already_printed`, `in_progress` (the same item's job
    from an abandoned run is still printing).

    While the job runs the item is "in flight" on the engine
    (`eng.print_in_flight`), so a new Print run never prints it twice; jobs
    are serialized on `eng.printer_lock`. After `eng.settings.print_timeout_s`
    the step gives up waiting (logged as `print_failed`, reason `timeout`);
    the worker may still finish and record the print.
    On failure or timeout the 'printer_sleepy' phrase is queued.
    """

    name = "print"
    track = True

    def __init__(self, item: str, render: Callable[[], Any], *, content_id: str | None = None,
                 extra: dict | None = None, tag: str | None = None):
        super().__init__(tag=tag)
        self.item = item
        self.render = render
        self.content_id = content_id
        self.extra = extra or {}
        self.printed = False
        self.skipped_reason: str | None = None

    def _skip_reason(self, eng) -> str | None:
        if eng.store.is_quiet():
            return "quiet"
        day = self.exp.data.get("day") if self.exp is not None else None
        if day is not None and eng.store.box_day() != day:
            return "day_rolled_over"
        if eng.store.daily.printed_today(self.item):
            return "already_printed"
        return None

    def start(self, eng):
        self.skipped_reason = self._skip_reason(eng)
        if self.skipped_reason is None and not eng.begin_print(self.item):
            self.skipped_reason = "in_progress"
        if self.skipped_reason:
            log.info("not printing %s: %s", self.item, self.skipped_reason)
            if self.exp is not None:
                self.exp.data["print_skipped"] = self.skipped_reason
            return
        self.timeout_s = eng.settings.print_timeout_s or None
        try:
            super().start(eng)
        except BaseException:
            eng.end_print(self.item)
            raise

    def work(self, eng, stop):
        try:
            with eng.printer_lock:
                if eng.store.daily.printed_today(self.item):
                    log.info("not printing %s: already printed", self.item)
                    return False
                img = self.render()
                eng.printer.print_image(img)
                eng.store.daily.mark_printed(self.item)
                self.printed = True
            try:
                eng.store.events.record_print(self.item, img, content_id=self.content_id, extra=self.extra)
            except Exception:
                log.exception("could not archive print of %s", self.item)
            log.info("printed %s", self.item)
            return True
        except BaseException as e:
            log.warning("print of %s failed: %r", self.item, e)
            eng.log_error("print_failed", item=self.item, error=repr(e))
            raise
        finally:
            eng.end_print(self.item)

    def finish(self, eng, result, error):
        if error is not None and self.exp is not None:
            self.exp.insert_next([PlayStep(phrase="printer_sleepy")])

    def on_timeout(self, eng):
        log.warning("print of %s still running after %.0fs; giving up waiting", self.item, self.timeout_s)
        eng.log_error("print_failed", item=self.item, error="timeout", reason="timeout")
        if self.exp is not None:
            self.exp.insert_next([PlayStep(phrase="printer_sleepy")])

    def cancel(self, eng):
        # Never interrupt a print in progress; the worker finishes the job.
        pass


def _unlink(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        log.warning("could not delete %s", path)
