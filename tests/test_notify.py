"""Notifications: the log line, the signed webhook, and the events that produce them.

No network. Every delivery goes through :class:`httpx.MockTransport`, which is the real
httpx client stack with the socket replaced -- so the headers, the body bytes and the
retry behaviour tested here are the ones a real receiver would see.

What must hold:

* the JSON body is the notice and an envelope, nothing more, and never a credential;
* a signature verifies with the secret, and fails with the wrong one, a changed body or
  a stale timestamp;
* a receiver that is down, slow or rude is retried a bounded number of times, logged,
  and never breaks the ticker, the worker, the reconciler or a request;
* approval requests, orphans and drift -- the three events that used to reach nobody --
  now produce notices, with an approval link that points at a page that exists.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bailment.catalog.loader import Catalog
from bailment.config import Settings
from bailment.engine.leases import LeaseTicker
from bailment.engine.reconciler import Reconciler
from bailment.engine.service import Caller, LeaseService, ProvisionRequest
from bailment.engine.worker import ProvisioningWorker
from bailment.models import Lease, utcnow
from bailment.notify import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    FanoutNotifier,
    LeaseNotice,
    WebhookNotifier,
    build_notifier,
    drain,
    log_notice,
    sign,
    verify_signature,
)
from bailment.providers.registry import ProviderRegistry
from bailment.secrets import SecretBox
from bailment.states import LeaseState
from conftest import SEALED_OUTPUT, SEALED_VALUE_PREFIX, RecordingProvider

SECRET = "whsec-test-only"
HOOK = "https://hooks.example.test/services/T000/B000/not-a-real-token"


class Receiver:
    """A webhook endpoint that records what it was sent and answers what it is told to."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses) or [204]
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(status)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]

    def kinds(self) -> list[str]:
        return [request.headers[EVENT_HEADER] for request in self.requests]


async def no_sleep(_: float) -> None:
    return None


def hook(receiver: Receiver, **options: Any) -> WebhookNotifier:  # noqa: ANN401
    return WebhookNotifier(
        HOOK, transport=receiver.transport, sleep=no_sleep, **{"secret": SECRET, **options}
    )


def sample(kind: str = "expiring", **overrides: Any) -> LeaseNotice:  # noqa: ANN401
    fields: dict[str, Any] = {
        "kind": kind,
        "lease_id": "3f2a9c1e-0000-4000-8000-000000000001",
        "golden_path_id": "sandbox",
        "requester": "agent-one",
        "on_behalf_of": "dana",
        "external_name": "bailment-sandbox-3f2a9c1e",
        "expires_at": utcnow() + timedelta(seconds=45),
        "seconds_remaining": 45,
        "message": "lease 3f2a9c1e (sandbox) expires in 45s. <Renew> & carry on.",
        "provider": "memory",
    }
    fields.update(overrides)
    return LeaseNotice(**fields)


# --------------------------------------------------------------------------------------
# The wire format
# --------------------------------------------------------------------------------------


async def test_a_json_delivery_is_the_notice_plus_an_envelope() -> None:
    receiver = Receiver()
    notice = sample()

    assert await hook(receiver).deliver(notice) is True

    (request,) = receiver.requests
    assert request.method == "POST"
    assert str(request.url) == HOOK
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"].startswith("bailment/")
    assert request.headers[EVENT_HEADER] == "expiring"
    body = json.loads(request.content)
    assert set(body) == {"event", "delivery_id", "sent_at", "bailment_version", "notice"}
    assert body["event"] == "expiring"
    assert body["delivery_id"] == request.headers[DELIVERY_HEADER]
    assert body["notice"] == json.loads(json.dumps(notice.as_dict()))


async def test_the_signature_verifies_with_the_secret_and_nothing_else() -> None:
    receiver = Receiver()
    await hook(receiver).deliver(sample())
    (request,) = receiver.requests
    header = request.headers[SIGNATURE_HEADER]
    body = request.content

    assert header.startswith("t=") and ",v1=" in header
    assert verify_signature(SECRET, body, header) is True
    assert verify_signature("some-other-secret", body, header) is False
    assert verify_signature(SECRET, body.replace(b"expiring", b"expired!"), header) is False
    assert verify_signature(SECRET, body, None) is False
    assert verify_signature(SECRET, body, "t=nonsense,v1=abc") is False

    # A captured delivery replayed an hour later is refused, however valid the MAC.
    timestamp = int(header.split(",")[0].removeprefix("t="))
    assert verify_signature(SECRET, body, header, now=timestamp + 3600) is False
    # Rotation: a header carrying the old and the new signature verifies under either.
    rotated = f"{header},v1={sign('next-secret', body, timestamp).split('v1=')[1]}"
    assert verify_signature("next-secret", body, rotated) is True
    assert verify_signature(SECRET, body, rotated) is True


async def test_without_a_secret_there_is_no_signature_header() -> None:
    receiver = Receiver()
    await hook(receiver, secret=None).deliver(sample())
    assert SIGNATURE_HEADER not in receiver.requests[0].headers


