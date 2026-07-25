# bailment dashboard

The web front end for the bailment provisioning broker. Next.js 15 (App Router),
TypeScript, Tailwind, TanStack Query. It is a browser client and nothing else: it holds no
credential, keeps no database, and every byte it renders came from the broker's REST API at
`/api/v1`.

```
npm install
cp .env.example .env.local     # point NEXT_PUBLIC_BAILMENT_API at your broker
npm run dev                    # http://localhost:3000
```

## Talking to the broker

Two settings have to agree, one on each side.

| Where | Setting | Value |
| --- | --- | --- |
| dashboard | `NEXT_PUBLIC_BAILMENT_API` | the broker origin, e.g. `http://127.0.0.1:8000` |
| broker | `BAILMENT_PUBLIC_BASE_URL` | this dashboard's origin, e.g. `http://localhost:3000` |

The broker's CORS policy is one origin, derived from `BAILMENT_PUBLIC_BASE_URL` — not a
wildcard, because it holds an approval endpoint and an approval that any origin can drive
out of a logged-in operator's browser is not an approval. If requests fail with nothing in
the broker's log, that mismatch is almost always why; the dashboard's error state says so
by name.

The allow-listed request headers are `Authorization`, `Content-Type` and
`X-Broker-API-Version`. The dashboard therefore never sends `X-Bailment-On-Behalf-Of` —
which is correct anyway, since a human at this screen acts as themselves.

### Tokens

There is no `BAILMENT_API_TOKEN` in this app's environment, and that is deliberate. A token
in the Next process would be used for every visitor, turning "who may approve a production
database branch" into "who can open a URL".

Instead the operator pastes a token into the connection dialog (sidebar, bottom left). It
is kept in `sessionStorage` for that tab, sent as a bearer header, and never written to a
cookie, a URL, a query string or the Next server. Close the tab and it is gone.

- an entry from `BAILMENT_ADMIN_TOKENS` is **operator tier**: sees every lease, approves,
  rejects, revokes anyone's lease, triggers a reconcile.
- an entry from `BAILMENT_API_TOKENS` is **agent tier**: sees its own leases. `/approvals`,
  `/reconcile` and `/providers` answer 403, and the dashboard renders that as "operator
  token required" with a button to switch, not as a fault.
- a broker running with `BAILMENT_ALLOW_ANONYMOUS` needs no token at all, at agent tier.
  The dashboard does not gate itself behind a token prompt for exactly this case.

## The one thing this dashboard will never show you

A credential.

Not behind a flag, not for an operator, not in a modal, not "just this once". The broker has
no endpoint that returns a decrypted value: its response models refuse at import time to
carry a field whose *name* looks like one, and its route module proves at import time that
no handler references the function that decrypts. The dashboard could not display a secret
if it wanted to.

What it shows instead, on `/leases/[id]`, is the binding *reference*, the names of the
sealed values, when they were last resolved and how many times — plus the command that puts
the real value into one process and nowhere else:

```
bailment exec <lease-id> -- <command>
```

The bindings panel says all of this in as many words. That copy is load-bearing: "missing"
invites somebody to look for a workaround, and "withheld by design" does not.

## Pages

| Route | What it is |
| --- | --- |
| `/` | Stat tiles — active leases, expiring within the hour, **orphans outstanding**, estimated live spend — plus recent activity and the last reconcile. |
| `/catalog` | Golden path cards. Each opens a request form generated from that path's JSON Schema. |
| `/leases` | Filterable table with a live countdown per lease. Rows expand to the full audit trail. |
| `/leases/[id]` | Inputs, the policy decision with its per-rule trace, provider reference, binding references, audit timeline, renew / revoke / retry. |
| `/approvals` | The pending queue: who asked, whether a principal was named behind them, the inputs against the path's defaults, cost, the policy reason verbatim, approve / reject. |
| `/reconcile` | Run history, orphans and drift per run, provider configuration, and a "run now" button that reports and cannot destroy. |

## Five decisions worth knowing before you edit this

**Forms are generated, never written.** `src/lib/schema-form.ts` turns a golden path's
`input_schema` — the same object the agent's MCP tool is built from — into controls. A
hand-written form per path would be a second definition of "what arguments does this take"
in a repository whose entire thesis is that a second definition drifts. A property this
generator cannot render becomes a JSON textarea with a visible note; nothing is ever
silently dropped, because a form that quietly omits a property the broker will validate is
precisely the failure being prevented.

