"""Small outbound job descriptions, keeping the introduction and requirements."""

from html.parser import HTMLParser
import re

MAX_JOB_DESCRIPTION = 4500


class _JobHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "li", "br", "h1", "h2", "h3", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "li", "h1", "h2", "h3", "div"}:
            self.parts.append("\n")

    def handle_data(self, data) -> None:
        if not self.hidden:
            self.parts.append(data)


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Never split PIIStripper tokens, including when compacting a masked payload.
    if cut.rfind("[[") > cut.rfind("]]"):
        cut = cut[:cut.rfind("[[")]
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


def compact_job_description(text: str) -> str:
    if re.search(r"</?[a-zA-Z][^>]*>", text):
        parser = _JobHTML()
        parser.feed(text)
        text = "".join(parser.parts)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines() if line.strip())
    if len(text) <= MAX_JOB_DESCRIPTION:
        return text
    intro = _excerpt(text, 1800)
    requirements = re.search(
        r"(?:requisitos|qualifica[çc][õo]es|requirements|o que voc[êe] precisa|habilidades|compet[êe]ncias)",
        text[len(intro):], re.IGNORECASE,
    )
    if requirements:
        start = len(intro) + requirements.start()
        return intro + "\n" + _excerpt(text[start:], MAX_JOB_DESCRIPTION - len(intro) - 1)
    return _excerpt(text, MAX_JOB_DESCRIPTION)
