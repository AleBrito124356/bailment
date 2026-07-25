# Policy

Policy is what stands between an autonomous process and a real cloud resource. This document
is the complete reference for it: what a rule can say, what a rule can see, and what happens
when a rule is wrong.

---

## The shape of a policy chain

Every golden path carries an ordered list of rules.

```yaml
policy:
  - when: input.env == "prod" and input.eviction == true
    effect: deny
    reason: >-
      Eviction on a production Redis turns a full database into silent data loss…

  - when: input.env == "prod"
    effect: require_approval
    approvers: [platform-team]
    reason: >-
      Anything standing in for production gets a human in the loop…

  - effect: allow
    reason: Dev and staging Redis databases are self-service.
```

**Rules are evaluated top to bottom and the first match wins.** Not "most specific wins", not
"deny overrides". First match wins is the only ordering semantics people reliably predict
correctly under pressure, and a policy nobody can predict during an incident is a policy
nobody trusts afterwards.

**The last rule must be unconditional.** A rule with no `when` matches everything and is
therefore the default. `GoldenPath` validation rejects a chain that does not end in one, and
rejects an unconditional rule anywhere *except* last, because an earlier one makes the rest of
the chain dead code.

### Effects

| effect | what happens |
|---|---|
| `allow` | The lease goes to `PENDING` and a worker picks it up. |
| `deny` | The lease goes to `REJECTED`, terminal. Nothing is provisioned. |
| `require_approval` | The lease goes to `AWAITING_APPROVAL` with an `Approval` row. Nothing is provisioned until a human decides. |

`approvers` is a list of principals allowed to decide. Empty means any operator. It is only
carried on a `require_approval` rule — putting it on an `allow` would invite a reader to see
consent in a list that was never one.

### Reasons

`reason` is not documentation. It is shown verbatim to the requester, returned in the MCP tool
result the model reads, and written to the audit log.

Write it for the person — or the model — who just got blocked, not for the person who wrote
the rule. A refusal that says "denied by policy" gets routed around: a model told that will
try the same request with different arguments, then a different golden path, then a shell
command that does the same thing. A refusal that names the rule, explains the reason and says
what to ask for instead closes the question:

> That subdomain is reserved. Names like www, api and admin are where this organisation's
> real services live or are expected to live … Pick a name that says what your change is
> instead, such as pr-1234, alice-checkout-fix or demo-2026-07: anything that is not on the
> reserved list is self-service and takes about a second.

---

## The context

These names are the user-facing API of the policy language. Changing one is a breaking change
to every golden path in every deployment. All of them are plain data — strings, numbers,
booleans and dicts. No object with behaviour ever enters the context, and no secret value ever
does either: bindings are encrypted and are not created until well after the decision.

| name | type | meaning |
|---|---|---|
| `input` | mapping | The validated request inputs. `input.env` or `input["env"]`. |
| `env` | any | Alias for `input.env`, present **only** when the request supplied an `env` input. |
| `requester` | string | The principal that called. For agent traffic, the agent's identity. |
| `on_behalf_of` | string or `null` | The human the agent is acting for. |
| `is_agent` | bool | True when the request arrived over MCP. |
| `golden_path` | string | The path id, so a shared rule fragment can discriminate. |
| `ttl_seconds` | int | The **effective** duration, after clamping to `max_ttl`. |
| `estimated_monthly_cost_usd` | float | From the path's cost model. |
| `estimated_hourly_cost_usd` | float | From the path's cost model. |
| `active_leases_for_requester` | int | Live leases this requester already holds, across all paths. |
| `hour_utc` | int | 0–23, always UTC. |
| `weekday` | int | 0–6, Monday is 0. The weekend is `weekday >= 5`. |

Notes worth having in mind while writing a rule:

- **`input` is closed.** Only keys the golden path's own JSON Schema declares can be present,
  because the schema is forced to `additionalProperties: false` before it is ever used. Agents
  are enthusiastic; an open schema lets one smuggle unvalidated keys straight through to a
  provider.
