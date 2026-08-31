"""QQ (multi-channel, phase 10) adapter package.

The only package that imports ``botpy``. Exposes the client builder (the
composition root's entry point) and the :class:`QQChannel` handler object
(constructed directly by the tests, without a live ``botpy`` client).
"""

from .bot import QQChannel, build_qq_client, deliver_qq_markdown

__all__ = ["QQChannel", "build_qq_client", "deliver_qq_markdown"]
