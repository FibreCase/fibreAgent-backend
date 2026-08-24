"""LLM package: the OpenAI-compatible LLM client."""

from .client import ChatMessage, LLMError, LLMResult, OpenAIClient

__all__ = ["ChatMessage", "LLMError", "LLMResult", "OpenAIClient"]
