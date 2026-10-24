"""Code-enforced action policy. The prompt asks the model to behave; this makes it.

Every tool call is classified before it runs:
  ALLOW           - read-only or clearly part of signing in / filtering / downloading
  NEEDS_APPROVAL  - may spend money, change settings, add a payee, or send data
  DENY            - outside the task's domain allowlist, unknown tool, or leaks a secret

The heuristics are deliberately conservative: when in doubt, ask the owner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from friday.vault.injector import LOCAL_HOSTS, host_allowed
from friday.vault.masking import Redactor


class Verdict(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    SPEND = "spend"  # a money commit: never a generic tap; routed to the spend gate or failed closed
    DENY = "deny"


# Words that mean the control itself commits a payment (not just navigates toward one).
PAY_COMMIT = re.compile(
    r"\b(pay( now)?|place (the )?order|buy( now)?|purchase|checkout|check out|"
    r"complete (the )?(order|purchase|payment)|confirm (and )?pay)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


READ_ONLY_TOOLS = {"read_page", "screenshot", "scroll", "wait_for", "extract_table"}
CONTROL_TOOLS = {"request_otp", "request_human", "request_approval", "finish"}
INTERACT_TOOLS = {"goto", "click", "type", "press", "select", "download_file"}
ALL_TOOLS = READ_ONLY_TOOLS | CONTROL_TOOLS | INTERACT_TOOLS

# Words on a control that mean "this changes something real".
RISKY = re.compile(
    r"\b(pay(ment)?|pay now|transfer|send( money)?|remit|add (a )?(new )?(payee|beneficiary|biller|recipient|card|account)|"
    r"beneficiar(y|ies)|payee|place (the )?order|buy|purchase|checkout|check out|subscribe|upgrade|renew|"
    r"cancel|delete|remove|close (my |the )?account|deactivate|activate|block( card)?|unblock|freeze|unfreeze|"
    r"update|change|edit|modify|save( changes)?|reset|set (a )?(new )?(pin|password|limit)|enable|disable|"
    r"authori[sz]e|approve|confirm( (payment|transfer|order|purchase))?|i agree|agree|accept|consent|sign (agreement|up)|"
    r"invest|redeem|withdraw|deposit|top.?up|recharge|donate|request (a )?(cheque|check|card|loan|limit)|"
    r"apply (for|now)|register|link (account|card)|unlink|opt.?in|opt.?out|send( otp)? to|share|post|publish|reply|"
    r"submit (request|application|complaint|dispute)|dispute|raise|lodge)\b",
    re.IGNORECASE,
)
# Words that are fine on a submit control when the form is an auth form.
AUTH_WORDS = re.compile(
    r"\b(log ?in|sign ?in|logon|log on|next|continue|proceed|verify|submit|validate|confirm|go|ok|enter|"
    r"authenticate|send( (the )?(code|otp))?|resend( (the )?(code|otp))?|get (the )?(code|otp)|trust (this )?(device|browser)|"
    r"remember (me|this device)|not now|skip|later|yes)\b",
    re.IGNORECASE,
)
SAFE_NAV_WORDS = re.compile(
    r"\b(statement|download|export|view|show|search|filter|apply|go|find|history|transactions|details|"
    r"accounts?|summary|home|dashboard|next|previous|prev|more|back|close|dismiss|ok|got it|menu|tab|"
    r"pdf|csv|excel|xls|month|year|period|date|from|to|period)\b",
    re.IGNORECASE,
)
# Links are navigation (GET). Only wording that suggests the link itself commits something needs approval;
# menu links like "Transfer money" are fine because the eventual submit is gated.
DESTRUCTIVE_LINK = re.compile(
    r"\b(cancel|delete|remove|close (my |the )?account|deactivate|unsubscribe|confirm|approve|authori[sz]e|"
    r"i agree|agree|accept|consent|block( card)?|unblock|freeze|stop (payment|cheque|check)|sign out of all)\b",
    re.IGNORECASE,
)
FILTER_FIELD_TYPES = {"date", "month", "week", "datetime-local", "select", "radio", "checkbox", "search", "number"}
LOGIN_HINT = re.compile(r"(user|login|log ?in|sign ?in|email|e-mail|customer|client|member|account|id|phone|mobile|otp|code|pin)", re.I)
DATE_HINT = re.compile(r"(date|from|to|start|end|period|month|year|day)", re.I)


def form_kind(form: dict[str, Any] | None) -> str:
    """auth | filter | search | other | none"""
    if not form:
        return "none"
    if form.get("hasPassword") or form.get("hasOtp"):
        return "auth"
    types = [t for t in form.get("fieldTypes", []) if t not in ("submit", "button", "reset", "image")]
    names = " ".join(form.get("fieldNames", []))
    text = (form.get("text") or "") + " " + names
    if types and all(t in FILTER_FIELD_TYPES for t in types):
        return "search" if "search" in types else "filter"
    free_text = [t for t in types if t in ("text", "email", "tel", "textarea", "")]
    if (
        len(free_text) <= 1
        and re.search(r"\b(log ?in|sign ?in|log ?on|sign-in)\b", text, re.I)
        and LOGIN_HINT.search(names)
    ):
        return "auth"  # username-first step of a multi-step login
    if free_text and all(DATE_HINT.search(n) for n in form.get("fieldNames", []) if n) and re.search(
        r"(statement|transaction|history|period|date)", text, re.I
    ):
        return "filter"
    if len(types) == 1 and types[0] in ("text", "search") and re.search(r"search|find|query", text, re.I):
        return "search"
    return "other"


@dataclass
class PolicyContext:
    allowed_domains: tuple[str, ...]
    redactor: Redactor


def url_allowed(url: str, domains: tuple[str, ...]) -> bool:
    p = urlparse(url)
    if p.scheme == "about":
        return True
    if p.scheme == "https":
        return host_allowed(p.hostname, domains)
    if p.scheme == "http" and p.hostname in LOCAL_HOSTS:
        return host_allowed(p.hostname, domains)
    return False


class ActionPolicy:
    def __init__(self, ctx: PolicyContext):
        self.ctx = ctx

    def decide(self, tool: str, args: dict[str, Any], element: dict[str, Any] | None = None) -> Decision:
        if tool not in ALL_TOOLS:
            return Decision(Verdict.DENY, f"unknown tool {tool!r}")
        if tool in READ_ONLY_TOOLS or tool in CONTROL_TOOLS:
            return Decision(Verdict.ALLOW, "read-only/control")
        if tool == "goto":
            url = str(args.get("url", ""))
            if not url_allowed(url, self.ctx.allowed_domains):
                return Decision(Verdict.DENY, f"{urlparse(url).hostname or url!r} is not on this task's domain allowlist")
            return Decision(Verdict.ALLOW, "allowlisted navigation")
        if tool == "download_file":
            if element and RISKY.search(element.get("name") or "") and not SAFE_NAV_WORDS.search(element.get("name") or ""):
                return Decision(Verdict.NEEDS_APPROVAL, f"download control is labelled {element.get('name')!r}")
            return Decision(Verdict.ALLOW, "download")
        if element is None and tool in ("click", "type", "select"):
            return Decision(Verdict.DENY, "unknown element ref; call read_page first")
        if tool == "click":
            return self._click(element or {})
        if tool == "press":
            key = str(args.get("key", ""))
            if key.lower() in ("enter", "numpadenter") and element and element.get("form"):
                return self._submit(element, f"Enter in {element.get('name')!r}")
            return Decision(Verdict.ALLOW, "key press")
        if tool == "select":
            kind = form_kind((element or {}).get("form"))
            if kind == "other" and RISKY.search(" ".join([(element or {}).get("name") or "", str(args.get("option", ""))])):
                return Decision(Verdict.NEEDS_APPROVAL, "selection in a form that may change something")
            return Decision(Verdict.ALLOW, "selection")
        if tool == "type":
            return self._type(args, element or {})
        return Decision(Verdict.DENY, "unhandled")

    def _click(self, el: dict[str, Any]) -> Decision:
        name = el.get("name") or ""
        if el.get("href") and not str(el["href"]).startswith("javascript:"):
            if not url_allowed(el["href"], self.ctx.allowed_domains):
                return Decision(Verdict.DENY, f"link leaves the allowlist ({urlparse(el['href']).hostname})")
            if DESTRUCTIVE_LINK.search(name):
                return Decision(Verdict.NEEDS_APPROVAL, f"link {name!r} may commit a change by itself")
            return Decision(Verdict.ALLOW, "allowlisted link")
        if el.get("isSubmit"):
            return self._submit(el, f"submit {name!r}")
        if el.get("type") in ("checkbox", "radio") and RISKY.search(name):
            kind = form_kind(el.get("form"))
            if kind in ("auth",) and re.search(r"remember|trust|keep me", name, re.I):
                return Decision(Verdict.ALLOW, "remember-this-device on sign-in")
            return Decision(Verdict.NEEDS_APPROVAL, f"checkbox {name!r} gives consent or changes a setting")
        if PAY_COMMIT.search(name):
            return Decision(Verdict.SPEND, f"control {name!r} commits a payment")
        if RISKY.search(name):
            kind = form_kind(el.get("form"))
            if kind == "auth" and AUTH_WORDS.search(name) and not re.search(r"pay|transfer|payee|beneficiar|order|buy", name, re.I):
                return Decision(Verdict.ALLOW, "sign-in control")
            return Decision(Verdict.NEEDS_APPROVAL, f"control {name!r} may change something")
        return Decision(Verdict.ALLOW, "non-mutating control")

    def _submit(self, el: dict[str, Any], what: str) -> Decision:
        name = el.get("name") or ""
        kind = form_kind(el.get("form"))
        if PAY_COMMIT.search(name):
            return Decision(Verdict.SPEND, f"{what} commits a payment")
        if re.search(r"pay|transfer|payee|beneficiar|order|buy|purchase|delete|close account", name, re.I):
            return Decision(Verdict.NEEDS_APPROVAL, f"{what} looks like a money or account change")
        if kind == "auth":
            return Decision(Verdict.ALLOW, f"{what} in a sign-in form")
        if kind in ("filter", "search"):
            return Decision(Verdict.ALLOW, f"{what} in a {kind} form")
        return Decision(Verdict.NEEDS_APPROVAL, f"{what} submits a form that may change something or send data")

    def _type(self, args: dict[str, Any], el: dict[str, Any]) -> Decision:
        text = str(args.get("text", ""))
        if text.startswith("secret:"):
            return Decision(Verdict.ALLOW, "secret reference (checked by the injector)")
        if self.ctx.redactor.redact(text) != text:
            return Decision(Verdict.DENY, "literal text contains a stored secret; use a secret: reference")
        if el.get("type") == "password":
            return Decision(Verdict.DENY, "password fields only accept secret: references")
        kind = form_kind(el.get("form"))
        hint = " ".join(filter(None, [el.get("name"), el.get("fieldHint")]))
        if kind in ("filter", "search"):
            return Decision(Verdict.ALLOW, f"typing into a {kind} form")
        if el.get("type") in FILTER_FIELD_TYPES or DATE_HINT.search(hint):
            return Decision(Verdict.ALLOW, "date/filter field")
        if kind == "auth" and LOGIN_HINT.search(hint):
            return Decision(Verdict.ALLOW, "sign-in field")
        if kind == "none" and re.search(r"search|find|filter", hint, re.I):
            return Decision(Verdict.ALLOW, "standalone search box")
        return Decision(Verdict.NEEDS_APPROVAL, f"typing free text into {hint or 'a field'!r} may send data")
