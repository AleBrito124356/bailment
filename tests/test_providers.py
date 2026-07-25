"""The provider contract, and the HTTP machinery every real provider inherits.

A provider is the only part of bailment that talks to something outside the process, so
it is also the only part that can lie to the rest of the system about reality. Three
properties are worth more than the rest and each has a block below:

**Absence of evidence is not evidence of absence.** A timeout, a 500 or a DNS failure is
never ``GONE``. A provider that collapsed them would let a lease be marked ``RELEASED``
while the resource kept billing, and the reconciler would never look at it again.

**Names come in, never out.** ``require_managed_name`` refuses a name without the marker
rather than repairing it, because rewriting it here breaks the correspondence write-ahead
naming exists to guarantee.

**Errors are not free text.** Provider APIs routinely echo the request back in their error
bodies, and the request may contain a connection URI.

The HTTP tests drive :class:`NeonProvider` through ``respx`` rather than a stub, because
the retry, pagination and error-parsing code is shared by all three real providers and a
stub would exercise none of it. ``base_delay=0`` keeps the backoff arithmetic honest
without any test actually sleeping.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from bailment.providers.base import (
    DEFAULT_RESOURCE_PREFIX,
    ManagedResource,
    ProviderConfigurationError,
    ProviderError,
    ProvisionResult,
    ResourceStatus,
    RetryPolicy,
    coerce_bool,
    coerce_int,
    coerce_str,
    is_managed_name,
    parse_timestamp,
    require_managed_name,
    resource_prefix,
    scrub,
    secret_text,
)
from bailment.providers.memory import MemoryProvider, default_memory_provider
from bailment.providers.neon import NEON_API_BASE, NeonProvider
from bailment.providers.registry import (
    ProviderRegistry,
    UnknownProvider,
    build_default_registry,
)

PROJECT = "proj-1"
BRANCHES = f"{NEON_API_BASE}/projects/{PROJECT}/branches"
CONNECTION_URI = "postgresql://owner:hunter2@ep-1.neon.tech/neondb"


@pytest.fixture
def neon() -> NeonProvider:
    """A configured Neon provider that never waits between retries."""
    return NeonProvider(
        api_key="neon-key",
        project_id=PROJECT,
        retry=RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0, max_retry_after=30.0),
    )


def branch(name: str, branch_id: str = "br-1", **extra: Any) -> dict[str, Any]:
    return {"id": branch_id, "name": name, "created_at": "2026-07-20T10:00:00Z", **extra}


# --------------------------------------------------------------------------------------
# Managed names
# --------------------------------------------------------------------------------------


def test_the_marker_is_read_from_the_environment_by_both_sides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine that mints a name and the providers that filter on it must never be able
    to disagree; if they do, every resource looks like an orphan at once."""
    assert resource_prefix() == DEFAULT_RESOURCE_PREFIX
    monkeypatch.setenv("BAILMENT_RESOURCE_PREFIX", "acme-")
    assert resource_prefix() == "acme-"
    assert is_managed_name("acme-thing") is True
    assert is_managed_name("bailment-thing") is False


@pytest.mark.parametrize(
    "name",
    ["", "handmade-db", "BAILMENT-loud", " bailment-space"],
)
def test_a_name_without_the_marker_is_not_ours(name: str) -> None:
    assert is_managed_name(name) is False


def test_require_managed_name_refuses_rather_than_repairing() -> None:
    """A provider that silently prefixed a bad name would create a resource the database
    cannot name -- precisely the failure write-ahead naming exists to prevent."""
    assert require_managed_name("neon", "bailment-ok-123", operation="create") == (
        "bailment-ok-123"
    )
    with pytest.raises(ProviderError, match="does not start with the managed prefix"):
        require_managed_name("neon", "handmade", operation="create")
    with pytest.raises(ProviderError, match="not a legal resource name"):
        require_managed_name("neon", "bailment-Upper", operation="create")
    with pytest.raises(ProviderError, match="not a legal resource name"):
        require_managed_name("neon", "bailment-" + "x" * 80, operation="create")


