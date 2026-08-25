"""Run: uv run --group dev python -m pytest tests

The global note cap used to be enforced by walking every namespace on every new note, so a
create cost O(all notes) while the notes were growing. `.notes-count` replaced that walk.
Two things have to hold, and the second is the one that would actually hurt if it broke:
the cost must stop scaling with the store, and the cap must still bind *exactly* — a cached
count that drifts low lets the cap be breached, which is worse than the walk it replaced.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")


def _scandir_calls(monkeypatch, work) -> int:
    """How many directories `work` reads. The unit that matters: the old code opened one
    per namespace, so this number grew with the store."""
    import store

    calls = 0
    real = os.scandir

    def counting(path):
        nonlocal calls
        calls += 1
        return real(path)

    monkeypatch.setattr(store.os, "scandir", counting)
    work()
    monkeypatch.setattr(store.os, "scandir", real)
    return calls


def _seed(root: Path, namespaces: int) -> None:
    import store

    for n in range(namespaces):
        store.note_set(root, f"ns{n}", "seed", "v")


@pytest.mark.parametrize("namespaces", [4, 60])
def test_a_new_note_reads_the_same_number_of_directories_at_any_store_size(
    tmp_path, monkeypatch, namespaces
):
    """Parametrised rather than looped so a failure names the size it failed at. The count
    must be identical for both, which is the whole claim — see the assertion below."""
    import store

    root = tmp_path / f"store{namespaces}"
    _seed(root, namespaces)
    (root / ".reaped").touch()  # reap is throttled; measure the create path, not a reap

    fresh = store.note_path(root, "ns0", "brand-new")
    reads = _scandir_calls(monkeypatch, lambda: store._check_note_capacity(root, fresh))
    (tmp_path / f"reads{namespaces}.txt").write_text(str(reads))
    # One directory: the caller's own namespace, for the per-namespace cap. The global cap
    # reads a file instead of walking. Two would already mean the global walk is back.
    assert reads == 1, f"{namespaces} namespaces cost {reads} directory reads, expected 1"


def test_the_count_survives_a_lost_file_by_walking(tmp_path) -> None:
    """The fallback is the safety property: anything wrong with the file must degrade to
    the old behaviour — the exact count, paid for by walking — and never to a wrong number.
    A create after the loss must also leave the file correct again."""
    import store

    _seed(tmp_path, 5)
    assert store._note_count(tmp_path) == 5

    (tmp_path / store.NOTES_FILE).unlink()
    assert store._note_count(tmp_path) == 5, "a missing count must be rebuilt by walking"

    (tmp_path / store.NOTES_FILE).write_text("not a number")
    assert store._note_count(tmp_path) == 5, "a malformed count must be rebuilt by walking"

    (tmp_path / store.NOTES_FILE).write_text("-3")
    assert store._note_count(tmp_path) == 5, "a negative count must be rebuilt by walking"

    store.note_set(tmp_path, "ns0", "another", "v")
    assert store._note_count(tmp_path) == 6
    assert int((tmp_path / store.NOTES_FILE).read_text()) == 6


def test_a_reap_reconciles_a_drifted_count(tmp_path, monkeypatch) -> None:
    """Drift is bounded by one reap interval rather than by hope. Writing a deliberately
    wrong count and running a reap must restore the truth — this is what keeps a lost
    increment (an unclean shutdown under CHAT_FSYNC=0) from being permanent."""
    import store

    _seed(tmp_path, 3)
    (tmp_path / store.NOTES_FILE).write_text("999")
    assert store._note_count(tmp_path) == 999, "premise: the bogus count is being read"

    monkeypatch.setattr(store, "REAP_EVERY", 0)  # due now, rather than in five minutes
    store._reap(tmp_path)
    assert store._note_count(tmp_path) == 3


# --------------------------------------------------------------------------- the cap

# A worker: create notes as fast as it can into one shared root, and report how many the
# store accepted. Run as a separate *process* because that is the thing being tested —
# production runs `uvicorn --workers 3`, so the gate has to hold across processes, and
# threads in one interpreter would not exercise the flock at all.
WORKER = """
import sys, json
sys.path.insert(0, {src!r})
import store
root, tag, attempts = sys.argv[1], sys.argv[2], int(sys.argv[3])
made = 0
for i in range(attempts):
    try:
        store.note_set(store.Path(root), "ns-%s-%d" % (tag, i), "k", "v")
        made += 1
    except store.StoreError:
        pass
