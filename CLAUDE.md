# AI Assistant Context

The tool-agnostic rules of engagement for **all** automated agents (integration
branch, worktrees, commits, PRs, merging, honest pushes, security) live in
[`AGENTS.md`](AGENTS.md) and are imported below. They are binding for any change
in this repo; `AGENTS.md` is the single source of truth.

@AGENTS.md

## What this repo is

A **Biffo plugin** — code that extends a core Biffo project, distributed and
mounted by it. See the core project's ADR-0003 (plugin system) and ADR-0021
(shared plugin hosting) for how a plugin is installed and run. This repo carries
the plugin's own manifest (`biffo.plugin.json`), source, and tests.