def test_a_provider_error_names_the_operation_and_is_never_retryable_when_config_is_wrong() -> None:
    error = ProviderError("neon", "boom", retryable=True, status_code=503, operation="create")
    assert "neon.create" in str(error)
    assert "[http 503]" in str(error)
    assert error.retryable is True

    config = ProviderConfigurationError("neon", "no key", operation="create")
    assert config.retryable is False


# --------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "failed for postgresql://user:hunter2@db.example.com/orders",
        'the request was {"password": "hunter2"}',
        "Authorization: Bearer hunter2xxxxxxxxxxxx",
        '{"connection_uri": "postgresql://u:hunter2@h/d"}',
        "dsn=hunter2",
    ],
)
def test_every_provider_error_message_is_scrubbed(text: str) -> None:
    """Provider APIs echo requests back in their error bodies, and the request may carry a
    connection URI or a token. This is a filter, not a guarantee -- the guarantee is that
    nothing puts a secret in a message on purpose -- but it catches that whole class."""
    assert "hunter2" not in scrub(text)
    assert "hunter2" not in ProviderError("neon", text).message


def test_a_scrubbed_message_is_truncated() -> None:
    assert scrub("x" * 5000).endswith("...<truncated>")


def test_a_provision_result_never_prints_its_own_secrets() -> None:
    """A dataclass that prints its own outputs turns every unhandled exception into a
    credential disclosure -- and a traceback is far more likely than an API response."""
    result = ProvisionResult(
        provider_resource_id="br-1",
        provider_ref={"branch_id": "br-1"},
        outputs={"DATABASE_URL": CONNECTION_URI, "PGPASSWORD": "hunter2"},
    )
    rendered = repr(result)
    assert "hunter2" not in rendered
    assert "2 redacted" in rendered
    assert "DATABASE_URL, PGPASSWORD" in rendered
    assert result.output_names == ["DATABASE_URL", "PGPASSWORD"]


def test_secret_text_unwraps_and_strips() -> None:
    from pydantic import SecretStr

    assert secret_text(SecretStr("  key  ")) == "key"
    assert secret_text("  key  ") == "key"
    assert secret_text(None) == ""


# --------------------------------------------------------------------------------------
# Input coercion
# --------------------------------------------------------------------------------------


def test_coercion_helpers_refuse_the_wrong_type_non_retryably() -> None:
    """Inputs arrive already validated, but "the schema checked it" is not a property to
    rely on when the value is about to be interpolated into a provider API call."""
    assert coerce_str("neon", {"a": "x"}, "a") == "x"
    assert coerce_str("neon", {}, "a") is None
    assert coerce_str("neon", {}, "a", default="d") == "d"
    with pytest.raises(ProviderError, match="must be a string"):
        coerce_str("neon", {"a": 1}, "a")

    assert coerce_int("neon", {"a": 5}, "a") == 5
    with pytest.raises(ProviderError, match="must be an integer"):
        coerce_int("neon", {"a": "5"}, "a")
    # bool is an int subclass in Python but not in JSON, and "ttl: true" must not become 1.
    with pytest.raises(ProviderError, match="must be an integer"):
        coerce_int("neon", {"a": True}, "a")

    assert coerce_bool("neon", {"a": True}, "a", default=False) is True
    assert coerce_bool("neon", {}, "a", default=True) is True
    with pytest.raises(ProviderError, match="must be a boolean"):
        coerce_bool("neon", {"a": "yes"}, "a", default=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-20T10:00:00Z", datetime(2026, 7, 20, 10, 0, tzinfo=UTC)),
        ("2026-07-20T10:00:00+00:00", datetime(2026, 7, 20, 10, 0, tzinfo=UTC)),
        ("2026-07-20T10:00:00", None),  # naive: dropped rather than guessed at
        ("not a timestamp", None),
        ("", None),
        (None, None),
        (12345, None),
    ],
)
def test_provider_timestamps_are_decoration_and_never_naive(raw: object, expected: Any) -> None:
    assert parse_timestamp(raw) == expected


# --------------------------------------------------------------------------------------
# The shared HTTP machinery
# --------------------------------------------------------------------------------------


async def test_a_rate_limit_is_retried_and_then_succeeds(neon: NeonProvider) -> None:
    async with respx.mock:
        route = respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            side_effect=[
                httpx.Response(429, json={"message": "slow down"}),
                httpx.Response(200, json={"project": {"id": PROJECT}}),
            ]
        )
        response = await neon._request("GET", f"/projects/{PROJECT}", operation="preflight")

    assert response.status_code == 200
    assert route.call_count == 2
    await neon.aclose()