**Countdowns never subtract a client clock from a server timestamp.** They start from the
broker's own `seconds_remaining` and tick down by elapsed local time since the response
landed (`src/lib/clock.ts`). Two readings of the same clock give a correct interval even
when that clock is wrong in absolute terms; a laptop four minutes fast would otherwise show
a live lease as expired. Relative timestamps ("4 minutes ago") are corrected by the offset
`/stats.at` reports.

**The policy trace is reconstructed, and the reconstruction is exact.** The broker persists
the decision and the index of the deciding rule, not the trace. Because the chain is
first-match-wins — a total order with one decision point — every other rule's outcome
follows: everything above was evaluated and did not match, everything below never ran. The
one ambiguity (did the rule match, or fail to evaluate?) is settled by the `policy_denied`
audit event, which carries the error. See `src/lib/policy-trace.ts`, including what happens
when the YAML has been edited since the lease was requested.

**Idempotency keys are derived from the request, not from the dialog.** The request form
regenerates its key whenever the inputs or the TTL change and holds it stable while they do
not. Pressing submit twice returns the same lease; editing a field and submitting again is
a genuinely new request. Keying it to the dialog session would silently replay the first
lease; omitting it would make a retry after a timeout create two databases.

**Orphans are amber, never red.** An orphan on this screen means the reconciler worked. Red
would say the system is broken, which is the opposite of true, and an interface that shouts
is one people learn to dismiss. `failed` is the only red in the product.

## Design

Ultra-clean light, Stripe/Vercel register. Zinc neutrals, one accent (`#2563EB`), Inter,
generous whitespace, `zinc-200` borders, essentially no shadows — the request dialog and the
tooltips are the only things that float, because they are the only things that should.

Dark mode is supported properly, through CSS variables defined twice in
`src/app/globals.css` and applied before first paint by an inline script in the layout. It
is derived from the light theme rather than the other way round.

Colour is rationed. Green means one thing: a resource exists and you may use it now.
`released` is the successful end of a lease and is still grey, because a table of fifty
finished leases in green drowns the two that are live — and "which of these can I use" is
the question the table is for.

Every page has a real empty state and a real error state. A fresh install with zero leases
must look intentional, not broken; and the four ways talking to a broker fails (not running,
no token, wrong tier, refused with a reason) are told apart, because the fix for each is
different.

### Fonts

There is no `next/font/google` call. It downloads at build time, and this repository has to
build on a machine with no network. The stack in `tailwind.config.ts` prefers a locally
installed Inter and falls back to the platform UI font. If you want Inter guaranteed, add
`@next/font` or self-host the files — that is a deployment choice, not a code change.

## Layout

```
src/
  app/                    routes: overview, catalog, leases, leases/[id], approvals, reconcile
  components/             feature components (approval card, audit timeline, policy trace, …)
    form/                 the schema-driven controls
    ui/                   primitives: button, card, badge, dialog, tooltip, field, skeleton
  lib/
    api.ts                the only place the broker is called; the only place the token is read
    auth.ts               token storage, and why it is sessionStorage
    clock.ts              skew-proof countdowns
    format.ts             durations, money, relative time
    policy-trace.ts       reconstructing PolicyEngine.explain
    queries.ts            every read, with its polling interval
    schema-form.ts        JSON Schema -> controls, validation, request body
    states.ts             lease state colours and blurbs, mirrored from states.py
    types.ts              the wire contract, mirrored from api/schemas.py
```

`src/lib/types.ts` is hand-maintained rather than generated, on purpose: it is the file
where somebody adding a field to a response has to type it out under a docstring saying
which fields must never exist.

## Known limitations

- **The activity feed is derived, not streamed.** There is no installation-wide audit
  endpoint — the trail is per lease and scoped by the same visibility predicate as the lease
  itself. The overview builds its feed from the three timestamps a lease row carries on its
  face (requested, activated, released) in one request, rather than fanning out N audit
  calls on every poll. The complete history of any lease is one click away.
- **Table filters live in component state, not the URL.** Reading them from
  `useSearchParams` would make them shareable and would force the page out of static
  rendering or into a Suspense boundary; for a screen polled every ten seconds, that trade
  did not look worth it.
- **Polling, not websockets.** The broker exposes no event stream. Intervals are per query
  (`src/lib/queries.ts`) and stop when the tab is hidden.

## Scripts

```
npm run dev        # development server
npm run build      # production build
npm run start      # serve the production build
npm run lint       # eslint
npm run typecheck  # tsc --noEmit
```
