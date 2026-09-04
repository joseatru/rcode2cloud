"""
migration.state
---------------
Checkpoint, cache, and atomic-JSON persistence.

The checkpoint is an append-only JSONL journal (the durable per-issue write
path) plus atomic temp-and-rename snapshots written at phase boundaries. That
split is load-bearing: rewriting the whole state file per issue was an O(n^2)
hot spot on a multi-thousand-issue project.
"""

import json
import os
import tempfile

from pathlib import Path

from .logging_setup import log
from .paths import (
    CHECKPOINT_FILE,
    CHECKPOINT_JOURNAL,
    UPLOAD_CACHE_FILE,
    USER_CACHE_FILE,
)


class StateMixin:
    """
    Checkpoint and cache persistence for GitLabMigration.

    Expects the host object to provide `checkpoint`, `user_cache`, and
    `upload_map`.
    """

    def _load_json(self, path: Path, default) -> dict:
        """
        Load JSON from path; return default if file is absent or unparseable.
        """
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning(f"Could not load {path} ({exc}). Starting fresh.")
        return default

    def _save_json(self, path: Path, data) -> None:
        """
        Write data as JSON to path atomically.

        Writes to a temporary file in the same directory and renames it over
        the target, so a crash mid-write cannot leave a truncated checkpoint
        that the next run refuses to parse.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def _load_checkpoint(self) -> dict:
        """
        Rebuild the checkpoint from its snapshot plus the append-only journal.

        The journal (migration_state.jsonl) is the write path: one line per
        migrated issue, so recording progress costs O(1) rather than
        rewriting the whole state file per issue. The snapshot
        (migration_state.json) is a periodic compaction of that journal.
        Later journal lines win over earlier ones and over the snapshot.

        Returns:
            dict mapping source IID (str) -> migration record.
        """
        state = dict(self._load_json(CHECKPOINT_FILE, default={}))

        if CHECKPOINT_JOURNAL.exists():
            replayed = 0
            skipped  = 0
            with open(CHECKPOINT_JOURNAL, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        state[str(record["source_iid"])] = record
                        replayed += 1
                    except (json.JSONDecodeError, KeyError, TypeError):
                        # A partial final line is expected after a hard kill.
                        skipped += 1
            if replayed or skipped:
                log.info(
                    f"Checkpoint journal replayed: {replayed} record(s)"
                    + (f", {skipped} unparseable line(s) ignored" if skipped else "")
                )

        return state

    def _journal_checkpoint(self, record: dict) -> None:
        """
        Append one migration record to the checkpoint journal.

        Args:
            record: Must contain a "source_iid" key.
        """
        CHECKPOINT_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with open(CHECKPOINT_JOURNAL, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
            handle.flush()

    def save_checkpoint(self) -> None:
        """
        Compact the in-memory checkpoint into an atomic snapshot on disk.

        Called at phase boundaries rather than per issue; per-issue durability
        comes from the journal.
        """
        self._save_json(CHECKPOINT_FILE, self.checkpoint)

    def save_user_cache(self) -> None:
        """Persist the destination user ID cache to disk."""
        self._save_json(USER_CACHE_FILE, self.user_cache)

    def save_upload_cache(self) -> None:
        """Persist the attachment upload map to disk."""
        self._save_json(UPLOAD_CACHE_FILE, self.upload_map)
