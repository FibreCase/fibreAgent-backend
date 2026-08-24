"""Telegram package: the bot adapter (handlers, typing, chunking, wiring)."""

from .bot import build_application, split_into_chunks

__all__ = ["build_application", "split_into_chunks"]
