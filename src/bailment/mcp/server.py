"""The MCP server, generated from the catalog, and written for a model to read.

Every tool an agent sees is derived from a golden path at startup. There is no second
place where a tool is declared, no decorator list, no registry of handlers to keep in
step with the YAML: :meth:`~bailment.catalog.schema.GoldenPath.input_schema_with_lease`
is used verbatim as the tool's input schema, and the tool's name and description come off
the same object the dashboard form and the OSB catalog entry are rendered from. Adding a
capability is adding a file.

**No tool returns a credential.** The caller here is always constructed with
:attr:`~bailment.engine.service.CallerKind.AGENT`, including when the token presented is
an admin token, because the security boundary is the surface and not the privilege. An
``AGENT`` caller is refused by ``resolve_binding`` and by ``approve``, so there is no
combination of token and argument that makes an MCP tool hand back a secret value or let
an agent approve its own request. What an agent gets is a ``bailment://binding/...``
reference and the exact ``bailment exec`` command that injects the value into a
subprocess it never reads.

----

**Why the result text is written the way it is.**

Tool results are the only channel through which this system talks to a model, and a model
does not read a result -- it acts on one. Four failure modes are known and each one is
answered deliberately:

*Agents route around vague refusals.* A model told "denied" will try the same request with
different arguments, then a different golden path, then a shell command that does the same
thing. So a denial states the policy reason verbatim and then says, in words, that
retrying will not help and that another route does not exist. The same applies to an
approval gate: it says a **human** must decide, gives the reason, and tells the agent to
stop and hand off rather than poll aggressively or find another way in.

*Agents forget that a call is asynchronous.* A queued provision returns the lease id, the
state, and an instruction to poll ``bailment_lease_status`` -- along with the fact that
calling the provision tool again provisions a second resource, which is the mistake this
sentence exists to prevent.

*Agents reason badly about time they cannot see.* Every result carries the seconds
remaining on the lease, so a model can decide for itself whether to renew before starting
something long. A lease that has not started its clock says so explicitly rather than
omitting the field, because an absent number reads as zero.

*Agents ask for the value.* An active lease's result says where the credential is, how to
use it, and that there is no code path that returns it. Explaining the ``bailment exec``
mechanism once, at the moment the model needs it, is far more effective than a policy
document it never sees.

An ``isError`` result means bailment could not carry out the call: the lease does not
exist, the arguments do not fit the schema, the state is wrong. A *policy* denial is not
an error -- the tool worked perfectly and the answer is no -- so it comes back as an
ordinary result whose text is unambiguous about being final. Marking a denial as an error
invites the retry loop the wording is there to prevent.

----

**Transport.** Streamable HTTP, mounted on the same FastAPI application, stateful. Stateful
because the MCP session id is what :attr:`bailment.models.Lease.agent_session` is for --
one runaway agent session has to be traceable end to end -- and because it is what makes a
repeated tool call idempotent: an identical provision call within one session carries the
same derived idempotency key and returns the same lease instead of a second resource. The
cost is that a deployment behind a load balancer must route by ``mcp-session-id``.

**The on-behalf-of header is a claim, not an identity.** ``X-Bailment-On-Behalf-Of`` is
recorded so the audit log can answer "which agent" and "who is accountable" separately.
It is supplied by the agent and is not authenticated. It grants nothing: the requester is
still the token's principal, and visibility is still decided from that. Policy rules must
therefore never grant privileges on ``on_behalf_of``; it is there to be read by a human
after the fact.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Final

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool, ToolAnnotations
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from bailment import __version__
from bailment.catalog.loader import Catalog
from bailment.catalog.schema import GoldenPath
from bailment.config import Settings
from bailment.engine.service import (
    Caller,
    LeaseService,
    LeaseView,
    ProvisionOutcome,
    ProvisionRequest,
    ServiceError,
)
from bailment.logging import get_logger
from bailment.providers.registry import ProviderRegistry
from bailment.states import LeaseState

__all__ = [
    "AGENT_SESSION_HEADER",
    "ON_BEHALF_OF_HEADER",
    "SERVER_INSTRUCTIONS",
    "BailmentMCP",
    "build_mcp_server",
]

log = get_logger("bailment.mcp")

#: Set by the agent to say which human it is working for. See the module docstring for
#: what this does and does not mean.
ON_BEHALF_OF_HEADER: Final = "X-Bailment-On-Behalf-Of"

#: Fallback session identifier, for a client that does not carry an MCP session id.
AGENT_SESSION_HEADER: Final = "X-Bailment-Agent-Session"

TOOL_LEASE_STATUS: Final = "bailment_lease_status"
TOOL_LIST_LEASES: Final = "bailment_list_leases"
TOOL_RELEASE_LEASE: Final = "bailment_release_lease"
TOOL_RENEW_LEASE: Final = "bailment_renew_lease"
TOOL_CATALOG: Final = "bailment_catalog"

#: Shown to the model once, when it connects. Deliberately short: it is competing for
#: attention with the system prompt and everything else in the context window, so it says
#: only the three things that change how the tools get used.
SERVER_INSTRUCTIONS: Final = (
    "bailment provisions real infrastructure on a time-boxed lease and hands you a "
    "capability rather than a credential.\n"
    "\n"
    "Three things worth knowing before you call anything:\n"
    "1. Provisioning is asynchronous. A provision tool returns a lease id and a state; "
    "poll bailment_lease_status until it is active. Calling the provision tool a second "
    "time creates a second resource.\n"
    "2. You will never receive a credential value. An active lease gives you a "
    "bailment://binding/... reference; run the command that needs the credential as "
    "'bailment exec <lease id> -- <command>' and bailment injects it into that process. "
    "There is no tool, argument or header that returns the value itself.\n"
    "3. Leases expire and the resource is then destroyed. Every result tells you how many "
    "seconds are left. Renew with bailment_renew_lease before the deadline, or release "
    "early with bailment_release_lease when you are finished -- releasing what you no "
    "longer need is the single most useful thing you can do here."
)


# --------------------------------------------------------------------------------------
# Tool schemas for the lease-management tools
# --------------------------------------------------------------------------------------


def _lease_id_property(purpose: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "description": (
            f"The lease id {purpose}. It is the 'lease_id' returned by the provision tool, "
            f"a uuid such as 3f2a9c1e-....  Not the golden path id and not a resource name."
        ),
    }


_STATUS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lease_id"],
    "properties": {"lease_id": _lease_id_property("to look up")},
}

_LIST_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "state": {
            "type": "string",
            "enum": [state.value for state in LeaseState],
            "description": "Only leases in this state.",
        },
        "live_only": {
            "type": "boolean",
            "description": (
                "Only leases that are believed to exist at a provider and therefore cost "
                "money. This is the filter to use when deciding what to release."
            ),
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
}

_RELEASE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lease_id"],
    "properties": {
        "lease_id": _lease_id_property("to end"),
        "reason": {
            "type": "string",
            "description": (
                "Why you are releasing it, in one sentence. Goes into the audit trail, "
                "where a human reads it later. 'finished with it' is a fine answer."
            ),
        },
    },
}

_RENEW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lease_id"],
    "properties": {
        "lease_id": _lease_id_property("to extend"),
        "ttl": {
            "type": "string",
            "description": (
                "How much longer you need, measured from now rather than added to the "
                "existing deadline: '2h', '45m', '1h30m'. Omit for the golden path's "
                "default. Anything above the path's ceiling is clamped to it."
            ),
        },
    },
}

_CATALOG_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}


# --------------------------------------------------------------------------------------
# Result rendering
# --------------------------------------------------------------------------------------


#: States in which no resource was ever created and none ever will be, so there is no
#: deadline to reason about and no point suggesting one is coming.
_NEVER_STARTS_STATES: frozenset[LeaseState] = frozenset({LeaseState.REJECTED, LeaseState.FAILED})


def _ok(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[TextContent(type="text", text=text)], isError=False)


def _failed(text: str) -> types.CallToolResult:
    """A call bailment could not carry out. Not the same thing as a policy denial."""
    return types.CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


def _clock(view: LeaseView) -> str:
    """One sentence about time. Never omitted, never rendered as a bare number."""
    remaining = view.seconds_remaining
    if remaining is None:
        if view.state in _NEVER_STARTS_STATES:
            # Telling a model that the clock "starts when the resource exists" for a lease
            # that was rejected invites it to wait for something that is never coming, and
            # a model that is waiting is a model that will eventually try again.
            return "There is no lease clock: nothing was created and nothing will be."
        return (
            f"The lease clock has not started. It starts when the resource exists and then "
            f"runs for {view.ttl_seconds} seconds."
        )
    if remaining <= 0:
        return (
            f"Seconds remaining: {remaining} (the deadline passed {-remaining} seconds ago; "
            f"teardown is queued and the credential is about to stop working)."
        )
    return f"Seconds remaining: {remaining}" + (
        f", until {view.expires_at.isoformat()}." if view.expires_at else "."
    )


def _facts(view: LeaseView) -> str:
    return (
        f"lease_id: {view.id}\n"
        f"state: {view.state.value}\n"
        f"golden_path: {view.golden_path_id}\n"
        f"{_clock(view)}"
    )


def _binding_block(view: LeaseView) -> str:
    if view.binding_reference is None:
        return "This lease produced no credential; the golden path declares no secret outputs."
    published = (
        "\n".join(f"  {name} = {value}" for name, value in sorted(view.outputs.items()))
        or "  (none)"
    )
    sealed = ", ".join(view.secret_output_names) or "(none)"
    return (
        f"Secret reference: {view.binding_reference}\n"
        f"Sealed values, by name (the values themselves are encrypted and are never "
        f"returned to you): {sealed}\n"
        f"Non-secret outputs, in plain text:\n{published}\n"
        f"\n"
        f"To use the credential, run the command that needs it through bailment:\n"
        f"  bailment exec {view.id} -- <your command>\n"
        f"bailment decrypts the value and puts it in that process's environment. It is not "
        f"printed, not passed on a command line and not returned by any tool. Do not look "
        f"for another way to read it; there is not one, and asking is refused."
    )


def _approval_block(view: LeaseView) -> str:
    approval = view.approval
    if approval is None:
        return "A human has to approve this request before anything is provisioned."
    approvers = ", ".join(approval.allowed_approvers) or "any platform operator"
    deadline = (
        f"\nIf nobody answers by {approval.deadline_at.isoformat()} the request is closed "
        f"and you will have to ask again."
        if approval.deadline_at
        else ""
    )
    link = f"\nThe approver's link is {approval.url}." if approval.url else ""
    return (
        f"Reason, verbatim from the policy that matched:\n"
        f"  {approval.reason}\n"
        f"\n"
        f"Who can approve it: {approvers}.{link}{deadline}"
    )


def _dedupe_notices(view: LeaseView, notices: Sequence[str]) -> list[str]:
    """Drop notices whose text the body already carries verbatim.

    The service attaches the policy reason as a notice, and the DENIED and
    APPROVAL REQUIRED bodies quote that same reason under "Reason, verbatim". Rendering
    both printed a full paragraph twice in the one result a model reads most carefully.
    Repetition in a tool result is not free: it spends context, and a model that sees the
    same sentence under two headings reasonably wonders which of the two it is being asked
    to treat as new information.
    """
    already = {
        text.strip() for text in (view.policy_reason, view.failure_reason) if text and text.strip()
    }
    seen: set[str] = set()
    kept: list[str] = []
    for notice in notices:
        stripped = notice.strip()
        if not stripped or stripped in already or stripped in seen:
            continue
        seen.add(stripped)
        kept.append(notice)
    return kept


def _render_view(view: LeaseView, *, notices: Sequence[str] = ()) -> str:
    """The one renderer. Every tool that shows a lease uses it, so a model sees one shape."""
    state = view.state
    kept = _dedupe_notices(view, notices)
    tail = ("\n\nAlso worth knowing:\n" + "\n".join(f"- {n}" for n in kept)) if kept else ""

    if state is LeaseState.AWAITING_APPROVAL:
        return (
            f"APPROVAL REQUIRED. A human must decide this. Nothing has been provisioned and "
            f"nothing is costing money.\n"
            f"\n"
            f"{_approval_block(view)}\n"
            f"\n"
            f"{_facts(view)}\n"
            f"\n"
            f"What to do now: tell the person you are working for that this request needs "
            f"their approval, and then stop and wait. Do not call the provision tool again "
            f"-- a second call raises a second approval request, not a faster one. Do not "
            f"look for another golden path or another mechanism that gets you the same "
            f"resource without the approval; the gate is deliberate and going around it is "
            f"the thing it exists to prevent. You may poll {TOOL_LEASE_STATUS} occasionally "
            f"to see whether it was answered.{tail}"
        )

    if state is LeaseState.REJECTED:
        reason = view.policy_reason or view.failure_reason or "no reason was recorded"
        return (
            f"DENIED. This request was refused and retrying will not help.\n"
            f"\n"
            f"Reason, verbatim:\n"
            f"  {reason}\n"
            f"\n"
            f"{_facts(view)}\n"
            f"\n"
            f"This is a decision, not a transient failure. The same request will be refused "
            f"again, and the same rule applies to the other golden paths that would give you "
            f"the same capability, so looking for another route is wasted effort. If you "
            f"think the reason does not apply to what you are doing, say so to the person "
            f"you are working for and let them take it up with the platform team.{tail}"
        )

    if state in (LeaseState.PENDING, LeaseState.PROVISIONING):
        return (
            f"QUEUED. The request was accepted and a worker is provisioning it. Nothing "
            f"exists yet.\n"
            f"\n"
            f"{_facts(view)}\n"
            f"\n"
            f'What to do now: call {TOOL_LEASE_STATUS} with lease_id "{view.id}" until the '
            f'state is "active", which usually takes a few seconds and occasionally a '
            f"minute or two. Do not call the provision tool again while you wait: a second "
            f"call creates a second resource that also costs money and also has to be "
            f'cleaned up. If the state becomes "failed", the reason will be in the '
            f"result.{tail}"
        )

    if state in (LeaseState.ACTIVE, LeaseState.EXPIRING):
        urgency = (
            "\n\nThis lease is inside its warning window: it ends soon. Renew it with "
            f"{TOOL_RENEW_LEASE} if you are still using it, or let it go."
            if state is LeaseState.EXPIRING
            else ""
        )
        return (
            f"ACTIVE. The resource exists and the lease is running.\n"
            f"\n"
            f"{_facts(view)}\n"
            f"\n"
            f"{_binding_block(view)}\n"
            f"\n"
            f"When the lease ends the resource is destroyed and the credential stops "
            f"working, mid-command if necessary. Renew with {TOOL_RENEW_LEASE} before then "
            f"if you need longer ({view.renewals} of {view.max_renewals} renewals used"
            f"{'' if view.renewable else '; this path is not renewable'}), and call "
            f"{TOOL_RELEASE_LEASE} as soon as you are finished rather than leaving it to "
            f"expire.{urgency}{tail}"
        )

    if state in (LeaseState.EXPIRED, LeaseState.REVOKED, LeaseState.DEPROVISIONING):
        return (
            f"ENDING. This lease is over and a worker is destroying the resource. The "
            f"credential has stopped working or is about to.\n"
            f"\n"
            f"{_facts(view)}\n"
            f"\n"
            f"There is nothing to wait for here. If you still need this capability, request "
            f"a new lease; a released one cannot be brought back.{tail}"
        )

    if state is LeaseState.RELEASED:
        return (
            f"RELEASED. The provider confirmed the resource is gone and the credential is "
            f"dead.\n"
            f"\n"
            f"{_facts(view)}\n"
            f"\n"
            f"Request a new lease if you need the capability again.{tail}"
        )

    if state is LeaseState.FAILED:
        return (
            f"FAILED. Provisioning did not work and nothing was left behind.\n"
            f"\n"
            f"Reason, verbatim:\n"
            f"  {view.failure_reason or 'no reason was recorded'}\n"
            f"\n"
            f"{_facts(view)}\n"
            f"\n"
            f"Read the reason before trying again. If it names something about your "
            f"arguments, fix that and request a new lease. If it names the provider or a "
            f"missing credential, that is a platform problem and repeating the request will "
            f"not solve it -- tell the person you are working for.{tail}"
        )

    if state is LeaseState.ORPHANED:
        return (
            f"ORPHANED. A resource may still exist at the provider and bailment could not "
            f"destroy it. A human has to deal with this.\n"
            f"\n"
            f"Detail: {view.failure_reason or 'no detail recorded'}\n"
            f"\n"
            f"{_facts(view)}\n"
            f"\n"
            f"Do not try to clean this up yourself and do not retry: bailment is already "
            f"reporting it on every reconcile run, which is how it gets fixed. Tell the "
            f"person you are working for that lease {view.id} is orphaned.{tail}"
        )

    return (
        f"UNKNOWN. bailment cannot currently tell what the provider has done with this "
        f"lease, and it will not guess.\n"
        f"\n"
        f"{_facts(view)}\n"
        f"\n"
        f"Poll {TOOL_LEASE_STATUS} again shortly. Do not assume the resource is gone and do "
        f"not assume it is usable.{tail}"
    )


def _render_outcome(outcome: ProvisionOutcome) -> types.CallToolResult:
    prefix = (
        "This idempotency key had already been used, so no new resource was created.\n\n"
        if outcome.replayed
        else ""
    )
    return _ok(prefix + _render_view(outcome.lease, notices=outcome.notices))


def _render_catalog(catalog: Catalog, registry: ProviderRegistry) -> str:
    lines: list[str] = [
        "Golden paths this broker offers. Each one has a tool of its own; call that tool "
        "to take out a lease.",
        "",
    ]
    for path in catalog.enabled():
        gated = any(rule.effect == "require_approval" for rule in path.policy)
        available = path.provider in registry and registry.get(path.provider).is_available()
        outputs = ", ".join(f"{o.name}{'' if o.secret else ' (plain text)'}" for o in path.outputs)
        lines.extend(
            [
                f"## {path.id} -- {path.name}",
                f"tool: {path.mcp_tool_name}",
                f"lease: {path.lease.default_ttl} by default, {path.lease.max_ttl} at most"
                + (
                    f", renewable {path.lease.max_renewals} time(s)"
                    if path.lease.renewable
                    else ", not renewable"
                ),
                f"cost: about ${path.cost.estimated_hourly_usd:.4f} per hour"
                if path.cost.estimated_hourly_usd
                else "cost: free",
                f"outputs: {outputs or '(none)'}",
                (
                    "approval: some requests on this path need a human to approve them; "
                    "the tool result will say so and why."
                    if gated
                    else "approval: not normally required."
                ),
                (
                    ""
                    if available
                    else "AVAILABILITY: the provider behind this path is not configured at "
                    "this installation, so a request will not be provisioned until an "
                    "operator fixes that."
                ),
                path.description.strip(),
                "",
            ]
        )
    if len(lines) == 2:
        lines.append("There are no enabled golden paths at this installation.")
    return "\n".join(lines)


def _render_lease_list(views: Sequence[LeaseView]) -> str:
    if not views:
        return (
            "You hold no leases matching that filter. Nothing of yours is running and "
            "nothing of yours is costing money."
        )
    rows = [
        f"- {view.id}  {view.state.value:<18} {view.golden_path_id:<14} "
        + (
            f"{view.seconds_remaining}s left"
            if view.seconds_remaining is not None
            else "clock not started"
        )
        for view in views
    ]
    live = sum(1 for view in views if view.usable)
    return (
        f"{len(views)} lease(s), {live} of them usable right now.\n"
        + "\n".join(rows)
        + f"\n\nCall {TOOL_LEASE_STATUS} with one of those ids for the detail, and "
        f"{TOOL_RELEASE_LEASE} for anything you have finished with."
    )


# --------------------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------------------


class BailmentMCP:
    """An MCP server whose tools are the catalog, mounted as an ASGI app.

    The instance is the ASGI application: ``Route("/mcp", endpoint=this)`` works because
    Starlette treats a non-function endpoint as a raw ASGI callable. Its lifespan has to
    be entered before it will serve anything -- :meth:`lifespan` is what
    :mod:`bailment.main` wires into the application's own lifespan.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        catalog: Catalog,
        registry: ProviderRegistry,
        settings: Settings,
        json_response: bool = False,
        stateless: bool = False,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.catalog = catalog
        """Swappable. A catalog reload assigns a new one and the next ``tools/list`` shows
        it; bailment does not emit ``notifications/tools/list_changed``, so a client that
        cached the old list keeps it until it asks again."""

        self.registry = registry
        self.settings = settings
        self.server: Server[Any, Any] = Server(
            name="bailment",
            version=__version__,
            instructions=SERVER_INSTRUCTIONS,
        )
        self.session_manager = StreamableHTTPSessionManager(
            app=self.server,
            json_response=json_response,
            stateless=stateless,
        )
        self._register()

    # -- ASGI ---------------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point, with the credential check in front of the transport.

        The check is here rather than only inside the tool handlers because
        ``tools/list`` has no error channel of its own: without this, anyone who can reach
        the port could enumerate every golden path this installation offers, complete with
        its descriptions and costs, while the OSB catalog next door requires a token for
        exactly the same information. A 401 with ``WWW-Authenticate`` is also what an MCP
        client expects, and is what makes it go and look for credentials.

        The tool handlers still authenticate for themselves. Two checks of one header is
        cheap, and the one that decides what a caller may *do* belongs next to the code
        that does it.
        """
        if scope["type"] == "http" and not self._credentials_ok(scope):
            response = JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": (
                        "this bailment broker did not recognise your credentials. Configure "
                        "the MCP client with 'Authorization: Bearer <token>' using a token "
                        "the operator issued."
                    ),
                },
                headers={"WWW-Authenticate": 'Bearer realm="bailment"'},
            )
            await response(scope, receive, send)
            return
        await self.session_manager.handle_request(scope, receive, send)

    def _credentials_ok(self, scope: Scope) -> bool:
        if self.settings.allow_anonymous:
            return True
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() == b"authorization":
                presented = _bearer(raw_value.decode("latin-1"))
                return presented is not None and self.settings.lookup_token(presented) is not None
        return False

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Run the session manager's task group for as long as the app is up."""
        async with self.session_manager.run():
            yield

    # -- caller identity ----------------------------------------------------------------

    def _http_request(self) -> Request | None:
        try:
            context = self.server.request_context
        except LookupError:  # pragma: no cover - only outside a request
            return None
        request = context.request
        return request if isinstance(request, Request) else None

    def _caller(self) -> Caller:
        """Who is calling. Always an agent; see the module docstring for why.

        An unauthenticated call is refused unless ``allow_anonymous`` is set, which is the
        local-demo switch. Note that the principal decides which leases are visible, so an
        installation running anonymously has one shared pool of leases -- which is fine for
        a demo and is why the switch is off by default.
        """
        request = self._http_request()
        # A plain dict, not the Headers object: starlette lowercases the keys on the way
        # in, so every lookup below is case-insensitive for free, and the no-request case
        # (a tool invoked over a transport that has no HTTP layer) needs no second branch.
        headers: Mapping[str, str] = dict(request.headers) if request is not None else {}
        presented = _bearer(headers.get("authorization", ""))
        identity = self.settings.lookup_token(presented) if presented else None
        session = headers.get("mcp-session-id") or headers.get(AGENT_SESSION_HEADER.lower())
        on_behalf_of = (headers.get(ON_BEHALF_OF_HEADER.lower()) or "").strip() or None

        if identity is None:
            if presented is None and self.settings.allow_anonymous:
                return Caller.agent("anonymous", on_behalf_of=on_behalf_of, session=session)
            raise _Unauthenticated(
                "this bailment broker did not recognise your credentials. The MCP client "
                "has to send 'Authorization: Bearer <token>' with a token the operator "
                "configured. Nothing you can do from here fixes this; tell the person you "
                "are working for."
            )
        return Caller.agent(identity.principal, on_behalf_of=on_behalf_of, session=session)

    def _idempotency_key(
        self, tool: str, arguments: Mapping[str, Any], caller: Caller
    ) -> str | None:
        """A key derived from the call itself, so a retry does not double-provision.

        Scoped to the MCP session. Without a session id there is no key at all, which is
        deliberate: a process-wide key would collapse two unrelated agents that happened to
        ask the same principal for the same thing into one lease, and quietly handing
        somebody else's resource to an agent is far worse than provisioning a second one.

        A retried call is therefore idempotent within a session and not across sessions,
        which matches what a retry actually is.
        """
        if not caller.session:
            return None
        canonical = json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"{tool}\x00{canonical}".encode()).hexdigest()[:32]
        return f"mcp:{caller.session[:64]}:{digest}"

    # -- tools --------------------------------------------------------------------------

    def _tools(self) -> list[Tool]:
        tools: list[Tool] = []
        for path in self.catalog.enabled():
            tools.append(
                Tool(
                    name=path.mcp_tool_name,
                    title=path.name,
                    description=_provision_description(path),
                    # Verbatim, and this is the point of the whole catalog design: the
                    # schema an agent validates against is the schema the dashboard form
                    # and the OSB plan were rendered from.
                    inputSchema=path.input_schema_with_lease(),
                    annotations=ToolAnnotations(
                        title=path.name,
                        readOnlyHint=False,
                        destructiveHint=False,
                        idempotentHint=False,
                        openWorldHint=True,
                    ),
                )
            )
        tools.extend(
            [
                Tool(
                    name=TOOL_LEASE_STATUS,
                    title="Check a lease",
                    description=(
                        "The current state of one lease, and what to do about it. This is "
                        "the tool to poll after a provision call: it tells you whether the "
                        "resource exists yet, how many seconds are left on the lease, and "
                        "how to use the credential once it is active."
                    ),
                    inputSchema=_STATUS_SCHEMA,
                    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
                ),
                Tool(
                    name=TOOL_LIST_LEASES,
                    title="List your leases",
                    description=(
                        "Every lease you hold, newest first. Use it to find out what you "
                        "have left running before asking for more, and to find the id of "
                        "something you provisioned earlier in this task."
                    ),
                    inputSchema=_LIST_SCHEMA,
                    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
                ),
                Tool(
                    name=TOOL_RENEW_LEASE,
                    title="Extend a lease",
                    description=(
                        "Push a lease's deadline out, measured from now. Only works while "
                        "the lease is active or expiring, only if the golden path allows "
                        "renewal, and only up to that path's limit. Renew before the "
                        "deadline: once a lease expires the resource is destroyed and there "
                        "is nothing left to extend."
                    ),
                    inputSchema=_RENEW_SCHEMA,
                    annotations=ToolAnnotations(
                        readOnlyHint=False, destructiveHint=False, idempotentHint=False
                    ),
                ),
                Tool(
                    name=TOOL_RELEASE_LEASE,
                    title="End a lease now",
                    description=(
                        "End a lease early and destroy the resource behind it. Do this as "
                        "soon as you have finished with something rather than leaving it to "
                        "expire -- it is the single most useful thing you can do here. It "
                        "cannot be undone: the resource and everything in it are destroyed, "
                        "and the credential stops working."
                    ),
                    inputSchema=_RELEASE_SCHEMA,
                    annotations=ToolAnnotations(
                        readOnlyHint=False, destructiveHint=True, idempotentHint=True
                    ),
                ),
                Tool(
                    name=TOOL_CATALOG,
                    title="What can be provisioned",
                    description=(
                        "The golden paths this installation offers, with the lease "
                        "durations, costs and which ones need a human to approve them. Call "
                        "it when you are not sure which provision tool you want, or to find "
                        "out why one you expected is missing."
                    ),
                    inputSchema=_CATALOG_SCHEMA,
                    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
                ),
            ]
        )
        return tools

    def _register(self) -> None:
        """Attach the two handlers the MCP protocol needs from us.

        The ``type: ignore`` pairs are the SDK's decorators, which carry no annotations of
        their own; under ``mypy --strict`` an unannotated decorator would silently make
        the functions below untyped, which is exactly what we do not want, so the
        suppression is scoped to the decorator line and the handlers stay checked.
        """
        server = self.server

        @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
        async def list_tools() -> list[Tool]:
            return self._tools()

        @server.call_tool()  # type: ignore[untyped-decorator]
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            try:
                caller = self._caller()
            except _Unauthenticated as exc:
                return _failed(str(exc))

            log.info(
                "mcp tool call",
                tool=name,
                principal=caller.principal,
                on_behalf_of=caller.on_behalf_of,
                agent_session=caller.session,
            )
            try:
                return await self._dispatch(name, arguments, caller)
            except _BadArguments as exc:
                return _failed(str(exc))
            except ServiceError as exc:
                # Every ServiceError carries a message written for the caller: the golden
                # path's policy reason, the schema problem, the state conflict. Passing it
                # through verbatim is the whole reason those messages are written that way.
                return _failed(str(exc))

    async def _dispatch(
        self, name: str, arguments: Mapping[str, Any], caller: Caller
    ) -> types.CallToolResult:
        path = self._path_for_tool(name)
        if path is not None:
            return await self._provision(path, arguments, caller, tool=name)
        if name == TOOL_LEASE_STATUS:
            return await self._status(_lease_id(arguments), caller)
        if name == TOOL_LIST_LEASES:
            return await self._list(arguments, caller)
        if name == TOOL_RENEW_LEASE:
            return await self._renew(arguments, caller)
        if name == TOOL_RELEASE_LEASE:
            return await self._release(arguments, caller)
        if name == TOOL_CATALOG:
            return _ok(_render_catalog(self.catalog, self.registry))
        return _failed(
            f"there is no tool called {name!r} at this bailment broker. Call "
            f"{TOOL_CATALOG} to see what is on offer."
        )

    def _path_for_tool(self, name: str) -> GoldenPath | None:
        for path in self.catalog.enabled():
            if path.mcp_tool_name == name:
                return path
        return None

    # -- handlers -----------------------------------------------------------------------

    def _service(self, session: AsyncSession) -> LeaseService:
        return LeaseService(
            session, catalog=self.catalog, registry=self.registry, settings=self.settings
        )

    async def _provision(
        self, path: GoldenPath, arguments: Mapping[str, Any], caller: Caller, *, tool: str
    ) -> types.CallToolResult:
        async with self.sessionmaker() as session:
            service = self._service(session)
            outcome = await service.request_provision(
                caller,
                ProvisionRequest(
                    golden_path_id=path.id,
                    inputs=dict(arguments),
                    idempotency_key=self._idempotency_key(tool, arguments, caller),
                ),
            )
        return _render_outcome(outcome)

    async def _status(self, lease_id: str, caller: Caller) -> types.CallToolResult:
        async with self.sessionmaker() as session:
            service = self._service(session)
            view = await service.get_lease(lease_id, caller)
        return _ok(_render_view(view))

    async def _list(self, arguments: Mapping[str, Any], caller: Caller) -> types.CallToolResult:
        raw_state = arguments.get("state")
        states = [LeaseState(str(raw_state))] if raw_state else None
        async with self.sessionmaker() as session:
            service = self._service(session)
            views = await service.list_leases(
                caller,
                states=states,
                live_only=bool(arguments.get("live_only", False)),
                limit=int(arguments.get("limit", 20)),
            )
        return _ok(_render_lease_list(views))

    async def _renew(self, arguments: Mapping[str, Any], caller: Caller) -> types.CallToolResult:
        ttl = arguments.get("ttl")
        async with self.sessionmaker() as session:
            service = self._service(session)
            outcome = await service.renew(
                _lease_id(arguments), caller, ttl=str(ttl) if ttl else None
            )
        return _ok(
            "RENEWED. The deadline moved.\n\n"
            + _render_view(outcome.lease, notices=outcome.notices)
        )

    async def _release(self, arguments: Mapping[str, Any], caller: Caller) -> types.CallToolResult:
        reason = str(arguments.get("reason") or "").strip() or (
            "released by the agent that requested it; no reason given"
        )
        async with self.sessionmaker() as session:
            service = self._service(session)
            view = await service.revoke(_lease_id(arguments), caller, reason=reason)
        return _ok(
            "RELEASING. The lease is over and the resource is queued for destruction. The "
            "credential stops working as soon as the provider confirms it is gone; there is "
            "nothing further for you to do and nothing to poll.\n\n" + _render_view(view)
        )


