"""The committed OpenAPI schema matches the app, so the generated TS client cannot drift."""

from admin_api import openapi


def test_committed_schema_is_current() -> None:
    committed = openapi.SCHEMA_PATH.read_text(encoding="utf-8")

    assert committed == openapi.render(), "run 'make client' and commit the result"
