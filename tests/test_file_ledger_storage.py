# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The file-backed ledger and its file agree after an append that fails."""

from __future__ import annotations

import builtins
import json
import os
import threading
from pathlib import Path

import pytest

from nomos.level_1.ledger import (
    FileLedgerStorage,
    LedgerAppendError,
    LedgerChain,
    LedgerEntryType,
)


def _chain(path: Path) -> LedgerChain:
    chain = LedgerChain(storage=FileLedgerStorage(path), chain_id="file-test")
    chain.initialize("test")
    return chain


def _sequences(path: Path) -> list[int]:
    return [json.loads(line)["sequence"] for line in path.read_text().splitlines() if line]


class _FailsOnClose:
    """A file whose close reports an error after the bytes reached the file."""

    def __init__(self, file, written: int | None) -> None:
        self._file = file
        self._written = written

    def __enter__(self):
        return self

    def write(self, text: str) -> int:
        return self._file.write(text if self._written is None else text[: self._written])

    def tell(self) -> int:
        return self._file.tell()

    def __exit__(self, *exc_info) -> None:
        self._file.close()
        raise OSError(5, "Input/output error")


@pytest.mark.parametrize("written", [None, 20], ids=["whole-line", "part-of-a-line"])
def test_failed_append_leaves_nothing_in_the_file_and_a_retry_writes_one_entry(
    tmp_path, monkeypatch, written
):
    path = tmp_path / "ledger.jsonl"
    chain = _chain(path)
    payload = {"intent_id": "intent-1", "final": {"state": "expired"}}
    real_open = builtins.open

    def failing_open(file, mode="r", *args, **kwargs):
        opened = real_open(file, mode, *args, **kwargs)
        return _FailsOnClose(opened, written) if "a" in mode else opened

    with monkeypatch.context() as patched:
        patched.setattr(builtins, "open", failing_open)
        with pytest.raises(OSError, match="Input/output error"):
            chain.append(entry_type=LedgerEntryType.INTENT_REVIEW_RECORDED, payload=payload)

    # The failed entry is in neither place, so the head is the same in both.
    assert _sequences(path) == [0]
    assert chain.length() == 1

    retried = chain.append(entry_type=LedgerEntryType.INTENT_REVIEW_RECORDED, payload=payload)

    assert retried.sequence == 1
    assert _sequences(path) == [0, 1]
    reloaded = FileLedgerStorage(path)
    assert [entry.sequence for entry in reloaded.iterate()] == [0, 1]
    assert reloaded.get_review_by_intent("intent-1").sequence == 1


@pytest.mark.parametrize("written", [None, 20], ids=["whole-line", "part-of-a-line"])
def test_ledger_refuses_appends_after_a_cut_back_that_failed(tmp_path, monkeypatch, written):
    """With the file in an unknown state, another append could repeat a sequence number."""
    path = tmp_path / "ledger.jsonl"
    chain = _chain(path)
    payload = {"intent_id": "intent-1"}
    real_open = builtins.open

    def failing_open(file, mode="r", *args, **kwargs):
        opened = real_open(file, mode, *args, **kwargs)
        return _FailsOnClose(opened, written) if "a" in mode else opened

    def failing_truncate(*_args, **_kwargs):
        raise OSError(30, "Read-only file system")

    with monkeypatch.context() as patched:
        patched.setattr(builtins, "open", failing_open)
        patched.setattr(os, "truncate", failing_truncate)
        with pytest.raises(OSError):
            chain.append(entry_type=LedgerEntryType.INTENT_REVIEW_RECORDED, payload=payload)

    size = path.stat().st_size
    with pytest.raises(LedgerAppendError, match="needs repair"):
        chain.append(entry_type=LedgerEntryType.INTENT_REVIEW_RECORDED, payload=payload)
    assert path.stat().st_size == size


def test_cut_back_does_not_remove_a_line_another_writer_added(tmp_path, monkeypatch):
    """Two ledgers on one file in one process: a failed append undoes only itself."""
    path = tmp_path / "ledger.jsonl"
    failing = _chain(path)
    other = LedgerChain(storage=FileLedgerStorage(path), chain_id="file-test")
    real_open = builtins.open
    appended: list[threading.Thread] = []

    class _OtherWriterArrives(_FailsOnClose):
        def write(self, text: str) -> int:
            # The other writer tries to append while this one is between
            # measuring the file and failing.
            thread = threading.Thread(
                target=other.append,
                kwargs={
                    "entry_type": LedgerEntryType.INTENT_REVIEW_RECORDED,
                    "payload": {"intent_id": "other"},
                },
            )
            thread.start()
            thread.join(timeout=0.5)
            appended.append(thread)
            return super().write(text)

    def failing_open(file, mode="r", *args, **kwargs):
        opened = real_open(file, mode, *args, **kwargs)
        if "a" in mode and threading.current_thread() is threading.main_thread():
            return _OtherWriterArrives(opened, None)
        return opened

    with monkeypatch.context() as patched:
        patched.setattr(builtins, "open", failing_open)
        with pytest.raises(OSError, match="Input/output error"):
            failing.append(
                entry_type=LedgerEntryType.INTENT_REVIEW_RECORDED, payload={"intent_id": "mine"}
            )
        appended[0].join(timeout=5)

    intents = [
        json.loads(line)["payload"].get("intent_id") for line in path.read_text().splitlines()
    ]
    assert intents[1:] == ["other"]