async def test_a_server_error_exhausts_the_budget_and_stays_retryable(
    neon: NeonProvider,
) -> None:
    """Retryable, so the lease's own backoff schedule takes over -- where the wait is
    observable and interruptible instead of being held inside a worker's claim."""
    async with respx.mock:
        route = respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            return_value=httpx.Response(503, json={"message": "unavailable"})
        )
        with pytest.raises(ProviderError) as caught:
            await neon._request("GET", f"/projects/{PROJECT}", operation="preflight")

    assert route.call_count == 3
    assert caught.value.retryable is True
    assert "giving up after 3 attempts" in caught.value.message
    assert "unavailable" in caught.value.message
    await neon.aclose()


async def test_a_client_error_is_not_retried_at_all(neon: NeonProvider) -> None:
    """4xx is a bug in the request, and retrying it only spends quota to get the same
    answer."""
    async with respx.mock:
        route = respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            return_value=httpx.Response(400, json={"message": "branch name is invalid"})
        )
        with pytest.raises(ProviderError) as caught:
            await neon._request("GET", f"/projects/{PROJECT}", operation="preflight")

    assert route.call_count == 1
    assert caught.value.retryable is False
    assert caught.value.status_code == 400
    assert "branch name is invalid" in caught.value.message
    await neon.aclose()


async def test_a_long_retry_after_is_handed_back_to_the_lease_schedule(
    neon: NeonProvider,
) -> None:
    """A worker that obeyed ``Retry-After: 600`` would hold its claim for ten minutes and
    stall every lease behind it."""
    async with respx.mock:
        route = respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "600"})
        )
        with pytest.raises(ProviderError) as caught:
            await neon._request("GET", f"/projects/{PROJECT}", operation="preflight")

    assert route.call_count == 1
    assert caught.value.retryable is True
    assert "longer than a worker should hold a claim" in caught.value.message
    await neon.aclose()


async def test_a_short_retry_after_is_obeyed(neon: NeonProvider) -> None:
    async with respx.mock:
        route = respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json={"project": {}}),
            ]
        )
        await neon._request("GET", f"/projects/{PROJECT}", operation="preflight")
    assert route.call_count == 2
    await neon.aclose()


async def test_a_transport_failure_is_retryable(neon: NeonProvider) -> None:
    async with respx.mock:
        respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            side_effect=httpx.ConnectTimeout("no route to host")
        )
        with pytest.raises(ProviderError) as caught:
            await neon._request("GET", f"/projects/{PROJECT}", operation="preflight")

    assert caught.value.retryable is True
    assert "timeout" in caught.value.message
    await neon.aclose()


async def test_a_redirect_is_never_followed(neon: NeonProvider) -> None:
    """Following one would replay the ``Authorization`` header at whatever host the
    provider named. Nothing here legitimately redirects."""
    async with respx.mock:
        respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.example/"})
        )
        elsewhere = respx.get("https://evil.example/").mock(
            return_value=httpx.Response(200, json={})
        )
        with pytest.raises(ProviderError):
            await neon._request("GET", f"/projects/{PROJECT}", operation="preflight")

    assert elsewhere.call_count == 0
    await neon.aclose()


async def test_an_allowed_status_comes_back_instead_of_raising(neon: NeonProvider) -> None:
    """That is how ``destroy`` and ``exists`` get to see a 404 and decide for themselves
    what it means -- the only place in the system allowed to conclude "gone"."""
    async with respx.mock:
        respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            return_value=httpx.Response(404, json={"message": "no such project"})
        )
        response = await neon._request(
            "GET", f"/projects/{PROJECT}", allow_status=(404,), operation="exists"
        )
    assert response.status_code == 404
    await neon.aclose()


async def test_an_error_body_that_carries_a_credential_is_scrubbed(
    neon: NeonProvider,
) -> None:
    async with respx.mock:
        respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            return_value=httpx.Response(
                400, json={"message": f"could not connect to {CONNECTION_URI}"}
            )
        )
        with pytest.raises(ProviderError) as caught:
            await neon._request("GET", f"/projects/{PROJECT}", operation="preflight")
    assert "hunter2" not in caught.value.message
    assert "hunter2" not in str(caught.value)
    await neon.aclose()


