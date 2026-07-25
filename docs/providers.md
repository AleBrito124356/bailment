# Providers

A provider is the only part of bailment that talks to something outside the process. It is
therefore the only part that can lie to the rest of the system about reality, and every rule
in this document exists to stop a specific lie.

---

## The contract

```python
class Provider(Protocol):
    name: str
    supports_reconciliation: bool

    def is_available(self) -> bool: ...

    async def preflight(self, *, external_name: str, inputs: dict[str, Any]) -> None: ...
    async def create(self, *, external_name: str, inputs: dict[str, Any]) -> ProvisionResult: ...
    async def destroy(self, *, external_name: str, provider_ref: dict[str, Any]) -> None: ...
    async def exists(
        self, *, external_name: str, provider_ref: dict[str, Any]
    ) -> ResourceStatus: ...
    async def list_managed(self) -> list[ManagedResource]: ...
```

Six methods. The engine holds providers only through this protocol, so anything a provider
knows that the engine needs has to be expressed here or in the returned dataclasses.

An optional second protocol, `ExplainsAvailability`, adds `availability_reason() -> str | None`
so a provider can say *why* it is unavailable. It is method-only so `isinstance` works, and
optional so a third-party provider implementing only `Provider` still registers cleanly.

### `is_available`

Whether this provider is configured well enough to be called at all. Missing credentials make
a provider *unavailable*, which the dashboard shows, the MCP catalog reflects and
`bailment providers` explains. It is never a startup crash.

### `preflight`

Validate without mutating. Raise `ProviderError` if `create` would fail. Runs before the lease
leaves `PENDING`, so a request doomed by a typo fails before anything has been named or
provisioned.

### `create`

Create the resource, stamped with `external_name`, and return a `ProvisionResult`.

**Must be idempotent.** Called twice with the same name, it adopts the existing resource and
returns the same reference and the *same credential* rather than creating a second one. A
fresh credential on adoption would leave the caller holding a secret for a resource that no
longer accepts it. Set `adopted=True` so the audit log records it; a burst of adoptions means
something upstream is retrying too eagerly.

### `destroy`

Destroy the resource. **Returning normally means it is gone.**

Destroying something already absent is success, not an error — teardown gets retried, and a
retry that errors on the second pass can never reach `RELEASED`. Anything else raises, and a
raise on the teardown path is what drives a lease to `ORPHANED`.

### `exists`

Returns `ResourceStatus.EXISTS`, `.GONE` or `.UNKNOWN`.

**`UNKNOWN` is a real answer and it is the important one.** A timeout, a 500, a DNS failure —
none of those are proof that a resource was deleted. A provider that collapses them into
`GONE` will cheerfully let the system mark a lease `RELEASED` while the resource keeps
billing.

### `list_managed`

Every resource carrying bailment's marker, and nothing else.

**Raises on failure. Never returns an empty list to signal an error** — the reconciler reads
an empty list as "the account is clean".

The `external_name` on each `ManagedResource` must be the value read back off the real
resource, not the value bailment supplied to the query. A provider that echoed back its own
filter would make every reconcile run agree with itself, which is not reconciliation.

---

## The three rules

### 1. Names come in, never out

`external_name` is computed and committed by the engine *before* the provider is called. The
provider's only job is to stamp it onto the real resource — as the resource's name, or in a
tag, label or comment field it fully controls.

If a provider invented its own name, a crash between "create" and "record the response" would
leave a resource that nothing in the database can be matched against, and orphan detection
would be reduced to guesswork.

This is also why `require_managed_name` **refuses** a name that does not carry the managed
prefix, rather than quietly adding one:

```python
require_managed_name(self.name, external_name, operation="create")
```

Rewriting the name here would break the very correspondence write-ahead naming exists to
guarantee. Call it first in `preflight`, `create` and anywhere else the name arrives from
outside.

### 2. Absence of evidence is not evidence of absence

Covered above under `exists`, and it applies to `list_managed` too. If you cannot answer, say
so; do not answer "nothing".

### 3. Errors are not free text

Provider APIs routinely echo the request back in their error bodies, and the request may
contain a connection URI or a token. Every message reaching a `ProviderError` goes through
`scrub()`, which redacts URL credentials, bearer/basic tokens and any `key: value` pair whose
key looks secret-ish, then truncates to 400 characters.

