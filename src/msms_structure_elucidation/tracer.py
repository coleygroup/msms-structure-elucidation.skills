"""
Reasoning trace writer — appends one JSON line per event to trace.jsonl.

Usage:
    tracer = Tracer(results_dir / "trace.jsonl")
    tracer.event("thinking", {"text": "..."})
    tracer.event("tool_call", {"name": "simulate_spectrum_iceberg", "input": {...}})
    tracer.event("tool_result", {"name": "simulate_spectrum_iceberg", "output": {...}})
    tracer.event("final_answer", {"text": "..."})
    tracer.close()
"""

import json
from datetime import datetime, timezone
from pathlib import Path


class Tracer:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", buffering=1)

    def event(self, kind: str, data: dict) -> None:
        """Append one event line to trace.jsonl."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **data,
        }
        self._f.write(json.dumps(record) + "\n")

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
