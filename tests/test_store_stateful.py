"""Hypothesis state machines over the store's lifecycle.

Run: uv run --group dev python -m pytest tests/test_store_stateful.py

Why a state machine and not more example tests. Every rule in `store.py` is easy to state
and easy to satisfy on its own; what is hard is satisfying all of them at once, in an order
nobody wrote a test for. Compaction rewrites a room, expiry hides records the file still
holds, the reaper deletes the file outright, and a conditional note write has to see the
value the last write left — so the interesting bugs live in the *sequences*: a seq reused
after a compaction that kept nothing, a cursor that skips a record because expiry and
`since` disagree about which end of the file to stop at, a guard note reclaimed while the
room it guards is still busy. Hypothesis generates those orderings; these machines say what
must be true after every one of them.

The model deliberately does not predict compaction byte-for-byte. Compaction's contract is
"keep the newest records that fit, drop the rest, never reorder and never renumber" — so
the machine reads back what survived and holds *that* to the contract, rather than
re-implementing `_compact` and testing the copy.

Time is modelled by moving the whole store into the past — every file's mtime *and* every
record's `ts`. Both, because the two lifetimes read different clocks: the reaper stats the
file, the `e-` class parses the record. Ageing one without the other produces a store that
has never existed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import store  # noqa: E402

# Every threshold is compared against a real clock — `time.time()` at the moment the
# reaper or the read runs — while the model counts whole seconds of simulated ageing. The
# two differ by however long the step took, so an age that lands *on* a threshold is
# genuinely ambiguous. Assertions are one-sided outside this band and skipped inside it:
# a test that flakes on its own timing teaches nothing about the code.
GUARD_SECONDS = 5

# Small enough that a handful of appends rotates a room, so compaction is reached inside a
# default-length run instead of being a case only a 10 MiB test could see. A record is
# ~90-140 bytes, so a room rotates every three or four messages.
RING_BYTES = 512

# Three rooms, not one per class. Every extra name divides the same step budget, and a run
# that touches five rooms twice each never reaches compaction; `store.py` distinguishes
# exactly two things about a name here — whether it is ephemeral and whether it is the
# events room — so one of each plus a plain room covers the behaviour and leaves the steps
# to go deep. Which names are unlisted or mailboxes is app.py's business, not the store's.
ROOMS = ("lobby", "e-fast", "d-owned")
NICKS = ("alice", "bob")
# `room-owners` is a *guard* namespace: its notes are exempt from the idle rule for as long
# as the room they name is still live, which is a rule with no meaning unless a note and a
# room of the same name are both in play. Hence the `d-owned` key.
NOTES = (("plans", "next"), ("topic", "lobby"), ("room-owners", "d-owned"))

# No character here is in INVISIBLE_CATEGORIES, so the value the model records is the value
# the store stores — `test_the_model_and_the_sweep_agree_on_these_values` holds that line
# rather than leaving it to a comment. Built to be non-empty and unpadded by construction
# rather than by `.filter()`: `clean_text` trims the ends and refuses what is left empty,
# and a filter that rejects most of what it is offered spends the budget on retries.
_VISIBLE = "abcdefghijklmnopqrstuvwxyz0123456789-_.,:!?éü日🙂"
SAFE_TEXT = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from(_VISIBLE),
    st.text(alphabet=_VISIBLE + " ", max_size=23).map(str.rstrip),
)


def _shift_records(path: Path, seconds: int) -> None:
    """Rewrite a room file with every `ts` moved `seconds` into the past."""
    lines = []
    for raw in path.read_bytes().splitlines():
        if not raw.strip():
            continue
        rec = json.loads(raw)  # every line here was written by store.append
        stamped = datetime.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        rec["ts"] = (stamped - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        lines.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    path.write_bytes(b"".join(lines))


def _age_world(root: Path, seconds: int) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        # Stat first. Rewriting a room to shift its records also stamps it with the
        # current mtime, so reading the mtime afterwards would age the file from *now*
        # instead of from where it already was — and every advance after the first would
        # silently reset the file's age to one step. The reaper reads mtime, so the whole
        # idle half of this machine would have been testing nothing.
        stat = path.stat()
        if path.suffix == ".jsonl":
            _shift_records(path, seconds)
        os.utime(path, (stat.st_atime - seconds, stat.st_mtime - seconds))


def _on_disk(root: Path, room: str) -> list[dict]:
    path = store.room_path(root, room)
    if not path.exists():
        return []
    return [json.loads(raw) for raw in path.read_bytes().splitlines() if raw.strip()]


class StoreLifecycle(RuleBasedStateMachine):
    """append / read / expire / compact / reap / CAS, in whatever order Hypothesis picks."""

    def __init__(self) -> None:
        super().__init__()
        self.root = Path(tempfile.mkdtemp(prefix="chat-stateful-"))
        tuning = {
            # A ring a few messages wide, so compaction is part of an ordinary run.
            "MAX_ROOM_BYTES": RING_BYTES,
            "COMPACT_KEEP_BYTES": RING_BYTES // 2,
            # Every write reaps. The production throttle would make "did this step reap?"
            # a function of wall-clock time, which is the one thing a model must not have
            # to guess; at zero the answer is always yes and the machine can hold the
            # reaper to its rule on every step.
            "REAP_EVERY": 0,
            # Snapshots are a sampled digest of the same numbers, taken on the write path.
            # They would add a second file to age and nothing to check.
            "SNAPSHOT_EVERY": 1 << 30,
        }
        self.saved = {name: getattr(store, name) for name in tuning}
        for name, value in tuning.items():
            setattr(store, name, value)

        # seq assigned so far, per room: the store's own counter, which must never go
        # backwards while the room's file survives.
        self.seq: dict[str, int] = dict.fromkeys(ROOMS, 0)
        # What each surviving seq should say, so compaction can be checked for having
        # dropped records rather than rewritten them.
        self.said: dict[str, dict[int, tuple[str, str]]] = {room: {} for room in ROOMS}
        # Simulated seconds since each record was written, and since each file last was.
        self.record_age: dict[str, dict[int, int]] = {room: {} for room in ROOMS}
        self.room_age: dict[str, int] = dict.fromkeys(ROOMS, 0)
        self.note_age: dict[tuple[str, str], int] = dict.fromkeys(NOTES, 0)
        self.notes: dict[tuple[str, str], str] = {}

    def teardown(self) -> None:
        for name, value in self.saved.items():
            setattr(store, name, value)
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------ the reaper, modelled
    #
    # Both verdict functions answer "gone", "kept", or None — None meaning an age sits
    # close enough to a threshold that the model and the reaper's real clock could
    # disagree, and nothing may be asserted either way. Every caller either acts on a
    # definite verdict or leaves the state to `_resync` to read back off the disk.

    def _room_verdict(self, room: str) -> str | None:
        on_disk = len(self.said[room])
        if not on_disk:
            return None  # nothing there for the reaper to take or to spare
        age = self.room_age[room]
        if abs(age - store.IDLE_SECONDS) <= GUARD_SECONDS:
            return None
        if age > store.IDLE_SECONDS:
            return "gone"
        # The stillborn rule counts what is *on disk*, so a compacted room can become
        # stillborn again — one surviving record and nobody has spoken for a day.
        if on_disk <= store.STILLBORN_MESSAGES:
            if abs(age - store.STILLBORN_SECONDS) <= GUARD_SECONDS:
                return None
            if age > store.STILLBORN_SECONDS:
                return "gone"
        return "kept"

    def _note_verdict(self, key: tuple[str, str]) -> str | None:
        ns, name = key
        if key not in self.notes:
            return None
        age = self.note_age[key]
        if abs(age - store.IDLE_SECONDS) <= GUARD_SECONDS:
            return None
        if age < store.IDLE_SECONDS:
            return "kept"
        # Past its own idle window. A guard note goes only when the room it guards does:
        # an allow-list that expired first would hand write access back to everyone, and a
        # replay counter that expired first would let a captured signed URL re-add a key
        # the owner had revoked. Tied to the room rather than exempt outright, so the state
        # stays bounded — once the room goes, its guards go with it.
        if ns in store.ROOM_GUARD_NS and name in ROOMS:
            return "gone" if not self.said[name] else self._room_verdict(name)
        return "gone"

    def _reap_model(self) -> None:
        """Apply the reaper's rules to the model, wherever the store would have run a pass
        — which, with REAP_EVERY at zero, is before every append and every note write."""
        for room in ROOMS:
            if self._room_verdict(room) == "gone":
                self.seq[room] = 0
                self.said[room].clear()
                self.record_age[room].clear()
        for key in NOTES:
            if self._note_verdict(key) == "gone":
                self.notes.pop(key, None)

    def _resync(self) -> None:
        """Adopt what is actually on disk, having first checked it against the model.

        Anything the model could only guess at — which records compaction kept, whether an
        age inside the guard band tipped the reaper — is read back here rather than
        predicted, so the following steps reason about the store as it now is.
        """
        for room in ROOMS:
            records = _on_disk(self.root, room)
            seqs = [rec["seq"] for rec in records]
            assert seqs == sorted(seqs), f"{room}: records are out of order on disk"
            assert len(set(seqs)) == len(seqs), f"{room}: a seq appears twice on disk"
            if seqs:
                assert seqs == list(range(seqs[0], seqs[-1] + 1)), (
                    f"{room}: compaction left a hole at {seqs}"
                )
                assert seqs[-1] == self.seq[room], (
                    f"{room}: newest on disk is {seqs[-1]}, {self.seq[room]} was assigned"
                )
            for rec in records:
                if rec["seq"] in self.said[room]:
                    assert (rec["from"], rec["text"]) == self.said[room][rec["seq"]], (
                        f"{room}: seq {rec['seq']} was rewritten, not merely retained"
                    )
            kept = set(seqs)
            self.said[room] = {s: v for s, v in self.said[room].items() if s in kept}
            self.record_age[room] = {s: a for s, a in self.record_age[room].items() if s in kept}
            if not seqs and not store.room_path(self.root, room).exists():
                # The file is gone, so the store's counter is gone with it: the next write
                # starts this room over at 1. Nothing else in the store restarts.
                self.seq[room] = 0
        for key in list(self.notes):
            if store.note_get(self.root, *key) is None:
                del self.notes[key]

    # ------------------------------------------------------------------ rules

    @rule(
        room=st.sampled_from(ROOMS),
        nick=st.sampled_from(NICKS),
        texts=st.lists(SAFE_TEXT, min_size=1, max_size=4),
    )
    def say(self, room: str, nick: str, texts: list[str]) -> None:
        """A burst rather than one line, because a step budget spent one message at a time
        never fills a ring: compaction, and the expiry that rides it, are only reachable
        from a room with a history."""
        self._reap_model()
        for text in texts:
            record = store.append(self.root, room, nick, text)
            assert record["seq"] == self.seq[room] + 1, (
                f"{room}: seq jumped from {self.seq[room]} to {record['seq']}"
            )
            self.seq[room] = record["seq"]
            self.said[room][record["seq"]] = (nick, text)
            self.record_age[room][record["seq"]] = 0
            self.room_age[room] = 0
        self._resync()

    @rule(
        room=st.sampled_from(ROOMS),
        limit=st.integers(min_value=1, max_value=8),
        since=st.one_of(st.none(), st.integers(min_value=0, max_value=40)),
    )
    def read(self, room: str, limit: int, since: int | None) -> None:
        view = store.read_messages(self.root, room, limit=limit, since=since)

        assert view["room"] == room
        assert view["count"] == len(view["messages"]) <= limit
        seqs = [m["seq"] for m in view["messages"]]
        assert seqs == sorted(set(seqs)), "a read must be ordered oldest-first, no repeats"
        if since is not None:
            assert all(s > since for s in seqs), "`since` returned something already seen"
        assert view["first_seq"] == (seqs[0] if seqs else None)
        assert view["last_seq"] == (seqs[-1] if seqs else (since or 0))
        for message in view["messages"]:
            assert (message["from"], message["text"]) == self.said[room][message["seq"]]

        expected = self._expected_read(room, limit, since)
        if expected is not None:
            assert seqs == expected, f"{room}: read {seqs}, expected {expected}"

    def _expected_read(self, room: str, limit: int, since: int | None) -> list[int] | None:
        """What the read must return, or None when an age sits on the expiry boundary."""
        ttl = store.EPHEMERAL_TTL_SECONDS if store.is_ephemeral(room) else None
        out: list[int] = []
        for seq in sorted(self.said[room], reverse=True):
            if since is not None and seq <= since:
                break
            if ttl is not None:
                age = self.record_age[room][seq]
                if abs(age - ttl) <= GUARD_SECONDS:
                    return None  # too close to call against a real clock
                if age > ttl:
                    break
            out.append(seq)
            if len(out) >= limit:
                break
        out.reverse()
        return out

    @rule(room=st.sampled_from(ROOMS))
    def last_seq_never_goes_backwards(self, room: str) -> None:
        """The counter is read back off the newest record, so it has to survive everything
        that rewrites the file. It restarts only when the file itself is gone — expiry
        hiding every record is not that, which is why `_compact` keeps the newest one even
        when it is expired."""
        assert store.last_seq(self.root, room) == self.seq[room]

    @rule(key=st.sampled_from(NOTES), value=SAFE_TEXT)
    def write_note(self, key: tuple[str, str], value: str) -> None:
        self._reap_model()
        store.note_set(self.root, *key, value)
        self.notes[key] = value
        self.note_age[key] = 0
        self._resync()

    @rule(key=st.sampled_from(NOTES), value=SAFE_TEXT, use_current=st.booleans())
    def compare_and_set(self, key: tuple[str, str], value: str, use_current: bool) -> None:
        """CAS is the only ordering primitive a note has, and it is what an accumulator or
        an acceptance record is built on: it must win exactly when the value it was handed
        is still there, and lose with the *actual* value attached so the loser can rebase
        without a second read."""
        self._reap_model()
        current = self.notes.get(key)
        expect = current if (use_current and current is not None) else f"stale-{value}"
        try:
            store.note_set(self.root, *key, value, expect=expect)
        except store.StoreConflictError as lost:
            assert expect != current, f"CAS lost against the value it was given: {expect!r}"
            assert lost.current == current, (
                f"409 carried {lost.current!r}, the note holds {current!r}"
            )
        else:
            assert expect == current, f"CAS won against {current!r} while expecting {expect!r}"
            self.notes[key] = value
            self.note_age[key] = 0
        self._resync()

    @rule(key=st.sampled_from(NOTES), value=SAFE_TEXT)
    def create_if_absent(self, key: tuple[str, str], value: str) -> None:
        self._reap_model()
        existed = self.notes.get(key)
        try:
            store.note_set(self.root, *key, value, expect_absent=True)
        except store.StoreConflictError as lost:
            assert existed is not None, "create-if-absent lost against a note that is not there"
            assert lost.current == existed
        else:
            assert existed is None, f"create-if-absent won against an existing {existed!r}"
            self.notes[key] = value
            self.note_age[key] = 0
        self._resync()

    # 61 rather than 60, and so on: an age that lands exactly on a threshold is the one
    # case the guard band has to throw away, so the steps are chosen not to aim at one.
    @rule(
        seconds=st.sampled_from(
            [
                61,
                store.EPHEMERAL_TTL_SECONDS + 61,
                store.STILLBORN_SECONDS + 61,
                # Just short of the idle rule, so one step can put a note past its own
                # window while leaving the room it guards inside its. That gap is the only
                # state in which the guard-note exemption does anything at all, and
                # reaching it by summing smaller steps takes more of them than a run has.
                store.IDLE_SECONDS - 3600,
                store.IDLE_SECONDS + 61,
            ]
        )
    )
    def advance(self, seconds: int) -> None:
        _age_world(self.root, seconds)
        for room in ROOMS:
            self.room_age[room] += seconds
            for seq in self.record_age[room]:
                self.record_age[room][seq] += seconds
        for key in NOTES:
            self.note_age[key] += seconds

    @rule()
    def reap(self) -> None:
        """The reaper is what makes the caps survivable: without it a hard limit only says
        who got there first. It has to take what is genuinely idle and nothing else."""
        expected_rooms = {room for room in ROOMS if self._room_verdict(room) == "gone"}
        survivors = {room for room in ROOMS if self._room_verdict(room) == "kept"}
        expected_notes = {key for key in NOTES if self._note_verdict(key) == "gone"}
        kept_notes = {key for key in self.notes if self._note_verdict(key) == "kept"}

        (self.root / ".reaped").unlink(missing_ok=True)
        store._reap(self.root)

        for room in expected_rooms:
            assert not store.room_path(self.root, room).exists(), f"{room} outlived its idle rule"
        for room in survivors:
            assert store.room_path(self.root, room).exists(), f"{room} was reaped while live"
        for key in expected_notes:
            assert store.note_get(self.root, *key) is None, f"note {key} outlived its idle rule"
        for key in kept_notes:
            assert store.note_get(self.root, *key) == self.notes[key], f"note {key} was reaped"

        self._reap_model()
        self._resync()

    # ------------------------------------------------------------------ invariants

    @invariant()
    def notes_hold_what_was_written(self) -> None:
        for key, value in self.notes.items():
            assert store.note_get(self.root, *key) == value, f"note {key} does not hold {value!r}"

    @invariant()
    def no_room_exceeds_its_ring(self) -> None:
        """Compaction has to leave the file *under* the ring, not merely at it — otherwise
        the next append re-triggers it and every write pays a full rewrite."""
        for path in (self.root / "rooms").glob("*.jsonl"):
            assert path.stat().st_size <= store.MAX_ROOM_BYTES, f"{path.name} is over its ring"

    @invariant()
    def every_record_on_disk_is_one_record(self) -> None:
        """One JSON object per line, always: a torn or fused line loses the record after it
        as well as itself, and the whole read path is built on the line being the frame."""
        for path in (self.root / "rooms").glob("*.jsonl"):
            for raw in path.read_bytes().splitlines():
                if raw.strip():
                    assert store._parse(raw) is not None, f"{path.name}: unparseable {raw[:60]!r}"

    @invariant()
    def lifetime_counters_only_go_up(self) -> None:
        """Nothing else in the store is monotonic — seq dies with its room, compaction drops
        lines, the reaper deletes files — so a digest that reports "messages since" has
        these four and nothing else."""
        counters = store.counters(self.root)
        previous = getattr(self, "_counters", dict.fromkeys(store.COUNTER_KEYS, 0))
        for name in store.COUNTER_KEYS:
            assert counters[name] >= previous[name], f"{name} went backwards"
        self._counters = counters


StoreLifecycle.TestCase.settings = settings(
    max_examples=40,
    stateful_step_count=60,
    deadline=None,
    # Every rule fsyncs, and `advance` rewrites the whole store. Slow by construction, and
    # slow is not the same as wrong.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    # A suite that finds a different bug on every run cannot be bisected, and CI is where
    # this runs. `--hypothesis-seed=random` still explores when someone wants it to.
    derandomize=True,
)

TestStoreLifecycle = StoreLifecycle.TestCase


def test_the_model_and_the_sweep_agree_on_these_values():
    """The machine records the value it sent, not the value the store kept, which is only
    sound while the two are the same string. `clean_text` rewrites anything invisible and
    trims the ends, so the generator is built to produce neither — and this is what keeps
    that true if either the alphabet or the sweep changes."""
    for sample in ("a", "hello world", "é日🙂", "a-b_c.d,e:f!g?h", "0123456789"):
        assert store.clean_text(sample) == sample


@given(SAFE_TEXT)
def test_the_generator_only_produces_text_the_sweep_leaves_alone(sample):
    assert store.clean_text(sample) == sample