`ProvisionResult` refuses to render its own outputs in `repr`:

```
ProvisionResult(provider_resource_id='br_abc123', adopted=False, outputs=<2 redacted: API_TOKEN, DATABASE_URL>)
```

because the single most likely way to leak a credential is a traceback, not an API response.

`ProviderError.retryable` is the flag the worker reads to decide between backing off and
giving up. 429, 5xx and transport failures are retryable; 4xx is a bug in the request and
retrying it only spends quota to get the same answer.

---

## `supports_reconciliation`

A provider that cannot stamp `external_name` onto the real resource — because the API has no
name, tag, label or comment field it controls — must set `supports_reconciliation = False` and
return an empty list from `list_managed`. The reconciler then skips the orphan-sweep direction
for it entirely, rather than concluding the account is clean.

Being honest about the gap is fine. Being silently unreconcilable is not.

---

## `HttpProvider`

Base class for providers that talk to a REST API. Holds one lazily-created
`httpx.AsyncClient` per instance so connections pool across a worker's lifetime, and
centralises retry, backoff and scrubbing so three implementations cannot end up with three
different opinions about what a 429 means.

```python
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 8.0
    max_retry_after: float = 30.0
```

`max_retry_after` matters more than it looks. A provider under load can answer
`Retry-After: 600`, and a worker that obeys it holds its claim for ten minutes and stalls
every other lease behind it. Past the cap, `HttpProvider` stops retrying inline and raises a
retryable error, handing the wait back to the lease's own backoff schedule where it can be
observed and interrupted.

There is no unbounded call anywhere in this system.

---

## The four built-in providers

All four are registered unconditionally, credentials or not. There is no "enabled providers"
list on purpose: a provider that is registered-but-unavailable produces the sentence naming
the variable somebody forgot to set, while a provider that was never registered produces
`UnknownProvider`, which reads like a typo in the catalog and sends the reader looking in the
wrong file.

```console
$ bailment providers
┌────────────┬──────────────────────────┬──────────────┬───────────────────────────┬──────────────┐
│ provider   │ status                   │ reconcilable │ missing settings          │ golden paths │
├────────────┼──────────────────────────┼──────────────┼───────────────────────────┼──────────────┤
│ cloudflare │ BAILMENT_CLOUDFLARE_API… │ yes          │ BAILMENT_CLOUDFLARE_API_… │ dns-record   │
│ memory     │ available                │ yes          │ -                         │ sandbox      │
│ neon       │ BAILMENT_NEON_API_KEY is │ yes          │ BAILMENT_NEON_API_KEY,    │ postgres     │
│            │ not set                  │              │ BAILMENT_NEON_PROJECT_ID  │              │
│ upstash    │ BAILMENT_UPSTASH_EMAIL   │ yes          │ BAILMENT_UPSTASH_EMAIL,   │ redis        │
│            │ is not set               │              │ BAILMENT_UPSTASH_API_KEY  │              │
└────────────┴──────────────────────────┴──────────────┴───────────────────────────┴──────────────┘
```

It reports the *names* of missing settings and never a credential, in whole or in part. A
truncated token is still a token to whoever is reading over a shoulder, and no operational
question is answered by a prefix.

### `memory` — the one that needs nothing

Not a stub. It creates resources, hands back credentials, refuses to double-provision on
retry, distinguishes a missing resource from an unreachable one, and enumerates only resources
carrying the marker — the same contract the three real providers meet against real APIs.

What it adds is the ability to *choose* the failure. Every interesting property of this system
is a property of its failure paths, and those are exactly the ones you cannot trigger on
demand against a real cloud API:

| hook | what it produces |
|---|---|
| `fail_next("create", half_succeed=True)` | The create call that really does create the resource and *then* fails to report it. The lease ends up `FAILED` while the resource bills. This is the orphan the reconciler exists to find. |
| `fail_next("destroy")` | Teardown that does not tear down. Must drive the lease to `ORPHANED` and never to `RELEASED`. |
| `vanish(name)` | A resource deleted out of band, by a human in a console. The lease still says `ACTIVE`. Drift in the other direction. |
| `force_status(name, UNKNOWN)` | A resource whose status cannot be determined, to prove nothing downstream rounds `UNKNOWN` to `GONE`. |
| `plant_orphan(name)` | A resource no lease has ever heard of, to prove the sweep finds resources rather than merely agreeing with the database about the ones it already knows. |

