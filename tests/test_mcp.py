"""The MCP server: the surface an agent actually touches.

Tools are dispatched directly rather than over the wire. The transport is the MCP SDK's
and is not bailment's to test; what is bailment's is the mapping from catalog to tool, the
caller kind every tool runs as, and the text a model reads.

The text matters more than it looks. A model does not read a tool result -- it acts on
one, and four failure modes are known:

*Agents route around vague refusals.* A denial states the policy reason verbatim and then
says, in words, that retrying will not help.

*Agents forget a call is asynchronous.* A queued provision says to poll, and says that
calling the provision tool again creates a second resource.

*Agents reason badly about time they cannot see.* Every result carries the seconds
remaining, and a lease whose clock has not started says so rather than omitting the field
-- an absent number reads as zero.

*Agents ask for the value.* An active lease says where the credential is, how to use it,
and that there is no code path that returns it.

So several tests below assert on sentences. They are assertions about behaviour: each one
is the sentence that prevents a specific thing a model does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from bailment.engine.service import Caller, CallerKind, LeaseView, ServiceError
from bailment.mcp.server import (
    SERVER_INSTRUCTIONS,
    TOOL_CATALOG,
    TOOL_LEASE_STATUS,
    TOOL_LIST_LEASES,
    TOOL_RELEASE_LEASE,
    TOOL_RENEW_LEASE,
    BailmentMCP,
    _BadArguments,
    _render_view,
    build_mcp_server,
)
from bailment.states import LeaseState
from conftest import PUBLISHED_OUTPUT, SEALED_OUTPUT, SEALED_VALUE_PREFIX, make_catalog, make_path

MANAGEMENT_TOOLS = {
    TOOL_LEASE_STATUS,
    TOOL_LIST_LEASES,
    TOOL_RENEW_LEASE,
    TOOL_RELEASE_LEASE,
    TOOL_CATALOG,
}


@pytest.fixture
def mcp(sessionmaker, catalog, registry, settings) -> BailmentMCP:
    return build_mcp_server(sessionmaker, catalog=catalog, registry=registry, settings=settings)


def text_of(result: CallToolResult) -> str:
    """The one string a model sees."""
    assert result.content
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def lease_id_of(body: str) -> str:
    """Pull the lease id back out of a tool result, exactly as a model would."""
    return body.split("lease_id: ")[1].split("\n")[0].strip()


async def call(mcp: BailmentMCP, tool: str, caller: Caller, **arguments: Any) -> CallToolResult:
    """Dispatch one tool, converting refusals the way the real handler does.

    ``BailmentMCP._register``'s ``call_tool`` catches ``_BadArguments`` and every
    ``ServiceError`` and returns the message as an ``isError`` result, because a model
    that gets an exception class name learns nothing. Mirroring that here means these
    tests assert on the text an agent actually receives rather than on a traceback the
    transport would have swallowed.
    """
    try:
        return await mcp._dispatch(tool, arguments, caller)
    except (_BadArguments, ServiceError) as exc:
        return CallToolResult(content=[TextContent(type="text", text=str(exc))], isError=True)


# --------------------------------------------------------------------------------------
# Tool generation
# --------------------------------------------------------------------------------------


def test_there_is_one_provision_tool_per_enabled_path_and_no_more(mcp: BailmentMCP) -> None:
    """Adding a capability is adding a file. There is no second place a tool is declared.

    A disabled path must not appear as a tool an agent can call, or the first thing the
    model learns is that the catalog lies.
    """
    names = {tool.name for tool in mcp._tools()}
    assert names == {
        "bailment_provision_sandbox",
        "bailment_provision_gated",
        "bailment_provision_forbidden",
        "bailment_provision_nowhere",
        *MANAGEMENT_TOOLS,
    }
    assert "bailment_provision_switched_off" not in names


def test_a_tool_name_is_namespaced_and_derived_from_the_path_id(mcp: BailmentMCP) -> None:
    """Namespaced so it cannot collide with another MCP server in the same client."""
    for tool in mcp._tools():
        assert tool.name.startswith("bailment_")
        assert "-" not in tool.name


def test_each_tool_schema_is_the_path_schema_verbatim(mcp: BailmentMCP, catalog) -> None:
    """The whole point of the catalog design: the schema an agent validates against is the
    schema the dashboard form and the OSB plan were rendered from."""
    tools = {tool.name: tool for tool in mcp._tools()}
    for path in catalog.enabled():
        tool = tools[path.mcp_tool_name]
        assert tool.inputSchema == path.input_schema_with_lease()
        assert tool.inputSchema["additionalProperties"] is False
        assert "ttl" in tool.inputSchema["properties"]
        assert tool.title == path.name


def test_a_provision_tool_description_carries_the_path_and_the_mechanics(
    mcp: BailmentMCP, catalog
) -> None:
    tools = {tool.name: tool for tool in mcp._tools()}
    sandbox = tools["bailment_provision_sandbox"]
    assert catalog.get("sandbox").description.strip() in sandbox.description
    assert "does not return a credential" in sandbox.description
    assert TOOL_LEASE_STATUS in sandbox.description
    assert "5m" in sandbox.description and "1h" in sandbox.description

    # Only a gated path warns about approval, or the warning stops meaning anything.
    assert "approve" in tools["bailment_provision_gated"].description
    assert "approve" not in sandbox.description


def test_the_management_tools_declare_their_annotations(mcp: BailmentMCP) -> None:
    """A client uses these to decide what to confirm with a human before calling."""
    tools = {tool.name: tool for tool in mcp._tools()}
    assert tools[TOOL_LEASE_STATUS].annotations.readOnlyHint is True
    assert tools[TOOL_CATALOG].annotations.readOnlyHint is True
    assert tools[TOOL_RELEASE_LEASE].annotations.destructiveHint is True
    assert tools[TOOL_RELEASE_LEASE].annotations.idempotentHint is True
    assert tools["bailment_provision_sandbox"].annotations.idempotentHint is False


def test_the_server_instructions_say_the_three_things_that_change_behaviour() -> None:
    """Competing for attention with the system prompt, so it says only what matters."""
    assert "poll bailment_lease_status" in SERVER_INSTRUCTIONS
    assert "never receive a credential value" in SERVER_INSTRUCTIONS
    assert "bailment exec" in SERVER_INSTRUCTIONS
    assert "Leases expire" in SERVER_INSTRUCTIONS
    assert len(SERVER_INSTRUCTIONS) < 1500


# --------------------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------------------


async def test_a_queued_provision_says_it_is_asynchronous_and_not_to_call_again(
    mcp: BailmentMCP, agent: Caller
) -> None:
    result = await call(mcp, "bailment_provision_sandbox", agent, name="from-an-agent")

    assert result.isError is False
    body = text_of(result)
    assert body.startswith("QUEUED.")
    assert TOOL_LEASE_STATUS in body
    assert "a second call creates a second resource" in body
    assert "Seconds remaining" not in body
    assert "The lease clock has not started" in body
    assert "bailment://binding/" not in body


async def test_a_denied_provision_returns_the_policy_reason_and_no_reference(
    mcp: BailmentMCP, agent: Caller
) -> None:
    """A model told only "denied" tries the same request with different arguments, then a
    different golden path, then a shell command that does the same thing.

    It also comes back as an ordinary result rather than ``isError``: the tool worked
    perfectly and the answer is no. Marking a denial as an error invites exactly the retry
    loop the wording exists to prevent.
    """
    result = await call(mcp, "bailment_provision_forbidden", agent)

    assert result.isError is False
    body = text_of(result)
    assert body.startswith("DENIED.")
    assert "this capability is not handed out at this installation" in body
    assert "retrying will not help" in body
    assert "looking for another route is wasted effort" in body
    assert "bailment://" not in body
    assert "bailment exec" not in body


async def test_an_approval_gate_tells_the_agent_to_stop_and_hand_off(
    mcp: BailmentMCP, agent: Caller
) -> None:
    result = await call(mcp, "bailment_provision_gated", agent, env="prod", name="orders")

    body = text_of(result)
    assert body.startswith("APPROVAL REQUIRED.")
    assert "A human must decide this" in body
    assert "branching production data is a decision a human makes" in body
    assert "stop and wait" in body
    assert "going around it is the thing it exists to prevent" in body
    assert "bailment://binding/" not in body


async def test_an_active_lease_returns_the_reference_the_exec_instruction_and_no_value(
    mcp: BailmentMCP, agent: Caller, worker, read_lease, sealed_values
) -> None:
    """The requirement, stated as one test.

    A model that is told where the credential is, how to use it, and that there is no
    other way to read it stops asking -- which is far more effective than a policy
    document it never sees.
    """
    queued = await call(mcp, "bailment_provision_sandbox", agent, name="live-one")
    lease_id = lease_id_of(text_of(queued))
    await worker.tick()

    lease = await read_lease(lease_id)
    values = sealed_values(lease)

    body = text_of(await call(mcp, TOOL_LEASE_STATUS, agent, lease_id=lease_id))

    assert body.startswith("ACTIVE.")
    assert f"bailment://binding/{lease.bindings[0].id}" in body
    assert f"bailment exec {lease_id} -- <your command>" in body
    assert "It is not printed" in body
    assert "there is not one, and asking is refused" in body

    # The sealed name is disclosed; its value is not.
    assert SEALED_OUTPUT in body
    assert values[SEALED_OUTPUT] not in body
    # The non-secret output is published in plain text, which is what makes the
    # classification visible rather than theoretical.
    assert f"{PUBLISHED_OUTPUT} = {values[PUBLISHED_OUTPUT]}" in body
    assert "Seconds remaining:" in body


async def test_no_tool_result_anywhere_contains_a_credential(
    mcp: BailmentMCP, agent: Caller, worker, read_lease, sealed_values
) -> None:
    """The MCP mirror of the API sweep: every tool, called, checked for the real value.

    ``tests/test_api.py`` excludes the ``/mcp`` transport from its HTTP walk and points
    here; this is that coverage.
    """
    queued = await call(mcp, "bailment_provision_sandbox", agent, name="swept")
    lease_id = lease_id_of(text_of(queued))
    await worker.tick()
    secret = sealed_values(await read_lease(lease_id))[SEALED_OUTPUT]
    assert secret.startswith(SEALED_VALUE_PREFIX)

    arguments: dict[str, dict[str, Any]] = {
        "bailment_provision_sandbox": {"name": "swept-again"},
        "bailment_provision_gated": {"env": "prod", "name": "swept"},
        "bailment_provision_forbidden": {},
        "bailment_provision_nowhere": {"name": "swept"},
        TOOL_LEASE_STATUS: {"lease_id": lease_id},
        TOOL_LIST_LEASES: {},
        TOOL_RENEW_LEASE: {"lease_id": lease_id, "ttl": "10m"},
        TOOL_CATALOG: {},
        # Last: it ends the lease everything above reads.
        TOOL_RELEASE_LEASE: {"lease_id": lease_id, "reason": "swept"},
    }
    tools = [tool.name for tool in mcp._tools()]
    assert set(arguments) == set(tools), "a tool was added without being swept"

    for name in arguments:
        body = text_of(await call(mcp, name, agent, **arguments[name]))
        assert secret not in body, name


# --------------------------------------------------------------------------------------
# The security boundary
# --------------------------------------------------------------------------------------


def test_an_mcp_caller_is_always_an_agent_whatever_token_it_presented(
    mcp: BailmentMCP, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary is the surface, not the privilege.

    An admin token presented over MCP still produces an ``AGENT`` caller, which
    ``resolve_binding`` and ``approve`` both refuse. There is no combination of token and
    argument that makes a tool hand back a secret or let an agent approve its own request.
    """
    monkeypatch.setattr(
        mcp,
        "_http_request",
        lambda: type("R", (), {"headers": {"authorization": "Bearer operator-token"}})(),
    )
    caller = mcp._caller()
    assert caller.principal == "operator"
    assert caller.kind is CallerKind.AGENT
    assert caller.may_read_secrets is False
    assert caller.may_see_everything is False


