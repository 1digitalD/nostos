from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol


class Notifier(Protocol):
    def send(self, *, title: str, body: str) -> None: ...


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    title: str
    body: str


class NullNotifier:
    def send(self, *, title: str, body: str) -> None:
        del title, body


class AppriseNotifier:
    def __init__(
        self,
        urls: Iterable[str],
        *,
        apprise_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._urls = tuple(url for url in urls if url.strip())
        self._apprise_factory = apprise_factory
        self._apprise_client: Any | None = None

    def send(self, *, title: str, body: str) -> None:
        if not self._urls:
            return
        client = self._client()
        if not client.notify(title=title, body=body):
            raise RuntimeError("Apprise notification failed")

    def _client(self) -> Any:
        if self._apprise_client is not None:
            return self._apprise_client
        client = _build_apprise_client(self._urls, apprise_factory=self._apprise_factory)
        self._apprise_client = client
        return client


def notifier_from_urls(
    urls: Iterable[str],
    *,
    apprise_factory: Callable[[], Any] | None = None,
) -> Notifier:
    normalized = tuple(url for url in urls if url.strip())
    if not normalized:
        return NullNotifier()
    return AppriseNotifier(normalized, apprise_factory=apprise_factory)


def _build_apprise_client(
    urls: Iterable[str],
    *,
    apprise_factory: Callable[[], Any] | None = None,
) -> Any:
    factory = apprise_factory
    if factory is None:
        from apprise import Apprise

        factory = Apprise

    client = factory()
    for url in urls:
        client.add(url)
    return client
