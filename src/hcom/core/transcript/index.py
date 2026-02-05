"""Thin index over transcript files.

Scans file once, stores (line_no, byte_offset, role, timestamp) per entry.
Consumers navigate by position and read raw entries on demand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .classify import classify_claude, classify_codex, classify_gemini


@dataclass(frozen=True, slots=True)
class IndexEntry:
    line_no: int
    byte_offset: int  # byte position in file (for JSONL) or message index (for JSON)
    role: str
    timestamp: str


class TranscriptIndex:
    """Index of transcript entries with on-demand raw access."""

    _cache: dict[tuple[str, float], TranscriptIndex] = {}

    def __init__(self, path: str, agent: str, entries: list[IndexEntry]):
        self.path = path
        self.agent = agent
        self._entries = entries

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, idx):
        return self._entries[idx]

    def __iter__(self):
        return iter(self._entries)

    @classmethod
    def build(cls, path: str, agent: str) -> TranscriptIndex:
        """Build index from transcript file. Cached by (path, mtime)."""
        p = Path(path)
        if not p.exists():
            return cls(path, agent, [])

        mtime = p.stat().st_mtime
        cache_key = (str(path), mtime)
        if cache_key in cls._cache:
            return cls._cache[cache_key]

        if agent == "gemini":
            entries = cls._build_gemini(path)
        else:
            classifier = classify_claude if agent == "claude" else classify_codex
            entries = cls._build_jsonl(path, classifier)

        index = cls(path, agent, entries)
        cls._cache[cache_key] = index
        return index

    @staticmethod
    def _build_jsonl(path: str, classifier) -> list[IndexEntry]:
        """Build index from JSONL file (Claude, Codex)."""
        entries = []
        with open(path, "rb") as f:
            byte_offset = 0
            line_no = 0
            for raw_line in f:
                line = raw_line.decode("utf-8", errors="replace")
                stripped = line.strip()
                if not stripped:
                    byte_offset += len(raw_line)
                    line_no += 1
                    continue
                try:
                    obj = json.loads(stripped)
                    role = classifier(obj)
                    timestamp = obj.get("timestamp", "")
                    entries.append(IndexEntry(line_no, byte_offset, role, timestamp))
                except json.JSONDecodeError:
                    entries.append(IndexEntry(line_no, byte_offset, "unknown", ""))
                byte_offset += len(raw_line)
                line_no += 1
        return entries

    @staticmethod
    def _build_gemini(path: str) -> list[IndexEntry]:
        """Build index from Gemini JSON file."""
        with open(path, "r") as f:
            data = json.load(f)
        messages = data.get("messages", [])
        entries = []
        for i, msg in enumerate(messages):
            role = classify_gemini(msg)
            timestamp = msg.get("timestamp", "")
            entries.append(IndexEntry(i, i, role, timestamp))
        return entries

    def user_entries(self) -> list[IndexEntry]:
        """Return entries with role 'user'."""
        return [e for e in self._entries if e.role == "user"]

    @property
    def _gemini_messages(self) -> list[dict]:
        """Lazy-cached Gemini messages array. Avoids re-reading JSON on every read_raw call."""
        if not hasattr(self, "_gemini_cache"):
            with open(self.path) as f:
                self._gemini_cache = json.load(f).get("messages", [])
        return self._gemini_cache

    def read_raw(self, entry: IndexEntry) -> dict:
        """Read and parse the raw JSON for an index entry."""
        if self.agent == "gemini":
            messages = self._gemini_messages
            if entry.line_no < len(messages):
                return messages[entry.line_no]
            return {}

        # JSONL: seek to byte offset
        with open(self.path, "rb") as f:
            f.seek(entry.byte_offset)
            raw_line = f.readline()
            try:
                return json.loads(raw_line)
            except json.JSONDecodeError:
                return {}
