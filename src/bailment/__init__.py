"""bailment -- a provisioning broker that hands out capabilities instead of credentials.

The name is the legal term: a bailment is when you hand someone your property for a
limited purpose and a limited time, and they are obliged to give it back. That is the
entire model. An agent asks for a database; it receives a lease and a reference, never a
connection string; the lease expires; the database is destroyed; the reconciler goes
looking for anything that survived the process anyway.

Layout, in dependency order (each layer may import the ones above it, never below):

* :mod:`bailment.states`   -- the lease state machine, the authority on transitions.
* :mod:`bailment.models`   -- SQLAlchemy models.
* :mod:`bailment.config`   -- settings, read from ``BAILMENT_*`` environment variables.
* :mod:`bailment.secrets`  -- the Fernet envelope every binding is stored inside.
* :mod:`bailment.logging`  -- structured logging, including the secret redactor.
* :mod:`bailment.db`       -- async engine and session helpers.
* :mod:`bailment.catalog`  -- golden path schema and loader.

This module is deliberately almost empty. Importing ``bailment`` must not read the
environment, open a database connection or touch the filesystem, because ``bailment
keygen`` has to work on a machine that has none of those things configured yet.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("bailment")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.1.0"

__all__ = ["__version__"]
