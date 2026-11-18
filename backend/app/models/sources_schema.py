"""Pydantic v2 discriminated-union schemas for public job discovery sources."""

from __future__ import annotations

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
    enabled: bool = True
    priority: SourcePriority = "high"
    respect_robots_txt: Literal[True] = True
    rate_limit_seconds: float = Field(default=0.5, ge=0.1, le=30.0)
    notes: str | None = Field(default=None, max_length=500)


class RssLocalSourceConfig(BaseSourceConfig):
    type: Literal["rss_local"]
    feeds: list[str] = Field(min_length=1)
    seed_pages: list[SeedPageEntry] = Field(default_factory=list)
    max_items: int = Field(default=24, ge=1, le=100)

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
    recency_days: int = Field(default=30, ge=1, le=180)
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
    max_items: int = Field(default=10, ge=1, le=100)


class LeverSourceConfig(BaseSourceConfig):
    type: Literal["lever"]
    board_token: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    company_name: str = Field(min_length=2, max_length=120)
    entry_level_local_only: bool = True
    max_items: int = Field(default=10, ge=1, le=100)


class EmpregareMcpSourceConfig(BaseSourceConfig):
    type: Literal["empregare_mcp"]
    endpoint: str = "https://www.empregare.com/api/mcp"
    derive_queries_from_catalog: bool = True
    max_queries: int = Field(default=9, ge=1, le=20)
    max_items: int = Field(default=10, ge=1, le=30)
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
        EmpregareMcpSourceConfig,
        PlaywrightPublicBoardConfig,
    ],
    Field(discriminator="type"),
]


class JobSourcesConfig(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    sources: list[JobSourceEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_source_ids(self) -> JobSourcesConfig:
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate source id in JobSourcesConfig")
        return self
