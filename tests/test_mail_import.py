"""Two archive formats, one normalised shape.

Gmail Takeout gives mbox; Outlook for Mac gives .olm. They are different enough
to tempt two code paths and similar enough that two paths would drift, so
everything downstream sees one record type and never learns which format a
message came from.
"""

from __future__ import annotations

import datetime
import io
import zipfile

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