class _Unauthenticated(Exception):
    """No usable credentials on an MCP request."""


class _BadArguments(Exception):
    """An argument is missing or the wrong shape.

    The transport validates arguments against the tool's input schema before this code
    runs, so reaching here means a client that skipped validation. The message is written
    for the model anyway, because a model that gets 'KeyError' learns nothing.
    """


def _lease_id(arguments: Mapping[str, Any]) -> str:
    value = arguments.get("lease_id")
    if not isinstance(value, str) or not value.strip():
        raise _BadArguments(
            "this tool needs a 'lease_id' argument: the uuid returned by the provision "
            f"tool, or one of the ids listed by {TOOL_LIST_LEASES}."
        )
    return value.strip()


def _bearer(header: str) -> str | None:
    scheme, _, rest = header.partition(" ")
    value = rest.strip()
    if scheme.strip().lower() != "bearer" or not value:
        return None
    return value


def _provision_description(path: GoldenPath) -> str:
    """The golden path's description, plus what an agent has to know to use the tool.

    The catalog description is written for a capable stranger and says what the thing is,
    what it costs and when not to use it. The paragraph appended here says what happens
    when the tool is called, which is a property of the broker rather than of the path and
    would otherwise have to be repeated in every YAML file.
    """
    gated = any(rule.effect == "require_approval" for rule in path.policy)
    approval = (
        " Some requests on this path need a human to approve them before anything is "
        "provisioned; if yours does, the result will say so and give the reason."
        if gated
        else ""
    )
    return (
        f"{path.description.strip()}\n"
        f"\n"
        f"Calling this tool takes out a lease; it does not return a credential. You get a "
        f"lease id and a state back, the work happens in the background, and you poll "
        f'{TOOL_LEASE_STATUS} until the state is "active". The lease runs for '
        f"{path.lease.default_ttl} unless you pass a different ttl (up to "
        f"{path.lease.max_ttl}), and when it ends the resource is destroyed.{approval}"
    )


def build_mcp_server(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    catalog: Catalog,
    registry: ProviderRegistry,
    settings: Settings,
    json_response: bool = False,
    stateless: bool = False,
) -> BailmentMCP:
    """Construct the MCP server. See :class:`BailmentMCP`."""
    return BailmentMCP(
        sessionmaker,
        catalog=catalog,
        registry=registry,
        settings=settings,
        json_response=json_response,
        stateless=stateless,
    )
