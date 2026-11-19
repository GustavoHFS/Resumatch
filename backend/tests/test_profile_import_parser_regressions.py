"""Deterministic regressions for the `.pdf`/`.docx` Base Resume parser.

All fixtures are synthetic: in-memory WordprocessingML, fake PDF pages and
`example.com` contacts. No real resume, PII or binary document is stored here.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest

from app.core.config import AppSettings
from app.services import profile_import_service as svc

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "backend" / "tests" / "fixtures" / "demo_examples"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def _cell(paragraphs: list[str], extra_xml: str = "") -> str:
    body = "".join(_p(text) for text in paragraphs)
    return f"<w:tc>{body}{extra_xml}</w:tc>"


def _table(rows: list[list[list[str]]], rich_cell_xml: str | None = None) -> str:
    rendered_rows: list[str] = []
    for row_index, row in enumerate(rows):
        cells: list[str] = []
        for cell_index, cell in enumerate(row):
            extra = rich_cell_xml if rich_cell_xml and row_index == 0 and cell_index == 0 else ""
            cells.append(_cell(cell, extra))
        rendered_rows.append(f"<w:tr>{''.join(cells)}</w:tr>")
    return f"<w:tbl>{''.join(rendered_rows)}</w:tbl>"


def _docx_bytes(body_xml: str, hyperlink: str | None = None) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{WORD_NS}"><w:body>{body_xml}</w:body></w:document>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        f'Target="{escape(hyperlink)}" TargetMode="External"/>'
        "</Relationships>"
    ) if hyperlink else (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", rels)
    return buffer.getvalue()


def _two_column_resume_body() -> str:
    left = [
        ["Contato", "Recife - PE | ana.lima@example.com | (81) 90000-1111"],
        ["COMPETÊNCIAS", "Python, SQL, Git"],
        ["IDIOMAS", "Português: Nativo"],
    ]
    right = [
        [
            "RESUMO PROFISSIONAL",
            "Profissional de TI com atuação em suporte, conﬁguração de redes e automação.",
        ],
        [
            "EXPERIÊNCIA PROFISSIONAL",
            "Analista de Suporte",
            "Alfa Sistemas",
            "Mar/2024 – Atual",
            "Atendimento a clientes e usuários.",
        ],
        [
            "FORMAÇÃO",
            "Bacharelado em Engenharia de Software",
            "FAFIRE",
            "Março de 2023 – Dezembro de 2027",
        ],
        [
            "CURSOS",
            "Agentes Autônomos (Agentic AI) — FIAP",
            "Formação em inteligência artificial generativa com automação de fluxos.",
        ],
    ]
    paired_rows: list[list[list[str]]] = []
    for index in range(max(len(left), len(right))):
        paired_rows.append([
            left[index] if index < len(left) else [],
            right[index],
        ])
    return _p("Ana Lima") + _table(paired_rows)


def test_docx_two_column_table_reads_left_column_then_right_column() -> None:
    body = (
        _p("Ana Lima")
        + _table([
            [["Contato", "ana.lima@example.com"], ["RESUMO", "Profissional de TI."]],
            [["COMPETÊNCIAS", "Python"], ["EXPERIÊNCIA", "Analista de Suporte"]],
        ])
    )
    text, _ = svc.extract_text_and_links_from_docx(_docx_bytes(body))
    lines = text.splitlines()
    assert " | " not in text
    assert lines == [
        "Ana Lima",
        "Contato",
        "ana.lima@example.com",
        "COMPETÊNCIAS",
        "Python",
        "RESUMO",
        "Profissional de TI.",
        "EXPERIÊNCIA",
        "Analista de Suporte",
    ]
    assert "" not in lines


def test_docx_one_column_table_and_nested_table_keep_order_without_duplicates() -> None:
    nested = _table([[["Dentro"]]])
    body = _table([[["Primeira"]], [["Segunda"]], [["Terceira"]]]) + _table([[["Quarta"]]])
    body_with_nested = _table(
        [[["Antes", "Depois"]]],
        rich_cell_xml=nested,
    )
    text, _ = svc.extract_text_and_links_from_docx(_docx_bytes(body))
    assert text.splitlines() == ["Primeira", "Segunda", "Terceira", "Quarta"]
    nested_text, _ = svc.extract_text_and_links_from_docx(_docx_bytes(body_with_nested))
    assert nested_text.splitlines().count("Dentro") == 1
    assert nested_text.splitlines() == ["Antes", "Depois", "Dentro"]


def test_docx_two_column_layout_feeds_integrated_heuristic_profile() -> None:
    raw_text, links = svc.extract_text_and_links_from_docx(_docx_bytes(_two_column_resume_body()))
    profile = svc.heuristic_parse_resume_text(raw_text, links)

    assert profile.header.full_name == "Ana Lima"
    assert profile.header.city == "Recife"
    assert profile.header.state == "PE"
    assert profile.header.email == "ana.lima@example.com"
    assert profile.summary.startswith("Profissional de TI")
    assert "ana.lima@example.com" not in profile.summary
    assert "Python" in profile.skills
    assert profile.languages[0].language == "Português"
    assert len(profile.experiences) == 1
    assert profile.experiences[0].role == "Analista de Suporte"
    assert profile.experiences[0].company == "Alfa Sistemas"
    assert "Atendimento a clientes e usuários." in profile.experiences[0].description
    assert len(profile.education) == 1
    assert profile.education[0].institution == "FAFIRE"
    assert profile.education[0].period == "Março de 2023 – Dezembro de 2027"
    assert len(profile.courses) == 1
    assert profile.courses[0].institution == "FIAP"
    assert "inteligência artificial" in profile.courses[0].description


def test_unicode_nfkc_and_pdf_break_rejoin_are_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        "TÍTULO DO CURRÍCULO\nAtendimento a clientes e\nusuários com ﬁbras ópticas.\n"
        "• Suporte a redes\n• Gestão de chamados",
        "Analista de Suporte\nAlfa Sistemas\n2024\nAtendimento a clientes e\nUsuários remotos",
    ]

    class _FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

        def get(self, _name: str):
            return None

    class _FakeReader:
        def __init__(self, _stream: io.BytesIO) -> None:
            self.pages = [_FakePage(text) for text in pages]

    monkeypatch.setattr(svc, "PdfReader", _FakeReader)
    text, _ = svc.extract_text_and_links_from_pdf(b"%PDF-fake")

    assert "clientes e usuários" in text
    assert "fibras ópticas" in text
    assert "ﬁ" not in text
    assert "• Suporte a redes\n• Gestão de chamados" in text
    assert "TÍTULO DO CURRÍCULO\nAtendimento" in text
    # Capitalised continuation stays separate: ambiguity keeps the hard break.
    assert "clientes e\nUsuários remotos" in text
    # Different PDF pages never get merged.
    assert "chamados\n\nAnalista de Suporte" in text


def test_city_state_in_mixed_preamble_lines_and_full_state_names() -> None:
    base = "Ana Lima\n{location}\nana.lima@example.com | (81) 90000-1111\nRESUMO\nTexto profissional."
    cases = [
        ("Recife - PE", ("Recife", "PE")),
        ("Recife, Pernambuco", ("Recife", "PE")),
        ("Juazeiro do Norte - CE", ("Juazeiro do Norte", "CE")),
        ("São Paulo/SP", ("São Paulo", "SP")),
        ("Recife, pernambuco", ("Recife", "PE")),
    ]
    for location, expected in cases:
        profile = svc.heuristic_parse_resume_text(base.format(location=location), [])
        assert (profile.header.city, profile.header.state) == expected
        assert "example.com" not in profile.summary

    mixed = (
        "Ana Lima\n"
        "Recife - PE | ana.lima@example.com | (81) 90000-0000\n"
        "RESUMO\n"
        "Texto profissional."
    )
    profile = svc.heuristic_parse_resume_text(mixed, [])
    assert (profile.header.city, profile.header.state) == ("Recife", "PE")
    assert "ana.lima@example.com" not in profile.summary
    assert "(81) 90000-0000" not in profile.summary

    inline_contacts = (
        "Ana Lima\n"
        "Recife - PE, Brasil ana.lima@example.com (81) 90000-0000\n"
        "RESUMO\n"
        "Texto profissional."
    )
    profile = svc.heuristic_parse_resume_text(inline_contacts, [])
    assert (profile.header.city, profile.header.state) == ("Recife", "PE")


def test_links_are_canonically_deduplicated_without_losing_distinct_urls() -> None:
    text = (
        "Ana Lima\n"
        "linkedin.com/in/demo\n"
        "https://github.com/AnaCase/Repo\n"
        "RESUMO\n"
        "Texto profissional."
    )
    profile = svc.heuristic_parse_resume_text(
        text,
        ["https://www.linkedin.com/in/demo/"],
    )
    urls = [link.url for link in profile.header.links]
    assert len(urls) == 2
    assert sum("linkedin.com/in/demo" in url for url in urls) == 1
    assert any("github.com/AnaCase/Repo" in url for url in urls)
    assert all(url.startswith("https://") for url in urls)

    assert len(svc._dedupe_urls([
        "https://www.linkedin.com/in/demo/",
        "https://linkedin.com/in/demo",
    ])) == 1
    assert len(svc._dedupe_urls([
        "https://example.com/a/?x=1",
        "https://example.com/a?x=2",
        "https://example.com/A",
    ])) == 3


def test_consecutive_experiences_without_blank_line_are_split() -> None:
    lines = [
        "Analista de Suporte",
        "Alfa Sistemas",
        "Mar/2022 – Dez/2023",
        "Atendimento a clientes e usuários.",
        "Desenvolvedor Júnior",
        "Beta Tecnologia",
        "Jan/2024 – Atual",
        "Desenvolvimento de APIs e automações.",
    ]
    entries = svc.parse_experience_blocks(lines)
    assert len(entries) == 2
    assert (entries[0].role, entries[0].company) == ("Analista de Suporte", "Alfa Sistemas")
    assert (entries[1].role, entries[1].company) == ("Desenvolvedor Júnior", "Beta Tecnologia")
    assert entries[0].period == "Mar/2022 – Dez/2023"
    assert entries[1].period == "Jan/2024 – Atual"
    assert "Atendimento" in entries[0].description
    assert "Desenvolvimento" in entries[1].description


def test_inline_header_with_ascii_hyphens_preserves_the_date_range() -> None:
    entries = svc.parse_experience_blocks([
        "Analista de Suporte - Alfa Sistemas - Mar/2022 - Dez/2023",
        "Atendimento a clientes e usuários.",
    ])
    assert len(entries) == 1
    assert entries[0].role == "Analista de Suporte"
    assert entries[0].company == "Alfa Sistemas"
    assert entries[0].period == "Mar/2022 - Dez/2023"
    assert "Atendimento" in entries[0].description


def test_two_line_header_with_company_and_period_on_the_same_line() -> None:
    lines = [
        "Analista de Suporte",
        "Alfa Sistemas · Mar/2022 – Dez/2023",
        "Atendimento a clientes e usuários.",
        "Desenvolvedor Júnior",
        "Beta Tecnologia | Jan/2024 – Atual",
        "Desenvolvimento de APIs e automações.",
    ]
    entries = svc.parse_experience_blocks(lines)
    assert len(entries) == 2
    assert entries[0].company == "Alfa Sistemas"
    assert entries[0].period == "Mar/2022 – Dez/2023"
    assert entries[1].company == "Beta Tecnologia"
    assert entries[1].period == "Jan/2024 – Atual"
    assert "Atendimento" in entries[0].description
    assert "Desenvolvimento" in entries[1].description


def test_experience_activities_without_temporal_evidence_do_not_open_new_job() -> None:
    lines = [
        "Analista de Suporte",
        "Alfa Sistemas",
        "Mar/2022 – Dez/2023",
        "Projetos internos",
        "Documentação técnica de processos",
    ]
    entries = svc.parse_experience_blocks(lines)
    assert len(entries) == 1
    assert "Projetos internos" in entries[0].description
    assert "Documentação técnica" in entries[0].description

    # A contract label alone is metadata for the current job, not a new period.
    contract_only = svc.parse_experience_blocks([
        "Analista de Suporte", "Alfa Sistemas", "Mar/2022 – Dez/2023",
        "Revisão de processos", "Equipe interna", "Contrato CLT",
    ])
    assert len(contract_only) == 1
    assert "Revisão de processos" in contract_only[0].description
    assert "Contrato CLT" in contract_only[0].description


def test_consecutive_jobs_without_descriptions_are_both_kept() -> None:
    lines = [
        "Analista de Suporte",
        "Alfa Sistemas",
        "Mar/2022 – Dez/2023",
        "Desenvolvedor Júnior",
        "Beta Tecnologia",
        "Jan/2024 – Atual",
    ]
    entries = svc.parse_experience_blocks(lines)
    assert len(entries) == 2
    assert entries[0].company == "Alfa Sistemas"
    assert entries[1].company == "Beta Tecnologia"
    assert entries[0].description == ""
    assert entries[1].description == ""


def test_education_accepts_institution_first_and_reverse_consecutively() -> None:
    institution_first = [
        "FAFIRE",
        "Bacharelado em Engenharia de Software",
        "Março de 2023 – Dezembro de 2027",
        "UNIBRA",
        "Tecnólogo em Análise e Desenvolvimento de Sistemas",
        "Fevereiro de 2024 – Dezembro de 2026",
    ]
    entries = svc.parse_education_blocks(institution_first)
    assert len(entries) == 2
    assert entries[0].institution == "FAFIRE"
    assert entries[0].degree == "Bacharelado em Engenharia de Software"
    assert entries[0].period == "Março de 2023 – Dezembro de 2027"
    assert entries[1].institution == "UNIBRA"
    assert entries[1].period == "Fevereiro de 2024 – Dezembro de 2026"

    reverse = [
        "Bacharelado em Engenharia de Software",
        "FAFIRE",
        "Março de 2023 – Dezembro de 2027",
        "Tecnólogo em Análise e Desenvolvimento de Sistemas",
        "UNIBRA",
        "Fevereiro de 2024 – Dezembro de 2026",
    ]
    entries = svc.parse_education_blocks(reverse)
    assert len(entries) == 2
    assert entries[0].institution == "FAFIRE"
    assert entries[1].institution == "UNIBRA"

    with_blank = svc.parse_education_blocks([
        "FAFIRE",
        "",
        "Bacharelado em Engenharia de Software",
        "Março de 2023 – Dezembro de 2027",
    ])
    assert len(with_blank) == 1
    assert with_blank[0].institution == "FAFIRE"
    assert with_blank[0].degree == "Bacharelado em Engenharia de Software"


@pytest.mark.parametrize(
    "institution",
    ["FAFIRE", "UNIBRA", "FBV", "FPS", "UFPB", "UFCG", "UTFPR", "UNIFESP", "UNICAP", "UNINTER", "ETEMAC"],
)
def test_known_institutions_are_associated_with_the_following_degree(institution: str) -> None:
    entries = svc.parse_education_blocks([
        institution,
        "Bacharelado em Sistemas de Informação",
        "Março de 2023 – Dezembro de 2027",
    ])
    assert len(entries) == 1
    assert entries[0].institution == institution
    assert entries[0].degree == "Bacharelado em Sistemas de Informação"
    assert entries[0].period == "Março de 2023 – Dezembro de 2027"


def test_inline_education_does_not_fragment_long_date_range() -> None:
    entries = svc.parse_education_blocks([
        "Graduação em Sistemas — UNINTER — Março de 2023 – Dezembro de 2027",
    ])
    assert len(entries) == 1
    assert entries[0].degree == "Graduação em Sistemas"
    assert entries[0].institution == "UNINTER"
    assert entries[0].period == "Março de 2023 – Dezembro de 2027"
    assert "2023" not in entries[0].institution


def test_course_long_description_stays_with_previous_course_and_metadata_is_not_institution() -> None:
    lines = [
        "Agentes Autônomos (Agentic AI) — FIAP",
        "Formação em inteligência artificial generativa com automação de fluxos e validação de dados.",
        "Java para Backend — Alura",
        "40 horas · Out/2026",
    ]
    courses = svc.parse_course_blocks(lines)
    assert len(courses) == 2
    assert courses[0].institution == "FIAP"
    assert "Formação em inteligência artificial" in courses[0].description
    assert courses[1].institution == "Alura"
    assert courses[1].workload == "40 horas"
    assert courses[1].completion_date == "Out/2026"

    distinct = svc.parse_course_blocks(["Scrum", "Kanban"])
    assert [course.title for course in distinct] == ["Scrum", "Kanban"]
    noun_phrase_titles = svc.parse_course_blocks(["Scrum", "Gestão de projetos", "Formação em Python"])
    assert [course.title for course in noun_phrase_titles] == [
        "Scrum",
        "Gestão de projetos",
        "Formação em Python",
    ]
    titled_with_metadata = svc.parse_course_blocks(["Scrum", "Python para Dados — 40 horas"])
    assert len(titled_with_metadata) == 2
    assert titled_with_metadata[1].title == "Python para Dados"
    assert titled_with_metadata[1].workload == "40 horas"

    long_date = svc.parse_course_blocks([
        "Gestão de Projetos — Alura",
        "Concluído em Março de 2026",
    ])
    assert len(long_date) == 1
    assert long_date[0].completion_date == "Março de 2026"

    parsed = svc._parse_single_course_chunk([
        "Curso de Redes",
        "Formação prática em infraestrutura, protocolos e diagnóstico.",
    ])
    assert parsed is not None
    assert parsed.institution == ""
    assert "Formação prática" in parsed.description

    short_continuation = svc.parse_course_blocks([
        "Scrum Master — Alura",
        "Formação completa em metodologias ágeis",
    ])
    assert len(short_continuation) == 1
    assert short_continuation[0].description == "Formação completa em metodologias ágeis"


def test_skills_split_only_outside_nested_parentheses() -> None:
    assert svc._split_skills_outside_parentheses("Java (POO, Collections), Python") == [
        "Java (POO, Collections)",
        "Python",
    ]
    assert svc._split_skills_outside_parentheses(
        "Java (POO, Collections), Python e SQL (Avançado, com joins)"
    ) == [
        "Java (POO, Collections)",
        "Python",
        "SQL (Avançado, com joins)",
    ]
    assert svc._split_skills_outside_parentheses(
        "Cloud (AWS (EC2, S3), Azure), Python"
    ) == ["Cloud (AWS (EC2, S3), Azure)", "Python"]

    text = (
        "Ana Lima\n"
        "RESUMO\n"
        "Texto profissional.\n"
        "COMPETÊNCIAS\n"
        "Java (POO, Collections), Python e SQL (Avançado, com joins)\n"
    )
    profile = svc.heuristic_parse_resume_text(text, [])
    assert "Java (POO, Collections)" in profile.skills
    assert "Python" in profile.skills
    assert "SQL (Avançado, com joins)" in profile.skills


@pytest.mark.anyio
async def test_two_column_docx_is_normalized_and_masked_before_gemini(
    tmp_path: Path,
) -> None:
    raw_bytes = _docx_bytes(_two_column_resume_body())
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content.decode("utf-8"))["contents"][0]["parts"][0]["text"]
        captured["sent"] = sent
        assert "ana.lima@example.com" not in sent
        assert "90000-1111" not in sent
        assert "\ufb01" not in sent
        assert "configuração de redes" in sent
        assert "Atendimento a clientes e usuários." in sent
        payload = {
            "full_name": "Ana Lima",
            "city": "Recife",
            "state": "PE",
            "summary": "Profissional de TI.",
            "experiences": [
                {
                    "role": "Analista de Suporte",
                    "company": "Alfa Sistemas",
                    "period": "Mar/2024 – Atual",
                    "location": "Recife, PE",
                    "description": "Atendimento a clientes e usuários.",
                }
            ],
            "education": [],
            "courses": [],
            "projects": [],
            "skills": ["Python"],
            "languages": [],
        }
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}],
                "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 40, "totalTokenCount": 120},
            },
        )

    settings = AppSettings(
        data_dir=tmp_path / "import_ws",
        examples_dir=EXAMPLES_DIR,
        gemini_api_key="dummy-key",
    )
    result = await svc.import_profile_from_uploaded_file(
        filename="curriculo.docx",
        raw_bytes=raw_bytes,
        settings=settings,
        llm_transport=httpx.MockTransport(handler),
    )

    assert result["extraction_mode"] == "ai"
    assert captured["sent"]
    assert result["profile_draft"]["header"]["email"] == "ana.lima@example.com"
    assert result["profile_draft"]["header"]["city"] == "Recife"
    assert result["profile_draft"]["header"]["state"] == "PE"
