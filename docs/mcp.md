# The MCP surface

This is the surface an AI agent sees. Everything in it is designed on one assumption: the
caller is a language model, it acts on tool results rather than reading them, and it may have
been prompt-injected five minutes ago.

---

## Connecting

Streamable HTTP, mounted on the same application as everything else, at `/mcp`.

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

Put that in `.mcp.json` in your project root, or:

```bash
claude mcp add --transport http bailment http://localhost:8080/mcp \
  --header "Authorization: Bearer demo-agent-token"
```

### Headers

| header | meaning |
|---|---|
| `Authorization: Bearer <token>` | Required unless `BAILMENT_ALLOW_ANONYMOUS` is on. Any configured token works — including an admin one, which grants **nothing extra** here. |
| `X-Bailment-On-Behalf-Of` | Which human the agent is working for. A claim, not an identity. |
| `X-Bailment-Agent-Session` | Fallback session id for a client that does not carry an MCP session id. |

`X-Bailment-On-Behalf-Of` is supplied by the agent and is not authenticated. It grants nothing:
the requester is still the token's principal, and visibility is still decided from that. It
exists so the audit log can answer "which agent did this" and "who is accountable for it" as
separate questions, because a log that conflates them is useless in exactly the incident where
you need it. **Policy rules must never grant privileges on `on_behalf_of`.**

### Sessions

The transport is **stateful**, deliberately, for two reasons:

- The MCP session id is what `Lease.agent_session` records, so one runaway agent session is
  traceable end to end.
- It makes a repeated tool call idempotent. An identical provision call within one session
  carries the same derived idempotency key and returns the same lease instead of a second
  resource.

The cost: a deployment behind a load balancer must route by `mcp-session-id`.

---

## The tools

One provision tool per golden path, generated from the catalog at startup, plus five
lifecycle tools. There is no decorator list and no handler registry to keep in step with the
YAML: `GoldenPath.input_schema_with_lease()` is used verbatim as the tool's input schema, and
the name and description come off the same object the dashboard form and the OSB catalog entry
are rendered from. **Adding a capability is adding a file.**

```
bailment_provision_sandbox      bailment_lease_status
bailment_provision_postgres     bailment_list_leases
bailment_provision_redis        bailment_renew_lease
bailment_provision_dns_record   bailment_release_lease
                                bailment_catalog
```

Tool names are namespaced `bailment_provision_<id>` with hyphens turned into underscores, so
they cannot collide with another MCP server's tools. `bailment catalog validate` fails if two
golden path ids collapse to the same tool name — an agent-facing collision would otherwise
only ever be discovered by an agent.

Every provision tool takes the golden path's own inputs plus a universal optional `ttl`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["name"],
  "properties": {
    "name": { "type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,39}$" },
    "simulate": { "type": "string", "enum": ["ok", "slow_create", "create_failure", "destroy_failure"] },
    "ttl": {
      "type": "string",
      "description": "How long to hold the lease, e.g. '2h' or '45m'. Defaults to 5m; anything above 1h is clamped to it."
    }
  }
}
```

The schema is closed. Agents are enthusiastic, and an open schema lets one smuggle
unvalidated keys straight through to a provider, so `additionalProperties: false` is forced
on before the schema is ever used.

---

## Server instructions

Shown to the model once, when it connects. Deliberately short: it is competing for attention
with the system prompt and everything else in the context window, so it says only the three
things that change how the tools get used.

```
bailment provisions real infrastructure on a time-boxed lease and hands you a capability
rather than a credential.

Three things worth knowing before you call anything:
1. Provisioning is asynchronous. A provision tool returns a lease id and a state; poll
   bailment_lease_status until it is active. Calling the provision tool a second time
   creates a second resource.
2. You will never receive a credential value. An active lease gives you a
   bailment://binding/... reference; run the command that needs the credential as
   'bailment exec <lease id> -- <command>' and bailment injects it into that process.
   There is no tool, argument or header that returns the value itself.
3. Leases expire and the resource is then destroyed. Every result tells you how many
   seconds are left. Renew with bailment_renew_lease before the deadline, or release
   early with bailment_release_lease when you are finished -- releasing what you no
   longer need is the single most useful thing you can do here.
