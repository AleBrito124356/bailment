# Contributing to bailment

Thanks for looking. This project is early and the most useful contributions right now are new
providers and real-world reports from anyone who points it at a live cloud account.

---

## Getting set up

Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/AleBrito124356/bailment
cd bailment
uv sync

uv run pytest          # 827 tests, under twenty seconds, no credentials, no network
uv run ruff check .
uv run mypy            # --strict, configured in pyproject.toml
```

The test suite needs no cloud account and touches no network. Every test runs against
`MemoryProvider`, which implements the full provider contract in a dict and can be told to
fail on demand — including the half-completed provision and the failed teardown that produce
both directions of drift. If you find yourself wanting a real API to test against, that is
usually a sign the test belongs at a different layer.

Two fixtures are worth knowing about before you write a test:

- `_isolated_environment` is autouse. It strips every `BAILMENT_*` variable and moves the
  working directory to a temporary one. Settings read a `.env` from the working directory, so
  without the chdir a developer with a real `.env` in the repo root gets a suite that passes
  for them and fails in CI.
- `make_path` / `make_catalog` build `GoldenPath` objects directly rather than round-tripping
  through YAML. Most tests want one specific policy rule, and parsing it from a file would
  make every behavioural test also a test of the loader. `tests/test_catalog.py` is where real
  files on a real disk get parsed.

Running the whole thing end to end locally:

```bash
export BAILMENT_ENCRYPTION_KEY=$(uv run bailment keygen)
export BAILMENT_API_TOKENS=agent:local-token
export BAILMENT_ADMIN_TOKENS=me:local-operator-token
uv run bailment db upgrade
uv run bailment serve
```

The dashboard is a separate Next.js app:

```bash
cd web
npm install
npm run dev            # http://localhost:3000
npm run typecheck && npm run lint && npm run build
```

---

## Before you open a pull request

- [ ] `uv run pytest` passes.
- [ ] `uv run ruff check .` is clean.
- [ ] `uv run mypy` is clean. The project runs `--strict`; annotate everything.
- [ ] `uv run bailment catalog validate` passes if you touched the catalog. (`--strict`
      also promotes warnings to failures, which on a checkout with no cloud credentials
      means every unconfigured provider fails it -- use it where the providers are real.)
- [ ] New behaviour has a test, and the test can fail. Delete the implementation line and
      check that it goes red.
- [ ] The PR description says *why*, not what. The diff already says what.

For anything that changes the state machine, the policy path, the secrets path or the
reconciler, please open an issue first. Those four have invariants that are easy to break in a
way that still passes every test, and it is much less frustrating to talk about the design
before you have written it.

---

## House style

The style here is unusual on purpose. Read `src/bailment/states.py` and
`src/bailment/models.py` before writing anything; they are the reference.

### Module docstrings explain why, not what

Every module opens with prose explaining the decisions in it and **what breaks without them**.
Not a summary of the code — the code is right there.

Bad:

```python
"""Provider registry.

Contains the ProviderRegistry class, which stores providers in a dict and provides
methods to register and retrieve them.
"""
```

Good — this is the real one:

```python
"""Provider lookup and availability reporting.

The registry keeps providers that cannot run. That is the whole design.

An installation with no Cloudflare token should not behave as though Cloudflare does not
exist -- it should say "cloudflare: CLOUDFLARE_API_TOKEN is not set". … the reconciler
must be able to tell "this provider has no orphans" apart from "this provider was never
asked", because the second one silently looks like the first in every dashboard that only
lists what is working.
"""
```

The test for a good docstring: does it tell the next reader something they could not have got
from the code in thirty seconds, and would they have made a mistake without it?

### Comments earn their place

A comment either explains non-obvious reasoning or warns about a trap. It never restates the
line below it.

```python
# No.
# Increment the attempt counter.
lease.attempts += 1

