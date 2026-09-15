"""Text helpers: cleaning user input, comparing open answers and formatting values for people."""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Iterable, List, Optional

_INVISIBLE_RANGES = [(0x00, 0x08), (0x0B, 0x0C), (0x0E, 0x1F), (0x7F, 0x7F), (0x200B, 0x200F), (0x202A, 0x202E),
                     (0x2060, 0x2060), (0xFEFF, 0xFEFF)]   # control characters, zero-width and direction marks
_INVISIBLE_RE = re.compile("[" + "".join(f"{chr(low)}-{chr(high)}" for low, high in _INVISIBLE_RANGES) + "]")
_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)
_EMAIL_RE = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")
_MARKDOWN_RE = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~<$:])")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")

TYPO_SIMILARITY = 0.85   # open answers this similar to an accepted answer count as right


def clean(value: object, limit: int, multiline: bool = False) -> str:
    """Trim input: invisible and control characters, repeated spaces and anything past ``limit``."""
    if value is None:
        return ""
    text = _INVISIBLE_RE.sub("", str(value).replace("\r\n", "\n").replace("\xa0", " "))
    if multiline:
        lines = [" ".join(line.split()) for line in text.split("\n")]
        text = "\n".join(lines).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        text = " ".join(text.split())
    return text[:limit].strip()


def clean_lines(value: object, limit: int, max_lines: int) -> List[str]:
    """One entry per non-empty line, without duplicates (case-insensitive)."""
    seen, lines = set(), []
    for line in str(value or "").splitlines():
        text = clean(line, limit)
        if text and normalize(text) not in seen:
            seen.add(normalize(text))
            lines.append(text)
    return lines[:max_lines]


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(value: object) -> str:
    """Lowercase, accent-free, punctuation collapsed: 'São Paulo!' and 'sao paulo' compare equal."""
    text = strip_accents(str(value or "")).casefold()
    return " ".join(_NON_WORD_RE.sub(" ", text).split())


def matches(answer: str, accepted: Iterable[str], tolerant: bool) -> bool:
    """Whether an open answer matches one of the accepted answers.

    Comparison ignores case, accents and punctuation. With ``tolerant`` small typos are accepted too,
    except in answers with digits (1945 and 1946 are different answers).
    """
    given = normalize(answer)
    if not given:
        return False
    for option in accepted:
        expected = normalize(option)
        if not expected:
            continue
        if given == expected:
            return True
        if tolerant and not any(ch.isdigit() for ch in given + expected) and min(len(given), len(expected)) >= 4:
            if difflib.SequenceMatcher(None, given, expected).ratio() >= TYPO_SIMILARITY:
                return True
    return False


def normalize_email(value: object) -> str:
    return clean(value, 254).lower()


def valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


def email_domain(value: str) -> str:
    match = _EMAIL_RE.match(value)
    return match.group(1).lower() if match else ""


def clean_domains(value: object, max_domains: int = 20) -> List[str]:
    """Domains typed as 'example.com, @example.org' → ['example.com', 'example.org'] (invalid ones dropped)."""
    domains = []
    for part in re.split(r"[\s,;]+", str(value or "").lower()):
        part = part.strip().lstrip("@").strip(".")
        if part and _DOMAIN_RE.match(part) and part not in domains:
            domains.append(part)
    return domains[:max_domains]


def domain_allowed(email: str, domains: Iterable[str]) -> bool:
    domains = list(domains)
    if not domains:
        return True
    domain = email_domain(email)
    return any(domain == allowed or domain.endswith("." + allowed) for allowed in domains)


def escape_markdown(value: str) -> str:
    """Show text typed by people literally in ``st.markdown`` (no links, images or formatting)."""
    return _MARKDOWN_RE.sub(r"\\\1", value).replace("\n", "  \n")


def only_digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def format_code(code: str) -> str:
    return f"{code[:3]} {code[3:]}" if len(code) == 6 else code


def format_seconds(seconds: Optional[float]) -> str:
    """12.3 → '12.3 s', 83 → '1 min 23 s'."""
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes} min {rest:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def format_clock(seconds: float) -> str:
    """Remaining time as mm:ss (or h:mm:ss)."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def plural(count: int, singular: str, plural_form: Optional[str] = None) -> str:
    return f"{count:,} {singular if count == 1 else (plural_form or singular + 's')}"
