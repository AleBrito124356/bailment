# Architecture

This document covers the shape of the system and, more usefully, the decisions that
produced that shape. Anywhere a choice looks arbitrary, the reason is written down. The
source files carry the same reasoning at a finer grain — `src/bailment/states.py` and
`src/bailment/models.py` are worth reading before this if you are about to change anything.

---

## The one-paragraph version

A **golden path** is a YAML file describing something a platform team is willing to hand
out. From it, bailment derives the MCP tool an agent sees, the form a human sees, the Open
Service Broker catalog entry, the policy chain and the lease ceilings. A **request** against
a golden path is validated, clamped, evaluated against policy and committed as a **lease**
row — with the resource's name already decided. A **worker** takes that row, calls a
**provider**, seals the credential and starts the clock. A **ticker** drives the lease to
expiry and a worker destroys the resource. A **reconciler** compares what the provider says
exists against what the lease table believes, in both directions.

---

## Layers

Each layer may import the ones above it and never the ones below.

```
states.py        the lease state machine. Imports nothing from bailment.
models.py        SQLAlchemy models.
config.py        settings, from BAILMENT_* environment variables.
secrets.py       the Fernet envelope.
logging.py       structured logging, including the secret redactor.
db.py            async engine and sessions.
catalog/         golden path schema and loader.
policy/          the expression evaluator and the rule-chain engine. Pure; no I/O.
providers/       the provider contract and four implementations.
engine/          service, worker, ticker, reconciler. The only layer that mutates a lease.
api/  mcp/  osb/ the three request surfaces.
cli.py           the operator's terminal, and the credential injection path.
main.py          the ASGI application: assembly, health, metrics.
```

`import bailment` reads no environment, opens no connection and touches no filesystem,
because `bailment keygen` has to work on a machine where none of those are configured yet.

---

## The state machine

`src/bailment/states.py` is the single authority. Nothing else encodes its own idea of the
lifecycle.

```
                          ┌──────────────────┐
       request ──────────▶│     PENDING      │
                          └────────┬─────────┘
             ┌─────────────────────┼──────────────────────┐
             ▼                     ▼                      ▼
    ┌────────────────┐    ┌────────────────┐      ┌──────────────┐
    │AWAITING_APPROVAL│──▶│  PROVISIONING  │      │   REJECTED   │ terminal
    └───────┬─────────┘   └───────┬────────┘      └──────────────┘
            │ declined            │ provider confirmed
            ▼                     ▼
      ┌──────────┐        ┌────────────────┐      ┌──────────────┐
      │ REJECTED │        │     ACTIVE     │─────▶│    FAILED    │ terminal
      └──────────┘        └───────┬────────┘      └──────────────┘
                                  │ warn_before
                                  ▼
                          ┌────────────────┐
                     ┌────│    EXPIRING    │  (renew → back to ACTIVE)
                     │    └───────┬────────┘
              revoke │            │ TTL elapsed
                     ▼            ▼
              ┌──────────┐  ┌──────────┐
              │ REVOKED  │  │ EXPIRED  │      intentions, not facts
              └────┬─────┘  └────┬─────┘
                   └──────┬──────┘
                          ▼
                  ┌────────────────┐
                  │ DEPROVISIONING │
                  └───┬────────┬───┘
       provider says  │        │  destroy failed, budget exhausted
       it is gone     ▼        ▼
              ┌──────────┐  ┌──────────┐
              │ RELEASED │  │ ORPHANED │◀── also entered from the reconciler,
              └──────────┘  └──────────┘    for a resource with no live lease
                 terminal      (retry-teardown → DEPROVISIONING)

  UNKNOWN sits beside all of it: reachable from any live state when the provider
  cannot be asked, and it leaves only when the provider answers.
```

**The rule that shapes everything else: `RELEASED` is a claim about reality.** A resource is
only considered gone once the *provider* has confirmed it. Nothing may write `RELEASED`
except the deprovision path and the reconciler. Everything else that wants a lease to end
asks for `EXPIRED` or `REVOKED`, which are *intentions*, and lets the engine do the work.

