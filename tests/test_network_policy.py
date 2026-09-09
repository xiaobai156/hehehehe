from types import SimpleNamespace

import pytest

from he_app.fetch import network_policy
from he_app.fetch.network_policy import (
    NetworkPolicyError,
    ResolvedTarget,
    resolve_public_target,
    verify_response_peer,
)


def _record(address: str):
    return (2, 1, 6, "", (address, 443))


def test_private_dns_answer_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        network_policy.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [_record("127.0.0.1")],
    )
    with pytest.raises(NetworkPolicyError, match="forbidden"):
        resolve_public_target("https://example.test/")


def test_mixed_public_and_private_dns_answer_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        network_policy.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            _record("93.184.216.34"),
            _record("10.0.0.1"),
        ],
    )
    with pytest.raises(NetworkPolicyError, match="10.0.0.1"):
        resolve_public_target("https://example.test/")


class _Socket:
    def __init__(self, address: str):
        self.address = address

    def getpeername(self):
        return self.address, 443


def _response(address: str):
    return SimpleNamespace(
        raw=SimpleNamespace(_connection=SimpleNamespace(sock=_Socket(address)))
    )


def test_peer_must_match_pre_resolved_set() -> None:
    target = ResolvedTarget(
        "https",
        "example.test",
        443,
        ("93.184.216.34",),
    )
    with pytest.raises(NetworkPolicyError, match="pre-resolved"):
        verify_response_peer(_response("1.1.1.1"), target)


def test_matching_public_peer_is_accepted() -> None:
    target = ResolvedTarget(
        "https",
        "example.test",
        443,
        ("93.184.216.34",),
    )
    assert verify_response_peer(_response("93.184.216.34"), target) == "93.184.216.34"


def test_url_credentials_are_rejected() -> None:
    with pytest.raises(NetworkPolicyError, match="credentials"):
        network_policy.validate_http_url("https://user:pass@example.test/")
