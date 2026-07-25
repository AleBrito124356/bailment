"""The MCP surface: the tools an AI agent sees, generated from the catalog.

Kept in its own package for the same reason :mod:`bailment.osb` is: this is the code path
whose caller is always an :attr:`~bailment.engine.service.CallerKind.AGENT`, and a
reviewer should be able to tell from an import line which code is talking to a model.

The package is named ``mcp`` and there is a third-party distribution also named ``mcp``.
Nothing collides: Python 3 resolves ``import mcp`` inside this package to the top-level
distribution, because implicit relative imports have not existed since Python 2.
"""

from __future__ import annotations

from bailment.mcp.server import BailmentMCP, build_mcp_server

__all__ = ["BailmentMCP", "build_mcp_server"]
