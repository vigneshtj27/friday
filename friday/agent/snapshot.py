"""Compact, model-friendly view of a page: interactive elements with refs + visible text.

Values of password fields, and of any field a secret was injected into, are
never included. The whole snapshot also passes through the redactor.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from friday.agent import isolated

log = logging.getLogger(__name__)

# Runs inside each frame. Tags elements with data-friday-ref and returns
# descriptors plus visible text. `prefix` keeps refs unique across frames.
WALKER_JS = r"""
([prefix, maxText]) => {
  const out = [];
  const OTP_RE = /(otp|one.?time|passcode|verification|security.?code|auth.?code|\bcode\b|pin)/i;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity) === 0) return false;
    if (el.closest('[aria-hidden="true"]')) return false;
    return true;
  };
  const clean = (t, n = 100) => (t || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(e => e.innerText).join(' ');
      if (t.trim()) return clean(t);
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.innerText.trim()) return clean(l.innerText);
    }
    const wrap = el.closest('label');
    if (wrap && wrap.innerText.trim()) return clean(wrap.innerText);
    if (el.placeholder) return clean(el.placeholder);
    if (el.title) return clean(el.title);
    if (el.alt) return clean(el.alt);
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && ['submit', 'button', 'reset'].includes(el.type)) return clean(el.value);
    if (!['input', 'select', 'textarea'].includes(tag)) {
      const t = clean(el.innerText || el.textContent);
      if (t) return t;
      const img = el.querySelector('img[alt]');
      if (img) return clean(img.alt);
    }
    return clean(el.name || el.id || '');
  };
  const roleOf = (el) => {
    const r = el.getAttribute('role');
    if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (['submit', 'button', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'password') return 'password';
      if (t === 'file') return 'file';
      if (['date', 'month', 'week', 'datetime-local'].includes(t)) return 'date';
      if (t === 'search') return 'searchbox';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'button';
  };
  const formInfo = (form) => {
    if (!form) return null;
    const inputs = Array.from(form.querySelectorAll('input, select, textarea')).filter(i => i.type !== 'hidden');
    return {
      action: form.getAttribute('action') ? new URL(form.getAttribute('action'), location.href).href : location.href,
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      hasPassword: inputs.some(i => i.type === 'password'),
      hasOtp: inputs.some(i => i.autocomplete === 'one-time-code' || OTP_RE.test([i.name, i.id, i.placeholder, i.getAttribute('aria-label')].join(' '))),
      // Structural payment-form signal: a card-number / CVV / expiry field, by autocomplete token or
      // by name/placeholder. This does not depend on the button's label, so an oddly-labelled pay
      // button in a card form is still recognised as a money commit.
      hasPayment: inputs.some(i => {
        const ac = (i.autocomplete || '').toLowerCase();
        if (['cc-number', 'cc-csc', 'cc-exp', 'cc-exp-month', 'cc-exp-year'].includes(ac)) return true;
        const hint = [i.name, i.id, i.placeholder, i.getAttribute('aria-label')].filter(Boolean).join(' ').toLowerCase();
        return /\b(card ?number|cardnumber|cc ?number|credit ?card|debit ?card|cvv|cvc|csc|security code|card expir)\b/.test(hint);
      }),
      fieldTypes: inputs.map(i => i.tagName.toLowerCase() === 'input' ? (i.type || 'text').toLowerCase() : i.tagName.toLowerCase()),
      fieldNames: inputs.map(i => clean([i.name, i.id, i.placeholder, i.getAttribute('aria-label')].filter(Boolean).join(' '), 60)),
      text: clean(form.innerText, 300),
    };
  };
  const sel = 'a[href], button, input, select, textarea, summary, [role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=option], [role=combobox], [role=switch], [onclick], [contenteditable=true]';
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (el.type === 'hidden' || !visible(el)) continue;
    const ref = `${prefix}e${++n}`;
    el.setAttribute('data-friday-ref', ref);
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.type || 'text').toLowerCase() : tag;
    const isField = ['input', 'textarea', 'select'].includes(tag) && !['submit', 'button', 'reset', 'image', 'checkbox', 'radio'].includes(type);
    const masked = type === 'password' || el.hasAttribute('data-friday-secret') || el.autocomplete === 'one-time-code';
    let value = null;
    if (isField) {
      if (tag === 'select') value = clean(el.selectedOptions[0]?.innerText || '', 60);
      else value = masked ? (el.value ? '•••• (filled)' : '') : clean(el.value, 80);
    }
    const form = el.form || el.closest('form');
    out.push({
      ref, tag, type, role: roleOf(el), name: labelOf(el), value,
      checked: (type === 'checkbox' || type === 'radio') ? el.checked : null,
      disabled: !!el.disabled, required: !!el.required,
      href: tag === 'a' ? el.href : null,
      autocomplete: el.autocomplete || null,
      inputmode: el.getAttribute('inputmode'), maxlength: el.maxLength > 0 ? el.maxLength : null,
      fieldHint: isField ? clean([el.name, el.id, el.placeholder].filter(Boolean).join(' '), 80) : null,
      isSubmit: (tag === 'button' && (el.type || 'submit') === 'submit' && !!form) || (tag === 'input' && ['submit', 'image'].includes(type)),
      form: formInfo(form),
      options: tag === 'select' ? Array.from(el.options).slice(0, 40).map(o => clean(o.innerText, 60)) : null,
    });
  }
  const text = clean(document.body ? document.body.innerText : '', maxText);
  return {
    url: location.href, title: document.title, text, elements: out,
    hasCaptcha: !!document.querySelector('.g-recaptcha, .h-captcha, .cf-turnstile, iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], iframe[src*="arkoselabs"], iframe[src*="funcaptcha"], [id*="captcha" i], [class*="captcha" i], img[src*="captcha" i]'),
  };
}
"""


@dataclass
class ElementInfo:
    ref: str
    frame_index: int
    data: dict[str, Any]

    @property
    def name(self) -> str:
        return self.data.get("name") or ""

    @property
    def form(self) -> dict[str, Any] | None:
        return self.data.get("form")


@dataclass
class Snapshot:
    url: str
    title: str
    text: str
    elements: list[ElementInfo]
    frame_urls: list[str]
    has_captcha: bool
    frames_text: list[str] = field(default_factory=list)

    def element(self, ref: str) -> ElementInfo | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def render(self, *, max_elements: int = 250) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title}"]
        if len(self.frame_urls) > 1:
            lines.append("Frames: " + ", ".join(self.frame_urls[1:6]))
        lines.append("Interactive elements:")
        for e in self.elements[:max_elements]:
            d = e.data
            bits = [f"[{e.ref}]", d["role"], json.dumps(d["name"])]
            if d.get("value") not in (None, ""):
                bits.append(f"value={json.dumps(d['value'])}")
            if d.get("checked") is not None:
                bits.append("checked" if d["checked"] else "unchecked")
            if d.get("disabled"):
                bits.append("disabled")
            if d.get("required"):
                bits.append("required")
            if d.get("options"):
                bits.append("options=" + json.dumps(d["options"][:15]))
            if d.get("href") and not str(d["href"]).startswith("javascript:"):
                bits.append(f"-> {d['href'][:120]}")
            lines.append("  " + " ".join(bits))
        if len(self.elements) > max_elements:
            lines.append(f"  ... {len(self.elements) - max_elements} more elements (scroll or be more specific)")
        lines.append("Visible text:")
        lines.append(self.text)
        for t in self.frames_text:
            if t:
                lines.append("Frame text: " + t)
        return "\n".join(lines)


async def take_snapshot(page: Any, *, max_text: int = 6000) -> Snapshot:
    elements: list[ElementInfo] = []
    frame_urls: list[str] = []
    frames_text: list[str] = []
    main: dict[str, Any] | None = None
    has_captcha = False
    for i, frame in enumerate(page.frames):
        prefix = "" if i == 0 else f"f{i}"
        try:
            data = await isolated.evaluate(frame, WALKER_JS, [prefix, max_text if i == 0 else 1500])
        except Exception as e:  # detached / cross-process frame mid-navigation
            log.debug("snapshot of frame %s failed: %s", frame.url, e)
            continue
        frame_urls.append(data["url"])
        has_captcha = has_captcha or data["hasCaptcha"]
        if i == 0:
            main = data
        else:
            frames_text.append(data["text"][:1500])
        for d in data["elements"]:
            elements.append(ElementInfo(d["ref"], i, d))
    main = main or {"url": page.url, "title": "", "text": ""}
    # Captcha widgets often live in cross-origin frames we could not walk.
    for f in page.frames:
        if any(k in f.url for k in ("recaptcha", "hcaptcha", "challenges.cloudflare.com", "arkoselabs", "funcaptcha")):
            has_captcha = True
    return Snapshot(main["url"], main.get("title", ""), main.get("text", ""), elements, frame_urls, has_captcha, frames_text)


def locator_for(page: Any, el: ElementInfo) -> Any:
    frame = page.frames[el.frame_index] if el.frame_index < len(page.frames) else page.main_frame
    return frame.locator(f'[data-friday-ref="{el.ref}"]')