def test_an_agent_cannot_approve_its_own_request_over_mcp(mcp: BailmentMCP, agent: Caller) -> None:
    """There is no approve tool at all, which is the strongest form of the check."""
    assert not any("approve" in tool.name for tool in mcp._tools())


async def test_an_unauthenticated_call_is_refused_unless_anonymous_is_switched_on(
    sessionmaker, catalog, registry, settings
) -> None:
    """``allow_anonymous`` is the local-demo switch and it is off by default, so an
    installation does not become an open broker by omission."""
    closed = build_mcp_server(sessionmaker, catalog=catalog, registry=registry, settings=settings)
    with pytest.raises(Exception, match="did not recognise your credentials"):
        closed._caller()

    open_settings = settings.model_copy(update={"allow_anonymous": True})
    opened = build_mcp_server(
        sessionmaker, catalog=catalog, registry=registry, settings=open_settings
    )
    assert opened._caller().principal == "anonymous"


def test_the_transport_refuses_an_unauthenticated_request_before_tools_list(
    mcp: BailmentMCP, settings
) -> None:
    """``tools/list`` has no error channel of its own.

    Without the check in front of the transport, anyone who could reach the port could
    enumerate every golden path this installation offers -- descriptions, costs and all --
    while the OSB catalog next door needs a token for the same information.
    """

    def scope(header: bytes | None) -> dict[str, Any]:
        headers = [(b"authorization", header)] if header is not None else []
        return {"type": "http", "headers": headers}

    assert mcp._credentials_ok(scope(None)) is False
    assert mcp._credentials_ok(scope(b"Bearer nope")) is False
    assert mcp._credentials_ok(scope(b"Basic operator-token")) is False
    assert mcp._credentials_ok(scope(b"Bearer agent-token")) is True
    assert mcp._credentials_ok(scope(b"Bearer operator-token")) is True