- **`ttl_seconds` is the clamped value**, not the requested one, so `ttl_seconds > 14400` is a
  statement about what will actually happen.
- **`is_agent` is the single most useful predicate in the language.** `is_agent and env ==
  "prod"` is the rule almost every platform team wants first.
- **`on_behalf_of` is not authenticated.** It is a claim supplied by the agent, recorded so
  the audit log can separate "which agent" from "who is accountable". Never grant privileges
  on it.
- **`hour_utc` is never local time.** A broker with operators in three timezones and one
  policy file has to pick one clock, and UTC is the one everybody can reason about from a log
  line.

---

## The expression language

### There is no `eval`

`eval` with a stripped `__builtins__` is not a sandbox, it is a puzzle, and the puzzle has
been solved publicly many times over — one attribute hop from any object reaches
`__class__`, then `__subclasses__`, then a file handle. There is no configuration of `eval`
that is safe to point at a string, so **no string is ever passed to it.** The expression is
parsed to an AST and interpreted by a tree-walker that can only reach values the caller
explicitly placed in the context dict.

There is no third-party expression engine either. CEL, JMESPath, simpleeval and friends are
each another dependency with another CVE feed, and the ones implemented in Python mostly
reduce to `eval` or to `getattr` chains anyway. The language bailment actually needs is about
forty lines of comparison and containment; owning it is cheaper than auditing someone else's.

### `getattr` is never called

`input.env` is sugar for `input["env"]`. That single rule is what makes the sandbox hold: with
`getattr`, every value reachable from the context becomes a doorway to its type, its module
and eventually the interpreter. Without it, a value is only ever data. Attribute access on
anything that is not a mapping is an error, not a fallback.

Dunder names and dunder attributes are rejected at compile time, in both positions.

### There is no arithmetic

`ast.BinOp` is rejected outright. It is not only that policies rarely need it —
`2 ** 999999999` and `"x" * 10**9` are a hang and an OOM written in four characters each,
evaluated before any node budget could react.

The consequence for rule authors: **anything a rule compares against is either a literal
worked out when the rule was written, or a value the broker precomputed.** This is a real
cost, and the shipped `postgres` path shows how to pay it honestly:

```yaml
  # The ceiling is $5 of awake compute for one branch, and at the $0.14/hour below that is
  # 36 hours, which is 129600 seconds.
  - when: ttl_seconds > 129600 or estimated_hourly_cost_usd > 0.20
    effect: require_approval
```

The second half of that condition is a tripwire rather than a request check:
`estimated_hourly_cost_usd` comes from the same file, so if somebody moves the path to a
larger compute class and forgets to re-derive the `129600`, every request starts needing
approval instead of the ceiling quietly becoming meaningless.

### The complete grammar

Permitted:

- **literals** — strings, integers, floats, and booleans/null in either the YAML spelling
  (`true`, `false`, `null`) or the Python one (`True`, `False`, `None`). Rules live in a YAML
  file and a language that rejected `true` there would be a papercut in every policy anybody
  ever writes.
- **names**, resolved only from the supplied context.
- **attribute access** — `input.env`, meaning `input["env"]`.
- **subscripts** — `input["env"]`, `some_list[0]`. No slices.
- **comparisons** — `==` `!=` `<` `<=` `>` `>=` `in` `not in`, including chained forms.
- **boolean operators** — `and`, `or`, `not`, with strict boolean operands and short-circuit
  evaluation.
- **unary minus** on numbers.
- **tuple, list and set displays**.
- **the fixed helper set** — `len`, `lower`, `upper`, `startswith`, `endswith`, `matches`,
  `any_of`.

Rejected at compile time, each with a message naming the construct: lambdas, comprehensions,
generator expressions, f-strings, the walrus, starred arguments, dict displays, conditional
expressions, `is` / `is not`, arithmetic and bitwise operators, slices, dunder identifiers,
and calls to anything outside the helper set.

