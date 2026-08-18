from __future__ import annotations

import os
from pathlib import Path
import runpy
import signal
import stat
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).parents[1]
DRIVER = ROOT / "scripts" / "backfill-fleet-sot.py"
PARSER = ROOT / "template" / ".scripts" / "lib" / "parse-fleet-env.py"


@pytest.mark.parametrize("interrupt_signal", [signal.SIGTERM, signal.SIGKILL])
def test_durable_batch_journal_recovers_after_first_exchange_process_death(
    tmp_path: Path,
    interrupt_signal: signal.Signals,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"first-original\n")
    second.write_bytes(b"second-original\n")
    journal = tmp_path / ".pjangler-backfill-transaction.json"
    harness = tmp_path / "interrupt-batch.py"
    harness.write_text(
        """from pathlib import Path
import os
import runpy
import signal
import sys

driver, parser, journal, first, second, signal_number = sys.argv[1:]
namespace = runpy.run_path(driver)
real_exchange = namespace["load_parser"](Path(parser))._exchange_paths
calls = 0

def exchange_then_die(left, right):
    global calls
    real_exchange(left, right)
    calls += 1
    if calls == 1:
        os.kill(os.getpid(), int(signal_number))

plan = namespace["BatchPlan"](exchange_then_die, Path(journal))
plan.plan_file(Path(first), b"first-updated\\n")
plan.plan_file(Path(second), b"second-updated\\n")
plan.apply()
""",
        encoding="utf-8",
    )

    interrupted = subprocess.run(
        [
            sys.executable,
            str(harness),
            str(DRIVER),
            str(PARSER),
            str(journal),
            str(first),
            str(second),
            str(int(interrupt_signal)),
        ],
        capture_output=True,
        check=False,
    )

    assert interrupted.returncode != 0
    namespace = runpy.run_path(str(DRIVER))
    exchange = namespace["load_parser"](PARSER)._exchange_paths
    namespace["BatchPlan"].recover_pending(journal, exchange)
    namespace["BatchPlan"].recover_pending(journal, exchange)

    assert first.read_bytes() == b"first-original\n"
    assert second.read_bytes() == b"second-original\n"
    assert not journal.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "first.txt",
        "interrupt-batch.py",
        "second.txt",
    ]


def test_durable_batch_journal_recovers_new_file_after_link_process_death(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing.txt"
    created = tmp_path / "created.txt"
    existing.write_bytes(b"existing-original\n")
    journal = tmp_path / ".pjangler-backfill-transaction.json"
    harness = tmp_path / "interrupt-new-file.py"
    harness.write_text(
        """from pathlib import Path
import os
import runpy
import signal
import sys

driver, parser, journal, existing, created = sys.argv[1:]
namespace = runpy.run_path(driver)
loaded_parser = namespace["load_parser"](Path(parser))
real_link = os.link

def link_then_die(left, right, *args, **kwargs):
    result = real_link(left, right, *args, **kwargs)
    if Path(right).name == Path(created).name:
        os.kill(os.getpid(), signal.SIGKILL)
    return result

namespace["os"].link = link_then_die
plan = namespace["BatchPlan"](loaded_parser._exchange_paths, Path(journal))
plan.plan_file(Path(created), b"created-content\\n")
plan.plan_file(Path(existing), b"existing-updated\\n")
plan.apply()
""",
        encoding="utf-8",
    )

    interrupted = subprocess.run(
        [
            sys.executable,
            str(harness),
            str(DRIVER),
            str(PARSER),
            str(journal),
            str(existing),
            str(created),
        ],
        capture_output=True,
        check=False,
    )

    assert interrupted.returncode == -signal.SIGKILL
    namespace = runpy.run_path(str(DRIVER))
    exchange = namespace["load_parser"](PARSER)._exchange_paths
    namespace["BatchPlan"].recover_pending(journal, exchange)
    namespace["BatchPlan"].recover_pending(journal, exchange)

    assert existing.read_bytes() == b"existing-original\n"
    assert not created.exists()
    assert not journal.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "existing.txt",
        "interrupt-new-file.py",
    ]


