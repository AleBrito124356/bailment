"""Telling people things: lease notices, a log line, and signed webhooks.

A broker whose approval gates are only useful if a human learns there is something to
approve, and whose orphans are only fixed if somebody hears about them, cannot keep its
news in a log file. This module is where every notice goes: always to the structured log,
and -- when ``BAILMENT_NOTIFY_WEBHOOK_URL`` is set -- to an HTTP endpoint, as plain JSON or
as a Slack-compatible message.

Four rules.

**Delivery never breaks the thing that is being reported on.** A webhook that times out,
answers 500 or does not resolve is retried a bounded number of times and then logged at
warning level. It never raises into the ticker, the worker, the reconciler or a request
handler: a lease must expire whether or not Slack is up.

**Notices are sent after the transaction that justified them commits.** Every emitter
collects them and hands them over once the commit has succeeded, because "your lease is
expiring" about a row that was rolled back is the worst message this system could send.

**Nothing credential-shaped is ever in a payload.** :class:`LeaseNotice` is plain data with
no field that could hold a secret, by construction; a payload is that and nothing more. The
webhook URL is itself treated as a secret -- a Slack URL *is* the credential -- so it lives
in a ``SecretStr`` and only its scheme and host ever reach a log line.

**Receivers can check it was us.** With ``BAILMENT_NOTIFY_WEBHOOK_SECRET`` set, every body is
signed with HMAC-SHA256 in an ``X-Bailment-Signature: t=<unix time>,v1=<hex>`` header over
``"<t>." + body``, the scheme Stripe popularised: the timestamp is inside the MAC, so a
captured delivery cannot be replayed later with a fresh one. :func:`verify_signature` is the
receiving side, for anybody writing a handler in Python.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import urlsplit

import httpx

from bailment import __version__
from bailment.logging import get_logger

if TYPE_CHECKING:
    from bailment.config import Settings

__all__ = [
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "SIGNATURE_HEADER",
    "FanoutNotifier",
    "LeaseNotice",
    "NoticeKind",
    "Notifier",
    "WebhookNotifier",
    "build_notifier",
    "drain",
    "log_notice",
    "notify_soon",
    "sign",
    "slack_payload",
    "verify_signature",
    "webhook_payload",
]

log = get_logger("bailment.notify")

NoticeKind = Literal[
    "expiring",
    "expired",
    "teardown_queued",
    "approval_expired",
    "approval_requested",
    "orphaned",
    "drift",
]

SIGNATURE_HEADER: Final = "X-Bailment-Signature"
EVENT_HEADER: Final = "X-Bailment-Event"
DELIVERY_HEADER: Final = "X-Bailment-Delivery"

#: How far a signature's timestamp may be from the receiver's clock. Five minutes, the
#: usual allowance for clock skew plus a slow retry.
DEFAULT_TOLERANCE_SECONDS: Final = 300


@dataclass(frozen=True, slots=True)
class LeaseNotice:
    """Something a human or an agent would want to be told about.

    Deliberately plain data with no credential-shaped fields on it, so that a deployment
    can hand it to Slack, to email or to a webhook without anybody having to audit what
    a notice contains first.
    """

    kind: NoticeKind
    lease_id: str
    golden_path_id: str
    requester: str
    on_behalf_of: str | None
    external_name: str | None
    expires_at: datetime | None
    seconds_remaining: int | None
    message: str
    provider: str | None = None
    url: str | None = None
    """Where a human acts on this: the lease's page in the dashboard, for an approval."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "lease_id": self.lease_id,
            "golden_path": self.golden_path_id,
            "provider": self.provider,
            "requester": self.requester,
            "on_behalf_of": self.on_behalf_of,
            "external_name": self.external_name,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "seconds_remaining": self.seconds_remaining,
            "message": self.message,
            "url": self.url,
        }


Notifier = Callable[[LeaseNotice], Awaitable[None]]


