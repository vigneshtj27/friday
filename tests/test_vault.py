from __future__ import annotations

import logging

import pytest

from friday.vault.crypto import Cipher, DecryptionError, decode_key, generate_key_b64
from friday.vault.masking import RedactingFilter, Redactor
from friday.vault.store import InvalidSecretName, SecretKind, SecretNotFound, SecretRef

from .conftest import TEST_KEY

# ---- crypto ----------------------------------------------------------------


def test_roundtrip_and_aad_binding():
    c = Cipher(TEST_KEY)
    blob = c.encrypt(b"hunter22", aad=b"a/b/password")
    assert b"hunter22" not in blob
    assert c.decrypt(blob, aad=b"a/b/password") == b"hunter22"
    with pytest.raises(DecryptionError):
        c.decrypt(blob, aad=b"a/c/password")  # moved to another name


def test_tamper_and_wrong_key_fail():
    c = Cipher(TEST_KEY)
    blob = bytearray(c.encrypt(b"x" * 20, aad=b"n"))
    blob[-1] ^= 1
    with pytest.raises(DecryptionError):
        c.decrypt(bytes(blob), aad=b"n")
    with pytest.raises(DecryptionError):
        Cipher(bytes(32)).decrypt(c.encrypt(b"y", aad=b"n"), aad=b"n")


def test_subkeys_are_independent():
    c = Cipher(TEST_KEY)
    blob = c.subcipher("secrets").encrypt(b"v", aad=b"n")
    with pytest.raises(DecryptionError):
        c.subcipher("files").decrypt(blob, aad=b"n")


def test_key_encoding():
    assert len(decode_key(generate_key_b64())) == 32
    assert "redacted" in repr(Cipher(TEST_KEY))


# ---- store -----------------------------------------------------------------


def test_store_never_exposes_values_except_reveal(ctx):
    ctx.vault.put("mybank/me/password", "S3cret-Pa55!", kind=SecretKind.PASSWORD)
    raw = ctx.db.query_one("SELECT ciphertext FROM secrets")["ciphertext"]
    assert b"S3cret-Pa55!" not in raw
    metas = ctx.vault.list()
    assert [m.name for m in metas] == ["mybank/me/password"]
    assert "S3cret" not in repr(metas)
    assert ctx.vault.reveal("mybank/me/password", purpose="test") == "S3cret-Pa55!"


def test_reveal_is_audited_by_name_only(ctx):
    ctx.vault.put("mybank/me/password", "S3cret-Pa55!")
    ctx.vault.reveal("mybank/me/password", purpose="fill@login.mybank.example")
    entries = ctx.audit.list()
    uses = [e for e in entries if e.action == "secret.use"]
    assert uses and uses[0].target == "mybank/me/password"
    dump = repr(entries)
    assert "S3cret-Pa55!" not in dump
    assert ctx.audit.verify_chain()


def test_audit_chain_detects_tampering(ctx):
    ctx.audit.record("a", x=1)
    ctx.audit.record("b", x=2)
    assert ctx.audit.verify_chain()
    ctx.db.execute("UPDATE audit_log SET details_json = '{\"x\":3}' WHERE action = 'a'")
    assert not ctx.audit.verify_chain()


def test_missing_and_invalid_names(ctx):
    with pytest.raises(SecretNotFound):
        ctx.vault.reveal("mybank/me/nope", purpose="t")
    for bad in ["", "UPPER/x", "../etc", "a//b", "a/b/", "/abs"]:
        with pytest.raises(InvalidSecretName):
            ctx.vault.put(bad, "value")


def test_delete_prefix_scoped(ctx):
    ctx.vault.put("mybank/me/password", "aaa111")
    ctx.vault.put("mybank/me/username", "me@example.com")
    ctx.vault.put("mybank/spouse/password", "bbb222")
    assert ctx.vault.delete_prefix("mybank/me/") == 2
    assert [m.name for m in ctx.vault.list()] == ["mybank/spouse/password"]


def test_secret_ref_parsing():
    assert SecretRef.parse("secret:mybank/me/password") == SecretRef("mybank/me/password")
    assert SecretRef.parse("hunter2") is None
    with pytest.raises(InvalidSecretName):
        SecretRef.coerce("hunter2")


# ---- masking ---------------------------------------------------------------


def test_redactor_masks_all_encodings():
    r = Redactor()
    r.register("mybank/me/password", "p@ss word&1")
    text = "typed p@ss word&1, url p%40ss%20word%261, form p%40ss+word%261"
    out = r.redact(text)
    assert "p@ss" not in out and "p%40ss" not in out
    assert out.count("[secret:mybank/me/password]") == 3


def test_redactor_nested_and_forget():
    r = Redactor()
    r.register("n", "topsecret")
    assert r.redact_obj({"a": ["x topsecret y", {"topsecret": 1}]}) == {
        "a": ["x [secret:n] y", {"[secret:n]": 1}]
    }
    r.forget("n")
    assert r.redact("topsecret") == "topsecret"


def test_redactor_refuses_tiny_values():
    r = Redactor()
    assert r.register("pin", "12") is False
    assert r.redact("a12b") == "a12b"


def test_values_registered_on_put_and_reveal(ctx):
    ctx.vault.put("mybank/me/password", "Put-Value-1")
    assert ctx.redactor.redact("Put-Value-1") == "[secret:mybank/me/password]"


def test_logging_filter_redacts_messages_args_and_tracebacks():
    r = Redactor()
    r.register("k", "zzsecretzz")
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    h = Capture()
    h.addFilter(RedactingFilter(r))
    logger = logging.getLogger("friday.test.redact")
    logger.addHandler(h)
    logger.propagate = False
    try:
        logger.warning("value=%s", "zzsecretzz")
        try:
            raise ValueError("bad zzsecretzz")
        except ValueError:
            logger.exception("boom")
    finally:
        logger.removeHandler(h)
    joined = "\n".join(records)
    assert "zzsecretzz" not in joined
    assert "[secret:k]" in joined


def test_redactor_keeps_secret_names_readable():
    r = Redactor()
    r.register("bank/alice/username", "alice")
    r.add_names("bank/alice/password")
    out = r.redact("typed secret:bank/alice/password for alice")
    assert out == "typed secret:bank/alice/password for [secret:bank/alice/username]"


def test_redactor_does_not_exempt_fake_secret_refs():
    r = Redactor()
    r.register("bank/alice/password", "hunter2pass")
    assert "hunter2pass" not in r.redact("leak: secret:hunter2pass")


def test_redactor_keeps_known_profile_ids_and_trailing_punctuation():
    r = Redactor()
    r.register("bank/alice/username", "alice")
    r.add_names("bank.alice", "bank/alice/password")
    assert r.redact("profile bank.alice: typed secret:bank/alice/password.") == "profile bank.alice: typed secret:bank/alice/password."
    assert r.redact("user alice logged in") == "user [secret:bank/alice/username] logged in"
    assert r.redact("other.alice") == "other.[secret:bank/alice/username]"  # unknown look-alike stays redacted