async def test_the_slack_format_is_one_escaped_text_field_with_a_link() -> None:
    receiver = Receiver()
    notice = sample("approval_requested", url="http://dashboard.test/leases/3f2a9c1e")
    await hook(receiver, fmt="slack").deliver(notice)

    body = receiver.bodies()[0]
    assert set(body) == {"text"}
    text = body["text"]
    assert text.startswith("*bailment: approval needed*")
    assert "`3f2a9c1e`" in text
    # Slack treats <, > and & as markup, so a message quoting them must escape them.
    assert "&lt;Renew&gt; &amp; carry on." in text
    assert "<http://dashboard.test/leases/3f2a9c1e|Open it in the dashboard>" in text


# --------------------------------------------------------------------------------------
# A receiver that misbehaves
# --------------------------------------------------------------------------------------


async def test_a_5xx_is_retried_then_given_up_on_without_raising() -> None:
    receiver = Receiver(500)
    delivered = await hook(receiver).deliver(sample())
    assert delivered is False
    assert len(receiver.requests) == 3


async def test_a_429_is_retried_and_a_later_success_counts() -> None:
    receiver = Receiver(429, 503, 200)
    assert await hook(receiver).deliver(sample()) is True
    assert len(receiver.requests) == 3
    # One delivery: every retry carries the same delivery id, so a receiver can dedupe.
    assert len({request.headers[DELIVERY_HEADER] for request in receiver.requests}) == 1


async def test_a_4xx_is_not_retried() -> None:
    """The receiver said no. Asking again is noise, not persistence."""
    receiver = Receiver(400)
    assert await hook(receiver).deliver(sample()) is False
    assert len(receiver.requests) == 1