# --------------------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------------------


def test_an_idempotency_key_is_derived_from_the_call_and_scoped_to_the_session(
    mcp: BailmentMCP,
) -> None:
    """A process-wide key would collapse two unrelated agents that happened to ask the
    same principal for the same thing into one lease, and quietly handing somebody else's
    resource to an agent is far worse than provisioning a second one."""
    first = Caller.agent("agent-one", session="session-a")
    second = Caller.agent("agent-one", session="session-b")
    arguments = {"name": "thing", "ttl": "10m"}

    key = mcp._idempotency_key("bailment_provision_sandbox", arguments, first)
    assert key is not None
    assert key.startswith("mcp:session-a:")
    # Stable under argument order, so a client that reserialises does not double-provision.
    assert key == mcp._idempotency_key(
        "bailment_provision_sandbox", {"ttl": "10m", "name": "thing"}, first
    )
    assert key != mcp._idempotency_key("bailment_provision_sandbox", arguments, second)
    assert key != mcp._idempotency_key("bailment_provision_sandbox", {"name": "other"}, first)
    assert key != mcp._idempotency_key("bailment_provision_gated", arguments, first)


def test_a_client_with_no_session_gets_no_key_at_all(mcp: BailmentMCP) -> None:
    """A retry is idempotent within a session and not across sessions, which matches what
    a retry actually is."""
    assert mcp._idempotency_key("t", {}, Caller.agent("agent-one")) is None


