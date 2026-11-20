"""Offline semantic HTML5 + Vanilla CSS A4 renderer with static Libre Franklin & Inter WOFF2.

- `standalone_preview=True`: references `/api/resumes/fonts/*.woff2` (~4 KB HTML payload per edit).
- `standalone_preview=False`: embeds Base64 WOFF2 only for the active webfont so Headless Chromium
  renders 100% offline with zero network calls.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.models.resume_schema import ResumeDraft, ResumeFontFamily
from app.domain.resume_policy import LAYOUT_MARGINS_MM

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
FONTS_DIR = TEMPLATES_DIR / "fonts"

ALLOWED_FONT_FILES = {
    "LibreFranklin-Regular.woff2",
    "LibreFranklin-Bold.woff2",
    "Inter-Regular.woff2",
    "Inter-Bold.woff2",
}

FONT_FAMILY_STACKS: dict[ResumeFontFamily, str] = {
    "Libre Franklin": '"Libre Franklin", Arial, sans-serif',
    "Inter": '"Inter", Arial, sans-serif',
    "Arial": "Arial, Helvetica, sans-serif",
    "Georgia": 'Georgia, "Times New Roman", serif',
    "Times New Roman": '"Times New Roman", Times, Georgia, serif',
}

PREVIEW_FONT_FACE_CSS = (
    "@font-face {\n"
    '  font-family: "Libre Franklin";\n'
    "  font-style: normal;\n"
    "  font-weight: 400;\n"
    '  src: url("/api/resumes/fonts/LibreFranklin-Regular.woff2") format("woff2");\n'
    "}\n"
    "@font-face {\n"
    '  font-family: "Libre Franklin";\n'
    "  font-style: normal;\n"
    "  font-weight: 700;\n"
    '  src: url("/api/resumes/fonts/LibreFranklin-Bold.woff2") format("woff2");\n'
    "}\n"
    "@font-face {\n"
    '  font-family: "Inter";\n'
    "  font-style: normal;\n"
    "  font-weight: 400;\n"
    '  src: url("/api/resumes/fonts/Inter-Regular.woff2") format("woff2");\n'
    "}\n"
    "@font-face {\n"
    '  font-family: "Inter";\n'
    "  font-style: normal;\n"
    "  font-weight: 700;\n"
    '  src: url("/api/resumes/fonts/Inter-Bold.woff2") format("woff2");\n'
    "}\n"
)


def _inline_pair_css(family: str, reg_file: str, bold_file: str) -> str:
    reg_b64 = base64.b64encode((FONTS_DIR / reg_file).read_bytes()).decode("ascii")
    bold_b64 = base64.b64encode((FONTS_DIR / bold_file).read_bytes()).decode("ascii")
    return (
        "@font-face {\n"
        f'  font-family: "{family}";\n'
        "  font-style: normal;\n"
        "  font-weight: 400;\n"
        f'  src: url("data:font/woff2;base64,{reg_b64}") format("woff2");\n'
        "}\n"
        "@font-face {\n"
        f'  font-family: "{family}";\n'
        "  font-style: normal;\n"
        "  font-weight: 700;\n"
        f'  src: url("data:font/woff2;base64,{bold_b64}") format("woff2");\n'
        "}\n"
    )


@lru_cache(maxsize=1)
def _load_static_assets() -> tuple[Environment, str, dict[str, str]]:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    css_text = (TEMPLATES_DIR / "resume-a4.css").read_text(encoding="utf-8")
    inline_by_family = {
        "Libre Franklin": _inline_pair_css(
            "Libre Franklin", "LibreFranklin-Regular.woff2", "LibreFranklin-Bold.woff2"
        ),
        "Inter": _inline_pair_css("Inter", "Inter-Regular.woff2", "Inter-Bold.woff2"),
    }
    return env, css_text, inline_by_family


def render_resume_a4_html(
    draft: ResumeDraft,
    *,
    standalone_preview: bool = False,
) -> str:
    # Also normalizes legacy/model_copy drafts without altering their manual order.
    draft = ResumeDraft.model_validate(draft.model_dump())
    env, stylesheet_css, inline_by_family = _load_static_assets()
    template = env.get_template("resume_a4.html")
    font_stack = FONT_FAMILY_STACKS.get(draft.font_family, FONT_FAMILY_STACKS["Libre Franklin"])
    font_face_css = (
        PREVIEW_FONT_FACE_CSS
        if standalone_preview
        else inline_by_family.get(draft.font_family, "")
    )
    step = max(draft.layout_step, 3 if draft.compact_mode else 0) if draft.max_pages == 1 else 0
    margin = LAYOUT_MARGINS_MM[step]
    layout_css = (
        f":root {{ --page-margin: {margin}mm; --body-leading: {1.42 - step * .02:.2f}; "
        f"--section-gap: {8.5 - step * .8}pt; --entry-gap: {6 - step * .5}pt; "
        f"--bullet-gap: {2 - step * .25}pt; }}\n"
        f"@page {{ size: A4 portrait; margin: {margin}mm; }}"
    )
    return template.render(
        payload=draft,
        font_face_css=font_face_css,
        stylesheet_css=stylesheet_css,
        font_stack=font_stack,
        standalone_preview=standalone_preview,
        layout_css=layout_css,
    )