# Yes.
# 130 is the conventional shell code for SIGINT; wrapper scripts check for it.
raise typer.Exit(130) from None
```

### Prose is plain and direct

No marketing voice, no "powerful", no "seamlessly", no exclamation marks. Write the way you
would explain it to a colleague who is about to be paged about it.

### Error messages are written for the person who is stuck

Name the setting, the file, the field. Say what to do next. Every `raise` in `config.py` names
the environment variable and, where relevant, the command that produces a valid value.

```python
raise ConfigError(
    f"{ENV_PREFIX}ENCRYPTION_KEY is not set. Every binding bailment stores is "
    f"encrypted with it, so there is nothing sensible to do without one.\n"
    f"  Generate a key:  bailment keygen\n"
    f"  Then export it:  {ENV_PREFIX}ENCRYPTION_KEY=<the key>\n"
    …
)
```

---

## The rules that are not negotiable

These are not style. A pull request that breaks one of them will be declined regardless of
how good the rest of it is.

**No `eval`, no `exec`, no third-party expression engine anywhere in the policy path.** The
reasoning is in `policy/evaluator.py` and in [docs/policy.md](docs/policy.md). If the policy
language genuinely needs something it does not have, the answer is to extend the tree-walker,
not to reach for a library.

**A secret value never appears in an API response body an agent can read.** Not behind a flag,
not in a debug mode, not in an error message. `LeaseService.resolve_binding` is the only
function that decrypts. `bailment.api.routes` proves at import time that no handler in it can
reach a decryption path, and that check is not a formality — if your change makes the API fail
to start with a message about decryption, the check is working.

**Never log a secret, never persist one in plaintext, never put one in an exception message.**
`scrub()` exists as a filter, not as a guarantee. The guarantee is that nothing puts one there
on purpose.

**Timezone-aware datetimes only.** `bailment.models.utcnow`, never `datetime.utcnow()`, which
returns a naive value that silently shifts every comparison by the host's offset. `aware()`
exists because SQLite hands back naive values and subtracting one from an aware `now` raises —
on the demo and in CI, but never in the Postgres deployment somebody tested against.

**Every mutating operation is idempotent.** A retry must never double-provision. If you are
adding a code path that creates something, ask what happens when it runs twice, and write the
test.

**All I/O is async, and every provider call has an explicit timeout.** There is no unbounded
call in this system. `HttpProvider` centralises the retry and backoff rules so three provider
implementations cannot develop three opinions about what a 429 means.

**Fail closed.** If policy cannot be evaluated, deny. If provider state is unknown, do *not*
assume released. `ResourceStatus.UNKNOWN` is a real answer and collapsing it into `GONE` is
the most dangerous single change anybody could make here.

**Nothing writes `RELEASED` except the deprovision path and the reconciler.** It is a claim
about reality. Everything else asks for `EXPIRED` or `REVOKED` and lets the engine do the
work. If code could write `RELEASED` optimistically, a failed destroy would look identical to
a successful one and the orphan would be invisible forever.

**`external_name` is committed before the provider is called, and providers use it
unmodified.** See `models.py`.

**Do not add a dependency without saying so.** The dependency set in `pyproject.toml` is small
deliberately: this process holds an encryption key and cloud credentials, and every dependency
is another CVE feed and another supply-chain surface. If you genuinely need one, add it to
`pyproject.toml`, mention it in the PR description, and be ready to explain why the alternative
is worse.

---

## Adding a golden path

A golden path is one YAML file in `src/bailment/catalog/paths/`. Read
[docs/policy.md](docs/policy.md) first, then:

- Write the `description` as an instruction to a capable stranger: what it gives you, what it
  costs, and **when not to use it**. A model reads it as the tool description and acts on it.
- Only write policy rules against inputs the path marks `required`. A rule that reads an
  absent input fails to evaluate, and a rule that fails to evaluate denies the request — so an
  optional input a rule depends on is a path that breaks for exactly the callers who left it
  out.
- End the chain with an unconditional rule. Validation enforces it.
- Write the `reason` for whoever just got blocked, and say what to ask for instead.
- Run `bailment catalog validate`.

## Adding a provider

The full contract, the three rules and a worked example are in
[docs/providers.md](docs/providers.md), which ends with a checklist. The short version: names
come in and never out, `UNKNOWN` is a real answer, `list_managed` raises rather than returning
an empty list, and `create` called twice adopts rather than duplicating.

Tests use `respx` to mock the HTTP API. No test may need a real account.

---

## Reporting bugs

Include the version, the database (SQLite or Postgres), the provider, and what you expected
versus what happened. If a lease is stuck, `bailment lease get <id>` prints the operations
panel — attempts, the claim, the retry schedule — which is usually the whole answer.

**Security issues do not go in the issue tracker.** See [SECURITY.md](SECURITY.md).

---

## Code of conduct

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Licence

Contributions are licensed under Apache 2.0, the same as the project. There is no CLA.
