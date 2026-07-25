# bailment

**A provisioning broker that hands AI coding agents capabilities instead of credentials, on
time-boxed leases that destroy themselves.**

[![CI](https://github.com/AleBrito124356/bailment/actions/workflows/ci.yml/badge.svg)](https://github.com/AleBrito124356/bailment/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)

---

## The problem

Right now, in a lot of companies, this is how an AI coding agent gets a database:

```
export DATABASE_URL=postgresql://admin:...@prod-db.internal:5432/main
claude --dangerously-skip-permissions
```

A long-lived credential, a terminal, and hope. The credential does not expire. It is in the
model's context window, which means it is in a log somewhere, and a prompt injection in a
scraped web page or a poisoned dependency README is now a credential exfiltration primitive.
Whatever the agent creates with it, nobody is tracking; a branch database from a Tuesday
afternoon is still billing in March.

Platform teams have exactly one answer to this today, and the answer is "don't". That answer
does not survive contact with a team that has already shipped three features with an agent.

bailment is the other answer. An agent asks for a *capability* — "a Postgres database with
staging data, for two hours" — through a typed MCP tool. A policy engine decides. If it
allows, a worker provisions a real resource, seals the credential in an encrypted envelope,
and hands the agent a reference: `bailment://binding/3f2a...`. The agent never sees the
credential. When it needs to run something against the database it runs
`bailment exec <lease> -- pytest`, and the value is decrypted into that subprocess's
environment and nowhere else. Two hours later the lease expires and the database is deleted.
Then a reconciler goes looking for anything that survived the process anyway.

The name is the legal term. A bailment is when you hand someone your property for a limited
purpose and a limited time, and they are obliged to give it back.

---

## Quickstart, no cloud account

Everything below runs against the built-in `memory` provider. No API keys, no credit card,
no signup. It takes about a minute plus five minutes of watching a clock.

```bash
git clone https://github.com/AleBrito124356/bailment
cd bailment
docker compose up
```

Or without Docker, using [uv](https://docs.astral.sh/uv/):

```bash
uv sync
export BAILMENT_ENCRYPTION_KEY=$(uv run bailment keygen)
export BAILMENT_API_TOKENS=agent:demo-agent-token
export BAILMENT_ADMIN_TOKENS=alice:demo-operator-token
uv run bailment db upgrade
uv run bailment serve
```

Either way you now have a broker on `http://localhost:8080`. In a second terminal, ask for a
sandbox resource:

```bash
curl -s -X POST http://localhost:8080/api/v1/leases \
  -H 'Authorization: Bearer demo-agent-token' \
  -H 'Content-Type: application/json' \
  -d '{"golden_path":"sandbox","inputs":{"name":"hello"},"ttl":"5m"}'
```

```json
{
  "lease": {
    "id": "b63df949-480b-49f6-99f4-6e09753f5d0e",
    "golden_path": "sandbox",
    "provider": "memory",
    "state": "pending",
    "requester": "agent",
    "ttl_seconds": 300,
    "max_ttl_seconds": 3600,
    "policy_effect": "allow",
    "policy_reason": "The sandbox provider creates nothing outside this process, so there is nothing to gate. Take one.",
    "external_name": "bailment-sandbox-b63df949",
    "binding_reference": null,
    "usable": false
  },
  "replayed": false,
  "ttl_clamped": false,
  "notices": []
}
```

Note `external_name` on a lease that is still `pending`. The name the resource is going to
carry is committed to the database *before* any provider is called. That one detail is what
makes orphan detection possible at all; see [Write-ahead naming](#write-ahead-naming).

A few seconds later a worker has provisioned it:

```console
$ bailment lease list
┌──────────┬────────┬─────────────┬──────────┬───────────┬────────────┬─────────┬─────┐
│ id       │ state  │ golden path │ provider │ requester │ expires in │ created │ $/h │
├──────────┼────────┼─────────────┼──────────┼───────────┼────────────┼─────────┼─────┤
│ b63df949 │ active │ sandbox     │ memory   │ agent     │      4m36s │ 24s ago │   - │
└──────────┴────────┴─────────────┴──────────┴───────────┴────────────┴─────────┴─────┘
```

```console
$ bailment lease get b63df949
┌─ lease ─────────────────────────────────────────────────────────────────────────┐
│ id             b63df949-480b-49f6-99f4-6e09753f5d0e                             │
│ state          active                                                           │
│ golden path    sandbox @ memory                                                 │
│ requester      agent                                                            │
│ inputs         {"name": "hello", "simulate": "ok"}                              │
│ created        2026-07-25T18:52:49+00:00  (25s ago)                             │
│ activated      2026-07-25T18:52:49+00:00  (25s ago)                             │
│ expires        2026-07-25T18:57:49+00:00  4m34s                                 │
│ ttl            5m00s granted, 1h00m ceiling                                     │
│ renewals       0/2                                                              │
│ external name  bailment-sandbox-b63df949                                        │
└─────────────────────────────────────────────────────────────────────────────────┘
┌─ binding ───────────────────────────────────────────────────────────────────────┐
│ reference  bailment://binding/c0ef5a88-5e23-401a-85bd-6e5fe1ed5bcb              │
│ sealed     SANDBOX_URL                                                          │
│ published  SANDBOX_ID = bailment-sandbox-b63df949                               │
│ use        bailment exec b63df949 -- <command>                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

Both come from the same lease. `SANDBOX_ID` is declared `secret: false` in the golden path,
so it is printed. `SANDBOX_URL` is not, so what you get is its name. There is no flag that
turns the second line into the first — to use a sealed value, hand bailment the command that
needs it:

```console
$ bailment exec b63df949 -- python -c "import os; print(os.environ['SANDBOX_URL'][:24] + '...')"
memory://bailment-sandbo...
```

That is the only code path in the entire system that turns a reference back into a value.
It runs on the operator's machine, writes the plaintext into one child process's
environment, and records the access in the audit log before the child starts. If you want to
see what would be injected without decrypting anything, `bailment exec --dry-run` prints the
variable names and stops.

Now wait. One minute before the deadline the lease enters `EXPIRING` and a notice is emitted.
At zero it expires, a worker calls the provider's destroy path, and the resource is gone:

```
[info] lease notice  kind=expiring lease_id=88efb73b… seconds_remaining=29
       message='lease 88efb73b… (sandbox) expires in 29s. Renew it if you still need it;
                when it expires the resource is destroyed.'
[info] lease tick    expired=['88efb73b…'] queued_for_teardown=['88efb73b…']
[info] lease released  external_name=bailment-sandbox-88efb73b lease_id=88efb73b…
```

```console
$ bailment lease get 88efb73b
│ state          released                                                         │
│ expires        2026-07-25T18:54:49+00:00  -2m13s                                │
│ released       2026-07-25T18:54:55+00:00                                        │
```

Six seconds between the deadline and the provider confirming the resource was destroyed.
That gap is the resolution of `BAILMENT_LEASE_TICK_SECONDS`, and it is the honest measure of
what "the lease expires" is worth in a given deployment.

Finally, ask whether reality agrees with the database:

```console
$ bailment reconcile
┌────────────┬──────────────────────────────┬───────────┬────────┬─────────┬───────┬────────┐
│ provider   │ checked                      │ resources │ leases │ orphans │ drift │ errors │
├────────────┼──────────────────────────────┼───────────┼────────┼─────────┼───────┼────────┤
│ cloudflare │ provider is not configured,  │         0 │      0 │       0 │     0 │      0 │
│            │ so it cannot be asked what   │           │        │         │       │        │
│            │ exists                       │           │        │         │       │        │
│ memory     │ yes                          │         0 │      0 │       0 │     0 │      0 │
│ neon       │ provider is not configured…  │         0 │      0 │       0 │     0 │      0 │
│ upstash    │ provider is not configured…  │         0 │      0 │       0 │     0 │      0 │
└────────────┴──────────────────────────────┴───────────┴────────┴─────────┴───────┴────────┘
warning: not every provider could be swept, so this run says nothing about cloudflare, neon, upstash
```

Read the `checked` column before the `orphans` column. A provider that could not be asked
reports zero orphans and zero is not an answer. Most drift dashboards do not make that
distinction, and a clean-looking board that is clean because nothing ran is the failure mode
this table is shaped to prevent.

Try a refusal too, so you can see what an agent actually gets told:

```bash
curl -s -X POST http://localhost:8080/api/v1/leases \
  -H 'Authorization: Bearer demo-agent-token' -H 'Content-Type: application/json' \
  -d '{"golden_path":"dns-record","inputs":{"subdomain":"admin","target":"1.2.3.4","record_type":"A"}}'
```

```
"state": "rejected",
"policy_effect": "deny",
"policy_reason": "That subdomain is reserved. Names like www, api and admin are where this
 organisation's real services live … Pick a name that says what your change is instead, such
 as pr-1234, alice-checkout-fix or demo-2026-07: anything that is not on the reserved list is
 self-service and takes about a second."
```

The refusal names the rule, explains the reason and says what to ask for instead. That is not
politeness. A model told "denied" tries the same request with different arguments, then a
different path, then a shell command that does the same thing. A refusal that closes the
question is a refusal that does not get routed around.

---

## How it works

```
  agent (MCP)  ──┐
  human (web)  ──┼──▶  golden path  ──▶  policy  ──▶  lease row      ──▶  worker  ──▶  provider
  CI (OSB /v2) ──┘     (one YAML)        (no eval)    (named first)       (claims)     (Neon,
                                             │              │                           Upstash,
                                             │              │                           Cloudflare,
                                       require_approval     │                           memory)
                                             │              ▼
                                             ▼         binding sealed (Fernet)
                                        human decides       │
                                                            ▼
                                          agent receives  bailment://binding/<uuid>
                                                            │
                                          bailment exec ────┘──▶ subprocess env, nowhere else

                          ticker: ACTIVE → EXPIRING → EXPIRED → DEPROVISIONING → RELEASED
                     reconciler: provider reality ⇄ lease table, in both directions
```

1. **Request.** An agent calls `bailment_provision_postgres` over MCP, or a human posts to
   `/api/v1/leases`, or a CI system does `PUT /v2/service_instances/{id}`. All three land in
   the same service layer.
2. **Policy.** The golden path's rule chain is evaluated top to bottom, first match wins,
   against a fixed context of plain data. The answer is `allow`, `deny` or
   `require_approval`. A rule that *cannot* be evaluated denies. See [docs/policy.md](docs/policy.md).
3. **Lease.** The row is written with a deterministic `external_name` and committed before a
   worker can see it. TTL is clamped to the path's ceiling, and the response says so, so the
   caller learns its bounds instead of guessing.
4. **Provision.** A worker takes the row with a conditional claim, calls the provider, seals
   the returned credential with Fernet, and moves the lease to `ACTIVE`. The clock starts
   here, not at request time — a lease that waited three hours for an approver has not been
   holding a database for three hours.
5. **Teardown.** The ticker moves leases through `EXPIRING` → `EXPIRED`; a worker calls the
   provider's destroy path and only writes `RELEASED` when the provider confirms.
6. **Reconcile.** Periodically, and on demand, compare what the provider says exists against
   what the lease table believes.

### Write-ahead naming

`Lease.external_name` is computed and committed before the provider is called, never
assigned from the provider's response. If a worker dies between the create call and the
response, the row already knows the name the resource was going to carry, so the reconciler
can go and look for it. A system that names resources from the provider's response cannot do
this: a crash in that window leaves a real, billing resource that nothing on earth can
associate back to a request.

### The reconciler

Two directions, and they fail in completely different ways.

**Orphans.** A resource exists at the provider carrying bailment's marker, and no live lease
accounts for it. It is running, it is billing, and nothing in the system was ever going to
end it. These come from half-completed provisions, from teardowns that exhausted their retry
budget, and from workers killed at the wrong moment. Every provisioning system produces them.

**Vanished resources.** A lease we believe is `ACTIVE` whose resource is `GONE` — deleted by
a person in a console, usually. The lease is now a promise about something that does not
exist, and it keeps a binding resolvable and a quota slot occupied until somebody notices.

Reporting is always safe. Destroying is off by default and needs *two* switches: the global
`BAILMENT_RECONCILE_AUTO_DESTROY_ORPHANS` and an explicit per-provider allowlist, which from
a terminal means `--destroy` plus a confirmation that lists every resource by name first.
Nothing younger than a ten-minute grace period is ever flagged, because a resource created
ninety seconds ago by a worker that has not yet committed looks exactly like an orphan.

And `UNKNOWN` changes nothing — not the state, not a timestamp, not an audit row. A provider
that timed out has told us nothing, and the single most dangerous thing this system could do
is turn silence into `RELEASED`.

---

## Why not just X

Every tool below does something better than bailment does. Here is what.

### HashiCorp Vault

Vault is a mature, audited, battle-tested secrets platform with a dozen auth backends, a real
HSM story, transit encryption, an enormous ecosystem and a decade of production hardening.
Its dynamic secrets engines already issue short-lived database credentials with a TTL and
revoke them on expiry, and if what you need is *credential* lifecycle, Vault is better at it
than bailment will ever be. Use Vault.

The gap is that Vault leases a credential *to* a resource that already exists and that
something else created. It does not create the Neon branch, it does not delete it, and it
cannot tell you that a branch exists which nothing is accounting for. It also hands the
credential value to the caller, which for an agent means the value lands in the context
window. bailment creates and destroys the resource, and never returns the value.

The two compose well: Vault as the secrets backend under bailment's binding storage is a
sensible thing to want, and is not implemented today.

### Crossplane

Crossplane's reconciliation model is more mature than bailment's, its provider ecosystem is
vastly larger, and its composition system expresses infrastructure relationships that a flat
golden path simply cannot. If you already run Kubernetes and want declarative infrastructure
as custom resources, Crossplane is the right answer and this is not a close call.

The gap is the substrate and the interface. Crossplane needs a Kubernetes cluster to be the
control plane, which is a large thing to adopt for "let the agent have a database for two
hours". Its interface is `kubectl apply` and a YAML manifest, not a typed tool an agent can
call and a policy that can say "not from an agent, not against prod". And a Crossplane
Composite Resource has no TTL: it lives until somebody deletes it.

### Backstage

Backstage is a genuine software catalog with an ecosystem of plugins, a service ownership
model, TechDocs, and scaffolder templates that are more capable than bailment's golden paths.
If the problem is "developers cannot find anything and nobody knows who owns what", Backstage
solves a much bigger problem than this does.

The gap is that Backstage is a portal — a place a human clicks — and it is famously heavy to
run. Its scaffolder fires a template and is done; there is no lease, no expiry, no teardown,
and no reconciliation of what the templates created. And the interface is a web page, which
an agent cannot call. bailment is deliberately a broker with no UI ambitions beyond an
operator dashboard.

### Terraform (or OpenTofu) in CI

Terraform is the best tool in the world for describing infrastructure that should exist, its
provider coverage is not remotely comparable, and its state model, plan output and drift
detection against declared state are genuinely excellent. For anything permanent, use
Terraform.

The gap is that a Terraform run is a step in a pipeline. There is no TTL: the resource lives
until another run destroys it, and the `destroy` job that was supposed to run got skipped
because the pipeline was cancelled. There is no agent-facing surface — an agent that can run
`terraform apply` has your entire cloud, which is the problem restated rather than solved.
And `terraform plan` detects drift against *declared* state, which by construction cannot see
a resource that was created outside the state file. Orphans from a crashed apply are exactly
what it cannot show you.

### So what is left

| | credential brokering | creates the resource | TTL + auto-teardown | finds untracked resources | agent-callable surface |
|---|---|---|---|---|---|
| Vault | **yes, excellent** | no | credential only | no | no |
| Crossplane | via providers | **yes** | no | drift vs declared | no |
| Backstage | no | via templates | no | no | no |
| Terraform in CI | no | **yes** | no | drift vs state file | no |
| bailment | yes, by reference | yes | yes | **both directions** | **yes (MCP)** |

The last two columns are why this exists. Nothing else does lifecycle reconciliation of
brokered resources, and nothing else was designed on the assumption that the caller is a
language model that may have been prompt-injected five minutes ago.

---

## Configuration

Every setting is an environment variable prefixed `BAILMENT_`, or a line in a `.env` file in
the working directory. See [`.env.example`](.env.example) for the annotated full list.

The three that matter on day one:

```bash
BAILMENT_ENCRYPTION_KEY=          # from `bailment keygen`. Required to seal or open a binding.
BAILMENT_API_TOKENS=agent:...     # principal:token pairs that may request and read leases
BAILMENT_ADMIN_TOKENS=alice:...   # principal:token pairs that may additionally approve and revoke
```

bailment will not generate an encryption key for you at runtime. A key that appears by itself
is a key that changes on restart, and every binding written before that restart becomes
permanently unreadable — silently, and you find out during the incident rather than before
it. Rotate by moving the old value into `BAILMENT_PREVIOUS_ENCRYPTION_KEYS`, which is
decrypt-only, and letting the old leases drain.

Unrecognised `BAILMENT_*` variables are ignored and then reported at warning level on
startup. Rejecting them outright turns a stray variable in a compose file into a crash loop;
ignoring them in silence turns `BAILMENT_NEON_KEY` — note the missing `_API` — into an
unexplained "provider unavailable" that costs somebody an afternoon.

Storage defaults to SQLite so that cloning the repo and running the sandbox path needs no
database at all. Point `BAILMENT_DATABASE_URL` at `postgresql+asyncpg://...` for anything
with more than one worker.

---

## MCP client setup

bailment speaks streamable-HTTP MCP at `/mcp`. For Claude Code:

```json
{
  "mcpServers": {
    "bailment": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer demo-agent-token",
        "X-Bailment-On-Behalf-Of": "alice@example.com"
      }
    }
  }
}
```

Put that in `.mcp.json` in your project root, or add it with
`claude mcp add --transport http bailment http://localhost:8080/mcp`.

The tools the agent then sees are generated from the catalog, one provision tool per golden
path plus five lifecycle tools:

```
bailment_provision_sandbox      bailment_lease_status
bailment_provision_postgres     bailment_list_leases
bailment_provision_redis        bailment_renew_lease
bailment_provision_dns_record   bailment_release_lease
                                bailment_catalog
```

No MCP tool returns a credential value under any configuration. The caller is constructed as
an `AGENT` regardless of which token was presented, and `AGENT` is the kind that
`resolve_binding` and `approve` refuse outright — so an admin token presented over MCP still
cannot read a secret or approve its own request. The security boundary is the surface, not
the privilege.

`X-Bailment-On-Behalf-Of` is a claim, not an identity. It is recorded so the audit log can
answer "which agent did this" and "who is accountable for it" as separate questions. It is
not authenticated and grants nothing; never write a policy rule that gives privileges based
on it.

More in [docs/mcp.md](docs/mcp.md), including what the tool results say to the model and why.

---

## Writing a golden path

A golden path is one YAML file. From it, bailment derives the MCP tool, the dashboard form,
the OSB catalog entry, the policy chain, the lease ceilings and the binding shape — with no
second definition anywhere. That is not tidiness: if the agent-facing catalog and the
human-facing catalog can drift, they will, and the first time they do an agent gets a
capability a human was never shown.

```yaml
id: postgres
name: Postgres database (Neon branch)
provider: neon
tags: [database, postgres, neon]

description: |-
  A dedicated Postgres database, created as a branch of the team's Neon project…
  Use it to rehearse a migration or reproduce a bug. Do not point a deployed service at
  it: the connection string stops working the moment the lease expires.

inputs:                       # JSON Schema, used verbatim as the MCP tool's input schema
  type: object
  additionalProperties: false
  required: [env, name]
  properties:
    env: { type: string, enum: [dev, staging, prod] }
    name: { type: string, pattern: "^[a-z][a-z0-9-]{1,38}[a-z0-9]$" }

lease:
  default_ttl: "4h"
  max_ttl: "72h"
  warn_before: "30m"
  renewable: true
  max_renewals: 3

policy:
  - when: input.env == "prod"
    effect: require_approval
    reason: >-
      Branching production copies real customer data into a database that will be handed
      to an agent, so a human on the platform team has to agree to it…

  - effect: allow
    reason: Dev and staging branches within the standard spend are self-service.

cost:
  estimated_hourly_usd: 0.14
  estimated_monthly_usd: 102.20

outputs:
  - name: DATABASE_URL
    secret: true              # sealed, never returned by value
```

Write the `description` as an instruction to a capable stranger: what it gives you, what it
costs, and when not to use it. The model reads it as the tool description and acts on it.

Drop the file in `BAILMENT_CATALOG_DIR` and check it before shipping:

```bash
bailment catalog validate
bailment catalog list
```

Validation names the file and the field for every failure, and warns about the things that
are legal but almost certainly wrong — a path whose provider is registered but unconfigured,
two ids that collapse to the same MCP tool name, a path that declares no outputs. `--strict`
turns those warnings into failures, which is right in a deployment pipeline where the
providers really are configured and wrong in CI, where none of them are.

---

## Writing policy

Rules are evaluated top to bottom and the first match wins. The last rule must be
unconditional, so every request gets a decision.

```yaml
policy:
  - when: input.env == "prod" and input.eviction == true
    effect: deny
    reason: >-
      Eviction on a production Redis turns a full database into silent data loss…

  - when: is_agent and env == "prod"
    effect: require_approval
    reason: A human decides before an autonomous process touches production.
    approvers: [platform-team]

  - when: ttl_seconds > 129600 or estimated_hourly_cost_usd > 0.20
    effect: require_approval
    reason: This is longer than the spend pre-approved for a single branch…

  - effect: allow
    reason: Dev and staging within the standard spend are self-service.
```

**There is no `eval`, no `exec` and no third-party expression engine anywhere in the policy
path.** Expressions are parsed to an AST and interpreted by a tree-walker that can only reach
values the caller explicitly placed in the context. `getattr` is never called: `input.env` is
sugar for `input["env"]`, and attribute access on anything that is not a mapping is an error
rather than a fallback. That single rule is what makes the sandbox hold — with `getattr`,
every value becomes a doorway to its type, its module and eventually the interpreter.

Arithmetic is rejected outright. `2 ** 999999999` is a hang written in four characters, and a
rule can compare against a value the broker precomputed instead.

**A rule that cannot be evaluated denies the whole request.** Policy chains are written as
narrowing denials ending in an allow, so skipping a broken rule turns a deny into an allow,
silently, at the moment the operator has least reason to look. Loud and wrong beats quiet and
permissive.

The context a `when` may reference: `input`, `env`, `requester`, `on_behalf_of`, `is_agent`,
`golden_path`, `ttl_seconds`, `estimated_monthly_cost_usd`, `estimated_hourly_cost_usd`,
`active_leases_for_requester`, `hour_utc`, `weekday`. Full reference and the complete grammar
in [docs/policy.md](docs/policy.md).

---

## The security model

The design assumes the agent may turn hostile. Not because agents are malicious, but because
an agent that reads a web page, a dependency README or a GitHub issue is an agent that can be
prompt-injected, and after that it is an attacker with your credentials and your permissions.
So the interesting question is never "is this agent trustworthy" — it is "what can this
request do if the agent is currently under someone else's control".

- **A secret value never crosses the MCP boundary.** Not in a tool result, not in an error
  message, not behind a flag. `resolve_binding` is the only function that decrypts, it
  refuses `AGENT` and `SYSTEM` callers on caller *kind* rather than on a scope string, and
  the REST API proves at import time that no handler in it can reach a decryption path.
- **Values are Fernet-encrypted at rest** (AES-128-CBC + HMAC-SHA256), sealed as one envelope
  per binding so a URL and its token are revoked and rotated together.
- **Approval gates cannot be opened by the thing they gate.** An `AGENT` caller is refused by
  `approve`, and anonymous access maps to the *lower* tier, never the higher one.
- **Blast radius is bounded by time.** Even a fully compromised agent holds a capability that
  expires, against a resource that will be destroyed, on a path a human chose to offer.
- **Every decision is auditable**, with "which agent" and "who is accountable" recorded as
  separate fields, because an audit log that conflates them is useless in exactly the incident
  where you need it.

What bailment does **not** protect against: an agent misusing a capability it was legitimately
granted within the lease window; anyone with shell access to the broker host, who holds the
database and the encryption key; or a hostile platform engineer, since policy files are
trusted input. Full threat model in [SECURITY.md](SECURITY.md).

---

## Project status

**Alpha.** Version 0.1.0. Honestly:

- The engine, the policy evaluator, the state machine, the reconciler, the MCP server, the
  OSB surface and the CLI are complete and covered by **827 tests** that run in under twenty
  seconds with no cloud account and no network. `mypy --strict` is clean across the package.
- The `memory` provider is fully implemented, including the failure injection needed to
  produce both directions of drift on purpose. The Neon, Upstash and Cloudflare providers are
  implemented against their real APIs but have had far less real-world exposure than the
  parts the test suite can reach.
- **There are no Alembic migrations yet.** `bailment db upgrade` creates the schema directly
  on SQLite and refuses to guess on Postgres, which is the correct refusal but means a
  Postgres deployment currently needs `Base.metadata.create_all` run once by hand. This is
  the first thing to fix before anyone runs it seriously.
- The sandbox path documents a `simulate` input for producing failures on demand; the memory
  provider does not honour it yet, so the ORPHANED demo currently lives in
  `tests/test_reconciler.py` rather than on the command line.
- Nothing here has run in production anywhere. There has been no external security review.
  Do not put this in front of a cloud account you care about yet.

If you are evaluating it: run the quickstart, read
[`src/bailment/states.py`](src/bailment/states.py) and
[`src/bailment/policy/evaluator.py`](src/bailment/policy/evaluator.py), and judge it on those.

---

## Documentation

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The components, the request path, and the decisions that shaped them |
| [docs/policy.md](docs/policy.md) | The expression language, the context, and how to write rules that hold |
| [docs/providers.md](docs/providers.md) | The provider contract, the four built-ins, and how to write a fifth |
| [docs/mcp.md](docs/mcp.md) | The agent-facing surface: tools, results, sessions and refusals |
| [SECURITY.md](SECURITY.md) | Threat model, what is and is not protected, how to report a vulnerability |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to run the suite and what a good change looks like here |

---

## Contributing

Contributions are welcome, particularly new providers and real-world reports from anyone who
points this at a live cloud account.

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

The test suite needs no credentials and touches no network. Read
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request — the house style here is
unusual on purpose, and the sections on module docstrings and on what must never appear in a
response body are the ones worth reading twice.

---

## Lineage

The lease-and-broker shape here is a deliberate implementation of the
[**Open Service Broker API**](https://www.openservicebrokerapi.org/), a published Apache-2.0
specification for provisioning and binding services over a standard HTTP interface. Its
Kubernetes implementation, the `service-catalog` project, was archived in 2022, which is why
people often assume the spec died with it. It did not: the specification is alive at v2.15
and above, and any team that ever wrote a broker has a client for it in a repository
somewhere.

bailment implements OSB v2.16 at `/v2` (see [docs/architecture.md](docs/architecture.md) for
the three places it deliberately diverges), and adds the two things the spec never covered:
an agent-facing MCP surface, and lifecycle reconciliation of the resources a broker created.

---

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Alejandro Brito Olivera.