async def test_a_response_of_the_wrong_json_shape_is_a_non_retryable_error(
    neon: NeonProvider,
) -> None:
    async with respx.mock:
        respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            return_value=httpx.Response(200, text="not json")
        )
        response = await neon._request("GET", f"/projects/{PROJECT}", operation="exists")
        with pytest.raises(ProviderError, match="was not JSON"):
            neon._json_object(response, operation="exists")
        with pytest.raises(ProviderError, match="was not JSON"):
            neon._json_list(response, operation="exists")
    await neon.aclose()


async def test_a_json_array_where_an_object_belongs_is_refused(neon: NeonProvider) -> None:
    async with respx.mock:
        respx.get(f"{NEON_API_BASE}/projects/{PROJECT}").mock(
            return_value=httpx.Response(200, json=[1, 2])
        )
        response = await neon._request("GET", f"/projects/{PROJECT}", operation="exists")
        with pytest.raises(ProviderError, match="expected a JSON object"):
            neon._json_object(response, operation="exists")
        assert neon._json_list(response, operation="exists") == [1, 2]
    await neon.aclose()


async def test_an_unconfigured_provider_refuses_before_it_opens_a_connection() -> None:
    provider = NeonProvider(api_key="", project_id="")
    assert provider.is_available() is False
    assert "BAILMENT_NEON_API_KEY" in (provider.availability_reason() or "")

    with_key = NeonProvider(api_key="k", project_id="")
    assert "BAILMENT_NEON_PROJECT_ID" in (with_key.availability_reason() or "")

    with pytest.raises(ProviderConfigurationError):
        await provider._request("GET", "/projects", operation="preflight")


# --------------------------------------------------------------------------------------
# The Neon contract
# --------------------------------------------------------------------------------------


async def test_create_adopts_a_branch_that_already_carries_our_name(
    neon: NeonProvider,
) -> None:
    """The idempotent-retry path: a worker crashed after Neon created the branch but
    before the response was recorded. Creating a second one would leave the first
    unreferenced and unbilled-to-anybody."""
    async with respx.mock:
        listed = respx.get(BRANCHES).mock(
            return_value=httpx.Response(
                200, json={"branches": [branch("bailment-sandbox-abcd1234")]}
            )
        )
        created = respx.post(BRANCHES)
        respx.get(f"{BRANCHES}/br-1/databases").mock(
            return_value=httpx.Response(
                200, json={"databases": [{"name": "neondb", "owner_name": "owner"}]}
            )
        )
        respx.get(f"{NEON_API_BASE}/projects/{PROJECT}/connection_uri").mock(
            return_value=httpx.Response(200, json={"uri": CONNECTION_URI})
        )

        result = await neon.create(external_name="bailment-sandbox-abcd1234", inputs={})

    assert created.call_count == 0, "adopting must not create a second branch"
    assert listed.called
    assert result.adopted is True
    assert result.provider_resource_id == "br-1"
    await neon.aclose()


async def test_create_builds_the_outputs_from_the_connection_uri(neon: NeonProvider) -> None:
    """Reading the URI the create call already returned is two fewer round trips, and it
    cannot disagree with the credential we are about to hand out."""
    async with respx.mock:
        respx.get(BRANCHES).mock(return_value=httpx.Response(200, json={"branches": []}))
        respx.post(BRANCHES).mock(
            return_value=httpx.Response(
                201,
                json={
                    "branch": branch("bailment-sandbox-abcd1234"),
                    "connection_uris": [{"connection_uri": CONNECTION_URI}],
                },
            )
        )
        result = await neon.create(external_name="bailment-sandbox-abcd1234", inputs={})

    assert result.adopted is False
    assert result.outputs["DATABASE_URL"] == CONNECTION_URI
    assert result.outputs["PGUSER"] == "owner"
    assert result.outputs["PGPASSWORD"] == "hunter2"
    assert result.outputs["PGHOST"] == "ep-1.neon.tech"
    assert result.outputs["PGDATABASE"] == "neondb"
    assert result.provider_ref["branch_id"] == "br-1"
    # The name bailment supplied, echoed back off the real resource.
    assert result.provider_ref["branch_name"] == "bailment-sandbox-abcd1234"
    await neon.aclose()


