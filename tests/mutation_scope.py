"""What the periodic mutation run covers, and how it reports.

Run: uv run --group mutation python tests/mutation_scope.py --patterns
     uv run --group mutation python tests/mutation_scope.py --report

Mutation testing asks the only question coverage cannot: not "did a test execute this
line" but "would a test have *noticed* if this line were wrong". A green suite over a
mutated `>` that should be `>=` is a suite that measured nothing about that boundary.

It is scoped, and deliberately not run on every pull request. `src/` carries ~3600
mutants; a full pass is tens of minutes and most of it is noise — a mutated log string or
a reordered dict literal is not a defect anyone will ever ship. What is worth the machine
time is the code where being subtly wrong is expensive and being wrong is silent:

  ttl            An off-by-one in an idle threshold does not fail; it quietly keeps data
                 a week too long or deletes it a day too early, and either way nobody
                 finds out from a stack trace. The whole retention promise is four
                 comparisons.
  authorization  Every gate here fails *closed* by design, and a mutant that turns one
                 into fail-open is exactly the change no test on the happy path can see.
                 A signed lane that verifies nothing still returns 200.
  caps           These are the only thing standing between an anonymous, world-writable
                 service and its disk. A cap compared with the wrong operator holds right
                 up to the moment it matters.
  guidance       The refusal bodies are the service's real documentation for an agent that
                 already got something wrong — /llms.txt is what it reads *before*. A test
                 that asserts only the status code lets the correction rot.

The patterns are mutmut's mutant names: `<module>.x_<function>__mutmut_<n>`, where a
private `_reap` becomes `x__reap`. Grouping by theme rather than by module is the point —
`caps` reaches into three files, and a reader asking "is the rate limiter covered" should
not have to know which one it lives in.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCOPE: dict[str, tuple[str, ...]] = {
    "ttl": (
        "store.x__cutoff__*",  # what "expired" means for an e- room
        "store.x__expired__*",  # …and the per-record comparison behind it
        "store.x__reapable__*",  # the idle and stillborn thresholds
        "store.x__stillborn__*",  # "one message and nobody answered"
        "store.x__guards_a_live_room__*",  # a guard note outlives the idle rule
        "store.x__reap__*",  # the pass itself, including the recheck under the lock
        "store.x__compact__*",  # the ring, and the expiry that rides it
    ),
    "authorization": (
        "app.x__room_write_gate__*",  # mailboxes, owned rooms, allow-lists
        "app.x__note_write_gate__*",  # the two ownership namespaces
        "app.x__signer__*",  # nonce shape, then the signature
        "app.x__burn_nonce__*",  # single-use, by compare-and-set
        "app.x__allowed_keys__*",
        "app.x__reject_if_events_room__*",  # the server-written discovery log
        "store.x__last_nonce__*",  # replay protection inside the room's tail
        "didkey.x_public_key__*",  # the parse that decides what a did:key even is
        "didkey.x_is_did__*",
        "didkey.x_verify__*",  # fails closed or it means nothing
    ),
    "caps": (
        "store.x__check_room_capacity__*",
        "store.x__check_note_capacity__*",
        "store.x__check_capacity__*",
        "store.x__ring_limit__*",  # the full ring, or the floor under pressure
        "store.x_room_bytes_used__*",
        "store.x_clean_text__*",  # the character caps, and the invisible-character sweep
        "app.x_take__*",  # the rate limiter
        "app.x_refund__*",
        "app.x__room_create_gate__*",  # new rooms per IP per day
        "app.x_read_json__*",  # the body cap, on both the header and the stream
    ),
    "guidance": (
        "app.x_on_not_found__*",  # the route map a wrong URL gets back
        "app.x_on_method_not_allowed__*",
        "app.x_allowed_methods__*",  # …and the Allow header it has to carry
        "app.x_on_bad_input__*",
        "app.x_on_conflict__*",  # the current value, and what to do with it
        "app.x_limited__*",  # the bucket, the refill rate, the retry delay
        "app.x_budget_note__*",
    ),
}

# Statuses that mean the run did not do its job, as opposed to finding something. A
# survivor is a question for a human; these are a broken harness, and the difference has
# to be visible in the exit code or the report is decoration.
BROKEN = ("suspicious", "segfault", "no_tests", "check_was_interrupted_by_user")

STATS = Path("mutants/mutmut-cicd-stats.json")


def patterns() -> list[str]:
    return [pattern for group in SCOPE.values() for pattern in group]


def _survivors() -> list[str] | None:
    """The scoped mutants no test noticed, or None if `mutmut results` could not be read.

    None rather than an empty list, because the two mean opposite things and this report is
    the only thing anyone reads: "nothing survived" and "the tool that lists the survivors
    did not run" must never render the same.
    """
    result = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return sorted(
        line.strip().split(":")[0]
        for line in result.stdout.splitlines()
        if line.strip().endswith(": survived")
    )


def report() -> int:
    """Render the run as markdown and return the exit code the job should use."""
    stats = json.loads(STATS.read_text(encoding="utf-8"))
    checked = stats["killed"] + stats["survived"]
    score = (100 * stats["killed"] / checked) if checked else 0.0
    broken = {name: stats[name] for name in BROKEN if stats.get(name)}
    survivors = _survivors()

    # The survivor section decides one more way the run can be broken, so it is rendered
    # first and spliced in below whatever `broken` ends up saying.
    if not stats["survived"]:
        body = ["Nothing survived: every mutant in scope was caught by a test.", ""]
    elif survivors is None:
        broken["survivors_unreadable"] = stats["survived"]
        body = [
            f"{stats['survived']} mutant(s) survived and `mutmut results` could not be "
            "read, so this report cannot say which. Re-run it from the same working "
            "directory as the run.",
            "",
        ]
    else:
        body = [
            "### Survivors",
            "",
            "Each one is a change to this service that the suite would not have noticed. "
            "Some are genuinely untestable — an equivalent mutant, a boundary no caller "
            "can reach — and the rest are a missing test. `mutmut show <name>` prints the "
            "diff behind one.",
            "",
            "```",
            *survivors,
            "```",
        ]

    out = [
        "## Scoped mutation run",
        "",
        f"**{stats['killed']} killed, {stats['survived']} survived** "
        f"of {checked} mutants checked — {score:.0f}% caught.",
        "",
        "Scope: " + ", ".join(f"`{theme}`" for theme in SCOPE),
        "",
    ]
    if stats.get("timeout"):
        out += [
            f"{stats['timeout']} mutant(s) timed out. A timeout is usually a mutated loop "
            "bound rather than a harness fault, and counts as killed nowhere — worth a "
            "look if the number moves.",
            "",
        ]
    if broken:
        out += [
            "### The run itself is broken",
            "",
            "Not findings. Mutants were generated and never properly judged, so the "
            "numbers above understate what is uncovered:",
            "",
            *(f"- `{name}`: {count}" for name, count in broken.items()),
            "",
        ]
    out += body

    print("\n".join(out))
    return 1 if broken else 0


if __name__ == "__main__":
    if "--patterns" in sys.argv:
        print("\n".join(patterns()))
    elif "--report" in sys.argv:
        raise SystemExit(report())
    else:
        raise SystemExit(f"usage: {sys.argv[0]} [--patterns | --report]")