That separation is what makes orphan detection possible at all. If any code could write
`RELEASED` optimistically, a failed destroy would look identical to a successful one and the
orphan would be invisible forever.

`UNKNOWN` is a real state and not a placeholder. A provider that timed out has told us
nothing. Collapsing that into `GONE` — and then into `RELEASED` — is the single most
dangerous thing this system could do, so `UNKNOWN` changes no state, no timestamp and writes
no audit row beyond the observation itself.

---

## Write-ahead naming

`Lease.external_name` is computed and committed **before** the provider is called, never
assigned from the provider's response.

```
  request ──▶ compute external_name ──▶ COMMIT ──▶ worker claims ──▶ provider.create()
                                          ▲                              │
                                          │                              ▼
                        the row already knows the name         resource stamped with it
```

If the worker dies anywhere to the right of that commit, the row already knows the name and
tag the resource was going to carry, so the reconciler can go and look for it. A system that
names resources from the provider's response cannot do this: a crash in that window leaves a
real, billing resource that nothing on earth can associate back to a request.

This is why `require_managed_name` **refuses** a name lacking the managed prefix instead of
quietly adding one. A provider that repaired the name would create a resource the database
cannot name, which is exactly the failure the mechanism exists to prevent.

The prefix is `bailment-` by default and comes from `resource_prefix()` — a function rather
than a constant, because the engine that mints names and the providers that filter on them
must never be able to disagree. If they do, every resource looks like an orphan and the
reconciler tries to delete the entire fleet.

---

## Claims, not locks

A worker takes a lease by writing `claimed_by` and `claimed_at` under a conditional update.
There is no distributed lock anywhere in this system.

A claim expires on its own after five minutes. A worker that dies holding one does not wedge
the lease forever: the claim goes stale and another worker picks it up. There is nothing to
leak, nothing to reap, and no lock service to be down.

`Lease.attempts` and `Lease.next_attempt_at` carry the retry schedule, so backoff is a
property of the row rather than of a worker's memory. A worker restarting does not reset
anybody's budget.

---

## The request path

All three surfaces converge on `LeaseService`. None of them touch `bailment.models` directly.

```
  MCP tool call ─┐
  POST /api/v1/leases ─┼──▶ LeaseService.request_provision
  PUT /v2/service_instances/{id} ─┘        │
                                           ├─ 1. resolve the golden path
                                           ├─ 2. validate inputs against its JSON Schema
                                           ├─ 3. replay the idempotency key, if any
                                           ├─ 4. clamp the TTL to max_ttl
                                           ├─ 5. build the policy context
                                           ├─ 6. evaluate the policy chain
                                           ├─ 7. compute external_name
                                           └─ 8. COMMIT
                                                   │
                              allow ───────────────┼─────────────── require_approval
                                │                  │                        │
                                ▼                  ▼                        ▼
                            PENDING             REJECTED            AWAITING_APPROVAL
                                │                (deny)                     │
                                │                                  operator approves
                                ▼                                           │
                          worker claims ◀───────────────────────────────────┘
                                │
                     provider.preflight() ──▶ provider.create()
                                │
                     seal outputs with Fernet ──▶ Binding row
                                │
                                ▼
                             ACTIVE, expires_at = now + ttl
```

Four things about that order are deliberate.

**TTL is clamped before the policy context is built**, so a rule reading `ttl_seconds`
compares against the duration that will actually be granted rather than the one that was
requested. Clamping rather than rejecting is itself a choice: a rejected request teaches an
agent nothing and it retries with another guess, while a clamped one comes back with
`max_ttl_seconds` and a notice, and the agent stops guessing.

**The clock starts at `ACTIVE`, not at request time.** A lease that spent three hours in
`AWAITING_APPROVAL` has not been holding a database for three hours, and burning the TTL
while waiting for a human would hand the approver a lease that expires before the requester
can use it.

