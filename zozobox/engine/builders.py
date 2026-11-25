"""Experience builders, one per button role (SPEC 2.1 - 2.4).

Each builder reads the store and returns an `Experience` (or None when there
is nothing to do). The engine starts it and drives its steps.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..audio import Output
from ..config import Btn, Role
from ..printer import render_coloring_page, render_letter_card, render_mood_card, render_weather_card
from ..store import Kind, PRINT_ROTATION, Slot
from ..store.events import EventType
from .experience import Experience
from .sources import RECOGNIZED_MOODS, MoodResult
from .steps import CallStep, ClassifyStep, PauseStep, PlayStep, PrintStep, RecordStep, Step

if TYPE_CHECKING:  # pragma: no cover
    from ..store import ContentItem
    from .engine import Engine

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers ---


def media_file(eng: "Engine", item: "ContentItem | None", key: str) -> Path | None:
    """Absolute path of item.media[key] if it exists on disk."""
    if item is None or key not in item.media:
        return None
    try:
        p = eng.store.media_path(item.media[key])
    except Exception:
        log.warning("bad media path %r in %s", item.media.get(key), item.id)
        return None
    return p if p.is_file() else None


def new_media_rel(eng: "Engine", prefix: str, suffix: str = ".wav") -> str:
    local = eng.store.settings.local(eng.store.now())
    stamp = local.strftime("%H%M%S") + f"{local.microsecond // 1000:03d}"
    return f"{prefix}/{local:%Y/%m/%d}/{stamp}-{uuid.uuid4().hex[:8]}{suffix}"


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _open_image(path: Path | None):
    if path is None:
        return None
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.load()
            return im.copy()
    except Exception:
        log.exception("could not open image %s", path)
        return None


def printable_items(eng: "Engine") -> list[str]:
    """Rotation items that could print today (content is there; mood always)."""
    out = []
    for item in PRINT_ROTATION:
        if item == "mood":
            out.append(item)
            continue
        try:
            if _content_for(eng, item) is not None:
                out.append(item)
        except Exception:
            log.exception("content lookup failed for %s", item)
    return out


def _content_for(eng: "Engine", item: str) -> "ContentItem | None":
    kind = {"letter": Kind.LETTER, "weather": Kind.WEATHER, "coloring": Kind.COLORING}[item]
    c = eng.store.content.current_for_today(kind)
    if c is None:
        return None
    if item == "coloring" and media_file(eng, c, "art") is None:
        return None
    if item == "letter":
        letter = str(c.meta.get("letter") or "").strip()
        if len(letter) != 1 or not letter.isalpha():
            return None
    return c


def all_printed(eng: "Engine") -> bool:
    printed = eng.store.daily.printed_items()
    return all(i in printed for i in printable_items(eng))


# ------------------------------------------------------------------ print ---


def build_print(eng: "Engine", btn: Btn) -> Experience:
    store = eng.store
    item = store.daily.print_item
    day = store.box_day()
    # A job for this item still printing from an abandoned run counts as printed.
    already = store.daily.printed_today(item) or eng.print_in_flight(item)
    will_print = not already
    content = None
    steps: list[Step]
    if item == "mood":
        steps = mood_steps(eng, will_print)
    else:
        content = _content_for(eng, item)
        if content is None:
            log.info("print: no %s content today", item)
            steps = [PlayStep(phrase="print_not_ready")]
            will_print = False
        else:
            store.content.mark_served(content.id)
            steps = {"letter": letter_steps, "weather": weather_steps,
                     "coloring": coloring_steps}[item](eng, content, will_print)
    if already and (item == "mood" or content is not None):
        # Audio-only replay of an item already printed today.
        steps.append(CallStep(_maybe_all_printed, name="all_printed?"))

    def complete(eng: "Engine") -> None:
        skipped = exp.data.get("print_skipped")
        if skipped in ("quiet", "day_rolled_over"):
            # The print was refused, not done: treat the run like an abandoned
            # one (same item next time, content not consumed).
            log.info("print: %s not printed (%s); rotation not advanced", item, skipped)
            return
        if content is not None:
            eng.store.content.mark_completed(content.id)
        if eng.store.box_day() == day:
            nxt = eng.store.daily.advance_rotation()
            log.info("print: %s done, next is %s", item, nxt)
        else:
            log.info("print: %s finished after day rollover; rotation not advanced", item)

    exp = Experience(f"print:{item}", steps, button=btn, role=Role.PRINT, on_complete=complete)
    exp.data.update(item=item, will_print=will_print, day=day)
    return exp


def _maybe_all_printed(eng: "Engine", exp: Experience) -> None:
    if all_printed(eng):
        exp.insert_next([PlayStep(phrase="all_printed_today")])


def _print_tail(item: str, render: Callable[[], Any], *, content_id: str | None = None,
                extra: dict | None = None) -> list[Step]:
    return [
        PlayStep(phrase="here_is_your_card"),
        PrintStep(item, render, content_id=content_id, extra=extra),
    ]


def letter_steps(eng: "Engine", c: "ContentItem", will_print: bool) -> list[Step]:
    letter = str(c.meta.get("letter")).strip()
    words = [str(w) for w in (c.meta.get("words") or []) if str(w).strip()]
    steps: list[Step] = [PlayStep(media_file(eng, c, "intro"), label=f"letter:{letter}:intro")]
    for i, w in enumerate(words):
        steps.append(PlayStep(media_file(eng, c, f"word_{i}"), label=f"letter:{letter}:word:{w}"))
        steps.append(PauseStep(eng.settings.repeat_pause_s))
    praise = eng.pick_praise()
    if praise:
        steps.append(PlayStep(phrase=praise))
    if will_print:
        drawing_path = media_file(eng, c, "drawing")

        def render():
            return render_letter_card(letter, words, _open_image(drawing_path))

        steps += _print_tail("letter", render, content_id=c.id, extra={"letter": letter})
    return steps


def weather_steps(eng: "Engine", c: "ContentItem", will_print: bool) -> list[Step]:
    m = c.meta
    steps: list[Step] = [PlayStep(media_file(eng, c, "audio"), label="weather:summary")]
    if will_print:
        def render():
            return render_weather_card(
                str(m.get("summary") or ""), str(m.get("icon") or "cloudy"),
                _num(m.get("hi_c")), _num(m.get("lo_c")),
            )

        steps += _print_tail("weather", render, content_id=c.id)
    return steps


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def coloring_steps(eng: "Engine", c: "ContentItem", will_print: bool) -> list[Step]:
    steps: list[Step] = [PlayStep(media_file(eng, c, "intro"), label="coloring:intro")]
    if will_print:
        art_path = media_file(eng, c, "art")
        title = c.meta.get("title")

        def render():
            art = _open_image(art_path)
            if art is None:
                raise RuntimeError(f"coloring art unreadable: {art_path}")
            return render_coloring_page(art, title=title if isinstance(title, str) else None)

        steps += _print_tail("coloring", render, content_id=c.id)
    return steps


# ------------------------------------------------------------------- mood ---


class MoodFlow:
    """ask -> record -> classify -> (ask again once) -> say + print."""

    def __init__(self, eng: "Engine", will_print: bool):
        self.will_print = will_print
        self.attempt = 0

    def record_steps(self, eng: "Engine") -> list[Step]:
        self.attempt += 1
        rel = new_media_rel(eng, "mood")
        return [RecordStep(eng.store.media_path(rel), eng.settings.mood_record_s,
                           self.on_recorded, name=f"mood-record-{self.attempt}")]

    def on_recorded(self, eng: "Engine", step: RecordStep, rec, error) -> None:
        exp = step.exp
        if error is not None or rec is None:
            log.warning("mood recording failed: %r", error)
            eng.log_error("record_failed", what="mood", error=repr(error))
            self.conclude(eng, exp, None)
            return
        rel = eng.store.rel_media(rec.path) if Path(rec.path).exists() else None
        if rel is not None:
            eng.enqueue_upload(rel, rec.path)
        ts = step.started_at
        if rec.silent:
            eng.store.events.record_mood(
                "silent", transcript=None, source="box", audio=rel, ts=ts,
                extra={"attempt": self.attempt, "duration_s": rec.duration_s},
            )
            self.conclude(eng, exp, None)
            return
        attempt = self.attempt
        exp.insert_next([ClassifyStep(
            rec.path, lambda e, s, r: self.on_classified(e, s, r, rel, ts),
            timeout_s=eng.settings.classify_timeout_s,
            on_abandon=lambda e, s: self.log_pending(e, rel, ts, attempt, reason="abandoned"),
        )])

    @staticmethod
    def log_pending(eng: "Engine", rel, ts, attempt: int, *, transcript: str | None = None,
                    reason: str | None = None) -> None:
        """M7 recognizes it later and logs the mood with this timestamp."""
        payload = {"attempt": attempt, "audio": rel, "transcript": transcript}
        if reason is not None:
            payload["reason"] = reason
        eng.store.events.append(EventType.MOOD_PENDING, payload, media=rel, ts=ts)

    def on_classified(self, eng: "Engine", step: ClassifyStep, res: MoodResult, rel, ts) -> None:
        exp = step.exp
        if res.offline:
            self.log_pending(eng, rel, ts, self.attempt, transcript=res.transcript or None)
            self.conclude(eng, exp, None)
            return
        label = res.label if res.label in RECOGNIZED_MOODS else "unknown"
        eng.store.events.record_mood(
            label, transcript=res.transcript or None, source="online", audio=rel, ts=ts,
            extra={"attempt": self.attempt},
        )
        if label != "unknown":
            self.conclude(eng, exp, label)
        elif self.attempt < 2:
            exp.insert_next([PlayStep(phrase="ask_mood_again"), *self.record_steps(eng)])
        else:
            self.conclude(eng, exp, None)

    def conclude(self, eng: "Engine", exp: Experience, label: str | None) -> None:
        steps: list[Step] = []
        if label is not None:
            steps.append(PlayStep(phrase=f"you_feel_{label}"))
        exp.data["mood"] = label
        if self.will_print:
            steps += _print_tail("mood", lambda: render_mood_card(label),
                                 extra={"mood": label or "all"})
        exp.insert_next(steps)


def mood_steps(eng: "Engine", will_print: bool) -> list[Step]:
    flow = MoodFlow(eng, will_print)
    return [PlayStep(phrase="ask_mood"), *flow.record_steps(eng)]


# ---------------------------------------------------------------- grandma ---


def build_grandma(eng: "Engine", btn: Btn) -> Experience:
    store = eng.store
    steps: list[Step] = []
    msg = store.recordings.live(Slot.GRANDMA)
    if msg is not None:
        try:
            path = store.media_path(msg.media)
        except Exception:
            path = None
        if path is not None and path.is_file():
            def heard(eng: "Engine", rec_id=msg.id) -> None:
                eng.store.recordings.mark_heard(rec_id)
                eng.store.daily.mark_grandma_heard()

            steps.append(PlayStep(path, label="grandma_message", on_played=heard, tag="message"))
        else:
            log.warning("grandma message %s has no audio file", msg.id)
    steps.append(PlayStep(phrase="say_hi_grandma", tag="say_hi"))
    steps.append(PlayStep(eng.settings.beep, output=Output.SPEAKER, label="beep", tag="beep"))
    cap = max(1.0, min(float(store.settings.rec_cap_s), 300.0))
    rel = new_media_rel(eng, "recordings/zozo")
    steps.append(RecordStep(store.media_path(rel), cap, _grandma_saved, save_on_cancel=True,
                            name="grandma-reply", tag="record"))
    return Experience("grandma", steps, button=btn, role=Role.GRANDMA,
                      on_same_press=_grandma_same_press)


def _grandma_same_press(eng: "Engine", exp: Experience) -> None:
    step = exp.step
    if isinstance(step, RecordStep):
        step.stop_and_wait(eng)  # saved; the experience ends on the next drive
        return
    if step is not None and step.tag in ("message", "say_hi"):
        exp.skip_to(eng, "beep")


def _grandma_saved(eng: "Engine", step: RecordStep, rec, error) -> None:
    store = eng.store
    if error is not None or rec is None:
        log.warning("grandma reply recording failed: %r", error)
        eng.log_error("record_failed", what="grandma", error=repr(error))
        _discard(step.path)
        return
    if rec.silent or rec.duration_s <= 0:
        log.info("grandma reply silent (%.1fs); discarded, previous message kept", rec.duration_s)
        _discard(rec.path)
        store.events.append(EventType.RECORDING, {
            "slot": Slot.ZOZO.value, "saved": False, "reason": "silent",
            "duration_s": rec.duration_s})
        return
    rel = store.rel_media(rec.path)
    r = store.recordings.put(Slot.ZOZO, rel, duration_s=rec.duration_s, created_at=step.started_at)
    eng.enqueue_upload(rel, rec.path)
    store.events.append(EventType.RECORDING, {
        "slot": Slot.ZOZO.value, "saved": True, "rec_id": r.id, "duration_s": rec.duration_s},
        media=rel)
    log.info("saved grandma reply %s (%.1fs)", r.id, rec.duration_s)


def _discard(path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------- soundboard ---


def build_soundboard(eng: "Engine", btn: Btn) -> Experience | None:
    step = _clip_step(eng)
    if step is None:
        log.warning("soundboard: no clips")
        return None
    return Experience("soundboard", [step], button=btn, role=Role.SOUNDBOARD,
                      on_same_press=_soundboard_next)


def _clip_step(eng: "Engine") -> PlayStep | None:
    clip = eng.pick_clip()
    if clip is None:
        return None
    return PlayStep(clip, label=f"clip:{clip.name}")


def _soundboard_next(eng: "Engine", exp: Experience) -> None:
    step = _clip_step(eng)
    exp.skip_current(eng)
    if step is not None:
        exp.insert_next([step])


# ------------------------------------------------------------------ story ---


def story_chunk_keys(item: "ContentItem") -> list[str]:
    keys = item.meta.get("chunks")
    if isinstance(keys, list) and keys:
        return [str(k) for k in keys if str(k) in item.media]
    chunked = sorted((k for k in item.media if k.startswith("chunk")), key=_natural_key)
    if chunked:
        return chunked
    return ["audio"] if "audio" in item.media else []


def build_story(eng: "Engine", btn: Btn) -> Experience:
    store = eng.store
    item = store.content.current_for_today(Kind.STORY)
    paths = [media_file(eng, item, k) for k in story_chunk_keys(item)] if item else []
    paths = [p for p in paths if p is not None]
    if item is None or not paths:
        if item is not None:
            log.warning("story %s has no playable chunks", item.id)
        return Experience("story:none", [PlayStep(phrase="no_story_yet")], button=btn,
                          role=Role.STORY)
    store.content.mark_served(item.id)
    steps = [PlayStep(p, label=f"story:{item.id[:8]}:{i}") for i, p in enumerate(paths)]

    def complete(eng: "Engine", item_id=item.id) -> None:
        if not any(s.audible for s in steps):
            # Every chunk failed (audio device gone): nothing was heard.
            log.warning("story %s: no chunk played; not marked heard", item_id)
            return
        eng.store.content.mark_completed(item_id)
        eng.store.daily.mark_story_heard()

    return Experience("story", steps, button=btn, role=Role.STORY, on_complete=complete,
                      on_same_press=lambda eng, exp: exp.skip_current(eng))


def build_sleepy() -> Experience:
    return Experience("sleepy", [PlayStep(phrase="sleepy")])


BUILDERS: dict[Role, Callable[["Engine", Btn], Experience | None]] = {
    Role.PRINT: build_print,
    Role.GRANDMA: build_grandma,
    Role.SOUNDBOARD: build_soundboard,
    Role.STORY: build_story,
}