async def log_notice(notice: LeaseNotice) -> None:
    """The notifier that is always there. Structured log line, nothing else.

    A default that tried to reach a network would make the ticker's correctness depend on
    something outside the process, and a default that did nothing at all would make the
    expiry warning invisible in the one deployment shape everybody starts with.
    """
    log.info(
        "lease notice",
        kind=notice.kind,
        lease_id=notice.lease_id,
        golden_path=notice.golden_path_id,
        requester=notice.requester,
        external_name=notice.external_name,
        seconds_remaining=notice.seconds_remaining,
        url=notice.url,
        message=notice.message,
    )


# --------------------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------------------


def sign(secret: str, body: bytes, timestamp: int) -> str:
    """The ``X-Bailment-Signature`` value for ``body`` sent at ``timestamp``."""
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify_signature(
    secret: str,
    body: bytes,
    header: str | None,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Whether ``header`` is a valid, fresh signature of ``body``. For receivers.

    Compares in constant time, accepts several ``v1=`` values (so a sender can sign with
    two secrets while one is being rotated), and refuses a timestamp outside the tolerance
    so a captured delivery cannot be replayed later.
    """
    if not header:
        return False
    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return False
        elif key == "v1" and value:
            candidates.append(value)
    if timestamp is None or not candidates:
        return False
    if abs((time.time() if now is None else now) - timestamp) > tolerance_seconds:
        return False
    expected = sign(secret, body, timestamp).split("v1=", 1)[1]
    return any(hmac.compare_digest(expected, candidate) for candidate in candidates)


# --------------------------------------------------------------------------------------
# Payloads
# --------------------------------------------------------------------------------------

_HEADLINES: Final[dict[str, str]] = {
    "expiring": "lease expiring soon",
    "expired": "lease expired",
    "teardown_queued": "teardown re-queued",
    "approval_expired": "approval request timed out",
    "approval_requested": "approval needed",
    "orphaned": "resource orphaned",
    "drift": "resource vanished",
}


def webhook_payload(notice: LeaseNotice, *, delivery_id: str, sent_at: datetime) -> dict[str, Any]:
    """The JSON body. The notice, plus enough envelope to deduplicate and order deliveries."""
    return {
        "event": notice.kind,
        "delivery_id": delivery_id,
        "sent_at": sent_at.isoformat(),
        "bailment_version": __version__,
        "notice": notice.as_dict(),
    }


def _slack_escape(text: str) -> str:
    # Slack's mrkdwn treats these three as markup; everything else is literal.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(notice: LeaseNotice) -> dict[str, Any]:
    """A Slack incoming-webhook body: one ``text`` field, readable in a channel."""
    headline = _HEADLINES.get(notice.kind, notice.kind)
    lines = [
        f"*bailment: {headline}* -- lease `{notice.lease_id[:8]}` "
        f"({_slack_escape(notice.golden_path_id)}, requested by "
        f"{_slack_escape(notice.on_behalf_of or notice.requester)})",
        _slack_escape(notice.message),
    ]
    if notice.url:
        lines.append(f"<{notice.url}|Open it in the dashboard>")
    return {"text": "\n".join(lines)}


# --------------------------------------------------------------------------------------
# Notifiers
# --------------------------------------------------------------------------------------


class WebhookNotifier:
    """POSTs each notice to one URL, signed if a secret is set. Never raises.

    Bounded everywhere: a per-attempt timeout, a fixed number of attempts, and backoff
    between them measured in fractions of a second. 429 and 5xx and transport errors are
    retried; any other 4xx is a receiver that has told us no, and asking again is noise.
    """

    def __init__(
        self,
        url: str,
        *,
        secret: str | None = None,
        fmt: Literal["json", "slack"] = "json",
        timeout: float = 5.0,
        attempts: int = 3,
        backoff: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url
        self._secret = secret or None
        self.fmt = fmt
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self.backoff = backoff
        self._transport = transport
        self._sleep = sleep

    @property
    def destination(self) -> str:
        """Scheme and host only. The path of a Slack webhook URL is its credential."""
        parts = urlsplit(self._url)
        return f"{parts.scheme}://{parts.hostname or '?'}"

    def _body(self, notice: LeaseNotice, delivery_id: str) -> bytes:
        payload = (
            slack_payload(notice)
            if self.fmt == "slack"
            else webhook_payload(notice, delivery_id=delivery_id, sent_at=datetime.now(UTC))
        )
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    async def __call__(self, notice: LeaseNotice) -> None:
        await self.deliver(notice)

    async def deliver(self, notice: LeaseNotice) -> bool:
        """Send one notice. ``True`` if a 2xx came back, ``False`` if it was given up on."""
        delivery_id = str(uuid.uuid4())
        body = self._body(notice, delivery_id)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"bailment/{__version__}",
            EVENT_HEADER: notice.kind,
            DELIVERY_HEADER: delivery_id,
        }
        if self._secret:
            headers[SIGNATURE_HEADER] = sign(self._secret, body, int(time.time()))

        error = "not attempted"
        attempt = 0
        for attempt in range(1, self.attempts + 1):
            retry = True
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, transport=self._transport, follow_redirects=False
                ) as client:
                    response = await client.post(self._url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                # The class name only: an httpx error message can quote the URL.
                error = type(exc).__name__
            else:
                if 200 <= response.status_code < 300:
                    return True
                error = f"HTTP {response.status_code}"
                retry = response.status_code == 429 or response.status_code >= 500
            if not retry or attempt == self.attempts:
                break
            await self._sleep(self.backoff * (2 ** (attempt - 1)))

        log.warning(
            "webhook delivery failed; the notice is still in the log",
            destination=self.destination,
            kind=notice.kind,
            lease_id=notice.lease_id,
            delivery_id=delivery_id,
            attempts=attempt,
            error=error,
        )
        return False


class FanoutNotifier:
    """Hands a notice to several notifiers. One failing never stops the others."""

    def __init__(self, *targets: Notifier) -> None:
        self.targets: tuple[Notifier, ...] = targets

    async def __call__(self, notice: LeaseNotice) -> None:
        for target in self.targets:
            try:
                await target(notice)
            except Exception:
                log.exception("notifier failed", kind=notice.kind, lease_id=notice.lease_id)


def build_notifier(settings: Settings) -> Notifier:
    """The notifier this configuration asks for: the log, plus a webhook if one is set."""
    url = settings.notify_webhook_url
    if url is None:
        return log_notice
    secret = settings.notify_webhook_secret
    return FanoutNotifier(
        log_notice,
        WebhookNotifier(
            url.get_secret_value(),
            secret=secret.get_secret_value() if secret is not None else None,
            fmt=settings.notify_webhook_format,
            timeout=settings.notify_webhook_timeout_seconds,
        ),
    )


# --------------------------------------------------------------------------------------
# Off the request path
# --------------------------------------------------------------------------------------

_IN_FLIGHT: set[asyncio.Task[None]] = set()


async def _safely(notifier: Notifier, notices: tuple[LeaseNotice, ...]) -> None:
    for notice in notices:
        try:
            await notifier(notice)
        except Exception:
            log.exception("notifier failed", kind=notice.kind, lease_id=notice.lease_id)


def notify_soon(notifier: Notifier, *notices: LeaseNotice) -> asyncio.Task[None] | None:
    """Deliver in the background, so a slow receiver never slows an API response.

    Used by the request path. The task is held in a module-level set until it finishes --
    asyncio keeps only a weak reference to a running task -- and :func:`drain` waits for
    whatever is still in flight, which the application's shutdown does.
    """
    if not notices:
        return None
    task = asyncio.get_running_loop().create_task(_safely(notifier, notices))
    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)
    return task


async def drain(grace: float | None = None) -> None:
    """Wait for background deliveries. Anything still running at the deadline is left."""
    pending = [task for task in _IN_FLIGHT if not task.done()]
    if pending:
        await asyncio.wait(pending, timeout=grace)