```

---

## Why the results read the way they do

Tool results are the only channel through which this system talks to a model, and a model does
not read a result — it acts on one. Four failure modes are known, and each one is answered
deliberately.

### Agents route around vague refusals

A model told "denied" will try the same request with different arguments, then a different
golden path, then a shell command that does the same thing. So a denial states the policy
reason verbatim and then says, in words, that retrying will not help and that another route
does not exist.

Real output:

```
DENIED. This request was refused and retrying will not help.

Reason, verbatim:
  That subdomain is reserved. Names like www, api and admin are where this organisation's
  real services live or are expected to live … Pick a name that says what your change is
  instead, such as pr-1234, alice-checkout-fix or demo-2026-07: anything that is not on the
  reserved list is self-service and takes about a second.

lease_id: 6d13e1d3-df38-4687-b2b3-bbe5108a6b4f
state: rejected
golden_path: dns-record

This is a decision, not a transient failure. The same request will be refused again, and the
same rule applies to the other golden paths that would give you the same capability, so
looking for another route is wasted effort. If you think the reason does not apply to what
you are doing, say so to the person you are working for and let them take it up with the
platform team.
```

Note `isError: false`. **A policy denial is not an error** — the tool worked perfectly and the
answer is no. Marking it as an error invites exactly the retry loop the wording exists to
prevent. `isError: true` is reserved for "bailment could not carry out the call": the lease
does not exist, the arguments do not fit the schema, the state is wrong.

```
isError: true
Input validation error: 'NOT VALID' does not match '^[a-z0-9][a-z0-9-]{0,39}$'
```

An approval gate gets the same treatment: it says a **human** must decide, gives the reason,
and tells the agent to stop and hand off rather than poll aggressively or find another way in.

### Agents forget that a call is asynchronous

```
QUEUED. The request was accepted and a worker is provisioning it. Nothing exists yet.

lease_id: 347601b9-d03a-4c09-9fab-52700139524d
state: pending
golden_path: sandbox
The lease clock has not started. It starts when the resource exists and then runs for 3600 seconds.

What to do now: call bailment_lease_status with lease_id "347601b9-…" until the state is
"active", which usually takes a few seconds and occasionally a minute or two. Do not call the
provision tool again while you wait: a second call creates a second resource that also costs
money and also has to be cleaned up. If the state becomes "failed", the reason will be in the
result.

Also worth knowing:
- requested TTL of 99h was clamped to the maximum this golden path allows, 1h. Renew before
  it expires if you need longer; this path allows 2 renewal(s).
```

The clamp notice is the mechanism by which an agent learns its bounds. A rejected request
teaches it nothing and it retries with another guess; a clamped one comes back with the
ceiling named, and the guessing stops.

### Agents reason badly about time they cannot see

Every result carries the seconds remaining, so a model can decide for itself whether to renew
before starting something long. A lease that has not started its clock says so explicitly
rather than omitting the field, because an absent number reads as zero.

### Agents ask for the value

```
ACTIVE. The resource exists and the lease is running.

lease_id: 347601b9-d03a-4c09-9fab-52700139524d
state: active
golden_path: sandbox
Seconds remaining: 3593, until 2026-07-25T20:13:19.448552+00:00.

Secret reference: bailment://binding/e5d546e2-74da-4aa3-97ac-c4b0897d897d
Sealed values, by name (the values themselves are encrypted and are never returned to you):
SANDBOX_URL
Non-secret outputs, in plain text:
  SANDBOX_ID = bailment-sandbox-347601b9

To use the credential, run the command that needs it through bailment:
  bailment exec 347601b9-d03a-4c09-9fab-52700139524d -- <your command>
bailment decrypts the value and puts it in that process's environment. It is not printed, not
passed on a command line and not returned by any tool. Do not look for another way to read it;
there is not one, and asking is refused.

When the lease ends the resource is destroyed and the credential stops working, mid-command if
necessary. Renew with bailment_renew_lease before then if you need longer (1 of 2 renewals
used), and call bailment_release_lease as soon as you are finished rather than leaving it to
expire.
```

Explaining the `bailment exec` mechanism once, at the moment the model needs it, is far more
effective than a policy document it never sees.

---

## No tool returns a credential

The caller here is **always** constructed with `CallerKind.AGENT`, including when the token
presented is an admin token, because the security boundary is the surface and not the
privilege.

`AGENT` is refused by `LeaseService.resolve_binding` and by `LeaseService.approve`. So there
is no combination of token, argument or header that makes an MCP tool hand back a secret value
or let an agent approve its own request. An approval gate the gated thing can open is not a
gate.

What an agent gets is `bailment://binding/<uuid>` and the exact command that injects the value
into a subprocess it never reads.

