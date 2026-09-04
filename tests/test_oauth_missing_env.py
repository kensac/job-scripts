"""A worker without the credential must not look like a revoked grant.

transmission and the laptop failed probe_credentials on 2026-09-04 with a bare
KeyError whose entire message was "GOOGLE_OAUTH_CLIENT_ID", while five other
workers passed. probe_credentials exists only to notice a dead token, so the
one signal that must stay unambiguous was the one being muddied.
"""

import os

import pytest

from api import oauth


def test_missing_client_id_is_a_provider_error_not_a_reconnect(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    with pytest.raises(oauth.ProviderError) as exc:
        oauth._client_id()
    assert not isinstance(exc.value, oauth.NeedsReconnect)


def test_the_message_names_the_variable_and_the_worker(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("JOBTRACKER_WORKER_NAME", "transmission")
    with pytest.raises(oauth.ProviderError) as exc:
        oauth._client_secret()
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in str(exc.value)
    assert "transmission" in str(exc.value)


def test_present_value_is_returned_unchanged(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "abc.apps.googleusercontent.com")
    assert oauth._client_id() == "abc.apps.googleusercontent.com"
