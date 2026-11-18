# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The file-backed ledger and its file agree after an append that fails."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from nomos.level_1.ledger import FileLedgerStorage, LedgerChain, LedgerEntryType


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
