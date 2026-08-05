from __future__ import annotations

import base64
import json
import multiprocessing
import os
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gpt_image_bridge import oauth
from gpt_image_bridge.errors import BridgeError


NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)


def jwt(claims: dict) -> str:
    def encode(value: dict) -> str:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(claims)}."


def valid_access(account_id: str = "acct-default", *, minutes: int = 60) -> str:
    return jwt(
        {
            "exp": int((NOW + timedelta(minutes=minutes)).timestamp()),
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
            },
        }
    )


def write_auth(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def auth_payload(
    *,
    access_token: str | None = None,
    refresh_token: str | None = "refresh-secret",
    id_token: str | None = None,
    account_id: str | None = None,
    last_refresh: str | None = "2026-07-31T07:30:00.000Z",
    **extra,
) -> dict:
    tokens = {}
    if access_token is not None:
        tokens["access_token"] = access_token
    if refresh_token is not None:
        tokens["refresh_token"] = refresh_token
    if id_token is not None:
        tokens["id_token"] = id_token
    if account_id is not None:
        tokens["account_id"] = account_id
    payload = {"tokens": tokens, **extra}
    if last_refresh is not None:
        payload["last_refresh"] = last_refresh
    return payload


@pytest.fixture(autouse=True)
def forbid_unexpected_network(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("unexpected real network request")

    monkeypatch.setattr(oauth.urllib.request, "urlopen", fail)


def isolate_auth_paths(monkeypatch, tmp_path) -> tuple[Path, Path]:
    home = tmp_path / "private-home"
    home.mkdir()
    codex_home = tmp_path / "alternate-codex-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return home / ".codex" / "auth.json", codex_home / "auth.json"


def test_explicit_codex_home_has_priority(monkeypatch, tmp_path):
    standard, alternate = isolate_auth_paths(monkeypatch, tmp_path)
    write_auth(
        standard,
        auth_payload(
            access_token=valid_access("standard-account"),
            account_id="standard-account",
        ),
    )
    write_auth(
        alternate,
        auth_payload(
            access_token=valid_access("alternate-account"),
            account_id="alternate-account",
        ),
    )

    session = oauth.load_oauth_session(now=NOW)

    assert session.account_id == "alternate-account"
    assert session.source_path == alternate


def test_standard_auth_file_is_fallback_when_codex_home_file_is_missing(
    monkeypatch,
    tmp_path,
):
    standard, alternate = isolate_auth_paths(monkeypatch, tmp_path)
    write_auth(
        standard,
        auth_payload(
            access_token=valid_access("standard-account"),
            account_id="standard-account",
        ),
    )

    session = oauth.load_oauth_session(now=NOW)

    assert not alternate.exists()
    assert session.account_id == "standard-account"
    assert session.source_path == standard


def _hold_auth_lock(
    auth_path: str,
    acquired,
    release,
) -> None:
    with oauth._interprocess_auth_lock(Path(auth_path)):
        acquired.set()
        release.wait(10)


def _wait_for_auth_lock(
    auth_path: str,
    started,
    acquired,
) -> None:
    started.set()
    with oauth._interprocess_auth_lock(Path(auth_path)):
        acquired.set()


def test_auth_lock_serializes_independent_processes(tmp_path):
    auth_path = tmp_path / "auth.json"
    write_auth(
        auth_path,
        auth_payload(
            access_token=valid_access("account"),
            account_id="account",
        ),
    )
    context = multiprocessing.get_context("spawn")
    first_acquired = context.Event()
    first_release = context.Event()
    second_started = context.Event()
    second_acquired = context.Event()
    first = context.Process(
        target=_hold_auth_lock,
        args=(str(auth_path), first_acquired, first_release),
    )
    second = context.Process(
        target=_wait_for_auth_lock,
        args=(str(auth_path), second_started, second_acquired),
    )
    try:
        first.start()
        assert first_acquired.wait(10)
        second.start()
        assert second_started.wait(10)
        assert not second_acquired.wait(0.3)
        first_release.set()
        assert second_acquired.wait(10)
    finally:
        first_release.set()
        for process in (first, second):
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(10)
    assert first.exitcode == 0
    assert second.exitcode == 0


@pytest.mark.parametrize(
    ("claims", "expected_account", "expected_fedramp"),
    [
        (
            {
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "nested-account",
                    "chatgpt_account_is_fedramp": True,
                }
            },
            "nested-account",
            True,
        ),
        (
            {
                "chatgpt_account_id": "top-level-account",
                "chatgpt_account_is_fedramp": True,
            },
            "top-level-account",
            True,
        ),
        (
            {
                "organizations": [
                    {
                        "id": "organization-account",
                        "is_fedramp": True,
                    }
                ]
            },
            "organization-account",
            True,
        ),
    ],
)
def test_account_and_fedramp_are_derived_from_jwt(
    monkeypatch,
    tmp_path,
    claims,
    expected_account,
    expected_fedramp,
):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    claims = {
        "exp": int((NOW + timedelta(hours=1)).timestamp()),
        **claims,
    }
    write_auth(
        standard,
        auth_payload(
            access_token=jwt(claims),
            account_id=None,
        ),
    )

    session = oauth.load_oauth_session(now=NOW)

    assert session.account_id == expected_account
    assert session.is_fedramp is expected_fedramp


class FakeResponse:
    def __init__(self, payload: dict | bytes, status: int = 200):
        self.payload = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self, size: int = -1):
        return self.payload[:size]


