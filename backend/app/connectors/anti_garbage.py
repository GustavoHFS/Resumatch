"""Strict anti-garbage guard for HTML & Web Search job connectors.

Blocks navigation menus, language switchers, facet/filter URLs, category/state SEO pages,
and generic calls-to-action from ever entering `JobOpportunity` as fake jobs.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from app.domain.triage_engine import normalize_text

GARBAGE_EXACT_TITLES: set[str] = {
    "",
    "english",
    "francais",
    "deutsch",
    "italiano",
    "espanol",
    "portugues",
    "portugues (brasil)",
    "japanese",
    "chinese",
    "tipo de contrato",
    "nivel de experiencia",
    "modalidade de trabalho",
    "buscar vagas",
    "busca de vagas",
    "banco nacional de empregos",
    "bne",
    "ver vaga",
    "ver vagas",
    "ver todas",
    "ver todas as vagas",
    "ver mais",
    "mostre mais",
    "mostrar mais",
    "mais recentes",
    "mais relevantes",
    "saiba mais",
    "candidatar-se",
    "candidatar",
    "detalhes",
    "vagas",
    "vagas abertas",
    "oportunidades",
    "carreiras",
    "trabalhe conosco",
    "recrutadores",
    "para empresas",
    "empresas",
    "anunciar vaga",
    "anunciar vagas",
    "publicar vaga",
    "login",
    "entrar",
    "cadastre-se",
    "cadastrar curriculo",
    "area do candidato",
    "portal de carreiras",
    "limpar filtros",
    "filtrar",
    "inicio",
    "home",
    "contato",
    "sobre nos",
    "politica de privacidade",
}

GARBAGE_TITLE_PATTERNS = [
    # Category/state SEO titles like "Vagas no Acre", "Vagas em Alagoas", "Vagas em Recife"
    re.compile(r"^vagas?\s+(?:em|no|na|nos|nas)\s+[a-z\s/-]{2,40}$", re.IGNORECASE),
    # Broad category links like "Vagas de RH", "Vagas de Tecnologia", "Vagas de Administração"
    re.compile(
        r"^vagas\s+de\s+(?:rh|recursos\s+humanos|ti|tecnologia|informatica|vendas|comercio|administracao|marketing|engenharia|saude|educacao|financas|logistica)$",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:filtrar\s+por|ordenar\s+por|vagas\s+por)\b", re.IGNORECASE),
    re.compile(r"^\d[\d.,]*\+?\s*vagas?\s+(?:atuais|abertas|de\s+emprego|em\s+)", re.IGNORECASE),
]

FILTER_QUERY_KEYS = {
    "contract_type",
    "expertise",
    "ordenar_por",
    "sort",
    "filter",
    "city_name",
    "remoto",
    "page",
    "pagina",
}

GARBAGE_PATH_PATTERNS = [
    re.compile(r"/(?:login|signin|sign-in|cadastro|register|anunciar-vaga|recrutadores|empresas|blog|sobre|contato|privacidade|termos)(?:/|\.|$)", re.IGNORECASE),
    re.compile(r"/vagas/(?:todas|estado|area|cidade|cargo|nivel)(?:/|$)", re.IGNORECASE),
    re.compile(r"/(?:de|es|fr|it|ja|zh)/careers/?$", re.IGNORECASE),
    re.compile(r"^/(?:pt/)?careers/?$", re.IGNORECASE),
]

EXCLUSIVE_INTERNSHIP_HOSTS = {
    "portal.ciee.org.br",
    "ciee.org.br",
    "cieepe.org.br",
    "nube.com.br",
    "www.nube.com.br",
    "superestagios.com.br",
    "www.superestagios.com.br",
    "iel.org.br",
}


def is_garbage_job_candidate(title: str | None, url: str | None) -> bool:
    """Returns True if `(title, url)` looks like a menu item, language link, filter, or SEO index page."""
    norm_t = normalize_text(title or "").strip()
    if len(norm_t) < 4 or len(norm_t) > 200:
        return True
    if norm_t in GARBAGE_EXACT_TITLES:
        return True
    for pat in GARBAGE_TITLE_PATTERNS:
        if pat.search(norm_t):
            return True

    if not url:
        return True
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path or "/"

    if host in EXCLUSIVE_INTERNSHIP_HOSTS:
        return True

    if path in {"", "/"} and not parsed.query:
        return True

    for p_pat in GARBAGE_PATH_PATTERNS:
        if p_pat.search(path):
            return True

    q_keys = {k.lower() for k, _ in parse_qsl(parsed.query)}
    if q_keys & FILTER_QUERY_KEYS:
        return True

    # Portal-specific index vs individual ad rules
    if host == "vagas.com.br" and not re.search(r"/vagas/v\d+", path):
        return True
    if host == "catho.com.br" and not re.search(r"/vagas/[^/]+/\d{5,}", path):
        return True
    if host == "infojobs.com.br" and not re.search(r"/vaga-de-[^/]+__\d+\.aspx", path, re.I):
        return True
    if host == "programathor.com.br" and not re.search(r"^/jobs/\d+-", path):
        return True
    if host == "bne.com.br" and not re.search(r"^/vaga-de-emprego-", path, re.I):
        return True
    if host == "divulgavagas.com.br" and not re.search(r"/vaga-de-emprego/[^/]+-\d+", path, re.I):
        return True
    if host.endswith("indeed.com") and not (
        "/viewjob" in path or "/rc/clk" in path or "jk" in q_keys
    ):
        return True
    if host.endswith("glassdoor.com.br") and not (
        "/job-listing/" in path.lower() or "/vaga/" in path.lower() or "joblistingid" in q_keys
    ):
        return True
    if host.endswith("jooble.org") and "/desc/" not in path.lower() and "ckey=" not in parsed.query.lower():
        return True
    if host.endswith("talent.com") and not ("/view" in path and "id" in q_keys):
        return True
    if host == "jobs.jobvite.com" and "/job/" not in path:
        return True
    if host == "geekhunter.com" and "/jobs/" not in path:
        return True

    return False


def is_verified_individual_job_url(url: str) -> bool:
    """Checks that a URL points to a specific job posting rather than a generic listing/category."""
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path or ""
    query = parsed.query or ""

    if is_garbage_job_candidate("Analista de Suporte", url):
        return False

    known_patterns = [
        r"gupy\.io/jobs?/\w+",
        r"inhire\.app/vagas/[A-Za-z0-9-]+",
        r"linkedin\.com/jobs/view/",
        r"indeed\.com/(?:viewjob|rc/clk)",
        r"vagas\.com\.br/vagas/v\d+",
        r"catho\.com\.br/vagas/[^/]+/\d{5,}",
        r"infojobs\.com\.br/vaga-de-[^/]+__\d+\.aspx",
        r"bne\.com\.br/vaga-de-emprego-",
        r"programathor\.com\.br/jobs/\d+-",
        r"geekhunter\.com/(?:pt/)?[^/]+/jobs/[a-z0-9-]+",
        r"coodesh\.com/(?:vagas|jobs)/[a-z0-9-]+",
        r"remotar\.com\.br/job/\d+",
        r"trampos\.co/oportunidades/\d+",
        r"divulgavagas\.com\.br/vaga-de-emprego/[^/]+-\d+",
        r"jobs\.jobvite\.com/[^/]+/job/[A-Za-z0-9]+",
        r"talent\.com/view\?id=\d+",
        r"greenhouse\.io/.+/jobs/\d+",
        r"lever\.co/[^/]+/[0-9a-f-]{12,}",
        r"workable\.com/.+/[A-Za-z0-9]+",
        r"ashbyhq\.com/[^/]+/[0-9a-f-]{12,}",
        r"recruitee\.com/o/[a-z0-9-]+",
        r"empregare\.com/pt-br/vaga-",
        r"github\.com/[^/]+/[^/]+/issues/\d+",
    ]
    full = f"{host}{path}" + (f"?{query}" if query else "")
    if any(re.search(pat, full, re.IGNORECASE) for pat in known_patterns):
        return True

    # Generic custom board individual job URL: path has >= 2 segments and job keyword + specific slug
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) >= 2 and any(
         seg in {"vaga", "vagas", "job", "jobs", "oportunidade", "oportunidades"}
        for seg in (s.lower() for s in segments[:-1])
    ):
        last = segments[-1].lower()
        if len(last) >= 4 and last not in {"vagas", "jobs", "todas", "search", "index"}:
            return True

    return False
