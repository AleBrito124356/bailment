"""The HTTP surfaces, and the sweep that says no route may hand back a credential.

The centrepiece is :func:`test_no_route_returns_a_decrypted_secret_value`. It is written
as a walk over every route the application actually mounts rather than as one assertion
per endpoint, for a reason that matters more than the tidiness: an endpoint added next
year is covered the day it is added, without anybody remembering to come back here. The
walk also asserts that it *reached* every route -- a sweep that quietly stopped covering
half the API would otherwise pass forever.

Two supporting properties make that sweep meaningful rather than decorative:

* it hunts for the exact plaintext of a live binding, decrypted for the test, not for a
  plausible-looking substring;
* it asserts that the one route which is *supposed* to return a credential does, which
  proves the detector works. Without that, a sweep that found nothing because nothing was
  rendered would look identical to a clean one.

That single exception is ``PUT /v2/.../service_bindings/...``. OSB defines a binding
response as carrying a ``credentials`` object; a broker that returned a reference instead
is a broker no platform can use. It requires an operator token, and the agent tier gets
403 -- which is the other half of the requirement this file covers.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any, NamedTuple

import httpx
import pytest
from fastapi import FastAPI

from bailment.api.routes import (
    DECRYPTION_NAMES,
    GUARDED_MODULES,
    assert_handlers_never_decrypt,
    referenced_names,
)
from bailment.api.schemas import (
    JUSTIFIED_FIELD_NAMES,
    RESPONSE_MODELS,
    assert_no_secret_fields,
    secret_looking_fields,
)
from bailment.main import create_app
from bailment.models import Lease
from bailment.osb.router import OSB_API_VERSION, plan_id_for, service_id_for
from bailment.states import LeaseState
from conftest import PUBLISHED_OUTPUT, SEALED_OUTPUT, SEALED_VALUE_PREFIX

OPERATOR = {"Authorization": "Bearer operator-token"}
AGENT = {"Authorization": "Bearer agent-token"}
OSB_HEADERS = {"X-Broker-API-Version": OSB_API_VERSION}


@pytest.fixture
def app(settings, catalog, registry, sessionmaker) -> FastAPI:
    """The whole application, against the test database.

    ``run_engine=False``: nothing may tick underneath a test. The worker is driven by hand
    where a test needs one, so every assertion is about a state somebody put the row in.

    The lifespan is deliberately *not* entered here. Everything the HTTP surfaces need --
    settings, catalog, registry, session factory -- is put on ``app.state`` by
    :func:`create_app` itself, and the schema belongs to the ``engine`` fixture. What the
    lifespan adds beyond that is the MCP session manager, which owns an anyio task group;
    pytest-asyncio finalises an async fixture in a different task from the one that set it
    up, and a task group cannot be exited from a task that did not enter it.
    :func:`test_the_application_starts_and_stops_cleanly` runs the real lifespan inside a
    single test body, which is where that constraint holds.
    """
    return create_app(settings=settings, catalog=catalog, registry=registry, run_engine=False)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://broker.test"
    ) as opened:
        yield opened


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


async def test_a_request_with_no_token_is_refused_and_says_how_to_authenticate(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/catalog")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    detail = response.json()["detail"]
    assert detail["error"] == "unauthenticated"
    assert "BAILMENT_API_TOKENS" in detail["message"]


async def test_a_token_that_matches_nothing_is_refused_rather_than_downgraded(
    client: httpx.AsyncClient,
) -> None:
    """Falling back to anonymous would turn a typo'd token into a silently-downgraded
    identity, and the audit trail would record 'anonymous' for a request somebody believes
    they made as themselves."""
    response = await client.get("/api/v1/catalog", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert "not configured on this broker" in response.json()["detail"]["message"]


async def test_an_agent_token_cannot_reach_an_operator_endpoint(
    client: httpx.AsyncClient,
) -> None:
    """403, not 404: the caller authenticated and the endpoint exists."""
    response = await client.get("/api/v1/providers", headers=AGENT)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "operator_required"


async def test_an_operator_sees_disabled_paths_and_an_agent_does_not(
    client: httpx.AsyncClient,
) -> None:
    """A disabled path must not appear to a caller who could try to provision it, because
    the first thing that caller learns is that the catalog lies."""
    agent_view = (await client.get("/api/v1/catalog", headers=AGENT)).json()
    operator_view = (await client.get("/api/v1/catalog", headers=OPERATOR)).json()

    assert "switched-off" not in [item["id"] for item in agent_view["items"]]
    assert "switched-off" in [item["id"] for item in operator_view["items"]]
    assert agent_view["directory"] is None
    assert operator_view["directory"] is not None


# --------------------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------------------


async def test_requesting_a_lease_answers_201_with_a_location(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/leases",
        headers=AGENT,
        json={"golden_path": "sandbox", "inputs": {"name": "over-http"}},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["lease"]["state"] == "pending"
    assert body["replayed"] is False
    assert response.headers["Location"] == f"/api/v1/leases/{body['lease']['id']}"


async def test_a_replayed_idempotency_key_answers_200_not_201(
    client: httpx.AsyncClient,
) -> None:
    """201 asserts that something was created, and an idempotent replay is the promise
    that nothing was."""
    payload = {"golden_path": "sandbox", "inputs": {"name": "over-http"}}
    first = await client.post(
        "/api/v1/leases", headers={**AGENT, "Idempotency-Key": "task-42"}, json=payload
    )
    second = await client.post(
        "/api/v1/leases", headers={**AGENT, "Idempotency-Key": "task-42"}, json=payload
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["lease"]["id"] == first.json()["lease"]["id"]


async def test_two_different_idempotency_keys_in_one_request_are_refused(
    client: httpx.AsyncClient,
) -> None:
    """A caller that sent both meant one of them, and guessing which would decide whether
    this call provisions a second resource."""
    response = await client.post(
        "/api/v1/leases",
        headers={**AGENT, "Idempotency-Key": "from-the-header"},
        json={
            "golden_path": "sandbox",
            "inputs": {"name": "x"},
            "idempotency_key": "from-the-body",
        },
    )
    assert response.status_code == 400
    assert "two different idempotency keys" in response.json()["detail"]["message"]


async def test_a_denied_request_is_still_a_201_carrying_the_reason(
    client: httpx.AsyncClient,
) -> None:
    """The call worked perfectly; the answer is no. That is not an HTTP error."""
    response = await client.post(
        "/api/v1/leases", headers=AGENT, json={"golden_path": "forbidden", "inputs": {}}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["lease"]["state"] == "rejected"
    assert "not handed out" in body["lease"]["policy_reason"]


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        pytest.param(
            {"golden_path": "nope", "inputs": {}}, 404, "unknown_golden_path", id="unknown-path"
        ),
        pytest.param(
            {"golden_path": "switched-off", "inputs": {"name": "x"}},
            404,
            "unknown_golden_path",
            id="disabled-path-is-indistinguishable",
        ),
        pytest.param(
            {"golden_path": "sandbox", "inputs": {}}, 400, "invalid_request", id="missing-input"
        ),
        pytest.param(
            {"golden_path": "nowhere", "inputs": {"name": "x"}},
            503,
            "provider_unavailable",
            id="unregistered-provider",
        ),
    ],
)
async def test_service_refusals_map_onto_status_codes(
    client: httpx.AsyncClient, payload: dict[str, Any], status: int, code: str
) -> None:
    response = await client.post("/api/v1/leases", headers=AGENT, json=payload)
    assert response.status_code == status
    assert response.json()["detail"]["error"] == code


async def test_a_malformed_body_gets_the_same_error_envelope_as_everything_else(
    client: httpx.AsyncClient,
) -> None:
    """Two error shapes for a client to parse is one too many."""
    response = await client.post("/api/v1/leases", headers=AGENT, json={"nonsense": True})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_request"
    assert detail["problems"]


async def test_a_lease_belonging_to_somebody_else_is_a_404(
    client: httpx.AsyncClient, activate
) -> None:
    lease = await activate()
    response = await client.get(
        f"/api/v1/leases/{lease.id}", headers={"Authorization": "Bearer second-agent-token"}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "lease_not_found"


async def test_listing_is_scoped_and_paginated_without_a_total(
    client: httpx.AsyncClient, request_lease, agent, other_agent
) -> None:
    for index in range(3):
        await request_lease(caller=agent, inputs={"name": f"mine-{index}"})
    await request_lease(caller=other_agent, inputs={"name": "theirs"})

    page = (await client.get("/api/v1/leases?limit=2", headers=AGENT)).json()
    assert len(page["items"]) == 2
    assert page["has_more"] is True
    assert page["next_offset"] == 2
    assert "total" not in page

    rest = (await client.get("/api/v1/leases?limit=2&offset=2", headers=AGENT)).json()
    assert len(rest["items"]) == 1
    assert rest["has_more"] is False
    assert all(item["requester"] == "agent-one" for item in rest["items"])

    everything = (await client.get("/api/v1/leases", headers=OPERATOR)).json()
    assert len(everything["items"]) == 4


async def test_an_unknown_state_filter_is_a_400_listing_the_real_ones(
    client: httpx.AsyncClient,
) -> None:
    """A filter nobody can spell that silently matches nothing reads exactly like "there
    are no leases", which is the worst possible answer for a dashboard to give."""
    response = await client.get("/api/v1/leases?state=nonsense", headers=AGENT)
    assert response.status_code == 400
    assert "unknown lease state(s): nonsense" in response.json()["detail"]["message"]
    assert "orphaned" in response.json()["detail"]["message"]


