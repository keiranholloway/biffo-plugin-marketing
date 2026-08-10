# Biffo Plugin Repository Template

> **This is a skeleton, not a live repository.** It lives at
> `_skeletons/plugin-template/` inside the `biffo-template` monorepo — the
> same pattern `_skeletons/registry/` already uses for the plugin registry
> (see that directory's `README.md`). Nothing in here runs as part of
> biffo-template's own CI/CD, and this directory is **not** a member of the
> monorepo's pnpm or uv workspace (check `pnpm-workspace.yaml` and the root
> `pyproject.toml`'s `[tool.uv.workspace]` — neither lists `_skeletons/`).
> It exists to be **copied into a brand-new GitHub repository** when someone
> creates a Biffo plugin, per [ADR-0003](../../docs/ADR/0003-plugin-system-and-marketplace.md)
> section 2 ("Plugin Repository Structure"). No such repository
> (`keiranholloway/biffo-plugin-template` or otherwise) has actually been
> created — that was an explicit decision for this issue (#26): build the
> skeleton here, don't stand up a real external repo, so it can be reviewed
> and iterated on like any other code change in this monorepo.
>
> To use it: copy this entire directory's contents (minus this note) into a
> new repository, rename `marketing` throughout to your plugin's name,
> replace `biffo.plugin.json` with your own manifest, and follow the setup
> steps below.

## What's here

```
plugin-template/
├── .github/workflows/
│   ├── ci.yml              # lint, typecheck, test, manifest validation, security scans — runs on PR
│   └── release.yml         # semantic-release + PyPI publish — runs on `v*` tag push
├── biffo.plugin.json        # example manifest: one table, four generic-CRUD routes
├── registry-schema.json     # vendored copy of the registry's manifest JSON Schema (see below)
├── pyproject.toml           # depends on biffo-plugin-sdk
├── terraform/               # the plugin's own infra — Lambda, EventBridge subscription, Core API IAM
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── README.md
├── src/marketing/
│   ├── __init__.py
│   ├── manifest.py          # path to biffo.plugin.json
│   ├── plugin.py            # MarketingPlugin(BiffoPluginBase) — the reference implementation
│   └── main.py               # Lambda entrypoint dispatching EventBridge events
└── tests/
    ├── conftest.py
    ├── fakes.py              # in-memory fake of the Core API's generic CRUD routes
    └── test_marketing.py
```

This is the canonical plugin shape: the `BiffoPluginBase` subclass, the
`src/<name>/{plugin.py, main.py}` layout, the manifest, the `terraform/`
module, and the fake-Core-API test pattern.

The working first-party example to read alongside it is the **orchestration
engine** (`services/_plugins/orchestrator/` in the biffo-template monorepo) — same base
class, same layout, and it ships a real `terraform/` too. It has considerably
more moving parts (workflow definitions, actions, SigV4 calls into the Core
API); this template's `marketing` is deliberately the smallest version of
the same shape: one table (`marketing_widgets`), four routes, one idempotent
seed, one `@subscribe` handler.

### Seeding: not from `on_install()`

`BiffoPluginBase` requires `on_install()`, `on_uninstall()` and `on_upgrade()`,
and they are **not invoked** — ADR-0003 §9 described a `biffo plugin install`
that would call them and the call site was never built. Define them as no-ops,
as `plugin.py` does. An earlier revision of this skeleton seeded through
`on_install()`; anyone who copied it got a seed that silently never ran, the
plugin deploying clean with empty tables and the symptom surfacing wherever
those rows were read
([#709](https://github.com/keiranholloway/biffo-template/issues/709)).

Baseline data goes in one of two places instead, and `seed_default_widget()` in
`plugin.py` documents both at the call site:

- **If your plugin declares an `api_ingress` ASGI app** (ADR-0021), seed from
  that app's lifespan. The shared plugin host runs each mounted app's lifespan
  itself, because Starlette's `Mount` never delivers the lifespan scope — until
  [#948](https://github.com/keiranholloway/biffo-template/pull/948) a plugin's
  own `@app.on_event("startup")` was equally dead. It fires on every cold start,
  so the seed must be idempotent; Core's own
  `POST /api/v1/internal/plugins/me/config/seed` was not idempotent until
  [#1000](https://github.com/keiranholloway/biffo-template/pull/1000), so treat
  this route as new and check your own.
- **If it doesn't** — like `marketing`, which is event-only — there is no
  startup to hang anything on. Put the rows in a SQL module in the instance's
  `db/imports/<name>/`, applied by `biffo data apply` on every deploy.

> Earlier revisions of this README pointed at an RBAC reference plugin at
> `services/rbac/`. That plugin was removed by
> [ADR-0011](https://github.com/keiranholloway/biffo-template/blob/main/docs/ADR/0011-authorization-is-a-core-concern.md)
> (authorization is a Core concern, not a plugin) and no longer exists.

## Standalone repo, not a monorepo package

Unlike biffo-template's own in-monorepo plugins (e.g. `services/_plugins/orchestrator/`,
a uv workspace member that resolves `biffo-plugin-sdk` from the workspace and
so needs no `[build-system]` of its own), this template's `pyproject.toml` **does** declare
`[build-system]` (hatchling) because it's meant to live in its own
repository with its own independent `uv sync` / `uv.lock`, not inside
biffo-template's workspace. Once copied out, `uv sync && uv run pytest`
works standalone with no dependency on the rest of biffo-template.

## The `biffo-plugin-sdk` dependency: PyPI pin

`pyproject.toml` declares:

```toml
dependencies = [
  "biffo-plugin-sdk[sigv4,user-serving]>=1.2,<2.0",
  ...
]
```

This is a **PyPI-style version pin**, resolved with no local override. The
SDK carries its own independent semver — it is **not** tied to the
template's core version, so a major bump here means the plugin API broke and
nothing else — and biffo-template's
[`.github/workflows/publish-sdk.yml`](https://github.com/keiranholloway/biffo-template/blob/main/.github/workflows/publish-sdk.yml)
builds and publishes each release to PyPI (via Trusted Publishing) on a
pushed `sdk-v*` tag. As of this pin, PyPI carries `1.0.0`, `1.1.0` and
`1.2.0` — confirmed directly against PyPI's JSON API, not assumed.

`>=1.2` (not `>=1.1`, and `biffo.plugin.json`'s own `"biffo-plugin-sdk":
"^1.0"` is looser still) because `1.2.0` is the first release carrying
`SignedCoreClient.raw_request(..., extra_signed_headers=...)` — the one SDK
method that SigV4-signs a request AND carries an extra header (the calling
admin's own forwarded token) through that signature. This plugin's
`src/marketing/principal_client.py` depends on it for every call to Core's
internal, per-plugin CRUD mount; see that module's docstring, and issue #27,
for why a SigV4-only call is not a substitute (it authenticates the plugin,
not the user making the request, so `require_principal_crud_permission` has
no identity to authorise against — a silent authorization bug, not a loud
one).

**Ordering caveat, historical.** Earlier revisions of this pin
(`>=1.0,<2.0`) predated the SDK's first PyPI upload, and this section used to
document two local `[tool.uv.sources]` overrides (a path dependency into a
biffo-template checkout, or a git dependency against the unpublished SDK) to
unblock development in the meantime. Neither is needed now that the pin
resolves directly — if you find one still configured in a fork of this repo,
delete it.

## Manifest validation: why CI doesn't hard-gate on `registry-schema.json`

`ci.yml`'s `validate-manifest` job runs two checks:

1. **Authoritative, blocking**: `biffo_plugin_sdk.plugin.load_manifest()` —
   the real Pydantic model the SDK, the Core API's
   `discover_plugin_manifests()`, and `biffo plugin install` all actually
   validate a manifest against.
2. **Advisory, non-blocking** (`continue-on-error: true`): the vendored
   `registry-schema.json` (a copy of `_skeletons/registry/registry-schema.json`
   as of when this template was written), via
   `python -m jsonschema -i biffo.plugin.json registry-schema.json`.

These two checks currently **disagree**, and check 2 is expected to fail.
`registry-schema.json`'s `api_routes` shape predates issue #19's
declarative table/operation CRUD-synthesis redesign: it still requires a
free-form `handler` function-name field (this template's manifest has no
such field — routes are declared as `table`/`operation` pairs instead, per
the real `RouteDef`/`RouteDefinition` models), and its `path` regex
(`^/[a-z0-9/_-]+$`) rejects the literal `{`/`}` characters that every
single-row route (`read`/`update`/`delete`) is required to have in its path
(e.g. `/widgets/{id}`). This was verified directly, not assumed: running
`jsonschema` against this template's own example manifest produces six
errors, all in `api_routes`. `cli/src/lib/plugin-manifest.ts` in
biffo-template already documents this exact same finding and deliberately
validates against the real Pydantic/Zod models instead of
`registry-schema.json` for the same reason.

This template follows that precedent: the manifest you ship must pass
check 1 (it's what actually gets enforced at plugin-install and db-init
time); check 2 is kept for visibility, so a future correction of
`registry-schema.json` upstream is easy to notice (it'll start passing) but
never blocks your CI in the meantime. If `registry-schema.json` is
corrected, remove this note and consider making check 2 blocking too.

## Contribution guidelines

- **Conventional Commits.** `feat: ...`, `fix: ...`, `chore: ...`,
  `docs: ...`, `test: ...`, `refactor: ...`, `perf: ...` — the same types
  biffo-template's own root `CLAUDE.md` documents. `release.yml`'s
  changelog generation depends on this.
- **One plugin, one repo.** Don't vendor unrelated code here — this repo
  should contain exactly the plugin's manifest, source, tests, and
  `terraform/`. The plugin's infrastructure lives in `terraform/` at the repo
  root (ADR-0003 section 2), and **this template ships one** — see
  [`terraform/README.md`](terraform/). The example plugin does need it: it
  declares an `event_subscriptions` entry, so without a Lambda and an
  EventBridge rule that subscription never fires. `biffo plugin install`
  copies `terraform/` into the user's monorepo at `modules/plugins/<name>/`,
  and it does so **silently only if the directory exists** — a plugin that
  deletes it gets a clean install and dead event handlers. A CI guard in
  biffo-template (`pnpm --filter @biffo/cli check:plugin-terraform`) fails any
  manifest that declares `event_subscriptions` without a `terraform/`
  directory; keep both in step.
- **Every table gets `tenant_id`.** You never declare it yourself — it's
  auto-injected (ADR-0001). Declaring `id`, `tenant_id`, `created_at`, or
  `updated_at` in a table's `columns` fails manifest validation.
- **No database client.** Plugins talk to the Core API over HTTP via the SDK's
  Core client only (ADR-0002) — never `psycopg2`/`asyncpg`/SQLAlchemy against
  the database directly. `BiffoPluginBase.api` is a SigV4-signing
  `SignedCoreClient` by default (ADR-0009); `terraform/` grants the Lambda role
  `execute-api:Invoke` on `/api/v1/internal/*`, and you must add that role to
  the Core API's `BIFFO_SERVICE_PRINCIPAL_ARN_ALLOWLIST` — see
  [`terraform/README.md`](terraform/).
- **Run the full check suite before opening a PR:**
  ```bash
  uv sync --all-groups
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright
  uv run pytest --cov
  ```

## Branch protection setup

Once you've created the real repository from this skeleton, protect `main`
with the **same settings biffo-template's own root repo uses**, codified as
Terraform in `modules/source-control/github/main.tf`'s
`github_branch_protection.main` resource in this monorepo — copy that
module (or these settings) rather than inventing new ones:

| Setting                              | Value                                                                                                                                                                       |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Required status checks               | `strict = true`; contexts: `CI / Lint`, `CI / Type Check`, `CI / Test`, `CI / Validate biffo.plugin.json`, `CI / Secret Scan` (the job names from this repo's own `ci.yml`) |
| Required approving reviews           | 1                                                                                                                                                                           |
| Dismiss stale reviews on new commits | Yes                                                                                                                                                                         |
| Require review from Code Owners      | Yes (add a `.github/CODEOWNERS` file)                                                                                                                                       |
| Enforce for admins                   | Yes                                                                                                                                                                         |
| Require linear history               | Yes                                                                                                                                                                         |
| Allow force pushes                   | No                                                                                                                                                                          |
| Allow branch deletion                | No                                                                                                                                                                          |

If you manage the new repo with Terraform too (recommended — see this
monorepo's own "IaC as first-class citizen" convention), use
`modules/source-control/github/` directly rather than hand-writing the
equivalent `github_branch_protection` resource. If you're setting this up
by hand via the GitHub UI instead: **Settings → Branches → Add branch
protection rule**, pattern `main`, and set each field above to match.

## Releasing

`release.yml` triggers on `v*` tag pushes: it re-runs the full CI gate against
the tagged commit, verifies the versions agree, builds the sdist/wheel, and
cuts a GitHub Release.

### Three versions have to agree

Your plugin declares its version twice, and the tag is a third:

| where | what reads it |
| --- | --- |
| `pyproject.toml` `project.version` | the Python package |
| `biffo.plugin.json` `version` | the plugin registry — **and therefore the version the portal's plugin store shows** |
| the `v*` tag | the GitHub Release |

`release.yml` checks all three and fails the release on any mismatch.

The manifest one is the one to watch. It is easy to forget and it fails
silently: tag `v0.2.0` with a stale manifest and you get a `v0.2.0` release, a
`0.2.0` package, and a plugin store still advertising `0.1.0` — indefinitely,
with nothing anywhere reporting a problem. The registry sync re-derives
whatever the manifest says, so it propagates the stale value rather than
catching it. Bump both.

Note that the registry entry is published by `publish-registry.yml` when
`biffo.plugin.json` changes on `dev`, not by this workflow on a tag — the
marketplace reflects `dev`, and tagging is an independent decision.

### Getting into the plugin store

`publish-registry.yml` publishes your entry to the Biffo plugin registry, which
is what the portal's plugin store reads. It needs a `REGISTRY_PUBLISH_TOKEN`
secret (a fine-grained PAT with `contents: write` on the registry repo) and
warns-and-skips without one, so check its header comment before assuming your
plugin will appear.

If your repo is public you may not need the token: the registry also runs a
credential-free pull-based sync over the repos listed in its `sources.json`.
Adding yours there is enough.

### PyPI publishing is opt-in

The publish step is gated on a `PYPI_PUBLISH` repository variable and skips
with a notice until you set it, so a freshly-scaffolded plugin gets a green
release instead of one that always fails at the last step. The built
distribution still ships on the GitHub Release either way.

To enable it for your plugin:

1. Create the PyPI project.
2. Configure a **trusted publisher** (OIDC) for this repo at
   `https://pypi.org/manage/project/<name>/settings/publishing/` — preferred
   over a long-lived `PYPI_API_TOKEN`, since there is no stored secret to
   rotate or leak. The `id-token: write` permission is already in place.
3. `gh variable set PYPI_PUBLISH --body true`
