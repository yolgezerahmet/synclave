"""synclave — encrypted multi-node backup, replication, and agent mesh (A2A).

Eski ad: hermes-sync (v2.3.1) → rebrand: synclave (v1.0.0).
"""
__version__ = "2.5.0"

from . import (
    sync_motor,
    sync_common_knowledge,
    sync_memory,
    sync_retention,
    node_agent,
)

__all__ = [
    "sync_motor",
    "sync_common_knowledge",
    "sync_memory",
    "sync_retention",
    "node_agent",
    "__version__",
]
