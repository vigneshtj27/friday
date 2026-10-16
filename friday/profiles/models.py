from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def validate_slug(value: str, what: str = "id") -> str:
    if not SLUG_RE.match(value):
        raise ValueError(f"invalid {what} {value!r}: use 1-63 chars of a-z, 0-9, '-', '_'")
    return value


def validate_domain(value: str) -> str:
    value = value.strip().lower().lstrip(".")
    if not re.match(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)*[a-z0-9-]{1,63}$", value):
        raise ValueError(f"invalid domain {value!r}")
    return value


class Site(BaseModel):
    id: str
    name: str
    login_url: str | None = None
    domains: list[str] = Field(min_length=1)
    otp_domains: list[str] = Field(default_factory=list)
    # Sender addresses (or @domains) that email this site's one-time codes.
    otp_senders: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _v_id(cls, v: str) -> str:
        if v == "system":
            raise ValueError("'system' is reserved")
        return validate_slug(v, "site id")

    @field_validator("domains", "otp_domains")
    @classmethod
    def _v_domains(cls, v: list[str]) -> list[str]:
        return [validate_domain(d) for d in v]

    @property
    def all_domains(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*self.domains, *self.otp_domains]))


class ProxyConfig(BaseModel):
    server: str  # http://host:port | socks5://host:port
    username_secret: str | None = None  # vault names, never literal credentials
    password_secret: str | None = None
    bypass: str | None = None

    @field_validator("server")
    @classmethod
    def _v_server(cls, v: str) -> str:
        if not re.match(r"^(https?|socks5)://[^\s/@]+(:\d+)?/?$", v):
            raise ValueError("proxy server must look like scheme://host:port with no credentials in it")
        return v


class ProfileConfig(BaseModel):
    locale: str = "en-US"
    timezone: str | None = None  # IANA; must match egress IP
    window_width: int = 1920
    window_height: int = 1080
    driver: str | None = None  # override settings.driver for this profile
    proxy: ProxyConfig | None = None


class Profile(BaseModel):
    id: str
    site_id: str
    account: str
    config: ProfileConfig
    status: str = "new"  # new | seeded | logged_in | expired | needs_attention
    fingerprint: dict[str, Any] | None = None
    created_at: str
    last_used_at: str | None = None

    @property
    def secret_prefix(self) -> str:
        return f"{self.site_id}/{self.account}/"

    @staticmethod
    def make_id(site_id: str, account: str) -> str:
        # '.' is not a slug character, so ids can't collide across (site, account) pairs.
        return f"{site_id}.{account}"
