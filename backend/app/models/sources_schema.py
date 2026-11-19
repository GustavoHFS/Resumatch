"""Pydantic v2 discriminated-union schemas for public job discovery sources (v2.2)."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import Field, field_validator, model_validator

from app.models.profile_schema import HTTPS_URL_PATTERN, StrictModel

SourcePriority = Literal["high", "secondary"]


def validate_https_only(url: str) -> str:
    if not HTTPS_URL_PATTERN.match(url):
        raise ValueError(f"Source URL must use https:// (got '{url}')")
    return url


class SeedPageEntry(StrictModel):
    url: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    section_heading: str | None = None

    @field_validator("url")
    @classmethod
    def check_url(cls, value: str) -> str:
        return validate_https_only(value)


class SuspendedPageEntry(StrictModel):
    url: str
    reason: str = Field(min_length=5, max_length=300)

    @field_validator("url")
    @classmethod
    def check_url(cls, value: str) -> str:
        return validate_https_only(value)


class BaseSourceConfig(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    display_name: str = Field(default="", max_length=140)
    category: str = Field(default="Geral", max_length=80)
    enabled: bool = True
    priority: SourcePriority = "high"
    respect_robots_txt: Literal[True] = True
    rate_limit_seconds: float = Field(default=0.35, ge=0.1, le=30.0)
    notes: str | None = Field(default=None, max_length=500)


class RssLocalSourceConfig(BaseSourceConfig):
    type: Literal["rss_local"]
    feeds: list[str] = Field(min_length=1)
    seed_pages: list[SeedPageEntry] = Field(default_factory=list)
    max_items: int = Field(default=20, ge=1, le=100)

    @field_validator("feeds")
    @classmethod
    def check_feeds(cls, value: list[str]) -> list[str]:
        return [validate_https_only(url) for url in value]


class PublicPagesSourceConfig(BaseSourceConfig):
    type: Literal["public_pages"]
    pages: list[SeedPageEntry] = Field(min_length=1)
    suspended_pages: list[SuspendedPageEntry] = Field(default_factory=list)
    max_items: int = Field(default=10, ge=1, le=50)


class SitemapSourceConfig(BaseSourceConfig):
    type: Literal["sitemap_jsonld"]
    sitemaps: list[str] = Field(min_length=1)
    allowed_city_slugs: list[str] = Field(min_length=1)
    recency_days: int = Field(default=14, ge=1, le=180)
    max_items: int = Field(default=15, ge=1, le=100)

    @field_validator("sitemaps")
    @classmethod
    def check_sitemaps(cls, value: list[str]) -> list[str]:
        return [validate_https_only(url) for url in value]


class GreenhouseSourceConfig(BaseSourceConfig):
    type: Literal["greenhouse"]
    board_token: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    company_name: str = Field(min_length=2, max_length=120)
    entry_level_local_only: bool = True
    max_items: int = Field(default=12, ge=1, le=100)


class LeverSourceConfig(BaseSourceConfig):
    type: Literal["lever"]
    board_token: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    company_name: str = Field(min_length=2, max_length=120)
    entry_level_local_only: bool = True
    max_items: int = Field(default=12, ge=1, le=100)


class AshbySourceConfig(BaseSourceConfig):
    type: Literal["ashby"]
    board_slug: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$")
    company_name: str = Field(min_length=2, max_length=120)
    entry_level_local_only: bool = True
    max_items: int = Field(default=12, ge=1, le=100)


class RecruiteeSourceConfig(BaseSourceConfig):
    type: Literal["recruitee"]
    company_slug: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    company_name: str = Field(min_length=2, max_length=120)
    entry_level_local_only: bool = True
    max_items: int = Field(default=12, ge=1, le=100)


class GupyApiSourceConfig(BaseSourceConfig):
    type: Literal["gupy_api"]
    queries: list[str] = Field(default_factory=list, max_length=25)
    search_urls: list[str] = Field(default_factory=list, max_length=15)
    career_subdomain: str | None = Field(default=None, max_length=80)
    career_page_name: str | None = Field(default=None, max_length=120)
    company_name: str | None = Field(default=None, max_length=120)
    state_filter: str | None = Field(default=None, max_length=60)
    include_remote: bool = True
    max_items: int = Field(default=20, ge=1, le=100)

    @field_validator("search_urls")
    @classmethod
    def check_search_urls(cls, value: list[str]) -> list[str]:
        return [validate_https_only(url) for url in value]


class GitHubIssuesSourceConfig(BaseSourceConfig):
    type: Literal["github_issues"]
    repos: list[str] = Field(min_length=1, max_length=12)
    per_repo: int = Field(default=12, ge=1, le=40)
    max_items: int = Field(default=24, ge=1, le=100)

    @field_validator("repos")
    @classmethod
    def check_repos(cls, value: list[str]) -> list[str]:
        for repo in value:
            if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
                raise ValueError(f"Invalid GitHub repository slug '{repo}'")
        return value


class InhireSourceConfig(BaseSourceConfig):
    type: Literal["inhire"]
    tenant_slug: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    company_name: str = Field(min_length=2, max_length=120)
    career_url: str | None = None
    max_items: int = Field(default=12, ge=1, le=60)

    @field_validator("career_url")
    @classmethod
    def check_career_url(cls, value: str | None) -> str | None:
        return validate_https_only(value) if value else None


class WorkableSourceConfig(BaseSourceConfig):
    type: Literal["workable"]
    account_slug: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    company_name: str = Field(min_length=2, max_length=120)
    max_items: int = Field(default=12, ge=1, le=60)


class HtmlBoardSourceConfig(BaseSourceConfig):
    type: Literal["html_board"]
    search_urls: list[str] = Field(min_length=1, max_length=15)
    company_fallback: str | None = Field(default=None, max_length=120)
    location_fallback: str | None = Field(default=None, max_length=120)
    requires_login_for_apply: bool = False
    fetch_details: bool = False
    web_search_fallback: bool = True
    max_items: int = Field(default=15, ge=1, le=80)

    @field_validator("search_urls")
    @classmethod
    def check_search_urls(cls, value: list[str]) -> list[str]:
        return [validate_https_only(url) for url in value]


class WebSearchSourceConfig(BaseSourceConfig):
    type: Literal["web_search"]
    queries: list[str] = Field(default_factory=list, max_length=20)
    target_domains: list[str] = Field(default_factory=list, max_length=15)
    locations: list[str] = Field(
        default_factory=lambda: ["Brasil", "Remoto"],
        max_length=10,
    )
    company_fallback: str | None = Field(default=None, max_length=120)
    requires_login_for_apply: bool = True
    max_items: int = Field(default=20, ge=1, le=80)


class EmpregareMcpSourceConfig(BaseSourceConfig):
    type: Literal["empregare_mcp"]
    endpoint: str = "https://www.empregare.com/api/mcp"
    derive_queries_from_catalog: bool = True
    max_queries: int = Field(default=9, ge=1, le=20)
    max_items: int = Field(default=12, ge=1, le=40)
    public_detail_urls_by_id: dict[str, str] = Field(default_factory=dict)

    @field_validator("endpoint")
    @classmethod
    def check_endpoint(cls, value: str) -> str:
        return validate_https_only(value)


class PlaywrightPublicBoardConfig(BaseSourceConfig):
    type: Literal["playwright_public"]
    listing_url: str
    company_name: str = Field(min_length=2, max_length=120)
    max_items: int = Field(default=10, ge=1, le=30)

    @field_validator("listing_url")
    @classmethod
    def check_listing_url(cls, value: str) -> str:
        return validate_https_only(value)


JobSourceEntry = Annotated[
    Union[
        RssLocalSourceConfig,
        PublicPagesSourceConfig,
        SitemapSourceConfig,
        GreenhouseSourceConfig,
        LeverSourceConfig,
        AshbySourceConfig,
        RecruiteeSourceConfig,
        GupyApiSourceConfig,
        GitHubIssuesSourceConfig,
        InhireSourceConfig,
        WorkableSourceConfig,
        HtmlBoardSourceConfig,
        WebSearchSourceConfig,
        EmpregareMcpSourceConfig,
        PlaywrightPublicBoardConfig,
    ],
    Field(discriminator="type"),
]


class JobSourcesConfig(StrictModel):
    schema_version: Literal["1.0", "2.1", "2.2"] = "2.2"
    sources: list[JobSourceEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_source_ids(self) -> JobSourcesConfig:
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate source id in JobSourcesConfig")
        return self