async def test_a_repeated_call_in_one_session_returns_the_same_lease(
    mcp: BailmentMCP, agent: Caller, provider, worker
) -> None:
    first = text_of(await call(mcp, "bailment_provision_sandbox", agent, name="retried"))
    second = text_of(await call(mcp, "bailment_provision_sandbox", agent, name="retried"))

    assert "no new resource was created" in second
    assert lease_id_of(first) == lease_id_of(second)
    await worker.tick()
    assert provider.calls["create"] == 1


# --------------------------------------------------------------------------------------
# Lease management tools
# --------------------------------------------------------------------------------------


async def test_listing_leases_is_scoped_and_tells_the_agent_what_to_do_next(
    mcp: BailmentMCP, agent: Caller, other_agent: Caller, worker
) -> None:
    empty = text_of(await call(mcp, TOOL_LIST_LEASES, agent))
    assert "You hold no leases" in empty

    await call(mcp, "bailment_provision_sandbox", agent, name="mine")
    await call(mcp, "bailment_provision_sandbox", other_agent, name="theirs")
    await worker.tick()

    body = text_of(await call(mcp, TOOL_LIST_LEASES, agent))
    assert "1 lease(s)" in body
    assert "theirs" not in body
    assert TOOL_RELEASE_LEASE in body


