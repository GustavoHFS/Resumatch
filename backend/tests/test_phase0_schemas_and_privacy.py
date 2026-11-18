"""Phase 0 test suite: Pydantic v2 schemas, veracity invariants, privacy guard, and Git hooks."""

from __future__ import annotations

import copy
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
)
from scripts.bootstrap_demo import bootstrap_demo_workspace
from scripts.check_privacy import run_scan
from scripts.install_hooks import HOOK_MARKER, install_pre_commit_hook, is_hook_installed

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"


def _load_example(filename: str) -> dict:
    return json.loads((EXAMPLES_DIR / filename).read_text(encoding="utf-8"))


class TestPublicExampleFiles:
    def test_master_profile_example_is_valid_and_supports_pt_and_en_presets(self) -> None:
        raw = _load_example("master_profile.example.json")
        profile = MasterProfile.model_validate(raw)

        assert profile.header.full_name == "Marina Alves Costa"
        assert "dev" in profile.area_presets
        assert "suporte" in profile.area_presets
        assert "suporte-en" in profile.area_presets

        en_preset = profile.area_presets["suporte-en"]
        assert en_preset.locale == "en-US"
        assert en_preset.show_age is False
        assert en_preset.section_labels.summary == "PROFESSIONAL SUMMARY"
        assert en_preset.section_labels.experience == "EXPERIENCE"

        skills_by_id = {s.id: s for s in profile.skills}
        assert skills_by_id["glpi"].can_appear_on_resume is False
        assert skills_by_id["docker"].can_appear_on_resume is False
        assert skills_by_id["java"].can_appear_on_resume is True
        assert skills_by_id["windows"].can_appear_on_resume is True

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
        assert len(sources.sources) == 2
        assert all(s.respect_robots_txt is True for s in sources.sources)


class TestDomainVeracityInvariants:
    def test_rejects_self_deprecating_labels_in_skill_display_name(self) -> None:
        raw = _load_example("master_profile.example.json")
        raw["skills"][0]["display_name"] = "Java básico"
        with pytest.raises(ValidationError, match="Self-deprecating qualifier"):
            MasterProfile.model_validate(raw)

    @pytest.mark.parametrize(
        "bad_phrase",
        [
            "Atuação com Office (básico) e planilhas.",
            "Conhecimento em nível inicial de redes.",
            "Possui apenas noções de atendimento.",
            "Familiaridade básica com Linux corporativo.",
            "Experience with SQL at basic level.",
        ],
    )
    def test_rejects_self_deprecating_labels_in_bullets_and_summaries(
        self, bad_phrase: str
    ) -> None:
        raw = _load_example("master_profile.example.json")
        raw["experiences"][0]["bullets"][0]["text"] = bad_phrase
        with pytest.raises(ValidationError, match="Self-deprecating qualifier"):
            MasterProfile.model_validate(raw)

    def test_rejects_preparable_gap_skill_in_resume_preset(self) -> None:
        raw = _load_example("master_profile.example.json")
        # 'glpi' has veracity == 'preparable_gap'
        raw["area_presets"]["suporte"]["skill_groups"][0]["skill_ids"].append("glpi")
        with pytest.raises(ValidationError, match="non-resume skill 'glpi'"):
            MasterProfile.model_validate(raw)

    def test_rejects_in_progress_free_course_in_preset(self) -> None:
        raw = _load_example("master_profile.example.json")
        raw["area_presets"]["dev"]["included_course_ids"].append("course_docker_in_progress")
        with pytest.raises(ValidationError, match="non-completed course"):
            MasterProfile.model_validate(raw)

    def test_rejects_reserved_project_in_preset_or_summary_text(self) -> None:
        raw_ref = _load_example("master_profile.example.json")
        raw_a = copy.deepcopy(raw_ref)
        raw_a["area_presets"]["dev"]["included_project_ids"].append("proj_atlas_reserved")
        with pytest.raises(ValidationError, match="unapproved/reserved project"):
            MasterProfile.model_validate(raw_a)

        raw_b = copy.deepcopy(raw_ref)
        raw_b["area_presets"]["dev"]["default_summary"] += " Criadora do projeto AtlasFlow Sandbox."
        with pytest.raises(ValidationError, match="forbidden term 'AtlasFlow Sandbox'"):
            MasterProfile.model_validate(raw_b)

    def test_rejects_non_https_urls_and_invalid_cnpj_or_schedule(self) -> None:
        sources_raw = _load_example("sources.example.json")
        sources_raw["sources"][1]["feeds"] = ["http://insecure.example.com/feed"]
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
        # Legacy archive is present locally, so dynamic secrets must be loaded in memory
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

        # Ensure redacted detail never echoes the actual secret string
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

        # Copy scanner script into isolated temporary Git repo
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
        subprocess.run(
            ["git", "config", "user.email", "dev@example.com"],
            cwd=temp_repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Demo Dev"],
            cwd=temp_repo,
            check=True,
        )

        installed = install_pre_commit_hook(temp_repo)
        assert installed is True
        assert is_hook_installed(temp_repo) is True
        hook_content = (temp_repo / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
        assert HOOK_MARKER in hook_content

        # 1. Clean file commit must succeed
        clean_file = temp_repo / "README.md"
        clean_file.write_text("# Clean Public Repo\nContact: demo@example.com\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md", "scripts/check_privacy.py"],
            cwd=temp_repo,
            check=True,
        )
        ok_commit = subprocess.run(
            ["git", "commit", "-m", "chore: initial clean commit"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
        )
        assert ok_commit.returncode == 0, ok_commit.stderr

        # 2. Commit staging a private blocked company or Gmail address must be rejected!
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
        custom_marker = profile_path.read_text(encoding="utf-8").replace(
            "Marina Alves Costa", "Custom Candidate"
        )
        profile_path.write_text(custom_marker, encoding="utf-8")

        created_second = bootstrap_demo_workspace(REPO_ROOT, data_dir=target_data)
        assert created_second == []
        assert "Custom Candidate" in profile_path.read_text(encoding="utf-8")