State is process-local. That is fine for tests and the demo, and wrong for anything else: the
API, the worker and the reconciler only see the same resources when they run in one process.
There is a real provider for anything else. This is also why `docker-compose.yml` keeps the
worker, the ticker and the reconciler in one container.

> **Known gap.** The `sandbox` golden path declares a `simulate` input documenting `ok`,
> `slow_create`, `create_failure` and `destroy_failure`, but `MemoryProvider` does not read it
> yet. Requesting `simulate: destroy_failure` today provisions and releases normally. Until
> that is wired, the failure paths are demonstrated by `tests/test_reconciler.py` and
> `tests/test_worker.py`, which drive the hooks above directly. The same path also declares
> outputs `SANDBOX_URL` and `SANDBOX_ID` while the provider mints `DATABASE_URL` and
> `API_TOKEN`; the worker notices, logs `provider returned outputs the golden path does not
> declare`, seals them and publishes nothing, which is the correct fail-safe but is not what
> the path's description promises.

### `neon` — Postgres branches

A Neon branch is close to the ideal leased resource: copy-on-write from the parent, so
creating one is seconds rather than minutes, and deleting one actually reclaims the storage.

Two constraints shape it:

- **One project, from configuration.** `BAILMENT_NEON_PROJECT_ID` cannot be overridden per
  request. An agent that could name its own project could create a branch somewhere
  `list_managed` never looks, and a resource outside the reconciler's field of view is worse
  than no resource at all — it is a bill nobody will ever explain.
- **The branch name is the marker.** Neon branches have no tag or label field, so
  `external_name` is used verbatim as the branch name and `list_managed` filters on the
  prefix. That is why `destroy` re-reads the branch before deleting it: deletion is by id, and
  if the name at that id no longer matches what was recorded, somebody has renamed or replaced
  the branch and it is no longer ours to delete.

Settings: `BAILMENT_NEON_API_KEY`, `BAILMENT_NEON_PROJECT_ID`. Output: `DATABASE_URL`.

### `upstash` — Redis

Serverless Redis, created in a couple of seconds, billed per request. A good demonstration of
the difference between a credential and a capability: what comes back is a fresh database with
its own password, not a shared production Redis with a token somebody rotated last year.

The database name is the marker — Upstash has no tag field. Upstash also happily creates two
databases with the same name, so the adopt-on-retry check in `create` is not an optimisation:
without it, one retried provision leaves a second paid-for database that no lease will ever
tear down.

Settings: `BAILMENT_UPSTASH_EMAIL`, `BAILMENT_UPSTASH_API_KEY`. Outputs: `REDIS_URL`,
`REDIS_TOKEN`, sealed together so the two are revoked and expire as one thing.

### `cloudflare` — DNS records

DNS is the smallest useful thing to lease and the easiest to get catastrophically wrong. A
record is free and expires perfectly well — but the same API call that publishes a preview
hostname can also point a company's apex at an attacker or delegate the zone away. So this is
deliberately the most restrictive of the four:

- **One zone, from configuration**, never from request inputs.
- **A small allow-list of record types.** `A`, `AAAA`, `CNAME` and `TXT` affect one hostname.
  `NS` delegates a subtree, `MX` redirects mail, `CAA` decides who may issue certificates for
  the domain. Those are not capabilities to hand an agent by default.
- **The zone apex is refused** unless explicitly enabled. Cloudflare will happily accept a
  second apex `A` record and round-robin traffic to it, which takes half a site down without
  erroring at any point.

The marker lives in the record `comment` field, set to `external_name` exactly. That is what
makes DNS reconcilable at all: record *names* are chosen by the requester and cannot be
trusted to carry a prefix, so the comment is the only field bailment fully controls.
`list_managed` filters on it server-side and again locally.

Settings: `BAILMENT_CLOUDFLARE_API_TOKEN` (scoped `Zone:DNS:Edit`, not a Global API Key),
`BAILMENT_CLOUDFLARE_ZONE_ID`. Output: `FQDN`, marked `secret: false` — it is published in DNS
the moment it exists, and sealing it would only make it harder to use while protecting
nothing.

---

## Writing a provider