`is` gets its own message, because identity is almost never what a rule means:

```
'is' is not allowed in a policy expression; identity is almost never what a rule
means, use == or != instead
```

### Helpers

| helper | signature | notes |
|---|---|---|
| `len(x)` | string or collection → int | Refuses anything else. |
| `lower(s)`, `upper(s)` | string → string | Refuses non-strings; there is no coercion anywhere. |
| `startswith(s, prefix)` | prefix may be a string or a collection of strings | |
| `endswith(s, suffix)` | as above | |
| `matches(s, pattern)` | **full** match, not a search | Pattern ≤ 256 chars, subject ≤ 4096 chars. |
| `any_of(x, ...)` | `any_of(env, "dev", "staging")` or `any_of(env, allowed_list)` | The second form is what makes it useful against a context list. |

`matches` is a `fullmatch`. Anchor nothing; `matches(input.name, "pr-[0-9]+")` will not match
`pr-1234-extra`.

### Strictness, and why

Policy expressions do not use truthiness. `and`, `or` and `not` require boolean operands, and
an expression that produces anything other than `True` or `False` is a *failure*, not a
guess:

```
expression produced a str, but a policy rule must produce true or false;
add an explicit comparison
```

Writing `input.name` and meaning "if a name was given" is a bug that would silently allow
every request with a non-empty name and deny every request with an empty one. Write the
comparison out.

### Optional inputs

`in` on a mapping tests keys, and boolean operators short-circuit. Together those are the
supported way to write a rule against an input that may be absent:

```yaml
  - when: '"env" in input and input.env == "prod"'
    effect: require_approval
```

Without the guard, `input.env` on a request that omitted `env` is an evaluation failure, and
an evaluation failure denies the request. Which brings us to the most important paragraph in
this document.

---

## Failure is denial

**A rule whose expression cannot be evaluated denies the entire request.** Not "log a warning
and skip it". Not "treat it as not matching".

The tempting behaviour — skip the broken rule, carry on down the chain — is catastrophic here.
Policy chains are almost always written as a series of narrowing denials ending in an allow.
Skipping a broken rule turns a deny into an allow, silently, at exactly the moment the
operator has least reason to look. Loud and wrong beats quiet and permissive.

So a typo in a name, a golden path that stopped supplying an input, or a comparison between a
string and a number produces:

```
policy rule 2 of golden path 'postgres' could not be evaluated (unknown name 'enviroment';
the policy context provides 'active_leases_for_requester', 'env', 'estimated_hourly_cost_usd',
…); the request is denied because a policy that cannot be evaluated cannot be trusted
```

The practical rule for authors: **only compare against inputs the path marks `required`.** The
shipped `redis` path makes `eviction` required rather than optional for exactly this reason,
and says so in a comment:

> Policy compares against it, and a rule that reads an input the request did not supply fails
> to evaluate, which denies the request — so an optional input a rule depends on is a path
> that breaks for exactly the callers who left it out.

Catch this before shipping:

```bash
bailment catalog validate
```

Every expression in every rule is compiled at load time, so a malformed rule is a startup
failure rather than a surprise denial in front of an agent that is mid-task.

---

## Errors never carry values

Every message this module produces names identifiers, keys and type names. Never the data
behind them.

```
`input` has no key 'enviroment'; it provides 'env', 'name'          ← keys, not values
cannot order a str against an int in `input.size > 4`                ← types, not values
unexpected KeyError while evaluating the expression                  ← type only
```

That last one is deliberate: when an unexpected exception escapes evaluation, only its *type*
is reported, because a message from an arbitrary exception can carry a value out of the
context and into the audit log. The policy context is documented as secret-free, but
"documented as" is not "guaranteed to be", and a policy failure reason is both written to the
audit log and shown to the requester.