def install_refresh_response(monkeypatch, response_payload: dict):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {
            name.lower(): value for name, value in request.header_items()
        }
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse(response_payload)

    monkeypatch.setattr(oauth.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_refreshes_access_token_with_json_and_preserves_other_fields(
    monkeypatch,
    tmp_path,
):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    old_access = valid_access("old-account", minutes=4)
    old_id = jwt({"chatgpt_account_id": "old-account"})
    new_access = valid_access("new-account", minutes=60)
    new_id = jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "new-account",
                "chatgpt_account_is_fedramp": True,
            }
        }
    )
    write_auth(
        standard,
        auth_payload(
            access_token=old_access,
            refresh_token="old-refresh-secret",
            id_token=old_id,
            account_id="old-account",
            preserved_top_level={"value": 7},
        ),
    )
    original = json.loads(standard.read_text(encoding="utf-8"))
    original["tokens"]["preserved_token_field"] = "keep-me"
    standard.write_text(json.dumps(original), encoding="utf-8")
    captured = install_refresh_response(
        monkeypatch,
        {
            "access_token": new_access,
            "refresh_token": "new-refresh-secret",
            "id_token": new_id,
        },
    )

    session = oauth.load_oauth_session(timeout=17, now=NOW)

    assert captured == {
        "url": oauth.OPENAI_OAUTH_TOKEN_URL,
        "method": "POST",
        "headers": {
            "accept": "application/json",
            "content-type": "application/json",
        },
        "body": {
            "client_id": oauth.OPENAI_OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": "old-refresh-secret",
        },
        "timeout": 17.0,
    }
    assert session.access_token == new_access
    assert session.refresh_token == "new-refresh-secret"
    assert session.id_token == new_id
    assert session.account_id == "new-account"
    assert session.is_fedramp is True
    assert session.last_refresh == "2026-07-31T08:00:00.000Z"

    saved = json.loads(standard.read_text(encoding="utf-8"))
    assert saved["preserved_top_level"] == {"value": 7}
    assert saved["tokens"]["preserved_token_field"] == "keep-me"
    assert saved["tokens"]["access_token"] == new_access
    assert saved["tokens"]["refresh_token"] == "new-refresh-secret"
    assert saved["tokens"]["id_token"] == new_id
    assert saved["tokens"]["account_id"] == "new-account"
    assert saved["last_refresh"] == "2026-07-31T08:00:00.000Z"
    assert not list(standard.parent.glob(f".{standard.name}.*.tmp"))


def test_refreshes_when_last_refresh_is_older_than_55_minutes(
    monkeypatch,
    tmp_path,
):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    write_auth(
        standard,
        auth_payload(
            access_token=valid_access("account"),
            account_id="account",
            last_refresh="2026-07-31T07:04:59.000Z",
        ),
    )
    new_access = valid_access("account", minutes=120)
    install_refresh_response(
        monkeypatch,
        {"access_token": new_access},
    )

    session = oauth.load_oauth_session(now=NOW)

    assert session.access_token == new_access
    assert session.refresh_token == "refresh-secret"


def test_does_not_refresh_a_fresh_access_token(monkeypatch, tmp_path):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    access_token = valid_access("account", minutes=6)
    write_auth(
        standard,
        auth_payload(
            access_token=access_token,
            account_id="account",
            last_refresh="2026-07-31T07:06:00.000Z",
        ),
    )

    session = oauth.load_oauth_session(now=NOW)

    assert session.access_token == access_token