**Mutating service methods commit; readers do not.** This departs from the "endpoints
commit" convention in `db.py` for one reason: the durability of a lease row is part of its
meaning. A lease that exists only inside an uncommitted transaction is a lease no worker can
claim and no reconciler can match a resource against.

**Every mutating operation is idempotent.** An idempotency key returns the same lease, never
a second resource; over MCP the key is derived from the session plus the arguments, so a
model that calls the same tool twice in one session gets the same lease back.

---

## Secrets: one door

```
  provider returns {"DATABASE_URL": "postgres://..."}
                    │
                    ▼
       SecretBox.seal()  ── Fernet (AES-128-CBC + HMAC-SHA256) ──▶ Binding.ciphertext
                    │
                    ▼
       reference: bailment://binding/<uuid>
                    │
    ┌───────────────┼───────────────────────┬─────────────────────┐
    ▼               ▼                       ▼                     ▼
  MCP tool     REST /api/v1            dashboard            OSB /v2 binding
  reference    reference               output names         VALUES (operator token only)
   only          only                    only                      │
                                                                   ▼
                                                       LeaseService.resolve_binding
                                                       ── the only decrypting function
                                                          in the system ──
                                                                   ▲
                                                        bailment exec <lease> -- cmd
                                                        (values → child process env)
```

`resolve_binding` refuses any caller whose `CallerKind` is not `CLI` or `OPERATOR`. The check
is an explicit caller kind rather than a scope string, because a scope string is something a
future MCP tool handler can satisfy by accident and a caller kind is not.

Three reinforcements sit around it:

- **MCP callers are constructed as `AGENT` regardless of the token presented.** The security
  boundary is the surface, not the privilege. An admin token used over MCP still cannot read
  a secret or approve a request.
- **The REST API proves at import time that none of its handlers can decrypt.**
  `assert_handlers_never_decrypt` inspects the compiled code of every function in
  `bailment.api.routes` for the names that lead to plaintext, and the application fails to
  start if one appears. This is why the binding endpoint selects columns explicitly rather
  than loading whole `Binding` rows: naming the sealed column at all would trip the check.
- **Non-secret outputs are published without decrypting anything.** A golden path may declare
  an output `secret: false`. Those values still go into the sealed envelope — the envelope is
  one unit on purpose — but the worker *also* writes the non-secret subset into the
  `activated` audit event in plain text, so reads never open the envelope. The alternative,
  decrypting on every `get_lease` and filtering, would make the most-called method in the
  service a decryption path, and the first refactor that forgot the filter would be a
  disclosure.

An envelope holds the whole output dict rather than one value each, so a Redis URL and its
HTTP token — useless apart — are sealed, rotated and revoked as one thing. Decryption accepts
the active key plus any retired ones, so rotation is: prepend a new key, let the old leases
drain, delete the old key.

---

## Policy

Pure functions, no I/O, no logging, no clock of its own. The caller supplies the context,
records the audit event and persists the decision. That purity is what lets the dashboard run
a what-if evaluation without touching a database, and what makes the engine exhaustively
testable.

Rules are evaluated top to bottom, first match wins. Every path must end in an unconditional
rule; `GoldenPath` validation enforces it, and the engine denies anyway if the chain ever
ends without a decision.

**A rule that cannot be evaluated denies the entire request.** Policy chains are written as
narrowing denials ending in an allow, so skipping a broken rule turns a deny into an allow,
silently, at the moment the operator has least reason to look.

`evaluate()` and `explain()` share one private walk. An `explain` that re-derived the decision
separately would be a second policy engine, and the day the two disagree is the day the
dashboard starts lying about why an agent was allowed to do something.

Full reference: [policy.md](policy.md).

---

## Providers

The only part of bailment that talks to something outside the process, and therefore the only
part that can lie to the rest of the system about reality. The contract is six methods —
`is_available`, `preflight`, `create`, `destroy`, `exists`, `list_managed` — and three rules,
each of which exists to stop a specific lie. Full detail: [providers.md](providers.md).