---

## Resource ceilings

Chosen far above any rule a person would write by hand and far below anything that costs
measurable time. They are not a defence against an attacker with commit access — rules are
written by platform engineers — but against the ordinary accident of a generated or
copy-pasted expression taking the request path down with it.

| limit | value |
|---|---|
| expression length | 2 000 characters |
| AST nodes | 250 |
| nesting depth | 20 |
| elements in one list/tuple/set | 100 |
| `matches()` pattern length | 256 characters |
| `matches()` subject length | 4 096 characters |

Deeply parenthesised input that would blow the CPython parser's own stack is caught at parse
time and reported as "nested too deeply to parse", rather than becoming a `RecursionError`
somewhere up the call chain.

Compiled expressions are cached by exact source text, so a rule is parsed once per process and
editing it produces a different cache key rather than a stale hit.

**One acknowledged limit.** A pathological regex in a `matches()` pattern can still backtrack
for a long time on an adversarial subject. Patterns come from the golden path YAML, which is
the same trust level as the rest of the policy, so the exposure is a mistake by a platform
engineer rather than an escalation by a requester. The subject length cap bounds how bad that
mistake can get.

---

## Patterns worth stealing

**Production needs a human, and agents especially.**

```yaml
  - when: is_agent and env == "prod"
    effect: require_approval
    approvers: [platform-team]
    reason: >-
      An autonomous process asking for production is the case a person should look at.
      Say what you are trying to reproduce; these are usually granted in minutes.
```

**Stop a looping agent.**

```yaml
  - when: is_agent and active_leases_for_requester >= 5
    effect: deny
    reason: >-
      You already hold five live leases. Release what you have finished with
      (bailment_release_lease) before asking for more; if you genuinely need six at once,
      a human can approve that but an automated request cannot.
```

**Office hours for expensive things.**

```yaml
  - when: estimated_hourly_cost_usd > 0.50 and (hour_utc < 7 or hour_utc > 19 or weekday >= 5)
    effect: require_approval
    reason: >-
      Expensive resources requested outside working hours tend to run all weekend. Ask
      again on Monday, or an approver will pick this up.
```

**Reserved names, refused with an alternative.**

```yaml
  - when: input.subdomain in ["www", "api", "admin", "mail", "mx", "_dmarc", "ns1"]
    effect: deny
    reason: >-
      That subdomain is reserved … Pick a name that says what your change is instead, such
      as pr-1234: anything not on the reserved list is self-service and takes a second.
```

**The combination that produces an unreproducible bug.**

```yaml
  - when: input.env == "prod" and input.eviction == true
    effect: deny
    reason: >-
      Eviction on a production Redis turns a full database into silent data loss: keys
      disappear, writes keep succeeding, and the application has no way to tell.
```

Denied outright rather than sent to an approver, because there is no explanation that makes it
a good idea and an approval queue is a bad place to have that argument.

---

## Testing a rule

The engine is pure, so a rule can be tested with no database, no network and no broker:

```python
from bailment.catalog.loader import load_catalog
from bailment.policy.engine import RequestContext, explain

path = load_catalog("src/bailment/catalog/paths").get("postgres")

result = explain(
    path,
    RequestContext(
        requester="claude-code",
        is_agent=True,
        golden_path="postgres",
        inputs={"env": "prod", "name": "migrate-orders"},
        ttl_seconds=4 * 3600,
        estimated_hourly_cost_usd=0.14,
    ),
)

print(result.decision.effect)  # require_approval
print(result.decision.reason)
for trace in result.traces:
    print(trace.index, trace.outcome, trace.when)
```

`explain` returns the same decision `evaluate` would, plus a per-rule trace with one of four
outcomes — `matched`, `not_matched`, `error`, `not_evaluated`. `not_evaluated` means an
earlier rule already won. The dashboard renders exactly these, and the two share one code
path, so the trace can never disagree with the decision it explains.