def test_refresh_keeps_optional_tokens_when_response_omits_them(
    monkeypatch,
    tmp_path,
):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    old_id = jwt({"chatgpt_account_id": "account"})
    write_auth(
        standard,
        auth_payload(
            access_token=valid_access("account", minutes=4),
            refresh_token="existing-refresh",
            id_token=old_id,
            account_id="account",
        ),
    )
    new_access = valid_access("account", minutes=60)
    install_refresh_response(
        monkeypatch,
        {"access_token": new_access},
    )

    session = oauth.load_oauth_session(now=NOW)

    assert session.refresh_token == "existing-refresh"
    assert session.id_token == old_id
    saved = json.loads(standard.read_text(encoding="utf-8"))
    assert saved["tokens"]["refresh_token"] == "existing-refresh"
    assert saved["tokens"]["id_token"] == old_id


def test_adopts_external_auth_change_before_refresh(monkeypatch, tmp_path):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    write_auth(
        standard,
        auth_payload(
            access_token=valid_access("old-account", minutes=4),
            refresh_token="old-refresh",
            account_id="old-account",
        ),
    )
    external = auth_payload(
        access_token=valid_access("external-account", minutes=60),
        refresh_token="external-refresh",
        account_id="external-account",
        last_refresh="2026-07-31T07:59:00.000Z",
        external_update=True,
    )
    original_should_refresh = oauth._should_refresh
    changed = False

    def change_before_refresh(access_token, last_refresh, now):
        nonlocal changed
        result = original_should_refresh(access_token, last_refresh, now)
        if not changed:
            changed = True
            write_auth(standard, external)
        return result

    monkeypatch.setattr(oauth, "_should_refresh", change_before_refresh)

    session = oauth.load_oauth_session(now=NOW)

    assert session.account_id == "external-account"
    assert session.access_token == external["tokens"]["access_token"]
    assert json.loads(standard.read_text(encoding="utf-8")) == external


def test_discards_refresh_when_auth_changes_before_write(monkeypatch, tmp_path):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    write_auth(
        standard,
        auth_payload(
            access_token=valid_access("old-account", minutes=4),
            refresh_token="old-refresh",
            account_id="old-account",
        ),
    )
    external = auth_payload(
        access_token=valid_access("external-account", minutes=60),
        refresh_token="external-refresh",
        account_id="external-account",
        last_refresh="2026-07-31T07:59:00.000Z",
        external_update=True,
    )
    discarded_access = valid_access("discarded-account", minutes=60)

    def refresh_then_external_write(refresh_token, timeout):
        assert refresh_token == "old-refresh"
        assert timeout == 30.0
        write_auth(standard, external)
        return {
            "access_token": discarded_access,
            "refresh_token": "discarded-refresh",
        }

    monkeypatch.setattr(oauth, "_refresh_tokens", refresh_then_external_write)

    session = oauth.load_oauth_session(now=NOW)

    assert session.account_id == "external-account"
    assert session.access_token == external["tokens"]["access_token"]
    saved = json.loads(standard.read_text(encoding="utf-8"))
    assert saved == external
    assert discarded_access not in standard.read_text(encoding="utf-8")


def test_atomic_write_cas_preserves_change_made_while_staging(
    monkeypatch,
    tmp_path,
):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    write_auth(
        standard,
        auth_payload(
            access_token=valid_access("old-account", minutes=4),
            refresh_token="old-refresh",
            account_id="old-account",
        ),
    )
    external = auth_payload(
        access_token=valid_access("external-account", minutes=60),
        refresh_token="external-refresh",
        account_id="external-account",
        last_refresh="2026-07-31T07:59:00.000Z",
        external_update=True,
    )
    discarded_access = valid_access("discarded-account", minutes=60)
    monkeypatch.setattr(
        oauth,
        "_refresh_tokens",
        lambda _refresh_token, _timeout: {
            "access_token": discarded_access,
            "refresh_token": "discarded-refresh",
        },
    )
    original_dump = oauth.json.dump
    changed = False

    def dump_then_external_write(payload, handle, **kwargs):
        nonlocal changed
        original_dump(payload, handle, **kwargs)
        if not changed:
            changed = True
            write_auth(standard, external)

    monkeypatch.setattr(oauth.json, "dump", dump_then_external_write)

    session = oauth.load_oauth_session(now=NOW)

    assert changed is True
    assert session.account_id == "external-account"
    assert json.loads(standard.read_text(encoding="utf-8")) == external
    assert discarded_access not in standard.read_text(encoding="utf-8")
    assert not list(standard.parent.glob(f".{standard.name}.*.tmp"))


