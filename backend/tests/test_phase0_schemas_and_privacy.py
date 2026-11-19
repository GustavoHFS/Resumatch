"""Phase 0 test suite: Unified MasterProfile v2.0 schema, v1->v2 migration, privacy guard, and Git hooks."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import (
    BlockedCompaniesConfig,
    JobSourcesConfig,
    MasterProfile,
    SearchCriteria,
    SectionLabels,
)
from scripts.bootstrap_demo import bootstrap_demo_workspace
from scripts.check_privacy import run_scan
from scripts.install_hooks import HOOK_MARKER, install_pre_commit_hook, is_hook_installed
from scripts.migrate_profile_v2 import convert_v1_dict_to_v2

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "backend" / "tests" / "fixtures" / "demo_examples"


def _load_example(filename: str) -> dict:
    return json.loads((EXAMPLES_DIR / filename).read_text(encoding="utf-8"))


class TestPublicExampleFiles:
    def test_master_profile_v2_example_is_unified_clean_and_flexible(self) -> None:
        raw = _load_example("master_profile.example.json")
        profile = MasterProfile.model_validate(raw)

        assert profile.schema_version == "2.1"
        assert profile.header.full_name == "Marina Alves Costa"
        assert len(profile.summary) > 40
        assert len(profile.experiences) == 1
        assert "Help Desk" in profile.experiences[0].description
        assert profile.courses[0].workload == "40 horas"
        assert profile.courses[0].completion_date == "2026"
        assert not hasattr(profile.projects[0], "period_or_role")
        assert isinstance(profile.skills, list)
        assert all(isinstance(tag, str) for tag in profile.skills)
        assert "Java" in profile.skills
        assert "Windows" in profile.skills

        labels = SectionLabels()
        assert labels.summary == "RESUMO PROFISSIONAL"
        assert labels.experience == "EXPERIÊNCIA PROFISSIONAL"
        assert labels.courses == "CURSOS E CERTIFICAÇÕES"

    def test_search_criteria_example_is_valid(self) -> None:
        raw = _load_example("search_criteria.example.json")
        criteria = SearchCriteria.model_validate(raw)
        assert len(criteria.geography.allowed_cities) == 2
        assert criteria.career_focus.max_mandatory_experience_months == 24
        assert criteria.omissions_policy.allow_local_prep_with_operational_omissions is True

    def test_blocked_companies_example_is_valid(self) -> None:
        raw = _load_example("blocked_companies.example.json")
        blocked = BlockedCompaniesConfig.model_validate(raw)
        assert len(blocked.companies) == 1
        assert blocked.companies[0].preserve_in_work_history is True

    def test_sources_example_is_valid(self) -> None:
        raw = _load_example("sources.example.json")
        sources = JobSourcesConfig.model_validate(raw)
        assert len(sources.sources) >= 25
        categories = {s.category for s in sources.sources}
        assert {
            "Grandes Portais",
            "Tech & Remoto",
            "Polo Tech Recife",
            "Regionais de Pernambuco",
        }.issubset(categories)
        assert all(s.respect_robots_txt is True for s in sources.sources)
        assert all(bool(s.display_name) for s in sources.sources)


class TestUnifiedProfileFlexibilityAndMigration:
    def test_allows_empty_optional_sections_and_deduplicates_skill_tags(self) -> None:
        minimal = MasterProfile.model_validate(
            {
                "header": {"full_name": "Candidato Enxuto"},
                "skills": [" Python ", "python", "SQL", "", "FastAPI"],
            }
        )
        assert minimal.experiences == []
        assert minimal.education == []
        assert minimal.courses == []
        assert minimal.projects == []
        assert minimal.languages == []
        assert minimal.skills == ["Python", "SQL", "FastAPI"]

    def test_v1_to_v2_migration_consolidates_bullets_and_filters_gaps(self) -> None:
        legacy_v1 = {
            "schema_version": "1.0",
            "header": {
                "full_name": "Marina Alves Costa",
                "city": "Belo Horizonte",
                "state": "MG",
                "email": "marina@example.com",
                "phone": "(31) 90000-0000",
                "links": [{"label": "GitHub", "url": "https://github.com/marina-demo"}],
            },
            "education": [
                {
                    "id": "edu_1",
                    "degree": "Sistemas para Internet",
                    "institution": "UEEX",
                    "program_note": "Trilhas Digitais",
                    "period_label": "2026 - 2028",
                    "status": "in_progress",
                }
            ],
            "experiences": [
                {
                    "id": "exp_1",
                    "role": "Suporte Técnico",
                    "company": "Horizonte Tech",
                    "employment_type_label": "CLT",
                    "period_label": "2024",
                    "bullets": [
                        {"id": "b1", "text": "Atendimento presencial e remoto em Help Desk com AnyDesk."},
                        {"id": "b2", "text": "Administração de contas no Active Directory e e-mail."},
                    ],
                }
            ],
            "courses": [
                {"id": "c1", "title": "Agentes IA", "institution": "IDA", "details_label": "40h", "status": "completed"},
                {"id": "c2", "title": "Curso Livre Incompleto", "institution": "Web", "details_label": "10h", "status": "in_progress_free"},
            ],
            "projects": [
                {"id": "p1", "name": "Confidencial Interno", "status": "reserved"},
            ],
            "skills": [
                {"id": "win", "display_name": "Windows", "veracity": "confirmed_practice"},
                {"id": "py", "display_name": "Python", "veracity": "academic_foundation"},
                {"id": "glpi", "display_name": "GLPI", "veracity": "preparable_gap"},
                {"id": "k8s", "display_name": "Kubernetes", "veracity": "unconfirmed"},
            ],
            "languages": [{"id": "l1", "language": "Português", "proficiency": "Nativo"}],
            "area_presets": {
                "generalista": {"default_summary": "Resumo geral consolidado para migração."}
            },
        }
        migrated = convert_v1_dict_to_v2(legacy_v1)
        assert migrated.schema_version == "2.1"
        assert migrated.summary == "Resumo geral consolidado para migração."
        assert "Help Desk" in migrated.experiences[0].description
        assert "Active Directory" in migrated.experiences[0].description
        assert len(migrated.courses) == 1
        assert migrated.courses[0].workload == "40h"
        assert migrated.projects == []
        assert migrated.skills == ["Windows", "Python"]

    def test_rejects_non_https_urls_and_invalid_cnpj_or_schedule(self) -> None:
        sources_raw = _load_example("sources.example.json")
        rss_entry = next(s for s in sources_raw["sources"] if s["type"] == "rss_local")
        rss_entry["feeds"] = ["http://insecure.example.com/feed"]
        with pytest.raises(ValidationError, match="https://"):
            JobSourcesConfig.model_validate(sources_raw)

        blocked_raw = _load_example("blocked_companies.example.json")
        blocked_raw["companies"][0]["cnpj"] = "123456"
        with pytest.raises(ValidationError, match="CNPJ"):
            BlockedCompaniesConfig.model_validate(blocked_raw)

        criteria_raw = _load_example("search_criteria.example.json")
        criteria_raw["schedule"]["earliest_hour"] = 20
        criteria_raw["schedule"]["latest_hour"] = 18
        with pytest.raises(ValidationError, match="earliest_hour"):
            SearchCriteria.model_validate(criteria_raw)


class TestPrivacyScannerAndGitHook:
    def test_real_workspace_passes_privacy_scan_with_zero_leaks(self) -> None:
        violations, scanned_count, secret_count = run_scan(
            repo_root=REPO_ROOT,
            data_dir=REPO_ROOT / "user_data",
            staged_only=False,
        )
        assert scanned_count > 0
        assert secret_count >= 3
        assert violations == []

    def test_scanner_catches_generic_pii_and_dynamic_private_tokens(
        self, tmp_path: Path
    ) -> None:
        fake_repo = tmp_path / "repo"
        fake_data = fake_repo / "user_data"
        (fake_data / "config").mkdir(parents=True)
        (fake_data / "profile").mkdir(parents=True)

        (fake_data / "config" / "blocked_companies.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "companies": [
                        {"id": "secret-corp", "display_name": "Confidencial Sistemas Ltda"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (fake_data / "profile" / "master_profile.json").write_text(
            json.dumps(
                {
                    "header": {
                        "full_name": "Fulano Secreto Oliveira",
                        "neighborhood": "Bairro Oculto",
                    },
                    "projects": [{"name": "Projeto Sigiloso X", "status": "reserved"}],
                }
            ),
            encoding="utf-8",
        )

        synth_email = "usuario.real" + "@" + "gmail.com"
        synth_phone = "(81) " + "98877-6655"
        synth_cpf = "123.456." + "789-01"

        leaking_file = fake_repo / "src" / "leak.py"
        leaking_file.parent.mkdir(parents=True)
        leaking_file.write_text(
            "\n".join(
                [
                    f'EMAIL = "{synth_email}"',
                    f'PHONE = "{synth_phone}"',
                    f'CPF = "{synth_cpf}"',
                    'BLOCKED = "Confidencial Sistemas Ltda"',
                    'PROJECT = "Projeto Sigiloso X"',
                ]
            ),
            encoding="utf-8",
        )
        (fake_repo / "resume.pdf").write_bytes(b"%PDF-1.4 fake")

        violations, _, _ = run_scan(repo_root=fake_repo, data_dir=fake_data)
        categories = {v.category for v in violations}
        assert "PERSONAL_EMAIL" in categories
        assert "BR_PHONE_NUMBER" in categories
        assert "BR_CPF" in categories
        assert "PRIVATE_BLOCKED_COMPANY" in categories
        assert "PRIVATE_RESERVED_PROJECT" in categories
        assert "FORBIDDEN_BINARY_OR_DOC" in categories

        for v in violations:
            assert "Confidencial Sistemas Ltda" not in v.detail
            assert synth_email not in v.detail

    @pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")
    def test_pre_commit_hook_blocks_pii_commit_and_allows_clean_commit(
        self, tmp_path: Path
    ) -> None:
        temp_repo = tmp_path / "git_hook_test"
        (temp_repo / "scripts").mkdir(parents=True)
        (temp_repo / "user_data" / "config").mkdir(parents=True)

        shutil.copyfile(
            REPO_ROOT / "scripts" / "check_privacy.py",
            temp_repo / "scripts" / "check_privacy.py",
        )
        (temp_repo / "user_data" / "config" / "blocked_companies.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "companies": [{"id": "emp-x", "display_name": "Empresa Bloqueada Real"}],
                }
            ),
            encoding="utf-8",
        )

        subprocess.run(["git", "init"], cwd=temp_repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=temp_repo, check=True)
        subprocess.run(["git", "config", "user.name", "Demo Dev"], cwd=temp_repo, check=True)

        installed = install_pre_commit_hook(temp_repo)
        assert installed is True
        assert is_hook_installed(temp_repo) is True
        hook_content = (temp_repo / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
        assert HOOK_MARKER in hook_content

        clean_file = temp_repo / "README.md"
        clean_file.write_text("# Clean Public Repo\nContact: demo@example.com\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md", "scripts/check_privacy.py"], cwd=temp_repo, check=True)
        ok_commit = subprocess.run(
            ["git", "commit", "-m", "chore: initial clean commit"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        assert ok_commit.returncode == 0, ok_commit.stderr

        dirty_file = temp_repo / "notes.txt"
        dirty_file.write_text("Never apply to Empresa Bloqueada Real\n", encoding="utf-8")
        subprocess.run(["git", "add", "notes.txt"], cwd=temp_repo, check=True)
        blocked_commit = subprocess.run(
            ["git", "commit", "-m", "feat: accidental leak"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        assert blocked_commit.returncode != 0
        assert "PRIVACY BLOCKED" in blocked_commit.stderr
        assert "PRIVATE_BLOCKED_COMPANY" in blocked_commit.stderr


class TestDemoBootstrap:
    def test_bootstrap_copies_examples_without_overwriting_existing_data(
        self, tmp_path: Path
    ) -> None:
        target_data = tmp_path / "custom_user_data"
        created_first = bootstrap_demo_workspace(REPO_ROOT, data_dir=target_data)
        assert len(created_first) == 4

        profile_path = target_data / "profile" / "master_profile.json"
        custom_profile = json.loads(profile_path.read_text(encoding="utf-8"))
        custom_profile["header"]["full_name"] = "Custom Candidate"
        profile_path.write_text(json.dumps(custom_profile), encoding="utf-8")

        created_second = bootstrap_demo_workspace(REPO_ROOT, data_dir=target_data)
        assert created_second == []
        assert "Custom Candidate" in profile_path.read_text(encoding="utf-8")
