"""Task graph (phase 3).

Externalized task graph with the done-when contract:

    pending --start--> in_progress --done-when-satisfied--> done
       |                     |
       +--block--> blocked --retry--> pending
       +--cancel (from any state except done)

Criteria are checklists; plain lines count as unsatisfied:

    - [x] patch applied
    - [ ] integration tests pass
"""

from __future__ import annotations

from rinari.tasks import core
from rinari.tasks.service import TaskService

__all__ = ["TaskService", "core"]
