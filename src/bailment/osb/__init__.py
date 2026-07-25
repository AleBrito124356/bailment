"""Open Service Broker API v2.16.

One module, :mod:`bailment.osb.router`. It is a separate package rather than a file
because the OSB surface has a different audience and a different security posture from
every other surface in bailment -- it hands out credential *values* -- and a reviewer
should be able to tell from the import path alone which code is allowed to do that.
"""

from __future__ import annotations

from bailment.osb.router import (
    OSB_API_VERSION,
    build_osb_router,
    plan_id_for,
    service_id_for,
)

__all__ = ["OSB_API_VERSION", "build_osb_router", "plan_id_for", "service_id_for"]
