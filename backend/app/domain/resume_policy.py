"""Presentation rules: concise summaries and deterministic academic priority."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import TypeVar

T = TypeVar("T")
SUMMARY_LIMIT = 420
LAYOUT_MARGINS_MM = (14.0, 12.0, 10.5, 9.5)


def normalized(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold())
                   if not unicodedata.combining(c))


def clean_academic_text(text: str, *, keep_status: bool = False) -> str:
    def replace(match: re.Match[str]) -> str:
        content = match.group(1)
        if keep_status and re.fullmatch(
            r"(?:em andamento|cursando|atual|conclu[ií]do|previs[aã]o(?: de conclus[aã]o)?[: ]*\d{4}|\d{4}(?:\s*[-–—/]\s*\d{4})?)",
            content.strip(), re.IGNORECASE,
        ):
            return f" {content} "
        return " "
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\(([^()]*)\)", replace, text)
    return re.sub(r"\s+", " ", text.replace("(", " ").replace(")", " ")).strip(" ·—-;")


def academic_order(items: list[T], today: date | None = None) -> list[T]:
    year = (today or date.today()).year

    def key(item: T) -> tuple[int, int]:
        degree = normalized(getattr(item, "degree", ""))
        period = normalized(getattr(item, "period", ""))
        years = [int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", period)]
        latest = max(years, default=0)
        ongoing = any(word in period for word in ("andamento", "cursando", "atual", "previs", "presente"))
        ongoing = ongoing or (latest >= year and "concluid" not in period)
        higher = any(word in degree for word in (
            "graduacao", "bacharel", "licenciatura", "tecnologo", "mestrado",
            "doutorado", "especializacao", "pos", "mba",
        ))
        rank = 0 if ongoing else 1 if higher else 3 if "tecnic" in degree else 2
        return rank, -latest

    return sorted(items, key=key)  # stable ties preserve the user's original sequence


def concise_summary(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    sentences = re.findall(r"[^.!?]+[.!?]+(?:[\"']|(?=\s|$))?", text)
    selected: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if len(" ".join([*selected, sentence])) <= limit:
            selected.append(sentence)
        if len(selected) == 4:
            break
    if selected:
        return " ".join(selected)
    # An overlong single sentence: prefer a complete clause, then a word boundary.
    prefix = text[:limit - 1]
    for separator in (";", ":", ","):
        position = prefix.rfind(separator)
        if position >= limit // 3:
            return prefix[:position].rstrip() + "."
    words = prefix.rsplit(" ", 1)[0] if " " in prefix else ""
    return words.rstrip(" ,;:-") + "." if words else ""
