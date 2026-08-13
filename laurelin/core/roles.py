"""``Role``, and nothing else.

It lives alone at the bottom of the dependency graph because several modules
*below* :mod:`laurelin.core.models` now have to speak about privilege —
:mod:`laurelin.core.failure` declares the level that may read a failure record,
and :mod:`laurelin.core.serialize` compares roles for every response. ``models``
re-exports ``Role``, so ``from laurelin.core.models import Role`` keeps working
everywhere it already did.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """Ordered roles: viewer < editor < admin."""

    viewer = "viewer"
    editor = "editor"
    admin = "admin"

    @property
    def rank(self) -> int:
        return _ROLE_ORDER[self]

    def covers(self, required: "Role") -> bool:
        """True if this role grants at least ``required``'s privileges."""
        return self.rank >= required.rank


_ROLE_ORDER = {Role.viewer: 0, Role.editor: 1, Role.admin: 2}
