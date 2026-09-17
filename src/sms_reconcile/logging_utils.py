"""Logging helpers.

Two policies are enforced here:

* The API token must never reach a log record. ``install_redaction`` wraps the
  global LogRecord factory so any record whose message or string arguments
  contain the token is rewritten before any handler (including pytest's
  ``caplog``) sees it.
* Project *names* are treated as sensitive-ish and only appear in log lines
  when ``--verbose-names`` is given. Callers use :func:`describe_project` to
  build the identifier fragment for a log line.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

REDACTED = "***REDACTED***"

_installed_factory = None
_secrets: set[str] = set()


def redact(text: str) -> str:
    """Scrub every registered secret from ``text``."""
    for secret in _secrets:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    return text


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    # Exceptions and other objects are formatted lazily; scrub their text now.
    rendered = str(value)
    scrubbed = redact(rendered)
    return scrubbed if scrubbed != rendered else value


def install_redaction(secret: Optional[str]) -> None:
    """Register ``secret`` so it is scrubbed from every LogRecord created from now on."""
    global _installed_factory
    if secret:
        _secrets.add(secret)
    if _installed_factory is not None:
        return
    base_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base_factory(*args, **kwargs)
        record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact(v) for k, v in record.args.items()}
        return record

    logging.setLogRecordFactory(factory)
    _installed_factory = factory


def configure_logging(level: int) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(level)
    # httpx logs every request URL at INFO. URLs contain encoded project names,
    # so keep the HTTP libraries quiet unless the user is debugging.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


class NamePolicy:
    """Decides whether project names may appear in log lines."""

    def __init__(self, verbose_names: bool = False) -> None:
        self.verbose_names = verbose_names

    def describe(self, project_id: Optional[int], name: Optional[str]) -> str:
        if self.verbose_names and name:
            return f"project id={project_id} name={name!r}"
        return f"project id={project_id}"
