"""build_email_message: plain-text-only default, and the HTML/inline-logo
path used for a branded signature."""

from app.adapters.mime import build_email_message


def test_plain_text_only_when_no_signature():
    """Every transactional call site (verification, notifications, etc.)
    relies on this being byte-for-byte the old plain-text-only behavior."""
    msg = build_email_message("Hi Sarah,\n\nQuick note.\n\nMo")
    assert msg.is_multipart() is False
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content().strip() == "Hi Sarah,\n\nQuick note.\n\nMo"


def test_html_alternative_with_plain_fallback():
    msg = build_email_message(
        "Hi Sarah,\n\nQuick note.\n\nMo",
        signature_html="<p><strong>Mo</strong><br>Head of Sales</p>",
        signature_text="\n\n--\nMo, Head of Sales",
    )
    assert msg.is_multipart() is True
    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.walk())
    plain_part = next(p for p in parts if p.get_content_type() == "text/plain")
    html_part = next(p for p in parts if p.get_content_type() == "text/html")
    assert "Mo, Head of Sales" in plain_part.get_content()
    assert "Head of Sales" in html_part.get_content()


def test_body_is_html_escaped():
    """The body is LLM-generated and not otherwise controlled — it must
    never be able to inject markup into the HTML alternative."""
    msg = build_email_message(
        "Hi <script>alert(1)</script>,\n\nSecond line.",
        signature_html="<p>Mo</p>",
    )
    html_part = next(p for p in msg.walk() if p.get_content_type() == "text/html")
    html = html_part.get_content()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<br>" in html  # newlines converted for HTML rendering


def test_inline_logo_produces_related_part_with_matching_cid():
    msg = build_email_message(
        "Hi Sarah,\n\nQuick note.",
        signature_html='<p>Mo<br><img src="cid:logo"></p>',
        logo_bytes=b"\x89PNG\r\n\x1a\n" + b"FAKE",
        logo_content_type="image/png",
    )
    parts = list(msg.walk())
    assert any(p.get_content_type() == "multipart/related" for p in parts)
    image_part = next(p for p in parts if p.get_content_type() == "image/png")
    assert image_part.get("Content-ID") == "<logo>"
    html_part = next(p for p in parts if p.get_content_type() == "text/html")
    assert "cid:logo" in html_part.get_content()


def test_no_logo_bytes_skips_related_part():
    msg = build_email_message(
        "Hi Sarah,\n\nQuick note.",
        signature_html="<p>Mo</p>",
    )
    parts = list(msg.walk())
    assert not any(p.get_content_type() == "multipart/related" for p in parts)
    assert not any(p.get_content_type().startswith("image/") for p in parts)