print(json.dumps(made))
"""


def test_the_global_cap_binds_exactly_under_concurrent_processes(tmp_path) -> None:
    """The regression that would actually hurt. Four processes race to create past a small
    cap; the store must end up holding exactly the cap, never one more.

    One namespace per note, so the *global* cap is the one under test — MAX_NOTES_PER_NS is
    MAX_ROOMS, so workers sharing a namespace hit the per-namespace cap first and the global
    one is never reached.

    An off-by-one here is invisible on a quiet store and shows up as a breached cap under
    exactly the load the cap exists for, so it is worth the process spawns.
    """
    import store

    cap = 64
    # MAX_NOTES_TOTAL is a multiple of MAX_ROOMS, so the room cap that lands the global cap
    # exactly on `cap` is derived from the live constants rather than written out. Hard-
    # coding the multiplier here meant that raising it silently retargeted this test at a
    # cap four times what the name says, with the workers never reaching it and the
    # assertions below passing on an untested store.
    per_room = store.MAX_NOTES_TOTAL // store.MAX_ROOMS
    assert cap % per_room == 0, f"cap {cap} is not reachable at {per_room} notes per room"
    script = tmp_path / "worker.py"
    script.write_text(WORKER.format(src=SRC))
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("CHAT_")},
        "CHAT_MAX_ROOMS": str(cap // per_room),
    }
    root = tmp_path / "shared"
    root.mkdir()

    workers = [
        subprocess.Popen(
            [sys.executable, str(script), str(root), str(w), "20"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for w in range(4)
    ]
    accepted = 0
    for worker in workers:
        out, err = worker.communicate(timeout=120)
        assert worker.returncode == 0, f"worker failed: {err}"
        accepted += json.loads(out)

    on_disk = store._count_notes(root)
    assert on_disk == accepted, "every accepted write must be a note that exists"
    assert on_disk == cap, f"cap is {cap}, store holds {on_disk}"
    # …and the file agrees with the disk, or the next process starts from a wrong number.
    assert store._note_count(root) == cap


def test_the_global_cap_is_sized_against_the_disk_it_costs(tmp_path) -> None:
    """The cap is a disk number, so the arithmetic that justifies it is worth pinning.

    MAX_NOTES_TOTAL went 8 * MAX_ROOMS -> 32 * MAX_ROOMS to hold ~100k identity notes. What
    makes that affordable is stated in the source as a worst case, and a worst case nobody
    recomputes is how a cap gets raised past the volume it was sized for. Both halves are
    asserted: the reserved-namespace floor it must stay above, and the disk ceiling it must
    stay under.
    """
    import store

    reserved = (store.TOPIC_NS, store.OWNERS_NS, store.ALLOW_NS, store.NONCE_NS)
    assert store.MAX_NOTES_TOTAL >= len(reserved) * store.MAX_ROOMS, "reserved floor"

    worst_case = store.MAX_NOTES_TOTAL * store.MAX_VALUE_CHARS
    assert worst_case == 1342177280, f"1.25 GiB is the documented figure, got {worst_case}"
    # Notes and rooms share one volume, and the room budget is the number a deployment is
    # told to provision. Notes at a quarter of it is the trade the comment argues for; a
    # note cap that outgrew the room budget would be a re-provisioning, not a constant.
    assert worst_case < store.MAX_TOTAL_ROOM_BYTES
    assert worst_case * 4 == store.MAX_TOTAL_ROOM_BYTES, "notes = a quarter of the rooms"


def test_the_refusal_still_fires_at_the_global_cap(tmp_path, monkeypatch) -> None:
    """Raising the cap must move the refusal, not remove it. Small caps rather than 163,840
    real notes, exactly as the existing capacity tests do — what is under test is that the
    create path compares the *cached* count against whatever MAX_NOTES_TOTAL says, so the
    refusal has to arrive on the note after the last one the cap allows and name that cap.
    """
    import store

    cap = 6
    monkeypatch.setattr(store, "MAX_NOTES_TOTAL", cap)
    for i in range(cap):
        store.note_set(tmp_path, f"ns{i}", "k", "v")
    assert store._note_count(tmp_path) == cap, "the cache must track the creates it gated"

    with pytest.raises(store.StoreError, match=rf"note limit reached \({cap} across all"):
        store.note_set(tmp_path, "ns-over", "k", "v")
    # Refused on a new name only: the cap never silences a note somebody already owns.
    store.note_set(tmp_path, "ns0", "k", "v2")
    assert store._note_count(tmp_path) == cap, "an overwrite is not a create"


def test_the_cached_count_survives_reap_and_create_interleaving(tmp_path, monkeypatch) -> None:
    """Two writers of one number: creates increment it, reaps rewrite it from a walk. Run
    them alternately and the cache must equal the walk at every step.

    The failure this catches is a reap that rewrites a figure counted *before* its own
    deletions, or a create whose increment lands on a value a reap has since replaced —
    either leaves the cache permanently off by the notes made in that window, and a count
    that drifts low breaches the cap silently.
    """
    import store

    monkeypatch.setattr(store, "REAP_EVERY", 0)  # every pass is due, so they really alternate
    expected = 0
    for round_ in range(6):
        for n in range(3):
            store.note_set(tmp_path, f"ns{round_}", f"k{n}", "v")
            expected += 1
            assert store._note_count(tmp_path) == expected, f"after create {round_}.{n}"
        store._reap(tmp_path)
        # Nothing here is IDLE_SECONDS old, so a reap deletes nothing and the walk it writes
        # must agree with the increments — a reap is not allowed to lose a concurrent create.
        assert store._note_count(tmp_path) == expected, f"after reap {round_}"
        assert store._count_notes(tmp_path) == expected, "and the file must match the disk"


def test_a_stale_cache_over_admits_by_at_most_the_drift_a_reap_clears(
    tmp_path, monkeypatch
) -> None:
    """The cost of caching, stated as a bound and then held to it.

    A lost increment (an unclean shutdown under CHAT_FSYNC=0) leaves the count low, and a
    low count admits notes the cap should refuse. The claim in the source is that this is
    survivable because it is *bounded*: over-admission can never exceed the drift, and the
    next reap — at most REAP_EVERY away — rewrites the truth and the cap binds again. An
    unbounded version of this bug looks identical on a quiet store.
    """
    import store

    cap = 10
    drift = 3
    monkeypatch.setattr(store, "MAX_NOTES_TOTAL", cap)
    for i in range(cap):
        store.note_set(tmp_path, f"ns{i}", "k", "v")
    with pytest.raises(store.StoreError, match="across all namespaces"):
        store.note_set(tmp_path, "ns-full", "k", "v")

    # Lose `drift` increments. The reap marker is fresh from the seeding above, so nothing
    # reconciles until the reap this test runs itself — which is the window being measured.
    (tmp_path / store.NOTES_FILE).write_text(str(cap - drift))
    admitted = 0
    for i in range(drift + 5):
        try:
            store.note_set(tmp_path, f"ns-stale{i}", "k", "v")
            admitted += 1
        except store.StoreError:
            break
    assert admitted == drift, f"drift of {drift} admitted {admitted} — the overshoot is unbounded"
    assert store._count_notes(tmp_path) == cap + drift

    # …and the interval ends. The reap walks, writes the real figure, and the cap is hard
    # again at a store that is now genuinely over it.
    monkeypatch.setattr(store, "REAP_EVERY", 0)
    store._reap(tmp_path)
    assert store._note_count(tmp_path) == cap + drift
    with pytest.raises(store.StoreError, match="across all namespaces"):
        store.note_set(tmp_path, "ns-after-reap", "k", "v")
