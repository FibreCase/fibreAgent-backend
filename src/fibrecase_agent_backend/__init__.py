"""fibrecase-agent-backend.

A minimal personal AI agent backend: a Telegram adapter driving a
channel-agnostic agent service backed by an OpenAI-compatible LLM endpoint and
a persistent SQLite conversation store.

Keep this package init light — subpackages are imported where needed so that
importing the package does not pull in the OpenAI/Telegram SDKs.
"""

__version__ = "1.8.1"

__all__ = ["__version__"]
