from __future__ import annotations

import pytest

from confluence_reader.errors import AuthenticationError, ValidationError
from confluence_reader.transport import redact_secrets
from confluence_reader.utils import build_cql, decode_cursor, encode_cursor, parse_page_ref


def test_build_cql_combines_text_space_label_and_relative_date() -> None:
    cql = build_cql(
        query="Siemens S7",
        cql=None,
        space_keys=["ENG", "OPS"],
        labels=["plc", "runbook"],
        last_modified='now("-30d")',
    )
    assert 'text ~ "Siemens S7"' in cql
    assert 'space in ("ENG", "OPS")' in cql
    assert 'label = "plc"' in cql
    assert 'label = "runbook"' in cql
    assert 'lastmodified >= now("-30d")' in cql


def test_build_cql_rejects_query_and_raw_cql_together() -> None:
    with pytest.raises(ValidationError):
        build_cql(
            query="one",
            cql="type = page",
            space_keys=None,
            labels=None,
        )


def test_cursor_round_trip_and_invalid_cursor() -> None:
    value = "https://example.atlassian.net/wiki/api/v2/pages?cursor=abc"
    assert decode_cursor(encode_cursor(value)) == value
    with pytest.raises(ValidationError):
        decode_cursor("not-a-valid-cursor")


def test_page_reference_parser_handles_common_forms() -> None:
    assert parse_page_ref("12345") == ("12345", None, None)
    assert parse_page_ref("https://example.atlassian.net/wiki/pages/12345/Title")[0] == "12345"
    assert parse_page_ref("DOC/Release notes") == (None, "DOC", "Release notes")


def test_redact_secrets_covers_headers_and_bearer_tokens() -> None:
    value = "Authorization: Bearer abc.def\napi_token=secret\nBasic dXNlcjpwYXNz"
    redacted = redact_secrets(value)
    assert "abc.def" not in redacted
    assert "secret" not in redacted
    assert "dXNlcjpwYXNz" not in redacted


def test_error_payload_redacts_secret_named_details() -> None:
    payload = AuthenticationError(
        "Authorization: Bearer abc.def",
        details={"api_token": "secret", "url": "https://example.com"},
    ).to_dict()
    assert "abc.def" not in payload["error"]["message"]
    assert payload["error"]["details"]["api_token"] == "[REDACTED]"
