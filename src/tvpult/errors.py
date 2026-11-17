"""Errors that people read: a stable code with parameters, worded in both languages.

The words live in the dictionaries (`err.<code>` in locales/en.json and ru.json). An error's own text
(str(e), the logs) is English; the page and the command line show it in their language from the code.
The API answers {"error": "<English>", "code": "<code>", "params": {...}}.
"""

from __future__ import annotations

from typing import Any

from .i18n import strings, t


def _fill(text: str, params: dict[str, str]) -> str:
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):  # a placeholder without a value: show the template rather than fail
        return text


def known(code: str | None) -> bool:
    return bool(code) and f"err.{code}" in strings("en")


class TvpultError(RuntimeError):
    """An error with a code: TvpultError(code="offline", name="Living room").

    TvpultError("free text") still works — for messages that have no code (shown as they are).
    """

    def __init__(self, message: str | None = None, *, code: str | None = None, **params: Any):
        self.code = code
        self.params = {k: str(v) for k, v in params.items()}
        if message is None:
            message = _fill(strings("en")[f"err.{code}"], self.params) if known(code) else str(code)
        super().__init__(message)

    def localized(self) -> str:
        """The text in the current language (the command line's)."""
        return localized(self)

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": str(self)}
        if self.code:
            out["code"], out["params"] = self.code, self.params
        return out


def localized(e: BaseException) -> str:
    """Any exception as text for a person: by its code in the current language, else its own text."""
    code = getattr(e, "code", None)
    if known(code):
        return _fill(t(f"err.{code}"), getattr(e, "params", {}))
    return str(e)