async def test_state_filters_accept_repeated_and_comma_separated_values(
    client: httpx.AsyncClient, activate, request_lease
) -> None:
    await activate()
    await request_lease(inputs={"name": "pending-one"})
    body = (await client.get("/api/v1/leases?state=active,pending", headers=AGENT)).json()
    assert len(body["items"]) == 2
    body = (await client.get("/api/v1/leases?state=active&state=expiring", headers=AGENT)).json()
    assert len(body["items"]) == 1


# --------------------------------------------------------------------------------------
# Bindings: described, never disclosed
# --------------------------------------------------------------------------------------


async def test_the_bindings_endpoint_describes_a_credential_without_disclosing_it(
    client: httpx.AsyncClient, activate, sealed_values
) -> None:
    lease = await activate()
    values = sealed_values(lease)

    response = await client.get(f"/api/v1/leases/{lease.id}/bindings", headers=AGENT)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["reference"] == f"bailment://binding/{lease.bindings[0].id}"
    assert sorted(item["output_names"]) == sorted([SEALED_OUTPUT, PUBLISHED_OUTPUT])
    assert item["usable"] is True
    assert item["access_count"] == 0
    assert item["how_to_use"] == f"bailment exec {lease.id} -- <command>"
    assert values[SEALED_OUTPUT] not in response.text