If you want a credential *value* out of bailment over HTTP, that is the Open Service Broker
surface at `/v2`, it requires an operator token, and it is documented in
[architecture.md](architecture.md#lineage). The rule, stated once: **agents use MCP, humans and
CI systems use OSB.**

---

## The other tools

### `bailment_catalog`

What this installation is willing to hand out, rendered for a model: the lease durations, the
cost, whether approval is normally needed, which outputs come back in plain text, and whether
the provider behind each path can actually be called right now.

```
## postgres -- Postgres database (Neon branch)
tool: bailment_provision_postgres
lease: 4h by default, 72h at most, renewable 3 time(s)
cost: about $0.1400 per hour
outputs: DATABASE_URL
approval: some requests on this path need a human to approve them; the tool result will say
so and why.
AVAILABILITY: the provider behind this path is not configured at this installation, so a
request will not be provisioned until an operator fixes that.

A dedicated Postgres database, created as a branch of the team's Neon project…
```

The `AVAILABILITY` line matters more than it looks. Without it, an agent on an installation
with no Neon token would request a Postgres branch, watch it sit in `PENDING`, and start
guessing at what it did wrong.

### `bailment_list_leases`

Every lease this caller holds, newest first. Scoped by the service, not by a query parameter —
"show me everything" is the most natural thing in the world for an agent to try.

### `bailment_renew_lease`

Pushes the deadline out, **measured from now** rather than stacked onto the old one. Stacking
would let three renewals of a four-hour lease produce a sixteen-hour lease while every
individual number still looked like it respected `max_ttl`.

It also never moves the deadline *earlier*. Renewing with a shorter TTL than the time already
remaining leaves the lease where it is, because nobody has ever meant to cut their own lease
short:

```
RENEWED. The deadline moved.

ACTIVE. The resource exists and the lease is running.
…
Seconds remaining: 3593, until 2026-07-25T20:13:19.448552+00:00.
```

Renewals are capped by the path's `max_renewals`, so a lease cannot become permanent by
attrition. When the cap is hit the refusal says what to do instead:

> A resource that keeps needing extending has stopped being temporary; ask a platform operator
> for a permanent one.

### `bailment_release_lease`

End early and destroy the resource. This is the single most useful thing an agent can do, and
the server instructions say so.

```
RELEASING. The lease is over and the resource is queued for destruction. The credential stops
working as soon as the provider confirms it is gone; there is nothing further for you to do
and nothing to poll.

state: revoked

There is nothing to wait for here. If you still need this capability, request a new lease; a
released one cannot be brought back.
```

Note the state: `revoked`, not `released`. Releasing records an *intention*. `RELEASED` is a
claim about reality and is only written once the provider confirms the resource is gone. The
result says "there is nothing to poll" precisely so the model does not sit in a loop watching
for a state that means something different from what it assumed.

---

## Testing without an agent

Everything above was captured with `curl`. The transport is streamable HTTP, so an
`initialize` gets you a session id and the rest is JSON-RPC:

```bash
SID=$(curl -s -D - -X POST http://localhost:8080/mcp \
  -H 'Authorization: Bearer demo-agent-token' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-06-18","capabilities":{},
        "clientInfo":{"name":"curl","version":"1"}}}' \
  | grep -i '^mcp-session-id:' | tr -d '\r' | awk '{print $2}')

curl -s -X POST http://localhost:8080/mcp \
  -H "Authorization: Bearer demo-agent-token" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

curl -s -X POST http://localhost:8080/mcp \
  -H "Authorization: Bearer demo-agent-token" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

Responses come back as `text/event-stream`; strip the `data: ` prefix and you have JSON.

---

## If you are changing this surface

The tool result strings are not cosmetic. They are the interface between this system and a
model's decision-making, and every sentence in them is answering a specific way agents go
wrong. Before you shorten one, work out which failure mode it was written for. `mcp/server.py`
names all four in its module docstring, and `tests/test_mcp.py` asserts on the parts that
matter.

The one rule that must not move: **the caller is constructed as `AGENT`, always.** If a change
makes that conditional on anything — a token, a header, a setting — it has removed the entire
security boundary of this surface.
