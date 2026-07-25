# Security policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report privately through GitHub's [Security Advisories](https://github.com/AleBrito124356/bailment/security/advisories/new)
on this repository. That gives us a private thread and, if it turns out to be real, a CVE
and a coordinated disclosure.

If GitHub advisories are not workable for you, email **alejandrobritoolivera@gmail.com** with
`bailment security` in the subject line.

Please include:

- what you can do that you should not be able to do, stated as a capability rather than as a
  stack trace ("an agent token can read a binding value", not "line 412 crashes");
- the smallest reproduction you have — a failing test against the `memory` provider is ideal,
  since it needs no cloud account and no network;
- the version or commit, and whether the deployment was SQLite or Postgres.

**What to expect.** An acknowledgement within 72 hours, an assessment within a week, and a
fix or a written explanation of why it is not one. bailment is a single-maintainer alpha
project and there is no paid embargo process behind it; if that timeline does not work for
your disclosure policy, say so in the first message and we will agree something.

There is no bug bounty. There is credit in the advisory and in the release notes, unless you
ask us not to.

**Please do not** test against infrastructure you do not own, or against a third party's
bailment installation. Everything worth testing reproduces against the `memory` provider on
your own machine.

---

## Threat model

The whole design rests on one assumption, and it is worth stating plainly before anything
else:

> **An AI agent is a semi-trusted principal that may be prompt-injected at any moment.**

Not because agents are malicious. Because an agent that reads a web page, a dependency
README, a GitHub issue or a log file is an agent that can be handed instructions by whoever
wrote that text. After a successful injection, the agent is an attacker who holds the agent's
credentials and the agent's permissions, and who is *trying* to look like normal work.

So the interesting question is never "is this agent trustworthy". It is: **what can this
request accomplish if the agent making it is currently under someone else's control?** Every
control below is an answer to that question.

### Principals

| Principal | Trust | Can |
|---|---|---|
| **agent** (`BAILMENT_API_TOKENS`, or any token over MCP) | semi-trusted, assumed injectable | request leases, read its own leases, renew, release |
| **operator** (`BAILMENT_ADMIN_TOKENS`) | trusted human | everything an agent can do, plus approve, reject, revoke anyone's lease, re-drive teardown, read binding values over OSB |
| **anonymous** (`BAILMENT_ALLOW_ANONYMOUS`) | untrusted | exactly what an agent can do, never more |
| **platform engineer** (writes golden paths and policy) | fully trusted | defines what exists at all |
| **operator host** (shell on the broker machine) | fully trusted | holds the database and the encryption key |

Two of those deserve emphasis. `BAILMENT_ALLOW_ANONYMOUS` grants the *lower* tier and never
the higher one, so no single environment variable in a compose file can turn the broker into
an open one that also approves things. And a plain API token is agent-tier, not "human"-tier:
the question this broker asks is not "is this a person" but "may this principal end up
holding a credential", and the safe answer for anything that is not an explicitly configured
operator is no.

### What bailment protects against

**Credential exfiltration through the model's context window.**
No secret value crosses the MCP boundary — not in a tool result, not in an error message, not
behind an argument, not with an admin token. An MCP caller is constructed as `CallerKind.AGENT`
regardless of which token it presented, because the security boundary is the *surface* and not
the privilege; `LeaseService.resolve_binding` refuses that kind outright. An agent receives
`bailment://binding/<uuid>` and the exact command that injects the value into a subprocess it
never reads.

**Credential exfiltration through the REST API.**
No handler in `bailment.api.routes` may decrypt. This is not a convention:
`assert_handlers_never_decrypt` inspects the compiled code of every function in that module
at import time for the names that lead to plaintext, and the application fails to start if
one appears. The binding endpoint selects its columns explicitly rather than loading whole
`Binding` rows, because naming the sealed column at all would trip the check.

**Credential exposure at rest.**
Binding payloads are Fernet-sealed (AES-128-CBC with HMAC-SHA256 and a timestamp) into a
`LargeBinary` column, one envelope per binding so that values which are useless apart — a
Redis URL and its HTTP token — are sealed, rotated and revoked as one thing. Decryption
accepts the active key plus any retired ones, so rotating the key does not strand live
bindings.

**Credential exposure in logs and errors.**
`bailment.logging` installs a redactor over the whole logging pipeline, including uvicorn's
access log. Provider error messages are scrubbed before they reach a `ProviderError`, because
provider APIs routinely echo the request — which may contain a connection URI — back in their
error bodies. `ProvisionResult` refuses to render its own payload in `repr`, since the most
likely way to leak a credential is a traceback rather than an API response. The policy
evaluator never puts a *value* in an error message, only identifiers, keys and type names.

**Sandbox escape from a policy expression.**
There is no `eval`, no `exec` and no third-party expression engine anywhere in the policy
path. Expressions are parsed to an AST and interpreted by a tree-walker that can only reach
values the caller explicitly placed in the context. `getattr` is never called: `input.env` is
a dict lookup, and attribute access on a non-mapping is an error rather than a fallback.
Dunder names and attributes are rejected at compile time. Arithmetic is rejected outright,
because `2 ** 999999999` and `"x" * 10**9` are a hang and an OOM in four characters each,
evaluated before any node budget could react. Expression length, node count, nesting depth,
display size, regex pattern length and regex subject length are all bounded.

**Policy failing open.**
A rule whose expression cannot be evaluated denies the entire request and names itself in the
reason. Skipping a broken rule and continuing down the chain would turn a deny into an allow
silently, at exactly the moment nobody is looking. If policy cannot be evaluated, the answer
is no.

