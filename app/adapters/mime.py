"""Shared MIME body construction for outgoing email.

Used by both the Gmail and SMTP senders so the HTML/inline-logo logic
exists in exactly one place rather than being duplicated across adapters
that otherwise share an identical send(to, subject, body) contract.
"""

import html as html_lib
from email.message import EmailMessage


def build_email_message(body: str, *, signature_html: str | None = None,
                        signature_text: str | None = None,
                        logo_bytes: bytes | None = None,
                        logo_content_type: str | None = None) -> EmailMessage:
    """Build a message with the body (and To/Subject/From still to be set
    by the caller). Plain text only when signature_html is None — byte-
    identical to the original plain-text-only behavior, which every
    transactional call site (verification, notifications, etc.) still uses.
    Otherwise a multipart/alternative with a plain-text fallback (required
    for deliverability — an HTML-only message is a spam signal), plus a
    multipart/related inline image when a logo is supplied.
    """
    message = EmailMessage()
    if signature_html is None:
        message.set_content(body)
        return message

    message.set_content(body + (signature_text or ""))
    html_body = html_lib.escape(body).replace("\n", "<br>") + signature_html
    message.add_alternative(html_body, subtype="html")
    if logo_bytes and logo_content_type:
        html_part = message.get_payload()[1]
        subtype = logo_content_type.split("/")[-1]
        html_part.add_related(logo_bytes, "image", subtype, cid="<logo>")
    return message
