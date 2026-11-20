"""The portfolio demo works offline, without app startup or personal data access."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("width", [1440, 390])
def test_offline_showcase_complete_workflow(width: int, tmp_path: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            errors: list[str] = []
            external_requests: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: external_requests.append(request.url)
                    if request.url.startswith(("http:", "https:")) else None)
            page.goto((ROOT / "docs/index.html").as_uri())
            expect(page.locator(".job-row")).to_have_count(4)
            page.locator("#level").select_option("Estágio")
            page.locator("#source").select_option("GitHub")
            expect(page.locator(".job-row")).to_have_count(1)
            page.locator("#save-job").click()
            page.locator("#save-job").click()  # Saving twice must not duplicate cards.
            expect(page.locator("#saved-count")).to_have_text("1")
            page.locator("#tab-applications").click()
            expect(page.locator(".application-card")).to_have_count(1)
            expect(page.locator(".column").first).to_contain_text("A preparar")
            page.get_by_role("button", name="Abrir no Estúdio ✨", exact=True).click()
            expect(page.locator("#studio-job option")).to_have_count(2)
            expect(page.locator("#description")).to_have_value(re.compile("APIs REST"))
            for font in ["Libre Franklin", "Inter", "Times New Roman"]:
                page.locator("#font").select_option(font)
                assert font in page.locator("#paper").evaluate("el => getComputedStyle(el).fontFamily")
            page.locator("#mask").click()
            expect(page.locator("#privacy-preview")).to_have_text("[NOME_CANDIDATO]\n[EMAIL]\n[TELEFONE]")
            page.locator("#demo-pdf").click()
            expect(page.locator("#status")).to_contain_text("Adapte a demonstração")
            page.locator("#adapt").click()
            page.locator("#demo-pdf").click()
            expect(page.locator("#status")).to_contain_text("Currículo pronto")
            page.locator("#tab-applications").click()
            expect(page.locator(".column").nth(1)).to_contain_text("PDF demonstrativo vinculado")
            page.locator("#tab-jobs").click()
            page.locator("#search").fill("nenhum-resultado")
            expect(page.locator(".job-row")).to_have_count(0)
            page.locator("#clear-filters").click()
            expect(page.locator(".job-row")).to_have_count(4)
            page.locator("#tab-profile").click()
            expect(page.locator("#panel-profile")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert errors == []
            assert external_requests == []
            page.screenshot(path=str(tmp_path / f"showcase-{width}.png"), full_page=True)
        finally:
            browser.close()
