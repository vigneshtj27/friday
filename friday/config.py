from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FRIDAY_", env_file=".env", extra="ignore")

    # storage
    data_dir: Path = Path("./data")
    profiles_dir: Path | None = None
    db_path: Path | None = None
    volume_check: Literal["require", "warn"] = "require"
    volume_encrypted_assert: bool = False

    # master key
    master_key: SecretStr | None = None
    keyring_service: str = "friday"

    # browser
    # patchright is the default: real Google Chrome without the navigator.webdriver automation
    # flag that no ordinary user's browser carries. It is not fingerprint spoofing (see
    # docs/decisions.md). A profile with its own driver set overrides this; playwright and cdp
    # stay available by setting FRIDAY_DRIVER or a per-profile driver.
    driver: Literal["playwright", "patchright", "cdp"] = "patchright"
    # enforce: a sign-in run whose prerequisites fail (friday/preflight.py) doesn't touch the site.
    # warn: log and go ahead. off: skip the checks.
    preflight: Literal["enforce", "warn", "off"] = "enforce"
    chrome_channel: str = "chrome"
    chrome_executable: Path | None = None
    # Chrome's own sandbox. Needs user namespaces; in Docker that means a seccomp
    # profile that allows them (see docs/decisions.md). Turning it off is logged.
    chrome_sandbox: bool = True
    display_backend: Literal["auto", "xvfb", "native"] = "auto"
    screen_width: int = 1920
    screen_height: int = 1080
    default_locale: str = "en-US"
    default_timezone: str | None = None
    vnc_port_base: int = 5900

    # model
    model: str = "claude-opus-5-5"
    model_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    model_fallbacks: bool = True
    # Writes private to the owner (own sheet, drafts, memory) run without a card unless this is on.
    # Sends, shares, spending and anything other people see always need the owner's approval.
    approve_private_writes: bool = False
    planner_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    chat_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"  # the chat assistant: quick replies
    # Cumulative model-spend cap across all callers, enforced in code (friday/usage.py). 0 disables
    # it. At warn_fraction of the cap friday warns once in chat; at the cap it hard-stops new model
    # calls (a plan in flight ends as blocked, not half-done). The window resets the running total.
    model_budget_usd: float = 0.0
    model_budget_window: Literal["day", "month", "total"] = "month"
    model_budget_warn_fraction: float = 0.8

    # agent limits and pacing
    max_steps: int = 40
    max_wall_seconds: int = 900
    max_cost_usd: float = 3.0
    action_delay_ms: tuple[int, int] = (400, 1600)
    step_screenshots: bool = True
    approval_ttl_seconds: int = 600
    max_browsers: int = 2
    site_min_login_interval_seconds: int = 60
    site_backoff_base_seconds: int = 900
    site_backoff_max_seconds: int = 86400

    # statements
    default_currency: str = "INR"

    # OTP
    otp_timeout_seconds: int = 300
    email_otp_timeout_seconds: int = 90
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    sms_webhook_token: SecretStr | None = None

    # notifications: none | telegram | ntfy | whatsapp (comma-separate for several)
    notify: str = "none"
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str | None = None
    ntfy_reply_topic: str | None = None
    ntfy_token: SecretStr | None = None
    whatsapp_token: SecretStr | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_to: str | None = None
    whatsapp_verify_token: SecretStr | None = None
    whatsapp_app_secret: SecretStr | None = None

    # diagnosis
    geoip_url: str | None = "https://ipinfo.io/json"
    # Neutral pages the browser itself loads to check for leaks (friday/browsercheck.py). Never a bank.
    ip_echo_url: str = "https://api.ipify.org/?format=json"
    ip6_echo_url: str | None = "https://api6.ipify.org/?format=json"
    webrtc_stun_url: str | None = "stun:stun.l.google.com:19302"
    diag_alt_proxy: str | None = None

    # web
    bind_host: str = "127.0.0.1"
    port: int = 8765
    public_base_url: str = "http://127.0.0.1:8765"
    admin_password_hash: SecretStr | None = None
    cookie_secure: bool = False
    session_ttl_seconds: int = 12 * 3600
    handoff_ttl_seconds: int = 1800  # extended while the remote view is connected

    @property
    def resolved_profiles_dir(self) -> Path:
        return (self.profiles_dir or self.data_dir / "profiles").resolve()

    @property
    def resolved_db_path(self) -> Path:
        return (self.db_path or self.data_dir / "friday.db").resolve()

    def check_safe_environment(self) -> None:
        """Refuse configurations that would leak secrets into logs."""
        debug = os.environ.get("DEBUG", "")
        if "pw:" in debug or debug.strip() == "*":
            # Playwright's pw:api / pw:protocol logs print fill() values verbatim.
            raise ConfigError("DEBUG enables Playwright protocol/API logging, which would log secrets. Unset it.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
