"""Offline semantic HTML5 + Vanilla CSS A4 renderer with embedded static Libre Franklin."""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.domain.resume_composer import ResumeDocumentPayload

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
FONTS_DIR = TEMPLATES_DIR / "fonts"


@lru_cache(maxsize=1)
def _load_static_assets() -> tuple[Environment, str, str]:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    css_text = (TEMPLATES_DIR / "resume-a4.css").read_text(encoding="utf-8")

    reg_b64 = base64.b64encode((FONTS_DIR / "LibreFranklin-Regular.woff2").read_bytes()).decode("ascii")
    bold_b64 = base64.b64encode((FONTS_DIR / "LibreFranklin-Bold.woff2").read_bytes()).decode("ascii")

    font_face_css = (
        "@font-face {\n"
        '  font-family: "Libre Franklin";\n'
        "  font-style: normal;\n"
        "  font-weight: 400;\n"
        f'  src: url("data:font/woff2;base64,{reg_b64}") format("woff2");\n'
        "}\n"
        "@font-face {\n"
        '  font-family: "Libre Franklin";\n'
        "  font-style: normal;\n"
        "  font-weight: 700;\n"
        f'  src: url("data:font/woff2;base64,{bold_b64}") format("woff2");\n'
        "}\n"
    )
    return env, font_face_css, css_text


def render_resume_a4_html(
    payload: ResumeDocumentPayload,
    *,
    standalone_preview: bool = False,
) -> str:
    env, font_face_css, stylesheet_css = _load_static_assets()
    template = env.get_template("resume_a4.html")
    return template.render(
        payload=payload,
        font_face_css=font_face_css,
        stylesheet_css=stylesheet_css,
        standalone_preview=standalone_preview,
    )
