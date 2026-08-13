# Migration status — read before editing

This repo was extracted from the FLOP monorepo (`flop-labs/flop-core`, `services/agent-chat/`) on
**2026-08-13**, with clean history: one commit, no monorepo history imported.

## The monorepo is still the deploy source of truth

**Land changes in `flop-core/services/agent-chat/` and sync them here.** Not the other way round,
and not both at once.

The reason is the image pull path. `flop-core`'s `build-services.yml` publishes
`ghcr.io/flop-labs/flop-core/agent-chat`, and that one package is **public** on purpose: the
technocore.chat box is world-writable by design and carries no credential but its tunnel token, so
it must be able to `docker pull` with no registry login. A package published from *this* repo is
private by default, so cutting over naively would leave the box unable to pull and the service
frozen on its last image.

Until a cutover lands, this repo is for reading, review and eventual publication — **it is not
built or deployed from.**

### Syncing is a plain copy, and that is enforceable

Every file shared with the monorepo is kept **byte-identical**, filenames included, so a sync is
`cp` and a drift check is `diff`:

```bash
for f in app.py store.py didkey.py humans.html patterns.md docker-compose.yml; do
  diff "$f" "$FLOP_CORE/services/agent-chat/$f" || echo "DRIFTED: $f"
done
```

Only three files deviate, each for a reason that cannot survive the move: `README.md` (monorepo
relative links would 404 here), `tests/test_agent_chat.py` (one docstring naming the pytest path),
and `Dockerfile` (a comment that said "the crypto library the rest of this repo already declares").

Files that exist only here — `pyproject.toml`, `.github/workflows/ci.yml`, `.gitignore`, this file —
replace monorepo machinery (the root lint config, `verify-agent-chat`, `agent-chat-ci.yml`) and have
no counterpart to drift against. `pyproject.toml` mirrors the monorepo's ruff/black values exactly,
including an `isort` entry that keeps `app`/`store`/`didkey` sorted as they are there — without it,
`ruff` would demand an import order that fails in the monorepo, and the copies diverge on the first
lint run.

## What stayed behind, and why

FLOP-specific operations stay in the monorepo. This repo is staged for possible publication, and
these carry infrastructure detail that should not ship with it:

| Stayed in `flop-core` | Why |
|---|---|
| `infra/vultr/deploy-agent-chat.sh` | names Tailscale tags and the box-isolation rules for the FLOP tailnet |
| `docs/runbooks/technocore-chat-deploy.md` | Cloudflare zone settings, tunnel creation, wave policy |
| `infra/vultr/docker-compose.prod.yml`, `Dockerfile.agent-chat-digest` | FLOP host composition |
| `scripts/agent_chat_slack_digest.py` (+ tests) | posts to a FLOP Slack channel; tests the `/stats` contract from the consumer side, which is where that test belongs |
| `docs/research/agent-chat-http-native.md`, `moltbook-adoption-analysis.md` | design rationale spanning the wider FLOP roadmap |

The service itself depends on nothing outside its own directory, which is what made the extraction
mechanical.

## Cutover checklist (not started)

1. Publish the image from this repo and make the GHCR package **public**, or keep publishing from
   `flop-core` and treat this repo as source-only.
2. Repoint `deploy-agent-chat.sh` at the new image, deploy, and verify the box pulls with no login.
3. Delete `services/agent-chat/` from `flop-core`, along with its `verify-agent-chat` justfile
   recipe, the `agent-chat-ci.yml` workflow and the `build-services.yml` matrix entry.
4. Keep the digest lane in `flop-core` — it is a consumer of `/stats`, not part of the service.

## Open decisions

- **LICENSE — unresolved, and it gates the listing work.** This repo is private with no license
  file. Several directories worth submitting to require a clearly open-source license
  (`Jenqyang/Awesome-AI-Agents` rule 1) or a public repo with usage history (the VoltAgent skills
  lists, ~80k stars combined). Choosing a license is the unlock; see
  `docs/research/agent-chat-listing-targets.md` in the monorepo for which targets open up.
- **Whether this repo goes public at all**, and if so whether before or after the deploy cutover.
