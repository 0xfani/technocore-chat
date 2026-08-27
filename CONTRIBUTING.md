# Contributing

Thanks for helping improve technocore-chat. Bug fixes, tests, documentation, and focused
enhancements are welcome.

Do not report exploitable vulnerabilities in a public issue or pull request. Follow
[`SECURITY.md`](SECURITY.md) to send a private report.

## Development setup

The project uses Python 3.12 and [`uv`](https://docs.astral.sh/uv/) for the environment and locked
dependencies:

```bash
uv sync --frozen
```

Run the service locally with a disposable data directory:

```bash
CHAT_ROOT=./data uv run uvicorn --app-dir src app:app --port 8080
```

Then check the health endpoint at <http://localhost:8080/healthz> or read the local manual at
<http://localhost:8080/llms.txt>.

## Making a change

- Keep each pull request focused on one problem. Bug fixes and small documentation improvements
  can go directly to a pull request; discuss substantial API or design changes in an issue first.
- Match the existing style and reuse established helpers and patterns where practical.
- Add tests for behavior that changes. A bug fix should include a regression test that fails
  without the fix and passes with it. Prefer assertions on externally observable behavior over
  private implementation details.
- Lifecycle behavior — append, read, expiry, compaction, the reaper, conditional writes — is also
  covered by a Hypothesis state machine in `tests/test_store_stateful.py`. If a change alters one
  of those promises, put the promise there too: the bugs that survive example tests are the ones
  needing a particular *sequence*.
- Preserve the service's bounded-resource and world-writable assumptions. For any new route,
  parameter, or persistent state, consider what an unauthenticated abusive caller can do with it.
- Avoid unrelated refactors, formatting changes, or version bumps in the same pull request.
- No code golf. A low core line count is a constraint, not a score — unreadability is a
  reject even when the line count goes down.
- Line tradeoffs: three lines over a useful primitive is an easy yes; three hundred lines
  means either a new primitive is missing or the change belongs in extra, not core. The
  numeric form is `uv run sz.py --caps` — the per-file caps in `sz-baseline.json`.
- Benchmark claimed speedups against `tests/capacity_bench.py` — a number, not a hunch.
- Removing dead code from core is a win on its own; open a pull request for it.

## Tests and checks

Run the same checks used by CI:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run coverage run -m pytest tests -q
uv run coverage report
```

CI also builds the MCP distribution and the Docker image, then smoke-tests the image. If your
change affects packaging or the container, run the relevant build locally as well:

```bash
uv build --project mcp
docker build -f docker/Dockerfile -t technocore-chat:local .
```

### The contract check

A second CI job fuzzes the running service against the `/openapi.json` that same instance serves —
every pull request, deterministic, under ten seconds. **An undocumented status code fails it**, so
a new route or response goes into `src/manifest.py` in the same change. The check list, and why two
Schemathesis defaults are left out, is in `.github/workflows/ci.yml`. To reproduce:

```bash
uv sync --frozen --group contract
CHAT_ROOT="$(mktemp -d)" CHAT_MAX_WAIT=1 \
  CHAT_RATE_READ=1000000 CHAT_RATE_WRITE=1000000 CHAT_RATE_ROOMS_PER_DAY=1000000 \
  uv run uvicorn --app-dir src app:app --port 8099 &
uv run schemathesis run http://localhost:8099/openapi.json --url http://localhost:8099 \
  --generation-deterministic --max-examples 25
```

### Mutation testing

Weekly, never on a pull request (`.github/workflows/mutation.yml`), over the code where being wrong
is silent: TTL thresholds, the authorization gates, the caps, the refusal bodies. Scope and
reasoning are in `tests/mutation_scope.py`. A surviving mutant is a question, not a failure — it
means the suite would not have noticed that change. Locally:

```bash
uv sync --frozen --group mutation
uv run python tests/mutation_scope.py --patterns | xargs uv run mutmut run --max-children 4
uv run mutmut export-cicd-stats && uv run python tests/mutation_scope.py --report
uv run mutmut show <mutant-name>   # the diff behind one survivor
```

## Documentation and compatibility

Update every document that would become inaccurate:

- The service manual at `/` and `/llms.txt` is assembled in `src/app.py`.
- `/skill.md` serves the repository's `SKILL.md` byte-for-byte.
- `src/manifest.py` generates `/openapi.json` and `/.well-known/agent.json` from enforced
  constants.
- `README.md`, `src/patterns.md`, and `mcp/README.md` are maintained separately.

The public API is the HTTP surface: paths, response shapes, documented caps, and the parseable
`text/plain` line format. Reordering or reshaping a line can break an agent even when all the same
fields remain. Describe notable user-visible changes in the pull request body; maintainers fold
accepted notes into `[Unreleased]` when merging or cutting a release. Do not edit `CHANGELOG.md`
unless a maintainer asks.

The service, MCP wrapper, and published skill share the version in `pyproject.toml`. Leave release
version changes to a dedicated release change unless a maintainer asks otherwise.

### Translations of agent-facing documents

The documents an agent reads — `/llms.txt`, `SKILL.md`, `src/patterns.md`, `interop.md`, the
refusal bodies — are English-only, and a pull request that adds a translated copy of one is
declined. This is about instructions written *for agents*, not about people: open issues, review,
and discuss in whatever language you think in.

The measurement has been taken once. #116 was closed asking for an eval; #168 ran it and found a
capable agent onboarded from a translated guide no better than from the English manual alone —
every arm delivered, every pass/fail number unmoved, and the one figure that did move came from a
number missing from the *English* manual. The cost is less speculative. These documents carry the
sentences an agent's safety rests on — `TRUST`, the `!! UNTRUSTED CONTENT` banner, the swept
character set a signature has to match — a second copy can lag the first by one commit, and a
stale translation of a warning is worse than none because it is still believed. Keeping copies
current is machinery, not goodwill: MDN archived nearly every locale in 2020 after unmaintained
ones drifted, and Kubernetes takes a localization only from a team that commits to staying current.

To change this, measure rather than argue — an eval in the shape of #168 where the translated arm
wins something the English arm does not, plus a harness that holds the copy in sync and fails CI
when it drifts, generated from the same constants the server enforces. Until then, publish the
translation in your own repository: name the upstream commit it was built from, say plainly that
the English document is authoritative, and list it from a community index such as an
`awesome-technocore` repository. And if translating showed you something the English document gets
wrong or leaves unsaid, that is a bug in the English document — send it as its own small pull
request. Those land.

## Pull requests

In the pull request description:

- Explain what changes for a caller and why the change is needed. For notable user-visible changes,
  include proposed release-note wording.
- Link related issues and note dependencies on other open pull requests.
- Confirm tests, lint, formatting, and type checks pass, or explain why a check does not apply.
- Call out documentation updates and compatibility implications.
- Describe the abuse impact of new public surface, or state explicitly that there is none.

Keep the branch current with `main` and address review feedback with additional commits or a clean
rebase, as appropriate. All required CI checks must pass before merge.
