"""Publication date normalization (ISO, RFC-2822, BR dates, PT-BR relative strings),
14-day recency window enforcement, and human-friendly Portuguese `published_ago` formatting.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

PT_MONTHS: dict[str, int] = {
    "jan": 1,
    "janeiro": 1,
    "fev": 2,
    "fevereiro": 2,
    "mar": 3,
    "marco": 3,
    "abr": 4,
    "abril": 4,
    "mai": 5,
    "maio": 5,
    "jun": 6,
    "junho": 6,
    "jul": 7,
    "julho": 7,
    "ago": 8,
    "agosto": 8,
    "set": 9,
    "setembro": 9,
    "out": 10,
    "outubro": 10,
    "nov": 11,
    "novembro": 11,
    "dez": 12,
    "dezembro": 12,
}


def _norm_ascii(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", (value or "").casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).strip()


def parse_published_at_utc(
    raw_val: Any,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Parses ISO-8601, RFC-2822/RSS, epoch, DD/MM/YYYY, and PT-BR/EN relative expressions into UTC."""
    if raw_val is None:
        return None

    ref_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if isinstance(raw_val, datetime):
        return raw_val.astimezone(timezone.utc) if raw_val.tzinfo else raw_val.replace(tzinfo=timezone.utc)

    if isinstance(raw_val, (int, float)):
        ts = float(raw_val)
        if ts > 1e12:
            ts /= 1000.0
        if 946684800 <= ts <= 4102444800:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        return None

    text = str(raw_val).strip()
    if not text:
        return None

    if re.fullmatch(r"\d{10,13}", text):
        ts = float(text)
        if len(text) >= 13:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    # 1. ISO-8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS...)
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        iso_cand = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso_cand)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                dt = datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                pass

    # 2. RFC-2822 / RSS pubDate
    if "," in text and re.search(r"\b20\d{2}\b", text):
        try:
            dt = parsedate_to_datetime(text)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    # 3. Brazilian numeric date DD/MM/YYYY or DD-MM-YYYY
    br_m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", text)
    if br_m:
        day, month, year = int(br_m.group(1)), int(br_m.group(2)), int(br_m.group(3))
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            pass

    norm = _norm_ascii(text)

    # 4. Brazilian written date (`18 de novembro de 2026` or `18 nov 2026`)
    written_m = re.search(
        r"\b(\d{1,2})\s+(?:de\s+)?([a-z]{3,9})\.?\s+(?:de\s+)?(20\d{2})\b",
        norm,
    )
    if written_m:
        day = int(written_m.group(1))
        month_num = PT_MONTHS.get(written_m.group(2))
        year = int(written_m.group(3))
        if month_num:
            try:
                return datetime(year, month_num, day, tzinfo=timezone.utc)
            except ValueError:
                pass

    # 5. Relative PT-BR / EN expressions (`hoje`, `ontem`, `há 3 horas`, `há 5 dias`, `2 days ago`)
    if re.search(r"\b(?:agora(?:\s+mesmo)?|recem[\s-]publicad[ao]|just\s+now)\b", norm):
        return ref_now
    if re.search(r"\b(?:hoje|today)\b", norm):
        return ref_now
    if re.search(r"\banteontem\b", norm):
        return ref_now - timedelta(days=2)
    if re.search(r"\b(?:ontem|yesterday)\b", norm):
        return ref_now - timedelta(days=1)

    if re.search(r"\b30\+\s*(?:dias|days)\b", norm):
        return ref_now - timedelta(days=35)

    rel_m = re.search(
        r"(?:ha\s+|cerca\s+de\s+|aprox\.?\s*|ultima\s+atualizacao\s*:\s*)?(\d+|um|uma)\s*\+?\s*"
        r"(minuto|minutos|min|hora|horas|h|dia|dias|d|semana|semanas|mes|meses|ano|anos|"
        r"minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)"
        r"(?:\s+atras|\s+ago|\b)",
        norm,
    )
    if rel_m:
        raw_qty = rel_m.group(1)
        qty = 1 if raw_qty in {"um", "uma"} else int(raw_qty)
        unit = rel_m.group(2)
        if unit in {"minuto", "minutos", "min", "minute", "minutes"}:
            return ref_now - timedelta(minutes=qty)
        if unit in {"hora", "horas", "h", "hour", "hours"}:
            return ref_now - timedelta(hours=qty)
        if unit in {"dia", "dias", "d", "day", "days"}:
            return ref_now - timedelta(days=qty)
        if unit in {"semana", "semanas", "week", "weeks"}:
            return ref_now - timedelta(weeks=qty)
        if unit in {"mes", "meses", "month", "months"}:
            return ref_now - timedelta(days=30 * qty)
        if unit in {"ano", "anos", "year", "years"}:
            return ref_now - timedelta(days=365 * qty)

    return None


def to_canonical_utc_iso(dt: datetime) -> str:
    """Formats a datetime as fixed-width UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SS+00:00`) for strict SQL sorting."""
    utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def normalize_published_iso(
    raw_val: Any,
    *,
    fallback_iso: str | None = None,
    now: datetime | None = None,
) -> str | None:
    parsed = parse_published_at_utc(raw_val, now=now)
    if parsed is not None:
        return to_canonical_utc_iso(parsed)
    if fallback_iso:
        fb_dt = parse_published_at_utc(fallback_iso, now=now)
        if fb_dt is not None:
            return to_canonical_utc_iso(fb_dt)
    return None


def is_published_within_days(
    raw_val: Any,
    *,
    max_days: int = 14,
    now: datetime | None = None,
    allow_missing: bool = True,
) -> bool:
    """Returns False when a job has an explicit publication date older than `max_days` (default 14 days)."""
    if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
        return allow_missing
    parsed = parse_published_at_utc(raw_val, now=now)
    if parsed is None:
        return allow_missing
    ref_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # Allow up to 14 days + 12h timezone margin for date-only stamps
    cutoff = ref_now - timedelta(days=max_days, hours=12)
    return parsed >= cutoff


def format_published_ago_pt(
    iso_or_raw: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """Formats a timestamp into clean Portuguese relative labels (`Há 2 horas`, `Ontem`, `Há 4 dias`)."""
    parsed = parse_published_at_utc(iso_or_raw, now=now)
    if parsed is None:
        return "Data não informada"

    ref_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    delta = ref_now - parsed
    total_seconds = max(0.0, delta.total_seconds())

    if total_seconds < 300:
        return "Agora mesmo"
    minutes = int(total_seconds // 60)
    if minutes < 60:
        return f"Há {minutes} min"
    hours = int(total_seconds // 3600)
    if hours == 1:
        return "Há 1 hora"
    if hours < 24:
        return f"Há {hours} horas"
    days = int(total_seconds // 86400)
    if days == 1:
        return "Ontem"
    if days < 30:
        return f"Há {days} dias"
    return parsed.strftime("%d/%m/%Y")