async def test_a_creation_conflict_is_re_read_rather_than_failed(neon: NeonProvider) -> None:
    """Lost a race with our own retry, or with another worker holding a stale claim. The
    resource we wanted now exists."""
    async with respx.mock:
        respx.get(BRANCHES).mock(
            side_effect=[
                httpx.Response(200, json={"branches": []}),
                httpx.Response(200, json={"branches": [branch("bailment-sandbox-abcd1234")]}),
            ]
        )
        respx.post(BRANCHES).mock(return_value=httpx.Response(409, json={"message": "exists"}))
        respx.get(f"{BRANCHES}/br-1/databases").mock(
            return_value=httpx.Response(
                200, json={"databases": [{"name": "neondb", "owner_name": "owner"}]}
            )
        )
        respx.get(f"{NEON_API_BASE}/projects/{PROJECT}/connection_uri").mock(
            return_value=httpx.Response(200, json={"uri": CONNECTION_URI})
        )
        result = await neon.create(external_name="bailment-sandbox-abcd1234", inputs={})

    assert result.adopted is True
    await neon.aclose()


async def test_create_refuses_a_name_that_is_not_ours(neon: NeonProvider) -> None:
    with pytest.raises(ProviderError, match="managed prefix"):
        await neon.create(external_name="handmade", inputs={})


async def test_destroy_finds_the_branch_by_name_when_the_id_was_never_recorded(
    neon: NeonProvider,
) -> None:
    """The write-ahead name earns its keep here: the worker died before it could record
    the branch id, and the name is the only thing that can find it."""
    async with respx.mock:
        respx.get(BRANCHES).mock(
            return_value=httpx.Response(
                200, json={"branches": [branch("bailment-sandbox-abcd1234")]}
            )
        )
        respx.get(f"{BRANCHES}/br-1").mock(
            return_value=httpx.Response(200, json={"branch": branch("bailment-sandbox-abcd1234")})
        )
        deleted = respx.delete(f"{BRANCHES}/br-1").mock(return_value=httpx.Response(200, json={}))
        await neon.destroy(external_name="bailment-sandbox-abcd1234", provider_ref={})

    assert deleted.call_count == 1
    await neon.aclose()


async def test_destroying_something_already_gone_is_success(neon: NeonProvider) -> None:
    """A retry of a partially completed teardown has to be able to finish cleanly."""
    async with respx.mock:
        respx.get(f"{BRANCHES}/br-1").mock(return_value=httpx.Response(404, json={}))
        await neon.destroy(
            external_name="bailment-sandbox-abcd1234", provider_ref={"branch_id": "br-1"}
        )
    await neon.aclose()


async def test_destroy_refuses_a_branch_that_has_been_renamed(neon: NeonProvider) -> None:
    """Deleting by id alone would destroy whatever now lives at that id.

    Refusing leaves the lease ORPHANED, which is visible and fixable. The alternative is
    invisible and is somebody's production branch.
    """
    async with respx.mock:
        respx.get(f"{BRANCHES}/br-1").mock(
            return_value=httpx.Response(200, json={"branch": branch("someone-elses-branch")})
        )
        deleted = respx.delete(f"{BRANCHES}/br-1")
        with pytest.raises(ProviderError, match="refusing to delete"):
            await neon.destroy(
                external_name="bailment-sandbox-abcd1234", provider_ref={"branch_id": "br-1"}
            )

    assert deleted.call_count == 0
    await neon.aclose()


async def test_exists_answers_unknown_rather_than_gone_when_it_cannot_tell(
    neon: NeonProvider,
) -> None:
    """The single most important negative result in the provider layer.

    Saying GONE here is how a live resource gets marked RELEASED and disappears from every
    report while it keeps billing.
    """
    async with respx.mock:
        respx.get(f"{BRANCHES}/br-1").mock(return_value=httpx.Response(503, json={}))
        status = await neon.exists(
            external_name="bailment-sandbox-abcd1234", provider_ref={"branch_id": "br-1"}
        )
    assert status is ResourceStatus.UNKNOWN
    await neon.aclose()