Four are shipped: `memory` (in-process, complete, with failure injection), `neon`, `upstash`
and `cloudflare`. A provider that cannot enumerate its own resources by bailment's marker must
declare `supports_reconciliation = False`, so the reconciler skips the orphan sweep for it
rather than concluding the account is clean.

---

## The reconciler

Three phases per provider, and the phasing is the design:

1. **Read** a snapshot of the lease rows.
2. **Call the provider** with no transaction open.
3. **Write**, re-reading each row and re-checking that its state is still the one that was
   observed.

No transaction is ever held across a network call, and no write is made on a stale read. A
worker may have moved a lease while the provider was being asked, and reconciling on stale
reads is how a reconciler starts causing the drift it exists to find.

Four rules govern it:

- **Report, do not delete.** Destroying needs two switches: the global
  `BAILMENT_RECONCILE_AUTO_DESTROY_ORPHANS` and this provider's name in an explicit
  allowlist. A tool that deletes cloud resources because somebody left a default on is a tool
  nobody installs twice.
- **Nothing younger than the grace period is flagged.** Ten minutes. Without it the
  reconciler races in-flight provisioning: a resource created ninety seconds ago by a worker
  that has not yet committed looks exactly like an orphan.
- **`UNKNOWN` changes nothing.** It is counted, so a provider answering `UNKNOWN` for a week
  is visible as a broken integration rather than as a clean account.
- **A provider that could not be swept is not a clean provider.** Every report says which
  providers were skipped and why, on the row, because "nothing found" and "nothing asked"
  must never render the same.

From a terminal, `--destroy` runs a reporting pass first, prints every resource it would
delete by name, asks, and then runs a **second full pass** rather than deleting from the
printed list — so a resource that acquired a live lease in the intervening seconds is no
longer an orphan and is left alone.

---

## Surfaces

| Path | Protocol | Audience | Returns credential values |
|---|---|---|---|
| `/mcp` | MCP, streamable HTTP, stateful | agents | **never** |
| `/api/v1` | REST | the dashboard, humans, scripts | never |
| `/v2` | Open Service Broker API 2.16 | CI systems, developer portals | binding endpoint only, operator token only |
| `/health`, `/health/live` | HTTP | probes | n/a |
| `/metrics` | Prometheus text | scrapers | n/a |

The rule, stated once: **agents use MCP, humans and CI systems use OSB.**

All four are assembled by one `create_app()` which loads the catalog **exactly once** and
hands the same immutable object to all of them. An MCP tool list and an OSB catalog rendered
from two different loads of the same directory can disagree, and the moment they disagree an
agent has a capability a human was never shown.

Order of assembly matters and is deliberate: logging first, so anything that fails afterwards
fails in the format the operator asked for; then the catalog, because a broken golden path
file must stop the process before it binds a port; then the providers, registered whether or
not they are configured, so a missing token reads as "cloudflare: not configured" rather than
turning up later as an unknown-provider error.

`/metrics` carries aggregates only — counts by state, cost, and the reconciler's numbers by
provider. No lease id, no principal, no external name. A metrics endpoint tends to be the
least-guarded thing in a deployment, so it carries the least.

`/health` reports which background components are running but does **not** fail the probe when
one has stopped. Taking the API out of the load balancer because a worker died would remove
the only surface that can answer "what happened to my lease" at the moment somebody most needs
to ask. `/health/live` is separate for the same class of reason: a liveness probe that fails
while the database is unreachable restarts every replica during a failover.

---

## Deployment shapes

**One process** — `bailment serve`. API, worker, ticker and reconciler together. Correct for
a single instance and for the demo, and correct for the `memory` provider specifically,
because its resources live in process memory and a reconciler somewhere else would ask an
empty provider what exists.

**API replicas plus workers** — `bailment serve --no-worker --no-reconcile` behind a load
balancer, and `bailment worker --ticker --reconcile` alongside. Two processes sharing a
`worker_id` will steal each other's in-flight work, so leave the default hostname-pid.