def test_recovery_is_idempotent_when_recovery_itself_dies_after_exchange(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"first-original\n")
    second.write_bytes(b"second-original\n")
    journal = tmp_path / ".pjangler-backfill-transaction.json"
    initial = tmp_path / "initial-crash.py"
    initial.write_text(
        """from pathlib import Path
import os, runpy, signal, sys
driver, parser, journal, first, second = sys.argv[1:]
namespace = runpy.run_path(driver)
real_exchange = namespace["load_parser"](Path(parser))._exchange_paths
calls = 0
def exchange_then_die(left, right):
    global calls
    real_exchange(left, right)
    calls += 1
    if calls == 1: os.kill(os.getpid(), signal.SIGKILL)
plan = namespace["BatchPlan"](exchange_then_die, Path(journal))
plan.plan_file(Path(first), b"first-updated\\n")
plan.plan_file(Path(second), b"second-updated\\n")
plan.apply()
""",
        encoding="utf-8",
    )
    assert subprocess.run(
        [sys.executable, str(initial), str(DRIVER), str(PARSER), str(journal), str(first), str(second)],
        check=False,
    ).returncode == -signal.SIGKILL

    recovery = tmp_path / "recovery-crash.py"
    recovery.write_text(
        """from pathlib import Path
import os, runpy, signal, sys
driver, parser, journal = sys.argv[1:]
namespace = runpy.run_path(driver)
real_exchange = namespace["load_parser"](Path(parser))._exchange_paths
def exchange_then_die(left, right):
    real_exchange(left, right)
    os.kill(os.getpid(), signal.SIGKILL)
namespace["BatchPlan"].recover_pending(Path(journal), exchange_then_die)
""",
        encoding="utf-8",
    )
    assert subprocess.run(
        [sys.executable, str(recovery), str(DRIVER), str(PARSER), str(journal)],
        check=False,
    ).returncode == -signal.SIGKILL

    namespace = runpy.run_path(str(DRIVER))
    namespace["BatchPlan"].recover_pending(
        journal,
        namespace["load_parser"](PARSER)._exchange_paths,
    )
    namespace["BatchPlan"].recover_pending(
        journal,
        namespace["load_parser"](PARSER)._exchange_paths,
    )
    assert first.read_bytes() == b"first-original\n"
    assert second.read_bytes() == b"second-original\n"
    assert not journal.exists()


def test_transaction_directory_lock_rejects_a_second_live_process(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    holder = tmp_path / "hold-lock.py"
    holder.write_text(
        """from pathlib import Path
import runpy, sys, time
driver, directory, ready, release = sys.argv[1:]
namespace = runpy.run_path(driver)
with namespace["TransactionLock"](Path(directory)):
    Path(ready).write_text("locked")
    while not Path(release).exists(): time.sleep(0.01)
""",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(holder), str(DRIVER), str(tmp_path), str(ready), str(release)],
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "lock holder did not become ready"
        namespace = runpy.run_path(str(DRIVER))
        with pytest.raises(namespace["BackfillError"], match="another backfill transaction is active"):
            with namespace["TransactionLock"](tmp_path):
                raise AssertionError("second process unexpectedly acquired the fleet lease")
    finally:
        release.write_text("release")
        assert process.wait(timeout=5) == 0


def test_created_directory_mode_is_exact_despite_umask(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(DRIVER))
    exchange = namespace["load_parser"](PARSER)._exchange_paths
    destination = tmp_path / "new" / "nested" / "value.txt"
    plan = namespace["BatchPlan"](exchange, tmp_path / ".transaction.json")
    plan.plan_file(destination, b"value\n", parent_mode=0o2750)

    previous_umask = os.umask(0o077)
    try:
        plan.apply()
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o2750
    assert stat.S_IMODE((tmp_path / "new" / "nested").stat().st_mode) == 0o2750
    assert destination.read_bytes() == b"value\n"


def test_journal_publication_failure_rolls_back_prepared_files_and_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(DRIVER))
    batch_plan = namespace["BatchPlan"]
    exchange = namespace["load_parser"](PARSER)._exchange_paths
    journal = tmp_path / ".transaction.json"
    destination = tmp_path / "new" / "nested" / "value.txt"
    plan = batch_plan(exchange, journal)
    plan.plan_file(destination, b"value\n")
    original_write = batch_plan._write_journal_document

    def publish_then_fail(cls, path, payload, exchange_paths, **kwargs):
        original_write(path, payload, exchange_paths, **kwargs)
        if kwargs.get("create"):
            raise OSError("synthetic post-publication failure")

    monkeypatch.setattr(
        batch_plan,
        "_write_journal_document",
        classmethod(publish_then_fail),
    )

    with pytest.raises(OSError, match="post-publication"):
        plan.apply()

    assert not destination.exists()
    assert not (tmp_path / "new").exists()
    assert not journal.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == []


