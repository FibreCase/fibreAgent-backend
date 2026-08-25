"""Channel-agnostic long-term memory (phase 2.5).

The :mod:`.text` helpers normalise memory text and deterministically rank stored
memories against a query. They are **pure Python** and stay free of any
Telegram / OpenAI-SDK / ORM imports, so the agent layer can depend on them
without pulling in those concerns. Persistence and per-scope retrieval live in
the repository; the pure logic here does no I/O.
"""

from __future__ import annotations

from .text import (
    MEMORY_REFERENCE_HEADER,
    MemoryCandidate,
    build_memory_reference_text,
    extract_terms,
    hash_scope,
    memory_reference_line,
    normalize_text,
    rank_memories,
)

__all__ = [
    "MEMORY_REFERENCE_HEADER",
    "MemoryCandidate",
    "normalize_text",
    "extract_terms",
    "rank_memories",
    "memory_reference_line",
    "build_memory_reference_text",
    "hash_scope",
]