async def test_the_live_only_filter_is_the_one_for_deciding_what_to_release(
    mcp: BailmentMCP, agent: Caller, worker
) -> None:
    await call(mcp, "bailment_provision_sandbox", agent, name="live")
    await worker.tick()
    await call(mcp, "bailment_provision_forbidden", agent)

    assert "2 lease(s)" in text_of(await call(mcp, TOOL_LIST_LEASES, agent))
    assert "1 lease(s)" in text_of(await call(mcp, TOOL_LIST_LEASES, agent, live_only=True))


async def test_releasing_says_there_is_nothing_left_to_poll(
    mcp: BailmentMCP, agent: Caller, worker, read_lease
) -> None:
    """The single most useful thing an agent can do here, so the result closes the loop
    rather than inviting a wait."""
    queued = await call(mcp, "bailment_provision_sandbox", agent, name="done-with")
    lease_id = lease_id_of(text_of(queued))
    await worker.tick()

    body = text_of(await call(mcp, TOOL_RELEASE_LEASE, agent, lease_id=lease_id, reason="done"))

    assert body.startswith("RELEASING.")
    assert "nothing further for you to do and nothing to poll" in body
    assert (await read_lease(lease_id)).state == LeaseState.REVOKED.value

    await worker.tick()
    assert (await read_lease(lease_id)).state == LeaseState.RELEASED.value


async def test_releasing_without_a_reason_still_records_one(
    mcp: BailmentMCP, agent: Caller, worker, read_lease
) -> None:
    queued = await call(mcp, "bailment_provision_sandbox", agent, name="done-with")
    lease_id = lease_id_of(text_of(queued))
    await worker.tick()

    await call(mcp, TOOL_RELEASE_LEASE, agent, lease_id=lease_id)
    events = (await read_lease(lease_id)).events
    revoked = next(event for event in events if event.action == "revoked")
    assert "no reason given" in revoked.detail["reason"]


async def test_renewing_reports_the_new_deadline(mcp: BailmentMCP, agent: Caller, worker) -> None:
    queued = await call(mcp, "bailment_provision_sandbox", agent, name="longer")
    lease_id = lease_id_of(text_of(queued))
    await worker.tick()

    body = text_of(await call(mcp, TOOL_RENEW_LEASE, agent, lease_id=lease_id, ttl="30m"))
    assert body.startswith("RENEWED.")
    assert "Seconds remaining:" in body
    assert "1 of 2 renewals used" in body


async def test_the_catalog_tool_says_what_is_on_offer_and_what_is_not_usable(
    mcp: BailmentMCP, agent: Caller
) -> None:
    body = text_of(await call(mcp, TOOL_CATALOG, agent))

    assert "## sandbox" in body
    assert "tool: bailment_provision_sandbox" in body
    assert "5m by default, 1h at most, renewable 2 time(s)" in body
    assert f"{PUBLISHED_OUTPUT} (plain text)" in body
    assert "## switched-off" not in body
    # A path whose provider nobody registered says so, rather than looking healthy.
    assert "AVAILABILITY:" in body
    assert "approval: some requests on this path need a human" in body


# --------------------------------------------------------------------------------------
# Failure results
# --------------------------------------------------------------------------------------


async def test_an_unknown_tool_is_an_error_that_points_at_the_catalog(
    mcp: BailmentMCP, agent: Caller
) -> None:
    result = await call(mcp, "bailment_provision_something_else", agent)
    assert result.isError is True
    assert TOOL_CATALOG in text_of(result)


