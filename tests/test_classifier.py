from __future__ import annotations

import pytest

from friday.login.classifier import LoginState, Observation, classify
from friday.login.diagnosis import fingerprint_findings, rank_causes


def inp(type_="text", name="", hint="", autocomplete=None, maxlength=None):
    return {"tag": "input", "type": type_, "name": name, "fieldHint": hint, "autocomplete": autocomplete, "maxlength": maxlength}


USER = inp("text", "Customer ID", "username u")
PW = inp("password", "Password", "password p")


def obs(text="", inputs=(), **kw):
    return Observation(kw.pop("url", "https://bank.example/login"), kw.pop("title", "Bank"), text, list(inputs), **kw)


@pytest.mark.parametrize(
    "o,expected",
    [
        (obs("Sign in to online banking", [USER, PW]), LoginState.LOGIN_FORM),
        (obs("Welcome back, Alice. Account summary. Sign out"), LoginState.LOGGED_IN),
        (obs("Incorrect Customer ID or password. Sign in", [USER, PW]), LoginState.CREDENTIALS_REJECTED),
        (obs("Access Denied. We detected unusual activity from your network.", [], status=403), LoginState.ACCESS_DENIED_OR_BLOCKED),
        (obs("Access Denied. We detected unusual activity from your network. Reference ID: 18.af2c.9d31"), LoginState.ACCESS_DENIED_OR_BLOCKED),
        (obs("Please verify you are human. Sign in", [USER, PW], has_captcha=True), LoginState.CAPTCHA),
        (obs("We have sent a one-time password to your registered email a***e@example.com.", [inp("text", "One-time code", "code c", "one-time-code")]), LoginState.NEEDS_OTP_EMAIL),
        (obs("We have sent a one-time password by SMS to your mobile number ending 42.", [inp("text", "One-time code", "code", "one-time-code")]), LoginState.NEEDS_OTP_SMS),
        (obs("Enter the 6-digit code from your authenticator app.", [inp("text", "Code", "code")]), LoginState.NEEDS_TOTP),
        (obs("Check your phone. We sent a notification to your app. Approve the sign-in request to continue."), LoginState.NEEDS_APPROVAL_IN_APP),
        (obs("Answer your security question: What was your first pet's name?", [inp("text", "Answer", "answer")]), LoginState.SECURITY_QUESTION),
        (obs("", [], load_error="net::ERR_CONNECTION_REFUSED"), LoginState.PAGE_FAILED),
        (obs("", [], url="chrome-error://chromewebdata/"), LoginState.PAGE_FAILED),
        (obs("Your account has been locked after too many failed attempts.", [USER, PW]), LoginState.ACCESS_DENIED_OR_BLOCKED),
        (obs("Lorem ipsum dolor sit amet, a page about mortgages."), LoginState.UNKNOWN),
    ],
)
def test_rules(o, expected):
    assert classify(o).state == expected


def test_silent_bounce_needs_a_credential_submit():
    o = obs("Sign in to online banking", [USER, PW])
    assert classify(o, just_submitted_credentials=True).state == LoginState.SILENT_BOUNCE
    assert classify(o).state == LoginState.LOGIN_FORM
    err = obs("Incorrect password. Sign in", [USER, PW])
    assert classify(err, just_submitted_credentials=True).state == LoginState.CREDENTIALS_REJECTED


def test_split_code_boxes_count_as_otp():
    boxes = [inp("tel", f"d{i}", "", None, 1) for i in range(6)]
    c = classify(obs("Enter the code we texted to your phone", boxes))
    assert c.state == LoginState.NEEDS_OTP_SMS and "sms" in c.channels


def test_email_and_sms_both_mentioned_reports_channels():
    c = classify(obs("Code sent to your email and mobile ending 42", [inp("text", "OTP", "otp")]))
    assert set(c.channels) == {"email", "sms"}


def test_logged_in_low_confidence_without_hints():
    c = classify(obs("Preferences and options. Sign out"))
    assert c.state == LoginState.LOGGED_IN and c.confidence < 0.7


def test_page_text_cannot_fake_logged_in_while_form_shown():
    # A login page that mentions "sign out" in its footer is still a login form.
    c = classify(obs("Sign in. Remember to sign out on shared computers.", [USER, PW]))
    assert c.state == LoginState.LOGIN_FORM


# ---- diagnosis ranking -------------------------------------------------------


