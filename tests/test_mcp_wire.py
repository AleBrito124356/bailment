"""The MCP surface over the real wire: uvicorn, streamable HTTP, and the SDK's own client.

``tests/test_mcp.py`` dispatches tools directly, which is the right place to pin down what
a model reads. It cannot see anything between the socket and the dispatcher -- the ASGI
route, the credential check in front of the transport, the session manager, the SDK's
handler registration -- and that is exactly where an SDK upgrade breaks things. mcp 2.0
removed the decorator API :meth:`bailment.mcp.server.BailmentMCP._register` is built on,
and the direct-dispatch suite kept passing, because it never asked the SDK to register
anything.

So these tests stand the whole application up on a real port and talk to it the way Claude
Code does: an HTTP client, an ``initialize`` handshake, ``tools/list`` and ``tools/call``
as JSON-RPC over streamable HTTP. The last test proves the harness is not agreeing with
itself -- an application whose handlers were never registered fails it.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, InitializeResult, TextContent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bailment import db as db_module
from bailment.config import Settings
from bailment.db import build_engine
from bailment.engine.worker import ProvisioningWorker
from bailment.main import create_app
from bailment.mcp import server as mcp_server_module
from bailment.mcp.server import (
    ON_BEHALF_OF_HEADER,
    SERVER_INSTRUCTIONS,
    TOOL_CATALOG,
    TOOL_LEASE_STATUS,
    TOOL_LIST_LEASES,
    TOOL_RELEASE_LEASE,
    TOOL_RENEW_LEASE,
    check_mcp_sdk,
)
from bailment.models import Base, Lease
from bailment.providers.registry import ProviderRegistry
from bailment.secrets import SecretBox
from conftest import SEALED_VALUE_PREFIX, RecordingProvider, build_test_catalog

try:  # the newer name; the deprecated one is kept for older releases inside the pin.
    from mcp.client.streamable_http import streamable_http_client as _open_transport

    _NEW_CLIENT = True
except ImportError:  # pragma: no cover - depends on the installed SDK
    from mcp.client.streamable_http import streamablehttp_client as _open_transport

    _NEW_CLIENT = False

AGENT_TOKEN = "agent-token"


@dataclass
class Broker:
    url: str
    worker: ProvisioningWorker
    sessionmaker: async_sessionmaker[AsyncSession]


@contextlib.asynccontextmanager
async def running_broker(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secret_box: SecretBox,
) -> AsyncIterator[Broker]:
    """The full application on an ephemeral 127.0.0.1 port, lifespan and all.

    A context manager used inside each test rather than a fixture, for the reason the
    ``app`` fixture in ``test_api.py`` gives: the MCP session manager owns an anyio task
    group, and pytest-asyncio tears async fixtures down from a different task. Here the
    lifespan runs inside uvicorn's own serve task, which is also the task that exits it.

    The database is a file, not ``:memory:``: uvicorn's connections and the test's worker
    must see one database through separate connections, which is what a real deployment
    does and what an in-memory database cannot.
    """
    path = (tmp_path / "wire.db").as_posix()
    scoped = settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{path}"})
    engine = build_engine(scoped)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    # create_app and the lifespan reach for the process-wide engine; point it here.
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_sessionmaker", factory)

    catalog = build_test_catalog()
    registry = ProviderRegistry()
    registry.register(RecordingProvider(latency_range=(0.0, 0.0)))
    application = create_app(settings=scoped, catalog=catalog, registry=registry, run_engine=False)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            application, lifespan="on", log_config=None, access_log=False, log_level="warning"
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started or serving.done():
                break
            await asyncio.sleep(0.025)
        if serving.done():
            serving.result()  # re-raise whatever stopped it
        assert server.started, "uvicorn did not start within five seconds"
        yield Broker(
            url=f"http://127.0.0.1:{port}/mcp",
            worker=ProvisioningWorker(
                factory,
                catalog=catalog,
                registry=registry,
                settings=scoped,
                secret_box=secret_box,
            ),
            sessionmaker=factory,
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=15)
        listener.close()
        await engine.dispose()


@dataclass
class Connected:
    session: ClientSession
    session_id: Callable[[], str | None]
    handshake: InitializeResult


@contextlib.asynccontextmanager
async def mcp_session(url: str, headers: dict[str, str]) -> AsyncIterator[Connected]:
    """An initialised SDK client session, exactly as an MCP host would open one."""
    if _NEW_CLIENT:
        async with (
            httpx.AsyncClient(headers=headers, timeout=30) as http,
            _open_transport(url, http_client=http) as (read, write, session_id),
            ClientSession(read, write) as session,
        ):
            yield Connected(session, session_id, await session.initialize())
    else:  # pragma: no cover - depends on the installed SDK
        async with (
            _open_transport(url, headers=headers) as (read, write, session_id),
            ClientSession(read, write) as session,
        ):
            yield Connected(session, session_id, await session.initialize())


def text_of(result: CallToolResult) -> str:
    assert result.content, "a tool result with no content"
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def lease_id_of(body: str) -> str:
    return body.split("lease_id: ")[1].split("\n")[0].strip()


# --------------------------------------------------------------------------------------
# The transport
# --------------------------------------------------------------------------------------


async def test_the_wire_refuses_a_caller_with_no_token_and_says_how_to_authenticate(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_box: SecretBox
) -> None:
    """``tools/list`` has no error channel of its own, so the refusal has to happen at the
    HTTP layer -- and it has to be the 401 plus ``WWW-Authenticate`` that sends an MCP
    client off to look for credentials, rather than a 200 carrying an error it ignores."""
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "wire-test", "version": "0"},
        },
    }
    async with (
        running_broker(settings, tmp_path, monkeypatch, secret_box) as broker,
        httpx.AsyncClient(timeout=10) as http,
    ):
        anonymous = await http.post(
            broker.url,
            json=initialize,
            headers={"Accept": "application/json, text/event-stream"},
        )
        wrong = await http.post(
            broker.url,
            json=initialize,
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer not-a-token-anybody-issued",
            },
        )

    for response in (anonymous, wrong):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Bearer realm="bailment"'
        assert "Authorization: Bearer" in response.json()["message"]


async def test_an_agent_provisions_a_sandbox_end_to_end_over_streamable_http(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_box: SecretBox
) -> None:
    """The whole agent journey, as JSON-RPC over a socket.

    initialize -> tools/list -> provision -> (a worker runs) -> lease_status -> release.
    Every tool result is collected and swept for the sealed value at the end, because the
    one property the MCP surface must never lose is that a credential does not cross it.
    """
    headers = {"Authorization": f"Bearer {AGENT_TOKEN}", ON_BEHALF_OF_HEADER: "dana"}
    seen: list[str] = []

    async with (
        running_broker(settings, tmp_path, monkeypatch, secret_box) as broker,
        mcp_session(broker.url, headers) as connected,
    ):
        session = connected.session
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        catalog = build_test_catalog()
        expected = {path.mcp_tool_name for path in catalog.enabled()} | {
            TOOL_LEASE_STATUS,
            TOOL_LIST_LEASES,
            TOOL_RENEW_LEASE,
            TOOL_RELEASE_LEASE,
            TOOL_CATALOG,
        }
        assert names == expected
        sandbox_tool = next(t for t in tools.tools if t.name == "bailment_provision_sandbox")
        # The schema an agent validates against is the golden path's, verbatim, plus ttl.
        assert sandbox_tool.inputSchema["properties"]["name"]["maxLength"] == 40
        assert "ttl" in sandbox_tool.inputSchema["properties"]

        queued = await session.call_tool("bailment_provision_sandbox", {"name": "wire"})
        seen.append(text_of(queued))
        assert queued.isError is False
        assert text_of(queued).startswith("QUEUED.")
        lease_id = lease_id_of(text_of(queued))

        # Same arguments, same MCP session: the derived idempotency key must hand back the
        # same lease rather than provision a second resource.
        again = await session.call_tool("bailment_provision_sandbox", {"name": "wire"})
        seen.append(text_of(again))
        assert lease_id_of(text_of(again)) == lease_id
        assert "no new resource was created" in text_of(again)

        tick = await broker.worker.tick()
        assert tick.provisioned == [lease_id]

        status = await session.call_tool(TOOL_LEASE_STATUS, {"lease_id": lease_id})
        seen.append(text_of(status))
        assert status.isError is False
        assert text_of(status).startswith("ACTIVE.")
        assert "bailment://binding/" in text_of(status)
        assert f"bailment exec {lease_id} -- <your command>" in text_of(status)

        listed = await session.call_tool(TOOL_LIST_LEASES, {"live_only": True})
        seen.append(text_of(listed))
        assert lease_id in text_of(listed)

        released = await session.call_tool(
            TOOL_RELEASE_LEASE, {"lease_id": lease_id, "reason": "wire test finished"}
        )
        seen.append(text_of(released))
        assert text_of(released).startswith("RELEASING.")

        mcp_session_id = connected.session_id()

    async with broker.sessionmaker() as opened:
        lease = await opened.get(Lease, lease_id)
        assert lease is not None
        # The mcp-session-id header made it from the socket to the audit columns.
        assert mcp_session_id and lease.agent_session == mcp_session_id
        assert lease.on_behalf_of == "dana"
        assert lease.requester == "agent-one"
        assert lease.state == "revoked"

    for text in seen:
        assert SEALED_VALUE_PREFIX not in text, "a sealed value crossed the MCP boundary"


async def test_the_handshake_carries_the_server_instructions(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_box: SecretBox
) -> None:
    headers = {"Authorization": f"Bearer {AGENT_TOKEN}"}
    async with (
        running_broker(settings, tmp_path, monkeypatch, secret_box) as broker,
        mcp_session(broker.url, headers) as connected,
    ):
        initialized = connected.handshake
    assert initialized.serverInfo.name == "bailment"
    assert initialized.instructions == SERVER_INSTRUCTIONS
    assert initialized.capabilities.tools is not None


async def test_a_denial_over_the_wire_is_a_result_not_an_error(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_box: SecretBox
) -> None:
    """A policy denial is the tool working perfectly. ``isError`` would invite the retry
    loop the wording exists to prevent -- and the flag is decided by the SDK's
    serialisation, which the direct-dispatch tests never exercise."""
    headers = {"Authorization": f"Bearer {AGENT_TOKEN}"}
    async with (
        running_broker(settings, tmp_path, monkeypatch, secret_box) as broker,
        mcp_session(broker.url, headers) as connected,
    ):
        denied = await connected.session.call_tool("bailment_provision_forbidden", {})
        missing = await connected.session.call_tool(
            TOOL_LEASE_STATUS, {"lease_id": "no-such-lease"}
        )
    assert denied.isError is False
    assert text_of(denied).startswith("DENIED.")
    assert missing.isError is True
    assert "no lease with id" in text_of(missing)


# --------------------------------------------------------------------------------------
# The harness itself
# --------------------------------------------------------------------------------------


async def test_the_wire_harness_catches_an_app_whose_handlers_were_never_registered(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_box: SecretBox
) -> None:
    """The regression this file exists for, reproduced on purpose.

    Under mcp 2.x the handler registration is what broke. Simulate that by building an
    application whose MCP server registers nothing: the HTTP endpoint still answers and the
    handshake still succeeds, and ``tools/list`` must then fail -- or the tests above would
    pass against a broker no agent could use.
    """

    def register_nothing(self: Any) -> None:
        del self

    monkeypatch.setattr(mcp_server_module.BailmentMCP, "_register", register_nothing)
    headers = {"Authorization": f"Bearer {AGENT_TOKEN}"}
    async with (
        running_broker(settings, tmp_path, monkeypatch, secret_box) as broker,
        mcp_session(broker.url, headers) as connected,
    ):
        with pytest.raises(McpError):
            await connected.session.list_tools()


def test_an_unsupported_sdk_is_refused_at_import_with_the_fix_in_the_message() -> None:
    """mcp 2.x's low-level Server has no decorator API. The refusal names the pin and the
    command that fixes it, instead of an AttributeError from inside create_app."""

    class DecoratorlessServer:
        """The shape of ``mcp.server.lowlevel.Server`` in mcp 2.x."""

    with pytest.raises(ImportError) as caught:
        check_mcp_sdk("2.2.0", DecoratorlessServer)
    message = str(caught.value)
    assert "mcp>=1.19,<2" in message
    assert "mcp 2.2.0 is installed" in message
    assert "no list_tools()/call_tool() decorator API" in message
    assert "pip install" in message

    # A 2.x that somehow kept the decorators is still outside what this code was run on.
    with pytest.raises(ImportError):
        check_mcp_sdk("2.0.0")
    # Below the floor the import works and every tool call then fails; refuse it here.
    with pytest.raises(ImportError, match="validation error"):
        check_mcp_sdk("1.18.0")
    # And the SDK actually installed here passes, or this module could not be imported.
    check_mcp_sdk()
