"""Suite-wide safety: no test may reach the network. Provider code is exercised through
fakes beneath the adapters; an accidental real call (e.g. a Google token refresh) fails
loudly instead of contacting a provider."""

import socket

import pytest


class NetworkDisabledError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise NetworkDisabledError("network access is disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
