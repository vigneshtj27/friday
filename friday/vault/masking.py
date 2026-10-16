"""Redaction of secret values from any text that leaves the process.

Every value revealed from the vault (and, later, every OTP code) is registered
here. Logs, audit details, traces and anything shown to the model pass
through `Redactor.redact`.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any
from urllib.parse import quote, quote_plus

MIN_REDACTABLE = 3
# Identifiers are not sensitive: secret names (secret:mybank/alice/password) and profile ids
# (mybank.alice). Keep known ones readable even when a segment equals a secret value
# (an account label that is also the username). Unknown look-alikes are still redacted.
SECRET_REF = re.compile(r"(\[?secret:[a-z0-9][a-z0-9_./-]*[a-z0-9_-]\]?|\b[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_-]*\b)")


class Redactor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, str] = {}  # value -> name
        self._names: set[str] = set()  # known identifiers: secret names and profile ids
        self._pattern: re.Pattern[str] | None = None

    def add_names(self, *names: str) -> None:
        self._names.update(names)

    def register(self, name: str, value: str) -> bool:
        """Remember `value` so it is masked everywhere. Returns False if too short to mask safely."""
        if len(value) < MIN_REDACTABLE:
            return False
        with self._lock:
            for variant in {value, quote(value, safe=""), quote_plus(value)}:
                self._values[variant] = name
            self._rebuild()
        return True

    def forget(self, name: str) -> None:
        with self._lock:
            self._values = {v: n for v, n in self._values.items() if n != name}
            self._rebuild()

    def _rebuild(self) -> None:
        if not self._values:
            self._pattern = None
            return
        alts = sorted(self._values, key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(v) for v in alts))

    def redact(self, text: str) -> str:
        pattern = self._pattern
        if pattern is None or not text:
            return text
        values = self._values
        names = self._names | set(values.values())
        sub = lambda s: pattern.sub(lambda m: f"[secret:{values.get(m.group(0), '?')}]", s)  # noqa: E731
        parts = SECRET_REF.split(text)  # odd indexes are secret references
        for i, part in enumerate(parts):
            # Only references to known secret names are exempt; a page echoing "secret:<value>" is still redacted.
            if i % 2 == 0 or part.strip("[]").removeprefix("secret:") not in names:
                parts[i] = sub(part)
        return "".join(parts)

    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {self.redact(str(k)): self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple | set):
            return [self.redact_obj(v) for v in obj]
        return obj


REDACTOR = Redactor()


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor = REDACTOR):
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor.redact(record.getMessage())
        record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            # Pre-render the traceback through the redactor so the cached text is clean.
            record.exc_text = self.redactor.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging(level: int = logging.INFO, redactor: Redactor = REDACTOR) -> None:
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter(redactor))