async def test_exists_answers_gone_only_on_a_definite_404(neon: NeonProvider) -> None:
    async with respx.mock:
        respx.get(f"{BRANCHES}/br-1").mock(return_value=httpx.Response(404, json={}))
        gone = await neon.exists(
            external_name="bailment-sandbox-abcd1234", provider_ref={"branch_id": "br-1"}
        )
        respx.get(f"{BRANCHES}/br-1").mock(
            return_value=httpx.Response(200, json={"branch": branch("bailment-sandbox-abcd1234")})
        )
        present = await neon.exists(
            external_name="bailment-sandbox-abcd1234", provider_ref={"branch_id": "br-1"}
        )
    assert gone is ResourceStatus.GONE
    assert present is ResourceStatus.EXISTS
    await neon.aclose()


async def test_an_unconfigured_provider_answers_unknown_not_gone() -> None:
    provider = NeonProvider(api_key="", project_id="")
    status = await provider.exists(external_name="bailment-x", provider_ref={})
    assert status is ResourceStatus.UNKNOWN


async def test_list_managed_reports_only_the_branches_carrying_the_marker(
    neon: NeonProvider,
) -> None:
    """The prefix check is the whole safety story for the orphan sweep: Neon has no way to
    mark a branch as ours other than its name."""
    async with respx.mock:
        respx.get(BRANCHES).mock(
            return_value=httpx.Response(
                200,
                json={
                    "branches": [
                        branch("bailment-sandbox-abcd1234", "br-1"),
                        branch("production", "br-2"),
                        branch("bailment-postgres-ffff0000", "br-3"),
                        {"name": "no-id"},
                    ]
                },
            )
        )
        managed = await neon.list_managed()

    assert [resource.external_name for resource in managed] == [
        "bailment-sandbox-abcd1234",
        "bailment-postgres-ffff0000",
    ]
    assert managed[0].created_at == datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    assert managed[0].provider_ref["branch_id"] == "br-1"
    await neon.aclose()


async def test_list_managed_raises_rather_than_returning_an_empty_list(
    neon: NeonProvider,
) -> None:
    """The reconciler reads an empty list as "the account is clean"."""
    async with respx.mock:
        respx.get(BRANCHES).mock(return_value=httpx.Response(500, json={"message": "down"}))
        with pytest.raises(ProviderError):
            await neon.list_managed()
    await neon.aclose()


async def test_pagination_is_followed(neon: NeonProvider) -> None:
    async with respx.mock:
        first = [branch(f"bailment-p-{index:08d}", f"br-{index}") for index in range(100)]
        respx.get(BRANCHES).mock(
            side_effect=[
                httpx.Response(
                    200, json={"branches": first, "pagination": {"cursor": "next-page"}}
                ),
                httpx.Response(200, json={"branches": [branch("bailment-last", "br-last")]}),
            ]
        )
        managed = await neon.list_managed()

    assert len(managed) == 101
    assert managed[-1].external_name == "bailment-last"
    await neon.aclose()


# --------------------------------------------------------------------------------------
# The memory provider is a real provider
# --------------------------------------------------------------------------------------


async def test_the_memory_provider_meets_the_same_contract() -> None:
    """It is not a stub: it adopts on retry, distinguishes missing from unreachable, and
    enumerates only what carries the marker."""
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    name = "bailment-sandbox-abcd1234"

    await provider.preflight(external_name=name, inputs={})
    first = await provider.create(external_name=name, inputs={"note": "hello"})
    second = await provider.create(external_name=name, inputs={})

    assert first.adopted is False
    assert second.adopted is True
    assert second.outputs == first.outputs
    assert provider.calls["create"] == 2
    assert len(provider.snapshot()) == 1

    assert await provider.exists(external_name=name, provider_ref={}) is ResourceStatus.EXISTS
    provider.force_status(name, ResourceStatus.UNKNOWN)
    assert await provider.exists(external_name=name, provider_ref={}) is ResourceStatus.UNKNOWN
    provider.force_status(name, None)

    await provider.destroy(external_name=name, provider_ref={})
    assert await provider.exists(external_name=name, provider_ref={}) is ResourceStatus.GONE
    # Destroying something already gone is success.
    await provider.destroy(external_name=name, provider_ref={})


