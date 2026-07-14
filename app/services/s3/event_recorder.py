"""EventRecorder — append-only JSONL capture of every normalized event and
engine decision, one file per session day.  Feed the files back through
ReplayEngine to re-run the pipeline deterministically."""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class EventRecorder:
    def __init__(self, directory: str, enabled: bool = True) -> None:
        self._enabled = enabled
        self._dir = Path(directory)
        self._fh = None
        if enabled:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                path = self._dir / f"s3_{datetime.now():%Y%m%d}.jsonl"
                self._fh = path.open("a", encoding="utf-8", buffering=1)
                logger.info("[S3] recording events → %s", path)
            except OSError as exc:
                logger.error("[S3] recorder disabled: %s", exc)
                self._enabled = False

    def record(self, kind: str, payload: Any) -> None:
        if not self._enabled or self._fh is None:
            return
        try:
            if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
                body = dataclasses.asdict(payload)
            elif isinstance(payload, dict):
                body = payload
            else:
                body = {"repr": repr(payload)}
            self._fh.write(json.dumps(
                {"t": time.time(), "kind": kind, "data": body},
                default=str, separators=(",", ":"),
            ) + "\n")
        except (TypeError, ValueError, OSError) as exc:
            logger.debug("[S3] record failed (%s): %s", kind, exc)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