async def test_the_audit_trail_reads_oldest_first_and_is_scrubbed(
    client: httpx.AsyncClient, activate, sealed_values
) -> None:
    lease = await activate()
    values = sealed_values(lease)

    body = (await client.get(f"/api/v1/leases/{lease.id}/audit", headers=AGENT)).json()

    actions = [item["action"] for item in body["items"]]
    # Oldest first, because it is a story and stories start at the beginning. Rows written
    # in the same instant tie-break on a uuid, so only actions separated by real work --
    # here, a whole worker tick -- have an order worth asserting on.
    assert set(actions) == {"requested", "queued", "claimed", "provisioning", "activated"}
    assert actions.index("requested") < actions.index("activated")
    assert values[SEALED_OUTPUT] not in str(body)
    # The published output is a *non-secret* one, and it is redacted here anyway because
    # the audit blob passes through the log redactor on the way out and its name looks
    # credential-shaped. Over-redaction is the accepted failure.
    assert body["lease_id"] == lease.id


# --------------------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------------------


async def test_the_approval_queue_is_operator_only_and_oldest_first(
    client: httpx.AsyncClient, request_lease
) -> None:
    first = await request_lease("gated", inputs={"env": "prod", "name": "one"})
    second = await request_lease("gated", inputs={"env": "prod", "name": "two"})

    assert (await client.get("/api/v1/approvals", headers=AGENT)).status_code == 403

    body = (await client.get("/api/v1/approvals", headers=OPERATOR)).json()
    assert [item["lease_id"] for item in body["items"]] == [first.lease.id, second.lease.id]
    entry = body["items"][0]
    assert entry["requester"] == "agent-one"
    assert entry["on_behalf_of"] == "dana"
    assert entry["inputs"] == {"env": "prod", "name": "one"}
    assert entry["seconds_until_deadline"] > 0
    assert entry["waiting_seconds"] >= 0


