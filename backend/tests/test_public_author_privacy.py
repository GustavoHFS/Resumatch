"""Public credits must not disable private contact or path detection."""

from pathlib import Path

import pytest

from scripts.check_privacy import (
    AUTHOR_CREDIT_FILES, PUBLIC_AUTHOR, PUBLIC_AUTHOR_SHORT,
    DynamicSecret, check_path_policy, scan_file_contents,
)


@pytest.mark.parametrize("credit_path", [*sorted(AUTHOR_CREDIT_FILES), "docs/index.html"])
def test_author_credit_allows_only_name_not_contacts(tmp_path: Path, credit_path: str) -> None:
    file = tmp_path / "credit.txt"
    secrets = [DynamicSecret("PRIVATE_CANDIDATE_NAME", PUBLIC_AUTHOR),
               DynamicSecret("PRIVATE_CANDIDATE_NAME", PUBLIC_AUTHOR_SHORT)]
    file.write_text(PUBLIC_AUTHOR + "\n" + PUBLIC_AUTHOR_SHORT, encoding="utf-8")
    assert scan_file_contents(file, Path(credit_path), secrets) == []
    email = "private.person" + "@" + "gmail.com"
    phone = "(81) " + "98877-6655"
    address = "Rua Fictícia de Teste, 123"
    file.write_text(f"{PUBLIC_AUTHOR}\n{email}\n{phone}\n{address}", encoding="utf-8")
    secrets.append(DynamicSecret("PRIVATE_ADDRESS", address))
    categories = {v.category for v in scan_file_contents(file, Path(credit_path), secrets)}
    assert {"PERSONAL_EMAIL", "BR_PHONE_NUMBER", "PRIVATE_ADDRESS"} <= categories


def test_author_name_elsewhere_and_all_private_paths_remain_blocked(tmp_path: Path) -> None:
    file = tmp_path / "profile.txt"
    file.write_text(PUBLIC_AUTHOR, encoding="utf-8")
    assert scan_file_contents(file, Path("examples/profile.json"), [])
    assert check_path_policy(Path("user_data/.gitkeep"))
    assert check_path_policy(Path("user_data/profile/master_profile.json"))
    assert check_path_policy(Path(".env"))