def test_fingerprint_findings_flags_automation_and_ip():
    fp = {"webdriver": True, "headlessUA": False, "window": [1920, 1080, 1920, 960], "plugins": 5, "languages": ["en-US"],
          "timezone": "UTC", "webglRenderer": "ANGLE (Google, SwiftShader)", "language": "en-US"}
    geo = {"timezone": "Asia/Kolkata", "org": "AS16509 Amazon.com, Inc.", "country": "IN"}
    f = " | ".join(x["finding"] for x in fingerprint_findings(fp, geo))
    assert "webdriver" in f and "SwiftShader" in f and "does not match egress IP timezone" in f and "datacenter" in f


def test_rank_unconfirmed_without_experiments():
    report = {"trigger_state": "SILENT_BOUNCE", "capture": {"navigations": [{"status": 200, "set_cookie": False}]},
              "fingerprint_findings": [{"severity": "high", "finding": "navigator.webdriver is true: the page can see the browser is automated"}]}
    causes, nxt = rank_causes(report)
    assert causes[0]["cause"] == "automation_fingerprint"
    assert all(not c["confirmed"] and "unconfirmed" in c["note"] for c in causes)
    assert "by hand" in nxt  # cheapest informative experiment first


def test_manual_login_success_confirms_automation_signature():
    report = {"trigger_state": "SILENT_BOUNCE", "capture": {},
              "experiments": [{"name": "manual", "mode": "submit", "status": "done", "result_state": "LOGGED_IN"}]}
    causes, nxt = rank_causes(report)
    top = causes[0]
    assert top["cause"] == "automation_fingerprint" and top["confirmed"]
    assert "manual" not in nxt.lower() or "by hand" not in nxt


def test_manual_login_failure_points_away_from_automation():
    report = {"trigger_state": "ACCESS_DENIED_OR_BLOCKED", "capture": {"navigations": [{"status": 403}]},
              "experiments": [{"name": "manual", "mode": "submit", "status": "done", "result_state": "ACCESS_DENIED_OR_BLOCKED"},
                              {"name": "alt_proxy", "mode": "probe", "status": "done", "result_state": "LOGIN_FORM"}]}
    causes, _ = rank_causes(report)
    assert causes[0]["cause"] == "ip_reputation"
    auto = next(c for c in causes + [{"cause": "automation_fingerprint", "score": -9}] if c["cause"] == "automation_fingerprint")
    assert auto["score"] < causes[0]["score"]


CONFOUNDED_ENV = {"driver": "playwright", "pinned_webdriver": True, "egress": {"ip": "34.14.129.151", "org": "AS396982 Google LLC", "datacenter": True}}


def test_manual_bounce_under_playwright_is_inconclusive():
    """The live Amex case: a manual bounce in a webdriver-flagged browser doesn't clear automation."""
    manual = {"name": "manual", "mode": "submit", "status": "done", "result_state": "SILENT_BOUNCE"}
    report = {"profile_id": "amex.me", "trigger_state": "SILENT_BOUNCE", "capture": {}, "environment": CONFOUNDED_ENV,
              "experiments": [manual]}
    causes, nxt = rank_causes(report)
    assert manual["verdict"] == "INCONCLUSIVE"
    assert "preflight amex.me" in nxt and "INCONCLUSIVE" in nxt
    auto = next(c for c in causes if c["cause"] == "automation_fingerprint")
    assert auto["score"] > 0 and any("INCONCLUSIVE" in e for e in auto["evidence"])
    assert any("datacenter" in e for c in causes for e in c["evidence"])


def test_manual_bounce_under_patchright_discriminates():
    env = {"driver": "patchright", "pinned_webdriver": False, "egress": {}}
    manual = {"name": "manual", "mode": "submit", "status": "done", "result_state": "SILENT_BOUNCE"}
    causes, nxt = rank_causes({"trigger_state": "SILENT_BOUNCE", "capture": {}, "environment": env, "experiments": [manual]})
    assert "verdict" not in manual and "preflight" not in nxt
    assert any("also fails" in e for c in causes for e in c["evidence"])


def test_report_records_environment(ctx):
    p = ctx.profiles.create("mybank", "me")
    ctx.prefs.set("egress:mybank.me", {"ip": "34.14.129.151", "org": "AS396982 Google LLC", "asn": 396982, "datacenter": True})
    rid = ctx.diagnoser.create(p.id, None, "SILENT_BOUNCE", {"capture": {}})
    env = ctx.diagnoser.get(rid)["environment"]
    assert env["driver"] == ctx.profiles.effective_driver(p.config) and env["egress"]["asn"] == 396982
