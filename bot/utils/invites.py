"""Invite-link usage limits: parsing, labels and button presets.

Telegram lets an invite link carry a ``member_limit`` (1..99999): once that
many people have joined, Telegram itself stops admitting anyone through it.
This module holds the small, pure helpers around that number so the command
handler (``/invite 1m x10 Promo``), the button flow and the service layer all
speak the same dialect.

Accepted limit spellings (case-insensitive, may appear anywhere in the free
text after the duration):

* ``x5`` / ``×5`` / ``*5``   – prefix form
* ``5x`` / ``5×``            – suffix form
* ``limit=5`` / ``limit:5`` / ``limit 5`` / ``uses=5`` / ``max=5``
* ``once`` / ``single``      – exactly one use

Everything that is *not* a limit token is kept, in order, as the label.
"""
from __future__ import annotations

import re

#: Telegram's ceiling for ``ChatInviteLink.member_limit``.
MAX_MEMBER_LIMIT = 99_999

#: ``(button label, member_limit)`` pairs for the *New link → usage limit*
#: step. ``None`` means unlimited and is the first (full-width) button.
LIMIT_PRESETS: tuple[tuple[str, int | None], ...] = (
    ("♾ Unlimited", None),
    ("1 use", 1),
    ("5 uses", 5),
    ("10 uses", 10),
    ("25 uses", 25),
    ("50 uses", 50),
    ("100 uses", 100),
)

_ONCE_WORDS = frozenset({"once", "single", "single-use", "singleuse", "one-time", "onetime"})
_PREFIX_RE = re.compile(r"^[x×*](\d{1,6})$", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"^(\d{1,6})[x×]$", re.IGNORECASE)
_KEYED_RE = re.compile(r"^(?:limit|uses|max|cap)\s*[=:]\s*(\d{1,6})$", re.IGNORECASE)
_KEY_WORDS = frozenset({"limit", "uses", "max", "cap"})


class LimitError(ValueError):
    """Raised when a usage-limit token is recognised but its value is invalid."""


def _check(value: int) -> int:
    if not 1 <= value <= MAX_MEMBER_LIMIT:
        raise LimitError(f"Usage limit must be between 1 and {MAX_MEMBER_LIMIT}.")
    return value


def parse_limit_token(token: str) -> int | None:
    """Return the limit encoded by a single ``token`` or ``None`` if it is not one.

    >>> parse_limit_token("x5"), parse_limit_token("5x"), parse_limit_token("once")
    (5, 5, 1)
    >>> parse_limit_token("Gold") is None
    True
    """
    tok = token.strip().lower()
    if not tok:
        return None
    if tok in _ONCE_WORDS:
        return 1
    for rx in (_PREFIX_RE, _SUFFIX_RE, _KEYED_RE):
        m = rx.match(tok)
        if m:
            return _check(int(m.group(1)))
    return None


def split_limit(text: str | None) -> tuple[str | None, int | None]:
    """Split free text into ``(label, member_limit)``.

    The label keeps every word that is not a limit token, so
    ``"x10 Launch promo"`` → ``("Launch promo", 10)`` and
    ``"Gold plan"`` → ``("Gold plan", None)``. The two-word form
    ``limit 5`` is recognised too. When several limit tokens are present the
    last one wins. Raises :class:`LimitError` for out-of-range numbers.
    """
    if not text or not text.strip():
        return None, None
    words = text.split()
    label: list[str] = []
    limit: int | None = None
    i = 0
    while i < len(words):
        word = words[i]
        low = word.lower()
        # ``limit 5`` / ``uses: 5`` spelled as two tokens
        if low.rstrip("=:") in _KEY_WORDS and i + 1 < len(words) and words[i + 1].isdigit():
            limit = _check(int(words[i + 1]))
            i += 2
            continue
        found = parse_limit_token(word)
        if found is not None:
            limit = found
        else:
            label.append(word)
        i += 1
    name = " ".join(label).strip() or None
    return name, limit


def limit_label(member_limit: int | None) -> str:
    """Human label: ``unlimited``, ``1 use``, ``50 uses``."""
    if member_limit is None or int(member_limit) <= 0:
        return "unlimited"
    n = int(member_limit)
    return "1 use" if n == 1 else f"{n} uses"


def limit_arg(member_limit: int | None) -> str:
    """Command-style token for a limit: ``""``, ``once`` or ``x50``.

    Handy when echoing back an equivalent ``/invite`` command.
    """
    if member_limit is None or int(member_limit) <= 0:
        return ""
    return "once" if int(member_limit) == 1 else f"x{int(member_limit)}"