MCP sessions are stateful, so a deployment behind a load balancer must route by
`mcp-session-id`. That is the cost of being able to trace one runaway agent session end to
end and of deriving idempotency keys from it.

The database is SQLite by default and Postgres for anything with more than one worker. Schema
creation via `init_db` runs for SQLite only: `create_all` against a database with a *stale*
schema does nothing while looking exactly like success, and finding that out at the first
provision request is much worse than finding it out at `bailment db upgrade`.

> **Alpha caveat.** There are no Alembic migrations in this repository yet, so
> `bailment db upgrade` against Postgres correctly refuses rather than guessing. The compose
> file works around this with a one-shot `create_all` against a database it just created.
> Do not copy that pattern to a database you care about.

---

## Lineage

The lease-and-broker shape here is a deliberate implementation of the [**Open Service Broker
API**](https://www.openservicebrokerapi.org/), a published Apache-2.0 specification for
provisioning and binding services over a standard HTTP interface. Its Kubernetes
implementation, the `service-catalog` project, was archived in 2022, which is why the spec is
widely assumed to have died with it. It did not: the specification is alive at v2.15 and
above, Cloud Foundry drives it in production, several CI systems and internal developer
portals speak it, and any team that ever wrote a broker has a client for it in a repository
somewhere. Implementing it costs one module and buys every one of those callers.

The mapping is direct, because the golden path already contains everything a service needs:

| OSB | bailment |
|---|---|
| service (with exactly one plan) | golden path |
| `plan.schemas.service_instance.create.parameters` | `GoldenPath.inputs` |
| service instance | one lease |
| service binding | that lease's binding |

Service and plan ids are UUIDv5 values derived from the golden path id under a fixed
namespace, so they are stable across restarts, deployments and catalog reloads. A broker that
hands out fresh ids on restart breaks every platform that stored them.

**Three deliberate non-conformances**, stated plainly because a claim of conformance that is
not quite true is worse than a documented gap:

1. *Bindings are aliases, not resources.* A lease has one binding, sealed once when the
   resource was created. OSB models bindings as independently creatable, so a second `PUT`
   with a different binding id would be expected to mint a second credential. bailment cannot:
   the provider issued one credential, and inventing a second would mean holding one the
   reconciler cannot account for. Every OSB binding id on an instance resolves to the same
   underlying binding.
2. *Unbind does not revoke.* A bailment credential's lifetime is its lease's — that is the
   promise the project is built on — so `DELETE` on a binding is acknowledged and recorded,
   and the credential keeps working until the lease ends. To end a credential, delete the
   instance. Telling a platform "unbound" while leaving the credential live would be worse.
3. *Provisioning is always asynchronous.* Every request needs `accepts_incomplete=true`.
   bailment never calls a provider on the request thread — the write-ahead name has to be
   committed first, and a human may have to approve the request at all — so there is no code
   path that could answer `201 Created` truthfully.

**The credential divergence.** Everywhere else in bailment a secret value is unreachable. OSB
does not permit that: a service binding response carries a `credentials` object by definition,
and a broker returning a reference instead is a broker no platform can use. So `PUT
/v2/service_instances/{id}/service_bindings/{id}` returns values, and requires an **operator**
token to do it. An ordinary token gets 403. There is no configuration that lets an agent
through, because MCP callers are constructed as `AGENT` and `resolve_binding` refuses that
kind regardless of which HTTP path reached it.

---

## Further reading

The code is written to be read in this order:

1. [`states.py`](../src/bailment/states.py) — the lifecycle and why `RELEASED` is special.
2. [`models.py`](../src/bailment/models.py) — write-ahead naming and worker claims.
3. [`catalog/schema.py`](../src/bailment/catalog/schema.py) — the one definition everything
   else is derived from.
4. [`policy/evaluator.py`](../src/bailment/policy/evaluator.py) — why there is no `eval`.
5. [`engine/service.py`](../src/bailment/engine/service.py) — the four invariants that only
   hold if every caller goes through one door.
6. [`engine/reconciler.py`](../src/bailment/engine/reconciler.py) — the part that is hard.