async def test_the_memory_provider_refuses_an_unmanaged_name() -> None:
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    with pytest.raises(ProviderError, match="managed prefix"):
        await provider.create(external_name="handmade", inputs={})
    with pytest.raises(ProviderError, match="managed prefix"):
        await provider.preflight(external_name="handmade", inputs={})


async def test_the_memory_provider_reset_clears_everything() -> None:
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    provider.plant_orphan("bailment-x-1")
    provider.fail_next("create")
    provider.set_unavailable("nope")
    provider.reset()
    assert provider.snapshot() == {}
    assert provider.calls == {}
    assert provider.is_available() is True


def test_the_default_memory_provider_is_a_process_wide_singleton() -> None:
    """So an API request, a worker tick and a reconciler pass in one process act on the
    same fake cloud."""
    assert default_memory_provider() is default_memory_provider()


# --------------------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------------------


def test_the_registry_keeps_providers_that_cannot_run() -> None:
    """ "We do not offer that" produces ``UnknownProvider``, which reads like a typo in the
    catalog and sends the reader to the wrong file. "Nobody set the API key" has to be a
    different answer."""
    registry = ProviderRegistry()
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    provider.set_unavailable("BAILMENT_MEMORY_TOKEN is not set")
    registry.register(provider)

    assert registry.get("memory") is provider
    assert registry.names() == ["memory"]
    assert registry.available_names() == []
    assert registry.reconcilable() == []
    with pytest.raises(ProviderConfigurationError, match="BAILMENT_MEMORY_TOKEN"):
        registry.require_available("memory")

    report = registry.report()[0]
    assert report.available is False
    assert report.reason == "BAILMENT_MEMORY_TOKEN is not set"
    assert report.as_dict()["supports_reconciliation"] is True


def test_an_unknown_provider_lists_the_registered_ones() -> None:
    registry = ProviderRegistry()
    registry.register(MemoryProvider(latency_range=(0.0, 0.0)))
    with pytest.raises(UnknownProvider) as caught:
        registry.get("neon")
    assert "registered providers are memory" in str(caught.value)
    assert caught.value.name == "neon"


def test_registering_twice_needs_saying_so() -> None:
    registry = ProviderRegistry()
    first = MemoryProvider(latency_range=(0.0, 0.0))
    second = MemoryProvider(latency_range=(0.0, 0.0))
    registry.register(first)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(second)
    registry.register(second, replace=True)
    assert registry.get("memory") is second
    assert len(registry) == 1


def test_a_provider_with_no_name_is_refused() -> None:
    registry = ProviderRegistry()
    with pytest.raises(ValueError, match="no name"):
        registry.register(MemoryProvider(name="", latency_range=(0.0, 0.0)))


def test_a_provider_that_cannot_enumerate_is_left_out_of_the_sweep() -> None:
    """``list_managed`` on one returns nothing by contract, and an empty answer is the one
    thing the reconciler must never invent."""
    registry = ProviderRegistry()
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    provider.supports_reconciliation = False
    registry.register(provider)
    assert registry.reconcilable() == []


async def test_the_default_registry_holds_every_built_in_provider() -> None:
    """All four, unconditionally, including the ones with no credentials."""
    registry = build_default_registry()
    assert registry.names() == ["cloudflare", "memory", "neon", "upstash"]
    assert registry.available_names() == ["memory"]
    assert [status.name for status in registry.report() if not status.available] == [
        "cloudflare",
        "neon",
        "upstash",
    ]
    await registry.aclose()


def test_a_managed_resource_carries_what_the_provider_read_back() -> None:
    """``external_name`` is the value read off the real resource, not the filter bailment
    supplied -- a provider that echoed the filter would make every run agree with itself."""
    resource = ManagedResource(
        external_name="bailment-sandbox-abcd1234", provider_resource_id="br-1"
    )
    assert resource.provider_ref == {}
    assert resource.created_at is None


# --------------------------------------------------------------------------------------
# The `simulate` input
#
# These exist because the behaviour they cover was documented in the shipped `sandbox`
# golden path before it was implemented. The path's description promised that
# simulate=destroy_failure would strand a resource and produce an orphan; the provider
# never read the input, so the teardown succeeded and the headline demo quietly showed
# the opposite of what it claimed. Nothing failed -- that is what made it expensive.
# --------------------------------------------------------------------------------------