def test_request_context_contains_headers_and_all_secrets(monkeypatch):
    session = oauth.OAuthSession(
        access_token="access-secret",
        account_id="account-secret",
        refresh_token="refresh-secret",
        id_token="id-secret",
        is_fedramp=True,
        source_path=Path("private-auth-file"),
    )
    monkeypatch.setattr(oauth, "load_oauth_session", lambda timeout=30: session)

    base_url, headers, secrets = oauth.oauth_request_context(timeout=9)

    assert base_url == oauth.CODEX_API_BASE_URL
    assert headers == {
        "Authorization": "Bearer access-secret",
        "chatgpt-account-id": "account-secret",
        "X-OpenAI-Fedramp": "true",
    }
    assert secrets == (
        "access-secret",
        "refresh-secret",
        "id-secret",
        "account-secret",
    )
    assert "secret" not in repr(session)
    assert "private-auth-file" not in repr(session)


def test_missing_auth_error_does_not_expose_machine_path(
    monkeypatch,
    tmp_path,
):
    standard, alternate = isolate_auth_paths(monkeypatch, tmp_path)

    with pytest.raises(BridgeError) as captured:
        oauth.load_oauth_session(now=NOW)

    rendered = str(captured.value)
    assert captured.value.error_code == "codex_auth_missing"
    assert str(standard) not in rendered
    assert str(alternate) not in rendered


def test_malformed_auth_error_does_not_expose_content_or_path(
    monkeypatch,
    tmp_path,
):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    leaked_value = "token-that-must-not-appear"
    standard.parent.mkdir(parents=True)
    standard.write_text(f'{{"tokens":"{leaked_value}"', encoding="utf-8")

    with pytest.raises(BridgeError) as captured:
        oauth.load_oauth_session(now=NOW)

    rendered = str(captured.value)
    assert captured.value.error_code == "codex_auth_invalid"
    assert leaked_value not in rendered
    assert str(standard) not in rendered


def test_refresh_http_failure_does_not_expose_tokens_or_path(
    monkeypatch,
    tmp_path,
):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    access_token = valid_access("private-account", minutes=4)
    refresh_token = "refresh-token-that-must-not-appear"
    write_auth(
        standard,
        auth_payload(
            access_token=access_token,
            refresh_token=refresh_token,
            account_id="private-account",
        ),
    )

    def fail(_request, timeout):
        assert timeout == 11.0
        raise urllib.error.HTTPError(
            oauth.OPENAI_OAUTH_TOKEN_URL,
            401,
            f"{refresh_token} {standard}",
            {},
            None,
        )

    monkeypatch.setattr(oauth.urllib.request, "urlopen", fail)

    with pytest.raises(BridgeError) as captured:
        oauth.load_oauth_session(timeout=11, now=NOW)

    rendered = str(captured.value)
    assert captured.value.error_code == "codex_oauth_refresh_failed"
    assert captured.value.status == 401
    assert access_token not in rendered
    assert refresh_token not in rendered
    assert "private-account" not in rendered
    assert str(standard) not in rendered


def test_invalid_refresh_response_does_not_overwrite_auth(
    monkeypatch,
    tmp_path,
):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    write_auth(
        standard,
        auth_payload(
            access_token=valid_access("account", minutes=4),
            account_id="account",
        ),
    )
    before = standard.read_bytes()
    monkeypatch.setattr(
        oauth.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"not-json"),
    )

    with pytest.raises(BridgeError) as captured:
        oauth.load_oauth_session(now=NOW)

    assert captured.value.error_code == "codex_oauth_refresh_invalid"
    assert standard.read_bytes() == before


def test_expired_access_without_refresh_token_fails_without_network(
    monkeypatch,
    tmp_path,
):
    standard, _alternate = isolate_auth_paths(monkeypatch, tmp_path)
    write_auth(
        standard,
        auth_payload(
            access_token=valid_access("account", minutes=4),
            refresh_token=None,
            account_id="account",
        ),
    )

    with pytest.raises(BridgeError) as captured:
        oauth.load_oauth_session(now=NOW)

    assert captured.value.error_code == "codex_oauth_refresh_token_missing"


def test_source_contains_no_runtime_package_or_process_launcher():
    project_root = Path(__file__).resolve().parents[1]
    runtime_sources = [project_root / "nodes.py"]
    runtime_sources.extend(sorted((project_root / "gpt_image_bridge").glob("*.py")))
    source = "\n".join(
        path.read_text(encoding="utf-8").lower() for path in runtime_sources
    )

    for forbidden in (
        "subprocess",
        "npx",
        "openai-oauth",
        "oauth_port",
        "auto_start_oauth",
        "oauth_base_url",
    ):
        assert forbidden not in source