**An agent approving its own request.**
`CallerKind.AGENT` is refused by `LeaseService.approve`. An approval gate the gated thing can
open is not a gate.

**Unbounded blast radius in time.**
Every lease has a TTL, clamped to the golden path's ceiling. Renewals are capped, so a lease
cannot become permanent by attrition. Even a fully compromised agent holds a capability that
expires against a resource that will be destroyed.

**Resources escaping the accounting.**
Write-ahead naming plus the reconciler. The name a resource will carry is committed before
the provider is called, and the reconciler sweeps both directions: resources at the provider
with no live lease, and leases whose resource has vanished. `UNKNOWN` is a real answer that
changes nothing — a provider that timed out has told us nothing, and turning silence into
`RELEASED` is the one thing this system must never do.

**Runaway automation.**
`active_leases_for_requester` is in the policy context, so a rule can stop a looping agent
from provisioning forty databases. Idempotency keys mean a retried call returns the same
lease rather than a second resource, and the MCP session id derives one automatically.

**Cross-site request forgery against the approval endpoint.**
CORS allows exactly one origin, derived from `BAILMENT_PUBLIC_BASE_URL`. Not a wildcard: an
approval that any origin can drive out of a logged-in operator's browser is not an approval.

**Timing oracles on token comparison.**
Tokens are compared with `hmac.compare_digest`, and every configured entry is scanned rather
than returning on first match.

### What bailment does not protect against

Stated plainly, because a threat model that only lists wins is marketing.

- **Misuse of a capability that was legitimately granted.** If policy allows an agent a
  staging database for two hours, and the agent is injected during those two hours, it has a
  staging database for two hours. bailment bounds *what* and *how long*; it does not inspect
  what you do with the connection once you have it. Grant narrower paths and shorter TTLs.
- **Anyone with shell access to the broker host.** They hold the database, the encryption key
  and the `bailment exec` path. There is no defence here and pretending otherwise would be
  theatre; the CLI presents as an operator for exactly this reason.
- **A hostile or careless platform engineer.** Golden path files and policy rules are trusted
  input. A rule that allows everything allows everything. Review them like production code —
  `bailment catalog validate` in CI catches malformed rules, not unwise ones.
- **Provider-side compromise.** If a cloud provider's API is compromised or its credentials
  are stolen elsewhere, bailment's copy of that credential is not the interesting problem.
- **Denial of service.** There is no rate limiting on the API. Put it behind something that
  has some. A pathological regex in a `matches()` pattern can still backtrack for a while on
  an adversarial subject; the subject length cap bounds how bad that gets, but patterns come
  from trusted policy files and are treated as trusted.
- **Destruction of data inside a leased resource.** A lease expiring destroys the resource
  and everything in it. That is the promise, not a bug. Do not put anything in a leased
  resource that has to exist tomorrow.
- **Transport security.** bailment speaks plain HTTP and expects to sit behind a terminating
  proxy. Bearer tokens over unencrypted HTTP across an untrusted network are bearer tokens on
  the wire.
- **Multi-tenancy.** One installation, one trust domain. Visibility is scoped per requester,
  but this has not been designed or reviewed as a multi-tenant system.

### The statement worth repeating

**A secret value never crosses the MCP boundary.** There is no configuration, no token, no
argument and no header that makes an MCP tool return a credential. The only code path in the
system that decrypts a binding is `LeaseService.resolve_binding`, it refuses any caller that
is not the local CLI injection path or a human operator, and that check is an explicit caller
*kind* rather than a scope string — because a scope string is something a future tool handler
can satisfy by accident, and a caller kind is not.

If you find a path that violates this, it is the highest-severity class of bug this project
has. Please report it.

---

## Deployment hardening checklist

- [ ] `BAILMENT_ENCRYPTION_KEY` set from `bailment keygen`, stored in a secret manager, never
      committed. Rotate through `BAILMENT_PREVIOUS_ENCRYPTION_KEYS`; never by replacement.
- [ ] `BAILMENT_ALLOW_ANONYMOUS=false` (the default). Anything else is the local demo.
- [ ] Distinct, named tokens per principal — `agent-ci:...`, not a shared secret. The
      principal lands in every audit row, so an unnamed token is an unanswerable audit.
- [ ] `BAILMENT_ADMIN_TOKENS` held only by humans who should be able to approve.
- [ ] TLS terminated in front of the broker. Never expose it directly.
- [ ] `BAILMENT_PUBLIC_BASE_URL` set to the real dashboard origin, so CORS is right and
      approval links point somewhere a human can reach.
- [ ] `BAILMENT_RECONCILE_AUTO_DESTROY_ORPHANS=false` until you trust the tagging in your
      account, and then only with a per-provider allowlist.
- [ ] `BAILMENT_RESOURCE_PREFIX` set to something installation-specific if two bailment
      installations share one cloud account, so they do not reconcile each other's resources
      into oblivion.
- [ ] Provider credentials scoped as narrowly as the provider permits — a Neon token that can
      only touch one project, a Cloudflare token scoped to one zone.
- [ ] `/metrics` and `/health` not exposed publicly. They carry no lease ids and no
      principals by design, but they are the least-guarded thing in most deployments.
- [ ] Alerting on `bailment_orphans_found_total` and on
      `bailment_reconcile_last_run_timestamp_seconds` going stale. A reconciler that has
      stopped running reports no orphans at all.

---

## Supported versions

bailment is alpha at 0.1.x. Security fixes land on `main` and in the next release; there are
no backports to earlier tags yet. When 1.0 ships, this section will say something more
useful.
