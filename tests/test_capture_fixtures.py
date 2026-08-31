"""Redaction must not leak contact data, and must not mangle the page around it."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_fixtures.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_fixtures", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


capture_fixtures = _load()
redact = capture_fixtures.redact


def test_redacts_phone_numbers_in_every_common_format() -> None:
    for raw in (
        "Call (604) 555-1234 today",
        "or 778.555.9876.",
        "text 6045551234",
        "+1 604 555 1234",
        "604-555-1234",
    ):
        cleaned, counts = redact(raw)
        assert "555-555-5555" in cleaned, raw
        assert counts.get("phone"), raw


def test_redacts_emails_and_tel_and_reply_links() -> None:
    raw = (
        '<a href="tel:+16045551234">call</a>'
        '<a href="https://vancouver.craigslist.org/reply/van/apa/7712345678">reply</a>'
        "bob.smith+rent@example.co.uk"
    )
    cleaned, counts = redact(raw)
    assert "6045551234" not in cleaned
    assert "bob.smith+rent@example.co.uk" not in cleaned
    assert "craigslist.org/reply" not in cleaned
    assert counts["email"] >= 1
    assert counts["tel-link"] == 1
    assert counts["reply-link"] == 1


def test_redacts_json_ld_contact_fields_but_keeps_listing_facts() -> None:
    raw = '{"telephone": "604-555-1234", "email": "a@b.com", "price": "2950", "name": "2BR"}'
    cleaned, _counts = redact(raw)
    assert '"telephone": "redacted"' in cleaned
    assert '"email": "redacted"' in cleaned
    assert '"price": "2950"' in cleaned
    assert '"name": "2BR"' in cleaned


def test_long_digit_runs_inside_urls_are_left_alone() -> None:
    """A CDN hash is not a phone number. Mangling these breaks the fixture's photos."""
    for raw in (
        '<img src="https://pypi-camo.freetls.fastly.net/9706778018adad6f5bf682f55d7bbc226abe551c/x">',
        '<img src="https://images.craigslist.org/00r0r_abc1234567890_600x450.jpg">',
        '<a href="https://vancouver.craigslist.org/van/apa/d/title/7712345678.html">post</a>',
    ):
        cleaned, counts = redact(raw)
        assert cleaned == raw, counts


def test_redaction_reports_what_it_changed() -> None:
    cleaned, counts = redact("no contact data here")
    assert cleaned == "no contact data here"
    assert counts == {}


def test_masking_urls_does_not_stop_redaction_of_text_around_them() -> None:
    raw = (
        '<a href="https://vancouver.craigslist.org/van/apa/d/t/7712345678.html">2BR</a>'
        " Call (604) 555-1234 or email jane@example.com"
    )
    cleaned, counts = redact(raw)
    assert "7712345678.html" in cleaned, "post id must survive"
    assert "(604) 555-1234" not in cleaned
    assert "jane@example.com" not in cleaned
    assert counts["phone"] == 1
    assert counts["email"] == 1


def test_mailto_links_are_redacted() -> None:
    cleaned, counts = redact('<a href="mailto:landlord@example.com">email</a>')
    assert "landlord@example.com" not in cleaned
    assert counts["mailto-link"] == 1
