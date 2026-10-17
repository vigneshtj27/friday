from __future__ import annotations

import pytest

from friday.config import Settings
from friday.context import AppContext, build_context
from friday.profiles.models import Site
from friday.vault.masking import Redactor
from friday.web.auth import hash_password

TEST_KEY = bytes(range(32))
TEST_PASSWORD = "correct horse battery staple"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        volume_check="require",
        volume_encrypted_assert=True,
        display_backend="native",
        admin_password_hash=hash_password(TEST_PASSWORD),
        public_base_url="http://testserver",
    )


@pytest.fixture
def ctx(settings) -> AppContext:
    c = build_context(settings, master_key=TEST_KEY, redactor=Redactor(), chat_llm_factory=False)
    c.sites.upsert(Site(id="mybank", name="My Bank", login_url="https://login.mybank.example/", domains=["mybank.example"]))
    yield c
    c.db.close()
