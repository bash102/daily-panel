"""An experience: one button's sequence of steps, driven by the engine."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Iterable

from ..config import Btn, Role
from .steps import Step

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

log = logging.getLogger(__name__)


class Experience:
    """Steps run in order. Steps can insert follow-ups after themselves.

    - on_complete(eng): every step finished (not run when abandoned).
    - on_same_press(eng, exp): the same button was pressed again (skip rules).
    - on_abandon(eng): switched away / cancelled before the end.
    """

    def __init__(
        self,
        name: str,
        steps: Iterable[Step],
        *,
        button: Btn | None = None,
        role: Role | None = None,
        on_complete: Callable[["Engine"], None] | None = None,
        on_same_press: Callable[["Engine", "Experience"], None] | None = None,
        on_abandon: Callable[["Engine"], None] | None = None,
    ):
        self.name = name
        self.button = button
        self.role = role
        self.steps: list[Step] = []
        self.index = -1
        self.step: Step | None = None  # the running step, None between steps
        self.on_complete = on_complete
        self.on_same_press = on_same_press
        self.on_abandon = on_abandon
        self.finished = False
        self.completed = False
        self.data: dict = {}  # scratch space for builders
        self.add(steps)

    def _own(self, steps: Iterable[Step]) -> list[Step]:
        out = list(steps)
        for s in out:
            s.exp = self
        return out

    def add(self, steps: Iterable[Step]) -> None:
        self.steps.extend(self._own(steps))

    def insert_next(self, steps: Iterable[Step]) -> None:
        """Insert steps right after the current one."""
        at = self.index + 1
        self.steps[at:at] = self._own(steps)

    def next(self) -> Step | None:
        self.index += 1
        self.step = self.steps[self.index] if self.index < len(self.steps) else None
        return self.step

    @property
    def recording(self) -> bool:
        return self.step is not None and self.step.recording

    def skip_current(self, eng: "Engine") -> None:
        """Abandon the running step and move on to the next one."""
        if self.step is not None:
            self.step.cancel(eng)
            self.step = None

    def find_tag(self, tag: str) -> int | None:
        for i in range(max(self.index, 0), len(self.steps)):
            if self.steps[i].tag == tag:
                return i
        return None

    def skip_to(self, eng: "Engine", tag: str) -> bool:
        """Jump forward to the step tagged `tag` (cancelling the current one).
        False if it is not ahead of us."""
        i = self.find_tag(tag)
        if i is None or i <= self.index:
            return False
        self.skip_current(eng)
        self.index = i - 1
        return True

    def __repr__(self) -> str:
        return f"<Experience {self.name} step {self.index}/{len(self.steps)}>"