async def test_simulate_defaults_to_ok_when_a_path_does_not_offer_it() -> None:
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    result = await provider.create(external_name="bailment-x-1", inputs={})
    assert result.provider_resource_id
    await provider.destroy(external_name="bailment-x-1", provider_ref=result.provider_ref)
    assert (
        await provider.exists(external_name="bailment-x-1", provider_ref=result.provider_ref)
        is ResourceStatus.GONE
    )


async def test_an_unknown_simulate_value_is_rejected_not_ignored() -> None:
    """A typo must fail loudly. Treating it as 'ok' turns a failure demo into a success."""
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    with pytest.raises(ProviderError) as excinfo:
        await provider.preflight(
            external_name="bailment-x-2", inputs={"simulate": "destroy-failure"}
        )
    assert "unknown simulate value" in str(excinfo.value)
    assert not excinfo.value.retryable


async def test_simulate_create_failure_leaves_nothing_behind() -> None:
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    with pytest.raises(ProviderError) as excinfo:
        await provider.create(external_name="bailment-x-3", inputs={"simulate": "create_failure"})
    assert not excinfo.value.retryable
    # Nothing was recorded, so this is the clean failure the lease may call FAILED.
    assert await provider.list_managed() == []


async def test_simulate_destroy_failure_strands_the_resource() -> None:
    """The assertion that matters: the resource is STILL THERE after the failed destroy.

    A destroy that raises after removing the resource would produce an orphaned lease
    pointing at nothing, and the reconciler would sweep and report the account clean.
    """
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    created = await provider.create(
        external_name="bailment-x-4", inputs={"simulate": "destroy_failure"}
    )

    with pytest.raises(ProviderError) as excinfo:
        await provider.destroy(external_name="bailment-x-4", provider_ref=created.provider_ref)
    assert excinfo.value.retryable

    assert (
        await provider.exists(external_name="bailment-x-4", provider_ref=created.provider_ref)
        is ResourceStatus.EXISTS
    )
    assert [r.external_name for r in await provider.list_managed()] == ["bailment-x-4"]


async def test_relent_lets_a_stranded_resource_finally_be_destroyed() -> None:
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    created = await provider.create(
        external_name="bailment-x-5", inputs={"simulate": "destroy_failure"}
    )
    with pytest.raises(ProviderError):
        await provider.destroy(external_name="bailment-x-5", provider_ref=created.provider_ref)

    assert provider.relent("bailment-x-5") == 1
    await provider.destroy(external_name="bailment-x-5", provider_ref=created.provider_ref)
    assert await provider.list_managed() == []
    # Relenting again changes nothing and does not raise.
    assert provider.relent() == 0


async def test_the_simulate_choice_survives_into_teardown() -> None:
    """destroy() never sees the original inputs, so the wish must be stored on the resource."""
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    created = await provider.create(
        external_name="bailment-x-6", inputs={"simulate": "destroy_failure"}
    )
    # Teardown is handed only what was persisted -- deliberately no inputs.
    with pytest.raises(ProviderError):
        await provider.destroy(external_name="bailment-x-6", provider_ref=created.provider_ref)
    assert provider.snapshot()["bailment-x-6"].simulate == "destroy_failure"


async def test_outputs_are_minted_from_what_the_golden_path_declares() -> None:
    """One fake provider has to serve any path written against it, not just sandbox."""
    provider = MemoryProvider(latency_range=(0.0, 0.0))
    result = await provider.create(
        external_name="bailment-x-7",
        inputs={},
        declared_outputs=["SANDBOX_URL", "SANDBOX_ID", "REDIS_TOKEN", "SERVICE_FQDN"],
    )
    assert sorted(result.outputs) == ["REDIS_TOKEN", "SANDBOX_ID", "SANDBOX_URL", "SERVICE_FQDN"]
    assert result.outputs["SANDBOX_URL"].startswith("memory://")
    assert result.outputs["SANDBOX_ID"] == "bailment-x-7"
    assert result.outputs["SERVICE_FQDN"].endswith(".memory.invalid")
    # No stray keys the path never declared -- the worker would refuse to publish them.
    assert "DATABASE_URL" not in result.outputs
