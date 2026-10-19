from __future__ import annotations

import json
import logging
import sys
import time


class DiagnosticFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": time.time(), "level": record.levelname, "logger": record.name}
        for field in ("operation", "run_id", "backend", "error_type", "code"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        payload["event"] = record.msg if isinstance(record.msg, str) else "diagnostic"
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DiagnosticFormatter())
    logger = logging.getLogger("argus")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