```python
"""S3 buckets, leased.

Buckets carry tags, so the marker goes in a tag rather than in the name -- bucket names
are globally unique and a prefix collision across two installations is a bad afternoon.
"""

from typing import Any

from bailment.providers.base import (
    HttpProvider,
    ManagedResource,
    ProviderError,
    ProvisionResult,
    ResourceStatus,
    is_managed_name,
    require_managed_name,
)


class S3Provider(HttpProvider):
    name = "s3"
    supports_reconciliation = True
    base_url = "https://s3.example.com"

    def is_available(self) -> bool:
        return bool(self._access_key and self._secret_key)

    def availability_reason(self) -> str | None:
        if self.is_available():
            return None
        return "BAILMENT_S3_ACCESS_KEY is not set"

    async def preflight(self, *, external_name: str, inputs: dict[str, Any]) -> None:
        require_managed_name(self.name, external_name, operation="preflight")
        # Cheap checks only. No mutation, and no call that costs quota if avoidable.

    async def create(self, *, external_name: str, inputs: dict[str, Any]) -> ProvisionResult:
        require_managed_name(self.name, external_name, operation="create")

        existing = await self._find(external_name)
        if existing is not None:
            # Idempotent retry. Same resource, same credential.
            return ProvisionResult(
                provider_resource_id=existing["id"],
                provider_ref={"id": existing["id"], "name": external_name},
                outputs=await self._existing_credentials(existing),
                adopted=True,
            )

        created = await self._post(
            "/buckets",
            json={
                "name": external_name,
                "tags": {"managed-by": external_name},  # the marker
            },
        )
        return ProvisionResult(
            provider_resource_id=created["id"],
            provider_ref={"id": created["id"], "name": external_name},
            outputs={"S3_BUCKET": created["name"], "S3_SECRET_KEY": created["key"]},
            detail={"region": created["region"]},  # non-secret, goes in the audit trail
        )

    async def destroy(self, *, external_name: str, provider_ref: dict[str, Any]) -> None:
        try:
            await self._delete(f"/buckets/{provider_ref['id']}")
        except ProviderError as exc:
            if exc.status_code == 404:
                return  # already gone is success
            raise

    async def exists(self, *, external_name: str, provider_ref: dict[str, Any]) -> ResourceStatus:
        try:
            await self._get(f"/buckets/{provider_ref['id']}")
        except ProviderError as exc:
            if exc.status_code == 404:
                return ResourceStatus.GONE
            return ResourceStatus.UNKNOWN  # NOT GONE. We do not know.
        return ResourceStatus.EXISTS

    async def list_managed(self) -> list[ManagedResource]:
        page = await self._get("/buckets")  # raises on failure; never returns []
        return [
            ManagedResource(
                external_name=bucket["tags"]["managed-by"],
                provider_resource_id=bucket["id"],
                provider_ref={"id": bucket["id"], "name": bucket["name"]},
                created_at=parse_timestamp(bucket.get("created_at")),
            )
            for bucket in page["buckets"]
            # The defensive filter, even though the query already filtered. The API doing
            # the filtering is the same API that would happily return the whole account if
            # a parameter name changed in a future version.
            if is_managed_name(bucket.get("tags", {}).get("managed-by", ""))
        ]
```

Register it:

```python
from bailment.providers.registry import default_registry

default_registry().register(S3Provider())
```

Then declare a golden path with `provider: s3` and run `bailment catalog validate`,
which will tell you if the provider is unregistered or unconfigured before an agent finds out.

### The checklist before you open a pull request

- [ ] `require_managed_name` is called at the top of `preflight` and `create`, and the name is
      used unmodified.
- [ ] The marker is stamped somewhere `list_managed` can read back — name, tag, label or
      comment — and `list_managed` reads it off the resource rather than echoing the filter.
- [ ] `create` called twice with the same name adopts and returns the *same* credential.
- [ ] `destroy` on an absent resource returns normally.
- [ ] `exists` returns `UNKNOWN` for every failure that is not a definite 404.
- [ ] `list_managed` raises rather than returning a partial or empty list.
- [ ] Every HTTP call goes through `HttpProvider` — explicit timeouts, bounded retries,
      scrubbed errors.
- [ ] No credential appears in a log line, an exception message or a `repr`.
- [ ] Credentials are required through `Settings` and their absence makes the provider
      unavailable rather than crashing anything.
- [ ] Tests use `respx` to mock the API. No test needs a real account; the whole suite runs
      offline and that is a property worth keeping.