@pytest.mark.parametrize("tool", sorted({TOOL_LEASE_STATUS, TOOL_RENEW_LEASE, TOOL_RELEASE_LEASE}))
async def test_a_missing_lease_id_explains_what_one_is(
    mcp: BailmentMCP, agent: Caller, tool: str
) -> None:
    """A model that gets ``KeyError`` learns nothing.

    The transport validates arguments against the tool's input schema before this code
    runs, so reaching here means a client that skipped validation -- and the message is
    written for the model anyway.
    """
    result = await call(mcp, tool, agent)
    assert result.isError is True
    body = text_of(result)
    assert "lease_id" in body
    assert TOOL_LIST_LEASES in body


async def test_a_lease_belonging_to_somebody_else_is_not_found(
    mcp: BailmentMCP, agent: Caller, other_agent: Caller
) -> None:
    """Deliberately the same answer as an id that does not exist: confirming that a lease
    id is real to somebody who cannot read it is an enumeration oracle."""
    queued = await call(mcp, "bailment_provision_sandbox", agent, name="mine")
    lease_id = lease_id_of(text_of(queued))

    result = await call(mcp, TOOL_LEASE_STATUS, other_agent, lease_id=lease_id)
    assert result.isError is True
    assert "no lease with id" in text_of(result)


async def test_a_failed_lease_explains_whether_retrying_would_help(
    mcp: BailmentMCP, agent: Caller, worker, provider
) -> None:
    queued = await call(mcp, "bailment_provision_sandbox", agent, name="doomed")
    lease_id = lease_id_of(text_of(queued))
    provider.fail_next("create", message="the provider refused: quota exceeded")
    await worker.tick()

    body = text_of(await call(mcp, TOOL_LEASE_STATUS, agent, lease_id=lease_id))

    assert body.startswith("FAILED.")
    assert "quota exceeded" in body
    assert "Read the reason before trying again" in body


async def test_an_orphaned_lease_tells_the_agent_to_stop_and_tell_a_human(
    mcp: BailmentMCP, agent: Caller, worker, provider, sessionmaker
) -> None:
    """Do not try to clean this up yourself: bailment is already reporting it on every
    reconcile run, which is how it gets fixed."""
    from bailment.models import Lease

    queued = await call(mcp, "bailment_provision_sandbox", agent, name="orphan")
    lease_id = lease_id_of(text_of(queued))
    await worker.tick()
    await call(mcp, TOOL_RELEASE_LEASE, agent, lease_id=lease_id, reason="done")
    provider.fail_next("destroy", message="403 from the provider")
    await worker.tick()

    async with sessionmaker() as opened:
        lease = await opened.get(Lease, lease_id)
        assert lease is not None and lease.state == LeaseState.ORPHANED.value

    body = text_of(await call(mcp, TOOL_LEASE_STATUS, agent, lease_id=lease_id))
    assert body.startswith("ORPHANED.")
    assert "Do not try to clean this up yourself and do not retry" in body
    assert f"lease {lease_id} is orphaned" in body


async def test_an_unknown_state_refuses_to_guess(
    mcp: BailmentMCP, agent: Caller, worker, sessionmaker
) -> None:
    from bailment.models import Lease

    queued = await call(mcp, "bailment_provision_sandbox", agent, name="murky")
    lease_id = lease_id_of(text_of(queued))
    await worker.tick()
    async with sessionmaker() as opened:
        lease = await opened.get(Lease, lease_id)
        assert lease is not None
        lease.state = LeaseState.UNKNOWN.value
        await opened.commit()

    body = text_of(await call(mcp, TOOL_LEASE_STATUS, agent, lease_id=lease_id))
    assert body.startswith("UNKNOWN.")
    assert "it will not guess" in body
    assert "Do not assume the resource is gone and do not assume it is usable" in body


async def test_an_expiring_lease_is_told_it_is_inside_the_warning_window(
    mcp: BailmentMCP, agent: Caller, worker, ticker, sessionmaker
) -> None:
    from datetime import timedelta

    from bailment.models import Lease, utcnow

    queued = await call(mcp, "bailment_provision_sandbox", agent, name="ending")
    lease_id = lease_id_of(text_of(queued))
    await worker.tick()
    async with sessionmaker() as opened:
        lease = await opened.get(Lease, lease_id)
        assert lease is not None
        lease.expires_at = utcnow() + timedelta(seconds=20)
        await opened.commit()
    await ticker.tick()

    body = text_of(await call(mcp, TOOL_LEASE_STATUS, agent, lease_id=lease_id))
    assert "inside its warning window" in body
    assert TOOL_RENEW_LEASE in body