async def test_a_timeout_is_retried_and_logged_by_class_name_only() -> None:
    attempts: list[httpx.Request] = []

    def slow(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("timed out talking to " + str(request.url), request=request)

    notifier = WebhookNotifier(HOOK, transport=httpx.MockTransport(slow), sleep=no_sleep)
    assert await notifier.deliver(sample()) is False
    assert len(attempts) == 3
    # Only scheme and host are ever logged: a Slack URL's path is its credential.
    assert notifier.destination == "https://hooks.example.test"


async def test_a_dead_webhook_never_stops_the_ticker_expiring_a_lease(
    activate: Callable[..., Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    settings: Settings,
    read_lease: Callable[..., Any],
) -> None:
    """The lease must expire whether or not Slack is up."""
    lease = await activate()
    async with sessionmaker() as session:
        row = await session.get(Lease, lease.id)
        assert row is not None
        row.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()

    receiver = Receiver(500)
    ticker = LeaseTicker(
        sessionmaker,
        catalog=catalog,
        settings=settings,
        notifier=FanoutNotifier(log_notice, hook(receiver)),
    )
    report = await ticker.tick()

    assert report.expired == [lease.id]
    assert report.errors == []
    assert (await read_lease(lease.id)).state == LeaseState.EXPIRED.value
    # The overdue lease still warns first, then expires: two notices, three tries each.
    assert receiver.kinds() == ["expiring"] * 3 + ["expired"] * 3


async def test_one_notifier_raising_does_not_stop_the_next() -> None:
    delivered: list[str] = []

    async def broken(notice: LeaseNotice) -> None:
        raise RuntimeError("boom")

    async def working(notice: LeaseNotice) -> None:
        delivered.append(notice.kind)

    await FanoutNotifier(broken, working)(sample())
    assert delivered == ["expiring"]


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_without_a_url_notices_go_to_the_log_only(settings: Settings) -> None:
    assert build_notifier(settings) is log_notice


def test_a_configured_url_adds_a_signed_webhook(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "notify_webhook_url": Settings(notify_webhook_url=HOOK).notify_webhook_url,
            "notify_webhook_secret": Settings(notify_webhook_secret=SECRET).notify_webhook_secret,
            "notify_webhook_format": "slack",
        }
    )
    built = build_notifier(configured)
    assert isinstance(built, FanoutNotifier)
    webhook = built.targets[1]
    assert isinstance(webhook, WebhookNotifier)
    assert webhook.fmt == "slack"
    assert webhook.destination == "https://hooks.example.test"


def test_the_webhook_url_is_validated_and_kept_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(notify_webhook_url="ftp://files.example.test/drop")
    assert "absolute http:// or https:// URL" in str(caught.value)

    settings = Settings(notify_webhook_url=HOOK)
    assert "not-a-real-token" not in repr(settings)
    assert Settings(notify_webhook_url="  ").notify_webhook_url is None


# --------------------------------------------------------------------------------------
# The events that used to reach nobody
# --------------------------------------------------------------------------------------


async def test_an_approval_request_sends_a_notice_with_a_link_to_a_real_page(
    session: AsyncSession,
    catalog: Catalog,
    registry: ProviderRegistry,
    settings: Settings,
    secret_box: SecretBox,
) -> None:
    receiver = Receiver()
    service = LeaseService(
        session,
        catalog=catalog,
        registry=registry,
        settings=settings,
        secret_box=secret_box,
        notifier=hook(receiver),
    )
    outcome = await service.request_provision(
        Caller.agent("agent-one", on_behalf_of="dana", session="s-1"),
        ProvisionRequest(golden_path_id="gated", inputs={"env": "prod", "name": "orders"}),
    )
    await drain()

    assert outcome.lease.state is LeaseState.AWAITING_APPROVAL
    assert receiver.kinds() == ["approval_requested"]
    notice = receiver.bodies()[0]["notice"]
    # /leases/<id> is a page the dashboard has; /approvals/<id>, the old link, was not.
    assert notice["url"] == f"http://bailment.test/leases/{outcome.lease.id}"
    assert outcome.lease.approval is not None
    assert outcome.lease.approval.url == notice["url"]
    assert "branching production data is a decision a human makes" in notice["message"]
    assert "agent-one on behalf of dana" in notice["message"]
    assert notice["lease_id"] == outcome.lease.id
    assert verify_signature(
        SECRET, receiver.requests[0].content, receiver.requests[0].headers[SIGNATURE_HEADER]
    )


async def test_an_allowed_request_sends_no_approval_notice(
    request_lease: Callable[..., Any],
    session: AsyncSession,
    catalog: Catalog,
    registry: ProviderRegistry,
    settings: Settings,
) -> None:
    receiver = Receiver()
    service = LeaseService(
        session, catalog=catalog, registry=registry, settings=settings, notifier=hook(receiver)
    )
    await service.request_provision(
        Caller.agent("agent-one"), ProvisionRequest(golden_path_id="sandbox", inputs={"name": "x"})
    )
    await drain()
    assert receiver.requests == []


async def test_a_teardown_that_gives_up_sends_an_orphaned_notice(
    request_lease: Callable[..., Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    registry: ProviderRegistry,
    settings: Settings,
    secret_box: SecretBox,
    service: LeaseService,
    operator: Caller,
) -> None:
    receiver = Receiver()
    worker = ProvisioningWorker(
        sessionmaker,
        catalog=catalog,
        registry=registry,
        settings=settings,
        secret_box=secret_box,
        notifier=hook(receiver),
    )
    outcome = await request_lease(inputs={"name": "stuck", "simulate": "destroy_failure"})
    await worker.tick()
    await service.revoke(outcome.lease.id, operator, reason="done")

    for _ in range(settings.max_provision_attempts + 1):
        async with sessionmaker() as session:
            row = await session.get(Lease, outcome.lease.id)
            assert row is not None
            if row.state == LeaseState.ORPHANED.value:
                break
            row.next_attempt_at = None
            await session.commit()
        await worker.tick()

    assert receiver.kinds() == ["orphaned"]
    notice = receiver.bodies()[0]["notice"]
    assert notice["lease_id"] == outcome.lease.id
    assert notice["provider"] == "memory"
    assert "retry-teardown" in notice["message"]
    assert "simulated teardown failure" in notice["message"]


async def test_a_vanished_resource_sends_a_drift_notice(
    activate: Callable[..., Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: RecordingProvider,
    registry: ProviderRegistry,
    settings: Settings,
) -> None:
    lease = await activate()
    async with sessionmaker() as session:
        row = await session.get(Lease, lease.id)
        assert row is not None
        row.created_at = row.created_at - timedelta(hours=1)
        row.activated_at = (row.activated_at or utcnow()) - timedelta(hours=1)
        await session.commit()
    provider.vanish(lease.external_name)

    receiver = Receiver()
    summary = await Reconciler(
        sessionmaker, registry=registry, settings=settings, notifier=hook(receiver)
    ).run()

    assert summary.drift_found == 1
    assert receiver.kinds() == ["drift"]
    notice = receiver.bodies()[0]["notice"]
    assert notice["lease_id"] == lease.id
    assert "deleted outside bailment" in notice["message"]


async def test_no_payload_ever_carries_a_sealed_value(
    activate: Callable[..., Any],
    sessionmaker: async_sessionmaker[AsyncSession],
    catalog: Catalog,
    settings: Settings,
    sealed_values: Callable[..., dict[str, str]],
) -> None:
    """Walk a lease through warning and expiry with the webhook on, in both formats, and
    look for the credential in every byte that was sent."""
    lease = await activate()
    secret = sealed_values(lease)[SEALED_OUTPUT]
    assert secret.startswith(SEALED_VALUE_PREFIX)

    receiver = Receiver()
    ticker = LeaseTicker(
        sessionmaker,
        catalog=catalog,
        settings=settings,
        notifier=FanoutNotifier(hook(receiver), hook(receiver, fmt="slack")),
    )
    async with sessionmaker() as session:
        row = await session.get(Lease, lease.id)
        assert row is not None
        row.expires_at = utcnow() + timedelta(seconds=20)
        await session.commit()
    await ticker.tick()
    async with sessionmaker() as session:
        row = await session.get(Lease, lease.id)
        assert row is not None
        row.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    await ticker.tick()

    assert sorted(set(receiver.kinds())) == ["expired", "expiring"]
    for request in receiver.requests:
        assert secret.encode() not in request.content
        assert SEALED_VALUE_PREFIX.encode() not in request.content
        for value in request.headers.values():
            assert SEALED_VALUE_PREFIX not in value