async def test_approving_over_http_queues_the_lease(
    client: httpx.AsyncClient, request_lease, worker, read_lease
) -> None:
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})

    assert (
        await client.post(f"/api/v1/approvals/{outcome.lease.id}/approve", headers=AGENT, json={})
    ).status_code == 403

    response = await client.post(
        f"/api/v1/approvals/{outcome.lease.id}/approve",
        headers=OPERATOR,
        json={"note": "reproducing a bug"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "provisioning"

    await worker.tick()
    assert (await read_lease(outcome.lease.id)).state == LeaseState.ACTIVE.value


async def test_rejecting_needs_a_reason(client: httpx.AsyncClient, request_lease) -> None:
    outcome = await request_lease("gated", inputs={"env": "prod", "name": "orders"})
    empty = await client.post(
        f"/api/v1/approvals/{outcome.lease.id}/reject", headers=OPERATOR, json={"reason": ""}
    )
    assert empty.status_code == 422

    response = await client.post(
        f"/api/v1/approvals/{outcome.lease.id}/reject",
        headers=OPERATOR,
        json={"reason": "use staging instead"},
    )
    assert response.json()["state"] == "rejected"


# --------------------------------------------------------------------------------------
# Reconciliation from HTTP
# --------------------------------------------------------------------------------------


async def test_triggering_a_reconcile_from_http_can_never_destroy_anything(
    client: httpx.AsyncClient, provider, settings
) -> None:
    """Both switches are thrown in configuration and the endpoint still passes an empty
    allowlist, unconditionally. A tool that deletes cloud resources because somebody was
    persuaded to click a button is a tool nobody installs twice.
    """
    from datetime import timedelta

    settings.reconcile_auto_destroy_orphans = True
    provider.plant_orphan("bailment-sandbox-deadbeef")
    resource = provider.snapshot()["bailment-sandbox-deadbeef"]
    resource.created_at = resource.created_at - timedelta(hours=2)

    body = (await client.post("/api/v1/reconcile", headers=OPERATOR)).json()

    assert body["destroy_armed"] is False
    assert body["orphans_found"] == 1
    assert body["orphans_destroyed"] == 0
    assert body["clean"] is False
    assert "bailment-sandbox-deadbeef" in provider.snapshot()


async def test_reconcile_runs_are_listed_and_readable(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/reconcile", headers=OPERATOR)
    page = (await client.get("/api/v1/reconcile/runs", headers=OPERATOR)).json()
    assert len(page["items"]) == 1
    run_id = page["items"][0]["id"]

    detail = (await client.get(f"/api/v1/reconcile/runs/{run_id}", headers=OPERATOR)).json()
    assert detail["provider"] == "memory"
    assert detail["clean"] is True
    assert "detail" in detail

    missing = await client.get("/api/v1/reconcile/runs/nope", headers=OPERATOR)
    assert missing.status_code == 404


async def test_scoping_a_reconcile_to_an_unknown_provider_is_a_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/v1/reconcile?provider=nope", headers=OPERATOR)
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_provider"


# --------------------------------------------------------------------------------------
# Stats, providers, health
# --------------------------------------------------------------------------------------


async def test_stats_are_scoped_and_include_the_states_nobody_wants(
    client: httpx.AsyncClient, activate, request_lease, other_agent
) -> None:
    """The spend figure sums every state where a resource is believed to exist -- including
    ORPHANED and UNKNOWN. Excluding those would make the number agree with what bailment
    intended rather than with what is running."""
    await activate("gated", inputs={"env": "dev", "name": "costly"})
    await request_lease(caller=other_agent, inputs={"name": "theirs"})

    own = (await client.get("/api/v1/stats", headers=AGENT)).json()
    assert own["scope"] == "own"
    assert own["active_leases"] == 1
    assert own["total_leases"] == 1
    assert own["estimated_hourly_usd"] == pytest.approx(0.14)
    assert own["estimated_monthly_usd"] == pytest.approx(0.14 * 730, rel=1e-3)
    assert own["last_reconcile"] is None

    everything = (await client.get("/api/v1/stats", headers=OPERATOR)).json()
    assert everything["scope"] == "all"
    assert everything["total_leases"] == 2
    assert everything["by_state"]["pending"] == 1


async def test_expiring_soon_uses_the_window_it_was_given(
    client: httpx.AsyncClient, activate
) -> None:
    await activate()  # a 5m sandbox lease
    within_hour = (await client.get("/api/v1/stats?within=3600", headers=AGENT)).json()
    within_minute = (await client.get("/api/v1/stats?within=60", headers=AGENT)).json()
    assert within_hour["expiring_soon"] == 1
    assert within_minute["expiring_soon"] == 0


async def test_providers_are_listed_including_the_unconfigured_ones(
    client: httpx.AsyncClient, provider
) -> None:
    """ "We do not offer that" and "nobody has set the API key yet" are different answers."""
    provider.set_unavailable("BAILMENT_MEMORY_TOKEN is not set")
    body = (await client.get("/api/v1/providers", headers=OPERATOR)).json()
    entry = next(item for item in body["items"] if item["name"] == "memory")
    assert entry["available"] is False
    assert "BAILMENT_MEMORY_TOKEN" in (entry["reason"] or "")
    assert "sandbox" in entry["golden_paths"]


async def test_the_application_starts_and_stops_cleanly(
    settings, catalog, registry, sessionmaker
) -> None:
    """The real lifespan, including the MCP session manager's task group.

    Entered inside the test body rather than in a fixture; see the ``app`` fixture for why.
    """
    application = create_app(
        settings=settings, catalog=catalog, registry=registry, run_engine=False
    )
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://broker.test"
        ) as opened,
    ):
        response = await opened.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_reports_the_database_and_the_components(
    client: httpx.AsyncClient,
) -> None:
    live = await client.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "alive"

    health = await client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["database"]["ok"] is True
    assert body["golden_paths"] == 5
    # run_engine=False, so there is nothing running and the probe says so rather than
    # pretending.
    assert body["components"] == {}


async def test_metrics_are_aggregates_and_name_no_lease(
    client: httpx.AsyncClient, activate, sealed_values
) -> None:
    """A metrics endpoint tends to be the least-guarded thing in a deployment, so it
    carries the least."""
    lease = await activate()
    values = sealed_values(lease)

    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert 'bailment_leases{state="active"} 1' in response.text
    assert "bailment_orphans_found_total" in response.text
    assert lease.id not in response.text
    assert (lease.external_name or "!") not in response.text
    assert "agent-one" not in response.text
    assert values[SEALED_OUTPUT] not in response.text


# --------------------------------------------------------------------------------------
# The Open Service Broker surface
# --------------------------------------------------------------------------------------


async def test_the_osb_catalog_requires_a_version_header_and_a_token(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/v2/catalog", headers=OPERATOR)).status_code == 412
    assert (await client.get("/v2/catalog", headers=OSB_HEADERS)).status_code == 401

    response = await client.get("/v2/catalog", headers={**OSB_HEADERS, **OPERATOR})
    assert response.status_code == 200
    services = response.json()["services"]
    assert {service["name"] for service in services} == {"sandbox", "gated", "forbidden", "nowhere"}
    assert services[0]["id"] == service_id_for(services[0]["name"])


async def test_an_agent_token_cannot_resolve_a_binding(
    client: httpx.AsyncClient, osb_instance: OsbInstance, sealed_values, read_lease
) -> None:
    """The only surface that returns credential values, gated on caller kind.

    An ordinary token becomes a ``HUMAN`` and an admin token an ``OPERATOR``; MCP callers
    are always ``AGENT``, which ``resolve_binding`` refuses regardless of which HTTP path
    reached it. There is no configuration that opens this to an agent.
    """
    response = await client.put(
        osb_instance.binding_url,
        headers={**OSB_HEADERS, **AGENT},
        json={"service_id": osb_instance.service_id, "plan_id": osb_instance.plan_id},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "Forbidden"
    assert "operator token" in response.json()["description"]
    assert "use the MCP tools" in response.json()["description"]

    values = sealed_values(await read_lease(osb_instance.lease_id))
    assert values[SEALED_OUTPUT] not in response.text
    # ...and nothing was recorded as an access.
    lease = await read_lease(osb_instance.lease_id)
    assert lease.bindings[0].access_count == 0


async def test_an_operator_can_resolve_a_binding(
    client: httpx.AsyncClient, osb_instance: OsbInstance, sealed_values, read_lease
) -> None:
    response = await client.put(
        osb_instance.binding_url,
        headers={**OSB_HEADERS, **OPERATOR},
        json={"service_id": osb_instance.service_id, "plan_id": osb_instance.plan_id},
    )

    assert response.status_code == 201
    values = sealed_values(await read_lease(osb_instance.lease_id))
    assert response.json()["credentials"] == values
    assert response.json()["credentials"][SEALED_OUTPUT].startswith(SEALED_VALUE_PREFIX)

    lease = await read_lease(osb_instance.lease_id)
    assert lease.bindings[0].access_count == 1
    assert lease.bindings[0].last_accessed_at is not None


async def test_osb_provisioning_is_always_asynchronous(
    client: httpx.AsyncClient, osb_instance: OsbInstance
) -> None:
    """The lease row that names the resource has to be committed before any provider is
    called, and a request may need a human to approve it, so there is no code path that
    could answer 201 Created truthfully."""
    response = await client.put(
        f"/v2/service_instances/{uuid.uuid4()}",
        headers={**OSB_HEADERS, **OPERATOR},
        json={
            "service_id": osb_instance.service_id,
            "plan_id": osb_instance.plan_id,
            "parameters": {"name": "sync-please"},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"] == "AsyncRequired"


async def test_an_osb_instance_id_is_the_idempotency_key(
    client: httpx.AsyncClient, osb_instance: OsbInstance, session
) -> None:
    from sqlalchemy import func, select

    repeat = await client.put(
        f"/v2/service_instances/{osb_instance.instance_id}?accepts_incomplete=true",
        headers={**OSB_HEADERS, **OPERATOR},
        json={
            "service_id": osb_instance.service_id,
            "plan_id": osb_instance.plan_id,
            "parameters": {"name": "osb-swept"},
        },
    )
    assert repeat.status_code in (200, 202)
    total = await session.execute(select(func.count()).select_from(Lease))
    assert total.scalar_one() == 1


# --------------------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------------------


class OsbInstance(NamedTuple):
    instance_id: str
    binding_id: str
    lease_id: str
    service_id: str
    plan_id: str

    @property
    def binding_url(self) -> str:
        return f"/v2/service_instances/{self.instance_id}/service_bindings/{self.binding_id}"


@pytest.fixture
async def osb_instance(client: httpx.AsyncClient, worker, sessionmaker) -> OsbInstance:
    """One active OSB service instance, so the bind endpoint has something to bind.

    Provisioned through the OSB surface rather than injected, because the instance id has
    to land in ``Lease.idempotency_key`` with the ``osb:`` prefix for the endpoint to find
    it again -- and a fixture that faked that would test a lookup nothing else performs.
    """
    instance_id = str(uuid.uuid4())
    service_id = service_id_for("sandbox")
    plan_id = plan_id_for("sandbox")

    created = await client.put(
        f"/v2/service_instances/{instance_id}?accepts_incomplete=true",
        headers={**OSB_HEADERS, **OPERATOR},
        json={
            "service_id": service_id,
            "plan_id": plan_id,
            "parameters": {"name": "osb-swept"},
        },
    )
    assert created.status_code == 202, created.text
    await worker.tick()

    async with sessionmaker() as opened:
        from sqlalchemy import select

        lease_id = (
            (
                await opened.execute(
                    select(Lease.id).where(Lease.idempotency_key == f"osb:{instance_id}")
                )
            )
            .scalars()
            .one()
        )
        row = await opened.get(Lease, lease_id)
        assert row is not None and row.state == LeaseState.ACTIVE.value

    return OsbInstance(
        instance_id=instance_id,
        binding_id=str(uuid.uuid4()),
        lease_id=lease_id,
        service_id=service_id,
        plan_id=plan_id,
    )


def walk_routes(router: Any, prefix: str = "") -> Iterator[tuple[str, tuple[str, ...]]]:
    """Every path template the application actually mounts, with its methods.

    Written as a walk rather than a list because the whole value of the sweep below is
    that it covers routes nobody remembered to add to a list. Included routers, mounts and
    plain routes are all handled; a shape this does not understand yields nothing, which
    :func:`test_the_sweep_reaches_every_route` turns into a failure rather than a silent
    gap.
    """
    for route in getattr(router, "routes", []):
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            yield from walk_routes(included, prefix + (getattr(context, "prefix", "") or ""))
            continue
        if getattr(route, "routes", None) is not None:
            yield from walk_routes(route, prefix + (getattr(route, "path", "") or ""))
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        yield prefix + path, tuple(sorted(getattr(route, "methods", None) or ["GET"]))


#: The MCP transport. Excluded from the HTTP sweep because an MCP tool result is not an
#: HTTP response body and driving one needs a protocol handshake; ``tests/test_mcp.py``
#: sweeps every tool for the same leak. The exclusion is asserted to be exactly this set,
#: so a future route cannot join it by accident.
NOT_HTTP_ROUTES = frozenset({"/mcp", "/mcp/"})

#: The one endpoint in bailment that is supposed to return credential values. OSB defines
#: a binding response as carrying them; see the module docstring.
OSB_BIND = "/v2/service_instances/{instance_id}/service_bindings/{binding_id}"


def build_recipes(instance: OsbInstance, lease_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Query strings and bodies that get each route past its own front door.

    Rank orders the walk: reads first, then the bind, then the destructive ones, so that a
    revocation early in the sweep does not make every later read answer 409.
    """
    osb_query = {
        "accepts_incomplete": "true",
        "service_id": instance.service_id,
        "plan_id": instance.plan_id,
    }
    osb_body = {
        "service_id": instance.service_id,
        "plan_id": instance.plan_id,
        "parameters": {"name": "osb-swept"},
    }
    return {
        ("POST", "/api/v1/leases"): {
            "json": {"golden_path": "sandbox", "inputs": {"name": "swept"}},
            "rank": 2,
        },
        ("POST", "/api/v1/leases/{lease_id}/renew"): {"json": {"ttl": "10m"}, "rank": 2},
        ("POST", "/api/v1/approvals/{lease_id}/approve"): {"json": {"note": "swept"}, "rank": 2},
        ("POST", "/api/v1/approvals/{lease_id}/reject"): {
            "json": {"reason": "swept"},
            "rank": 2,
        },
        ("POST", "/api/v1/leases/{lease_id}/retry-teardown"): {"rank": 2},
        ("POST", "/api/v1/reconcile"): {"rank": 2},
        ("PUT", OSB_BIND): {"json": osb_body, "rank": 1},
        ("DELETE", OSB_BIND): {"params": osb_query, "rank": 3},
        ("PUT", "/v2/service_instances/{instance_id}"): {
            "json": osb_body,
            "params": osb_query,
            "rank": 3,
        },
        ("PATCH", "/v2/service_instances/{instance_id}"): {
            "json": osb_body,
            "params": osb_query,
            "rank": 3,
        },
        ("DELETE", "/v2/service_instances/{instance_id}"): {"params": osb_query, "rank": 4},
        ("POST", "/api/v1/leases/{lease_id}/revoke"): {
            "json": {"reason": "swept"},
            "rank": 4,
        },
    }


async def sweep(
    client: httpx.AsyncClient,
    app: FastAPI,
    instance: OsbInstance,
    run_id: str,
    *,
    headers: dict[str, str],
) -> dict[tuple[str, str], httpx.Response]:
    """Call every route the application mounts, once per method."""
    substitutions = {
        "lease_id": instance.lease_id,
        "golden_path_id": "sandbox",
        "run_id": run_id,
        "instance_id": instance.instance_id,
        "binding_id": instance.binding_id,
    }
    recipes = build_recipes(instance, instance.lease_id)
    calls: list[tuple[int, str, str, dict[str, Any]]] = []
    for path, methods in walk_routes(app.router):
        if path in NOT_HTTP_ROUTES:
            continue
        for method in methods:
            if method == "HEAD":
                continue
            recipe = dict(recipes.get((method, path), {}))
            calls.append((int(recipe.pop("rank", 0)), method, path, recipe))

    responses: dict[tuple[str, str], httpx.Response] = {}
    for _, method, path, recipe in sorted(calls, key=lambda call: (call[0], call[2], call[1])):
        url = re.sub(r"\{(\w+)\}", lambda m: substitutions[m.group(1)], path)
        responses[(method, path)] = await client.request(
            method,
            url,
            headers={**OSB_HEADERS, **headers},
            **recipe,
        )
    return responses


@pytest.fixture
async def swept(
    client: httpx.AsyncClient, app: FastAPI, osb_instance: OsbInstance
) -> dict[tuple[str, str], httpx.Response]:
    runs = (await client.post("/api/v1/reconcile", headers=OPERATOR)).json()
    listing = (await client.get("/api/v1/reconcile/runs", headers=OPERATOR)).json()
    del runs
    return await sweep(client, app, osb_instance, listing["items"][0]["id"], headers=OPERATOR)


async def test_the_sweep_reaches_every_route(
    swept: dict[tuple[str, str], httpx.Response], app: FastAPI
) -> None:
    """The sweep's own coverage check.

    Without this, a route walker that stopped understanding the application's structure
    would make every leak assertion below vacuously true.
    """
    expected = {
        (method, path)
        for path, methods in walk_routes(app.router)
        if path not in NOT_HTTP_ROUTES
        for method in methods
        if method != "HEAD"
    }
    assert expected
    assert set(swept) == expected
    assert len(swept) >= 25

    # And the routes that render a lease actually answered, rather than 404ing their way
    # to a clean result.
    for key in [
        ("GET", "/api/v1/leases"),
        ("GET", "/api/v1/leases/{lease_id}"),
        ("GET", "/api/v1/leases/{lease_id}/bindings"),
        ("GET", "/api/v1/leases/{lease_id}/audit"),
        ("GET", "/api/v1/stats"),
        ("GET", "/api/v1/catalog/{golden_path_id}"),
        ("GET", "/api/v1/reconcile/runs/{run_id}"),
        ("GET", "/v2/catalog"),
        ("GET", "/v2/service_instances/{instance_id}/last_operation"),
    ]:
        assert swept[key].status_code == 200, (key, swept[key].text)


async def test_no_route_returns_a_decrypted_secret_value(
    swept: dict[tuple[str, str], httpx.Response],
    osb_instance: OsbInstance,
    sealed_values,
    read_lease,
) -> None:
    """The rule, enforced over the whole application rather than endpoint by endpoint.

    A value in an HTTP response body is a value in the reverse proxy's access log, in the
    browser's memory, in the dashboard's DOM and in whatever APM tool records response
    payloads. Handing a credential to an *authorised* human over HTTP still ends with it
    in six places nobody chose. Consumers use ``bailment exec``, which puts it in one
    process image instead.
    """
    secret = sealed_values(await read_lease(osb_instance.lease_id))[SEALED_OUTPUT]
    assert secret.startswith(SEALED_VALUE_PREFIX)

    offenders = [
        f"{method} {path} -> {response.status_code}"
        for (method, path), response in swept.items()
        if path != OSB_BIND and secret in response.text
    ]
    assert offenders == []

    # Headers too: a Location or a Link is a response body somebody else's proxy logs.
    for (method, path), response in swept.items():
        if path == OSB_BIND:
            continue
        assert secret not in str(dict(response.headers)), f"{method} {path}"


async def test_the_one_documented_exception_really_does_return_the_credential(
    swept: dict[tuple[str, str], httpx.Response],
    osb_instance: OsbInstance,
    sealed_values,
    read_lease,
) -> None:
    """Proof that the sweep above can see a leak.

    If the OSB bind endpoint stopped returning credentials, every assertion in the sweep
    would still pass while proving nothing at all.
    """
    secret = sealed_values(await read_lease(osb_instance.lease_id))[SEALED_OUTPUT]
    response = swept[("PUT", OSB_BIND)]
    assert response.status_code == 201
    assert secret in response.text


async def test_the_sweep_finds_nothing_for_an_agent_token_either(
    client: httpx.AsyncClient,
    app: FastAPI,
    osb_instance: OsbInstance,
    sealed_values,
    read_lease,
) -> None:
    """Including the OSB bind endpoint, which is the whole point of gating it."""
    secret = sealed_values(await read_lease(osb_instance.lease_id))[SEALED_OUTPUT]
    responses = await sweep(client, app, osb_instance, "unused", headers=AGENT)
    for (method, path), response in responses.items():
        assert secret not in response.text, f"{method} {path}"


# --------------------------------------------------------------------------------------
# The structural guards
# --------------------------------------------------------------------------------------


def test_no_api_handler_can_reach_a_plaintext_credential() -> None:
    """A structural check, not a naming convention.

    The API's operator tier maps to a caller kind the service *permits* to decrypt, so
    nothing below :mod:`bailment.api.routes` would refuse a handler that asked. This is
    the thing that refuses, and it runs at import time.
    """
    assert_handlers_never_decrypt()
    for module_name in GUARDED_MODULES:
        module = __import__(module_name, fromlist=["*"])
        assert referenced_names(module) & DECRYPTION_NAMES == set()


def test_the_guard_catches_a_handler_that_would_decrypt() -> None:
    """The guard's own test. A check that never fires is a check nobody can trust."""
    import types

    from bailment.engine.service import LeaseService

    module = types.ModuleType("pretend_handlers")

    async def leaky(service: LeaseService, reference: str) -> dict[str, str]:
        return await service.resolve_binding(reference, None)  # type: ignore[arg-type]

    leaky.__module__ = module.__name__
    module.leaky = leaky  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="resolve_binding"):
        assert_handlers_never_decrypt(module)


def test_the_guard_sees_inside_a_comprehension() -> None:
    """A name used only inside a nested code object would otherwise be invisible."""
    import types

    module = types.ModuleType("pretend_nested")

    def sneaky(rows: list[Any]) -> list[Any]:
        return [row.ciphertext for row in rows]

    sneaky.__module__ = module.__name__
    module.sneaky = sneaky  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="ciphertext"):
        assert_handlers_never_decrypt(module)


def test_no_response_model_declares_a_credential_shaped_field() -> None:
    assert_no_secret_fields()
    for model in RESPONSE_MODELS:
        assert secret_looking_fields(model) == []


def test_the_response_model_guard_catches_a_new_field() -> None:
    from pydantic import BaseModel

    class Leaky(BaseModel):
        lease_id: str
        database_password: str

    assert secret_looking_fields(Leaky) == ["Leaky.database_password"]
    with pytest.raises(RuntimeError, match="database_password"):
        assert_no_secret_fields([Leaky])


def test_the_response_model_guard_looks_inside_nested_models() -> None:
    """A secret smuggled into a response would arrive nested inside a container far more
    plausibly than as a top-level field."""
    from pydantic import BaseModel

    class Inner(BaseModel):
        api_token: str

    class Outer(BaseModel):
        items: list[Inner]

    assert secret_looking_fields(Outer) == ["Inner.api_token"]


def test_the_justified_field_list_stays_short() -> None:
    """Adding a second entry should feel like a decision, so the list is pinned."""
    assert {"secret_output_names", "secret"} == JUSTIFIED_FIELD_NAMES