def test_parent_replacement_after_journal_publication_never_escapes_attested_dirfd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(DRIVER))
    batch_plan = namespace["BatchPlan"]
    exchange = namespace["load_parser"](PARSER)._exchange_paths
    intended = tmp_path / "intended"
    displaced = tmp_path / "displaced"
    outside = tmp_path / "outside"
    intended.mkdir()
    outside.mkdir()
    destination = intended / "value.txt"
    destination.write_bytes(b"original\n")
    journal = tmp_path / ".transaction.json"
    plan = batch_plan(exchange, journal)
    plan.plan_file(destination, b"updated\n")
    original_write = batch_plan._write_journal_document
    swapped = False

    def publish_then_swap(cls, path, payload, exchange_paths, **kwargs):
        nonlocal swapped
        original_write(path, payload, exchange_paths, **kwargs)
        if kwargs.get("create") and not swapped:
            intended.rename(displaced)
            intended.symlink_to(outside, target_is_directory=True)
            swapped = True

    monkeypatch.setattr(
        batch_plan,
        "_write_journal_document",
        classmethod(publish_then_swap),
    )

    with pytest.raises(
        namespace["BackfillError"],
        match=r"destination parent (changed|must be a real directory)",
    ):
        plan.apply()

    assert intended.is_symlink()
    assert list(outside.iterdir()) == []
    assert (displaced / "value.txt").read_bytes() == b"original\n"
    assert not journal.exists()
    assert not list(displaced.glob(".*.pjangler-backfill-*"))


def test_crash_recovery_removes_newly_created_directories(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_bytes(b"original\n")
    created = tmp_path / "new" / "nested" / "created.txt"
    journal = tmp_path / ".transaction.json"
    harness = tmp_path / "interrupt-created-directory.py"
    harness.write_text(
        """from pathlib import Path
import os, runpy, signal, sys
driver, parser, journal, existing, created = sys.argv[1:]
namespace = runpy.run_path(driver)
real_exchange = namespace["load_parser"](Path(parser))._exchange_paths
def exchange_then_die(left, right):
    real_exchange(left, right)
    os.kill(os.getpid(), signal.SIGKILL)
plan = namespace["BatchPlan"](exchange_then_die, Path(journal))
plan.plan_file(Path(created), b"created\\n")
plan.plan_file(Path(existing), b"updated\\n")
plan.apply()
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(harness),
            str(DRIVER),
            str(PARSER),
            str(journal),
            str(existing),
            str(created),
        ],
        check=False,
    )
    assert result.returncode == -signal.SIGKILL

    namespace = runpy.run_path(str(DRIVER))
    namespace["BatchPlan"].recover_pending(
        journal,
        namespace["load_parser"](PARSER)._exchange_paths,
    )
    assert existing.read_bytes() == b"original\n"
    assert not (tmp_path / "new").exists()
    assert not journal.exists()