async def test_a_notice_is_appended_rather_than_replacing_the_result(
    mcp: BailmentMCP, agent: Caller
) -> None:
    """A clamped TTL teaches the caller its bounds without hiding the answer."""
    body = text_of(await call(mcp, "bailment_provision_sandbox", agent, name="greedy", ttl="48h"))
    assert body.startswith("QUEUED.")
    assert "Also worth knowing:" in body
    assert "clamped" in body


# --------------------------------------------------------------------------------------
# Catalog reloads
# --------------------------------------------------------------------------------------


def test_swapping_the_catalog_changes_the_next_tool_list(mcp: BailmentMCP) -> None:
    """bailment does not emit ``notifications/tools/list_changed``; a client that cached
    the old list keeps it until it asks again, and that is worth knowing."""
    mcp.catalog = make_catalog(make_path("brand-new"))
    names = {tool.name for tool in mcp._tools()}
    assert names == {"bailment_provision_brand_new", *MANAGEMENT_TOOLS}


# --------------------------------------------------------------------------------------
# Result text a model actually has to act on
# --------------------------------------------------------------------------------------


def _view(**overrides: object) -> LeaseView:
    """A LeaseView with every required field filled, so a test states only what it tests."""
    base: dict[str, object] = {
        "id": "11111111-1111-1111-1111-111111111111",
        "golden_path_id": "dns-record",
        "provider": "cloudflare",
        "state": LeaseState.REJECTED,
        "requester": "agent-demo",
        "on_behalf_of": "alejandro",
        "inputs": {},
        "created_at": datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        "activated_at": None,
        "expires_at": None,
        "released_at": None,
        "seconds_remaining": None,
        "ttl_seconds": 28800,
        "max_ttl_seconds": 604800,
        "renewals": 0,
        "max_renewals": 3,
        "renewable": True,
        "policy_effect": "deny",
        "policy_reason": "reserved",
        "external_name": None,
        "estimated_hourly_usd": 0.0,
        "binding_reference": None,
        "outputs": {},
        "secret_output_names": (),
        "approval": None,
        "failure_reason": None,
    }
    base.update(overrides)
    return LeaseView(**base)  # type: ignore[arg-type]


async def test_a_denied_result_states_the_reason_exactly_once() -> None:
    """The service attaches the policy reason as a notice and the body quotes it too.

    Printing both put a full paragraph in the result twice. It is the single result a model
    reads most carefully, and duplicated text there invites it to treat the second copy as
    new information.
    """
    view = _view(
        golden_path_id="dns-record",
        state=LeaseState.REJECTED,
        ttl_seconds=28800,
        policy_reason="That subdomain is reserved; pick one that says what your change is.",
    )
    rendered = _render_view(view, notices=[view.policy_reason or ""])
    assert rendered.count("That subdomain is reserved") == 1
    assert "Also worth knowing" not in rendered
    assert "retrying will not help" in rendered


async def test_a_rejected_lease_is_not_told_its_clock_is_about_to_start() -> None:
    """A model told the clock 'starts when the resource exists' will wait for a resource
    that is never coming, and a waiting model eventually retries."""
    view = _view(
        golden_path_id="dns-record",
        state=LeaseState.REJECTED,
        ttl_seconds=28800,
        policy_reason="reserved",
    )
    rendered = _render_view(view)
    assert "no lease clock" in rendered
    assert "starts when the resource exists" not in rendered
    assert "28800" not in rendered


async def test_a_genuinely_new_notice_still_survives() -> None:
    """Deduping must not swallow information the body does not already carry."""
    view = _view(
        golden_path_id="sandbox",
        state=LeaseState.REJECTED,
        ttl_seconds=300,
        policy_reason="reserved name",
    )
    rendered = _render_view(view, notices=["reserved name", "the ttl you asked for was clamped"])
    assert rendered.count("reserved name") == 1
    assert "the ttl you asked for was clamped" in rendered
