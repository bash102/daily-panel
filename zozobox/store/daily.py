"""Per-'box day' state (SPEC 2.1, 2.4, 4).

A box day is the local date in the configured timezone, rolling over at
day_start (default 06:00) instead of midnight. Each day gets its own row, so
state "resets" simply because a new day reads an empty row: nothing printed,
rotation back at Letter, heard flags cleared. Old rows are kept as history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

from .db import Database, StoreError
from .settings import PRINT_ROTATION


@dataclass(frozen=True)
class DaySnapshot:
    day: date
    printed: frozenset[str]
    rotation_index: int
    story_heard: bool
    grandma_heard: bool

    @property
    def print_item(self) -> str:
        return PRINT_ROTATION[self.rotation_index % len(PRINT_ROTATION)]


def _empty() -> dict[str, Any]:
    return {"printed": [], "rotation": 0, "story_heard": False, "grandma_heard": False}


class DailyState:
    def __init__(self, db: Database, box_day: Callable[[datetime | None], date]):
        self._db = db
        self._box_day = box_day

    @property
    def today(self) -> date:
        return self._box_day(None)

    def _load(self, day: date) -> dict[str, Any]:
        r = self._db.query_one("SELECT data FROM daily WHERE day=?", (day.isoformat(),))
        data = _empty()
        if r is not None:
            try:
                data.update(json.loads(r["data"]))
            except (ValueError, TypeError):
                pass
        return data

    def _modify(self, fn: Callable[[dict[str, Any]], Any], day: date | None = None) -> Any:
        day = day or self.today
        with self._db.tx() as c:
            data = self._load(day)
            result = fn(data)
            c.execute(
                "INSERT INTO daily(day, data) VALUES (?,?) "
                "ON CONFLICT(day) DO UPDATE SET data=excluded.data",
                (day.isoformat(), json.dumps(data)),
            )
        return result

    def snapshot(self, day: date | None = None) -> DaySnapshot:
        day = day or self.today
        d = self._load(day)
        return DaySnapshot(
            day=day,
            printed=frozenset(d["printed"]),
            rotation_index=int(d["rotation"]) % len(PRINT_ROTATION),
            story_heard=bool(d["story_heard"]),
            grandma_heard=bool(d["grandma_heard"]),
        )

    # -- prints ---------------------------------------------------------------
    @staticmethod
    def _check_item(item: str) -> str:
        if item not in PRINT_ROTATION:
            raise StoreError(f"unknown print item {item!r}")
        return item

    def printed_today(self, item: str) -> bool:
        return self._check_item(item) in self.snapshot().printed

    def printed_items(self) -> frozenset[str]:
        return self.snapshot().printed

    def mark_printed(self, item: str) -> None:
        """Daily-limited print happened (not dashboard reprints, SPEC 2.1)."""
        self._check_item(item)

        def fn(d):
            if item not in d["printed"]:
                d["printed"].append(item)

        self._modify(fn)

    @property
    def print_item(self) -> str:
        """The item the next Print press plays. Restarts at Letter each box day."""
        return self.snapshot().print_item

    @property
    def rotation_index(self) -> int:
        return self.snapshot().rotation_index

    def advance_rotation(self) -> str:
        """Move to the next item (call when an item finishes) and return it."""

        def fn(d):
            d["rotation"] = (int(d["rotation"]) + 1) % len(PRINT_ROTATION)
            return PRINT_ROTATION[d["rotation"]]

        return self._modify(fn)

    # -- heard flags ----------------------------------------------------------
    @property
    def story_heard(self) -> bool:
        return self.snapshot().story_heard

    def mark_story_heard(self) -> None:
        self._modify(lambda d: d.__setitem__("story_heard", True))

    @property
    def grandma_heard(self) -> bool:
        return self.snapshot().grandma_heard

    def mark_grandma_heard(self) -> None:
        self._modify(lambda d: d.__setitem__("grandma_heard", True))
