"""Crash-safe annotation store.

Durable state = append-only JSONL journal (one record per PUT/DELETE).
``annotations.predictions.json`` is a consolidated snapshot in the main
project's prediction-file shape (``{item_id: [x1, y1, x2, y2]}``, normalized
0-1 XYXY, annotated items only) and is rewritten atomically on every change.
Recovery = replay the journal; a torn trailing line from a crash is ignored.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

JOURNAL_NAME = "annotations.jsonl"
SNAPSHOT_NAME = "annotations.predictions.json"
_META_FIELDS = ("annotator", "ts")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class AnnotationStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.journal_path = self.data_dir / JOURNAL_NAME
        self.snapshot_path = self.data_dir / SNAPSHOT_NAME
        self._lock = threading.Lock()
        self._state: dict[str, list[float]] = {}
        self._meta: dict[str, dict] = {}
        self._replay()

    def _replay(self) -> None:
        if not self.journal_path.is_file():
            return
        with self.journal_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn tail from a crash; journal remains truth
                item_id = record.get("id")
                if not isinstance(item_id, str) or not item_id:
                    continue
                bbox = record.get("bbox")
                if bbox is None:
                    self._state.pop(item_id, None)
                    self._meta.pop(item_id, None)
                elif isinstance(bbox, list) and len(bbox) == 4:
                    try:
                        self._state[item_id] = [float(v) for v in bbox]
                    except (TypeError, ValueError):
                        continue
                    self._meta[item_id] = {
                        field: record.get(field) for field in _META_FIELDS
                    }

    def get(self, item_id: str) -> list[float] | None:
        bbox = self._state.get(item_id)
        return list(bbox) if bbox is not None else None

    def meta(self, item_id: str) -> dict | None:
        entry = self._meta.get(item_id)
        return dict(entry) if entry is not None else None

    def all_boxes(self) -> dict[str, list[float]]:
        return {item_id: list(bbox) for item_id, bbox in self._state.items()}

    def annotated_count(self) -> int:
        return len(self._state)

    def set(self, item_id: str, bbox: list[float], annotator: str | None = None) -> list[float]:
        bbox = [float(v) for v in bbox]
        record = {"id": item_id, "bbox": bbox, "annotator": annotator, "ts": _now()}
        with self._lock:
            self._append(record)
            self._state[item_id] = bbox
            self._meta[item_id] = {"annotator": annotator, "ts": record["ts"]}
            self._write_snapshot()
        return bbox

    def delete(self, item_id: str, annotator: str | None = None) -> None:
        record = {"id": item_id, "bbox": None, "annotator": annotator, "ts": _now()}
        with self._lock:
            self._append(record)
            self._state.pop(item_id, None)
            self._meta.pop(item_id, None)
            self._write_snapshot()

    def _append(self, record: dict) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _write_snapshot(self) -> None:
        tmp = self.snapshot_path.with_name(SNAPSHOT_NAME + ".tmp")
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, self.snapshot_path)
