"""Two archive formats, one normalised shape.

Gmail Takeout gives mbox; Outlook for Mac gives .olm. They are different enough
to tempt two code paths and similar enough that two paths would drift, so
everything downstream sees one record type and never learns which format a
message came from.
"""

from __future__ import annotations

import datetime
import io
import tempfile
import zipfile
from pathlib import Path

import pytest

from core.mail_import import (
    ImportedMessage,
    _olm_entries,
    _olm_parse,
    read_archive,
    read_mbox,
    read_olm,
)

MBOX = """From bounce@x Mon Sep  1 10:00:00 2026
From: Careers <no-reply@greenhouse.io>
To: k@example.com
Subject: Your application
Date: Mon, 1 Sep 2026 10:00:00 -0400
Message-ID: <abc@greenhouse.io>
Content-Type: text/plain

Thanks for applying.

From bounce@y Mon Sep  1 11:00:00 2026
From: Bank <a@chase.com>
Subject: Statement
Date: Mon, 1 Sep 2026 11:00:00 +0000
Message-ID: <def@chase.com>
Content-Type: text/plain

Your statement is ready.
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_mbox_splits_and_normalises(tmp_path):
    msgs = list(read_mbox(_write(tmp_path, "a.mbox", MBOX)))
    assert [m.provider_message_id for m in msgs] == ["<abc@greenhouse.io>", "<def@chase.com>"]
    assert msgs[0].from_email == "no-reply@greenhouse.io"
    assert msgs[0].subject == "Your application"
    assert msgs[0].body_text == "Thanks for applying."


def test_dates_are_aware_utc(tmp_path):
    """A naive datetime here gets compared against now() in Postgres and is
    wrong by the container's offset - the trap that shifted every window in
    this product by four hours once already."""
    msgs = list(read_mbox(_write(tmp_path, "a.mbox", MBOX)))
    sent = msgs[0].sent_at
    assert sent is not None and sent.tzinfo is not None
    # 10:00 -0400 is 14:00 UTC, converted rather than relabelled.
    assert sent == datetime.datetime(2026, 9, 1, 14, 0, tzinfo=datetime.UTC)


def test_message_id_is_the_dedupe_key(tmp_path):
    """The four .olm archives overlap heavily with each other and with Takeout.
    Message-ID is the only identity stable across all of them, so the same
    message read from two formats must produce the same key."""
    mbox_msg = next(iter(read_mbox(_write(tmp_path, "a.mbox", MBOX))))
    olm = list(
        _olm_entries(
            b"<emails><email>"
            b"<OPFMessageCopyMessageID>&lt;abc@greenhouse.io&gt;</OPFMessageCopyMessageID>"
            b"<OPFMessageCopySubject>Your application</OPFMessageCopySubject>"
            b"</email></emails>",
            source="olm",
            origin="t.xml",
        )
    )
    assert olm[0].provider_message_id == mbox_msg.provider_message_id


def test_a_message_with_no_message_id_still_imports(tmp_path):
    """Archive exports produce partial messages. Falling back to a synthetic id
    keeps them, at the cost of not deduping them - which is the right trade,
    since dropping a message loses an outcome permanently."""
    text = "From b@x Mon Sep  1 10:00:00 2026\nFrom: a@b.test\nSubject: hi\n\nbody\n"
    msgs = list(read_mbox(_write(tmp_path, "b.mbox", text)))
    assert len(msgs) == 1
    assert msgs[0].provider_message_id.startswith("takeout-seq-")


def test_one_unparsable_message_does_not_abort_the_import(tmp_path):
    """38,685 messages: a single bad one aborting the run would be the worst
    possible failure mode, because the import is the expensive part."""
    text = MBOX + "From bad\n\x00\x00 not a message at all\n"
    msgs = list(read_mbox(_write(tmp_path, "c.mbox", text)))
    assert len(msgs) >= 2


def test_html_only_mail_yields_text(tmp_path):
    """ATS and marketing mail is frequently HTML-only. A plain-text-only reader
    would silently drop a large share of exactly the messages that matter."""
    text = (
        "From b@x Mon Sep  1 10:00:00 2026\n"
        "From: no-reply@ashbyhq.com\nSubject: Interview\n"
        "Message-ID: <h1@x>\nContent-Type: text/html\n\n"
        "<html><body><p>We would like to <b>invite</b> you.</p>"
        "<script>ignore()</script></body></html>\n"
    )
    msgs = list(read_mbox(_write(tmp_path, "d.mbox", text)))
    assert "invite" in (msgs[0].body_text or "")
    assert "ignore()" not in (msgs[0].body_text or "")


def test_doctype_is_refused():
    """ElementTree expands internal entities - verified, `<!ENTITY a "boom">`
    resolves - so a billion-laughs bomb would hang the import with no error.
    Outlook emits no DOCTYPE, so refusing them costs nothing legitimate."""
    assert _olm_parse(b'<!DOCTYPE x [<!ENTITY a "boom">]><e/>') is None
    assert _olm_parse(b"<emails><email/></emails>") is not None


def test_olm_reads_a_zip(tmp_path):
    xml = (
        b"<emails><email>"
        b"<OPFMessageCopyMessageID>&lt;z1@x&gt;</OPFMessageCopyMessageID>"
        b"<OPFMessageCopySubject>Offer</OPFMessageCopySubject>"
        b"<OPFMessageCopySentTime>2022-03-04T10:00:00Z</OPFMessageCopySentTime>"
        b"<OPFMessageCopyFromAddresses><emailAddress "
        b'OPFContactEmailAddressAddress="hr@acme.com"/></OPFMessageCopyFromAddresses>'
        b"<OPFMessageCopyBody>Pleased to offer you the role.</OPFMessageCopyBody>"
        b"</email></emails>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Accounts/x/Message/1.xml", xml)
        zf.writestr("Accounts/x/notes.txt", "ignored")
    path = tmp_path / "a.olm"
    path.write_bytes(buf.getvalue())

    msgs = list(read_olm(path))
    assert len(msgs) == 1
    assert msgs[0].from_email == "hr@acme.com"
    assert msgs[0].subject == "Offer"
    assert msgs[0].sent_at == datetime.datetime(2022, 3, 4, 10, 0, tzinfo=datetime.UTC)
    assert msgs[0].source == "olm"


def test_read_archive_dispatches_on_extension(tmp_path):
    assert isinstance(next(read_archive(_write(tmp_path, "a.mbox", MBOX))), ImportedMessage)
    with pytest.raises(ValueError, match="unsupported archive"):
        next(read_archive(_write(tmp_path, "a.txt", "x")))


def test_nul_bytes_are_stripped_from_every_text_field(tmp_path):
    """Postgres text columns cannot contain 0x00 and reject the whole INSERT
    if one appears - with no indication of which message or which field.

    Real mailboxes contain them: mis-declared encodings, binary fragments in
    malformed multipart bodies, truncated attachments. One NUL anywhere would
    fail an entire 38,685-message import, in a 1000-row batch, hours in. Found
    by running the real archive against a scratch database before production,
    which is why that is now the rule.
    """
    text = (
        "From b@x Mon Sep  1 10:00:00 2026\n"
        "From: Bad\x00Name <no-reply\x00@greenhouse.io>\n"
        "Subject: Your\x00application\n"
        "Message-ID: <nul\x001@x>\n"
        "Date: Mon, 1 Sep 2026 10:00:00 +0000\n"
        "Content-Type: text/plain\n\n"
        "Thanks for\x00 applying.\n"
    )
    p = tmp_path / "nul.mbox"
    p.write_text(text)
    msg = next(iter(read_mbox(p)))
    for value in (
        msg.provider_message_id,
        msg.subject,
        msg.from_email,
        msg.from_name,
        msg.body_text,
        *msg.to_emails,
    ):
        assert value is None or "\x00" not in value


def test_a_field_that_is_only_nul_becomes_none(tmp_path):
    """Not an empty string: an all-NUL subject carried no information, and
    NULL says that where '' would look like a deliberate blank."""
    text = (
        "From b@x Mon Sep  1 10:00:00 2026\n"
        "From: a@b.test\nSubject: \x00\x00\n"
        "Message-ID: <nul2@x>\n\nbody\n"
    )
    p = tmp_path / "nul2.mbox"
    p.write_text(text)
    msg = next(iter(read_mbox(p)))
    assert msg.subject is None


def test_the_threading_chain_survives_the_import():
    """provider_thread_id keeps only the FIRST References entry, which groups a
    reply with its root and cannot reconstruct a conversation that was
    forwarded, split or re-rooted. The chain is free at parse time and
    unrecoverable afterwards - the mbox is 4.2GB, and re-reading it to answer a
    question we could have answered on the way past is the expensive kind of
    cheap."""
    raw = (
        "From a@b.com\r\n"
        "Message-ID: <c3@acme.com>\r\n"
        "In-Reply-To: <c2@acme.com>\r\n"
        "References: <c1@acme.com> <c2@acme.com>\r\n"
        "From: Acme <a@acme.com>\r\n"
        "Subject: Re: Your application\r\n"
        "\r\nbody\r\n"
    )
    path = Path(tempfile.mkdtemp()) / "chain.mbox"
    path.write_text(raw)
    [msg] = list(read_mbox(path))

    assert msg.headers["message-id"] == "<c3@acme.com>"
    assert msg.headers["in-reply-to"] == "<c2@acme.com>"
    assert msg.headers["references"] == "<c1@acme.com> <c2@acme.com>"
    # The existing field keeps its meaning: the thread's origin.
    assert msg.provider_thread_id == "<c1@acme.com>"


def test_only_threading_headers_are_kept():
    """Storing every header would carry routing trace, spam scores and DKIM
    signatures for 67k messages to answer a question none of them are about."""
    raw = (
        "From a@b.com\r\n"
        "Message-ID: <x@acme.com>\r\n"
        "Received: from mx.example.com by mx2.example.com\r\n"
        "DKIM-Signature: v=1; a=rsa-sha256; d=acme.com\r\n"
        "X-Spam-Score: 0.1\r\n"
        "From: Acme <a@acme.com>\r\n"
        "Subject: Hello\r\n"
        "\r\nbody\r\n"
    )
    path = Path(tempfile.mkdtemp()) / "noisy.mbox"
    path.write_text(raw)
    [msg] = list(read_mbox(path))

    assert set(msg.headers) == {"message-id"}


def _mbox_bytes() -> bytes:
    return (
        b"From a@b.com\r\n"
        b"Message-ID: <z1@acme.com>\r\n"
        b"References: <z0@acme.com>\r\n"
        b"From: Acme <a@acme.com>\r\n"
        b"Subject: Re: Your application\r\n"
        b"\r\nbody one\r\n"
        b"From b@c.com\r\n"
        b"Message-ID: <z2@acme.com>\r\n"
        b"From: Acme <a@acme.com>\r\n"
        b"Subject: Another\r\n"
        b"\r\nbody two\r\n"
    )


def test_an_mbox_is_read_from_inside_a_takeout_zip():
    """The mbox is 4.24GB and the machine holding these archives was at 98%
    disk. Extracting first is a real risk for no benefit, and both paths
    stream, so neither holds the mailbox in memory."""
    path = Path(tempfile.mkdtemp()) / "takeout.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Takeout/Mail/All mail Including Spam and Trash.mbox", _mbox_bytes())

    msgs = list(read_mbox(path))
    assert [m.provider_message_id for m in msgs] == ["<z1@acme.com>", "<z2@acme.com>"]
    assert msgs[0].headers["references"] == "<z0@acme.com>"


def test_a_zip_holding_two_mailboxes_is_refused(tmp_path):
    """Picking one silently is how a partial archive gets imported as though it
    were the whole mailbox."""
    path = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("a.mbox", _mbox_bytes())
        archive.writestr("b.mbox", _mbox_bytes())

    with pytest.raises(ValueError, match=r"2 \.mbox files"):
        list(read_mbox(path))


def test_a_plain_mbox_still_reads(tmp_path):
    path = tmp_path / "plain.mbox"
    path.write_bytes(_mbox_bytes())
    assert len(list(read_mbox(path))) == 2


def test_olm_thread_topic_is_not_stored_as_a_thread_id():
    """OPFMessageCopyThreadTopic is a normalised SUBJECT. Stored in
    provider_thread_id it made every message sharing a subject line look like
    one conversation, which is how 56 confirmations from 32 employers became a
    single derived application."""
    messages = list(
        _olm_entries(
            b"<emails><email>"
            b"<OPFMessageCopyMessageID>&lt;one@x&gt;</OPFMessageCopyMessageID>"
            b"<OPFMessageCopySubject>Application Confirmation</OPFMessageCopySubject>"
            b"<OPFMessageCopyThreadTopic>Application Confirmation</OPFMessageCopyThreadTopic>"
            b"</email></emails>",
            source="olm",
            origin="t.xml",
        )
    )
    assert len(messages) == 1
    assert messages[0].provider_thread_id is None, "a subject is not a thread identity"
    assert messages[0].thread_topic == "Application Confirmation", "and it is not discarded"


def _olm_body(body: str = "", html: str = "") -> str | None:
    """One .olm message carrying the given body fields, through the real parser."""
    parts = b"<emails><email><OPFMessageCopyMessageID>&lt;b@x&gt;</OPFMessageCopyMessageID>"
    if body:
        parts += b"<OPFMessageCopyBody>" + body.encode() + b"</OPFMessageCopyBody>"
    if html:
        parts += b"<OPFMessageCopyHTMLBody>" + html.encode() + b"</OPFMessageCopyHTMLBody>"
    messages = list(_olm_entries(parts + b"</email></emails>", source="olm", origin="t.xml"))
    assert len(messages) == 1
    return messages[0].body_text


def test_an_olm_body_full_of_markup_is_converted_not_trusted():
    """OPFMessageCopyBody is named for text and holds raw markup on 96% of this
    corpus: 27,221 of 28,451 messages carry tags and 20,359 leak CSS. The old
    form was `body or _html_to_text(html)`, so a non-empty body short-circuited
    the conversion and a doctype declaration went into the field the classifier
    reads as prose."""
    text = _olm_body(
        body="&lt;!DOCTYPE html&gt;&lt;html&gt;&lt;body&gt;"
        "&lt;p&gt;Thanks for applying.&lt;/p&gt;&lt;/body&gt;&lt;/html&gt;"
    )
    assert text is not None
    assert "Thanks for applying." in text
    assert "<" not in text and "DOCTYPE" not in text


def test_a_genuinely_plain_olm_body_is_left_alone():
    text = _olm_body(body="Thanks for applying. We will be in touch.")
    assert text == "Thanks for applying. We will be in touch."


def test_an_angle_bracket_that_is_not_a_tag_does_not_trigger_conversion():
    """23 of 28,451 bodies contain a "<" with no closing tag - a bare address
    or an inequality. Matching on "<" alone would run them through a stripper
    that has nothing to strip and would eat the address."""
    text = _olm_body(body="Reply to &lt;careers@example.com&gt; if 3 &lt; 5.")
    assert text == "Reply to <careers@example.com> if 3 < 5."


def test_the_dedicated_html_field_is_still_preferred_when_the_body_is_markup():
    """Both fields carry markup on most of this corpus. The HTML field is the
    one the format intends for it, so it wins over re-stripping the body."""
    text = _olm_body(
        body="&lt;p&gt;stale&lt;/p&gt;",
        html="&lt;p&gt;current&lt;/p&gt;",
    )
    assert text is not None
    assert "current" in text and "stale" not in text


def test_an_html_only_message_still_converts():
    text = _olm_body(html="&lt;p&gt;Interview scheduled.&lt;/p&gt;")
    assert text is not None
    assert "Interview scheduled." in text


def test_a_document_with_no_text_becomes_empty_rather_than_markup():
    """Five of 400 sampled real messages convert to nothing - calendar invites
    whose whole body is `<html><head><meta></head></html>`. There is no text to
    lose, and empty is the honest answer where raw markup would poison the
    classifier."""
    text = _olm_body(
        body='&lt;html&gt;&lt;head&gt;&lt;meta charset="utf-8"&gt;&lt;/head&gt;&lt;/html&gt;'
    )
    assert not (text or "").strip()
