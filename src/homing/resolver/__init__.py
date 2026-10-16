"""Multi-strategy element resolver.

Semantic first, brittle last, every attempt recorded.
"""

from homing.resolver.resolver import (
    Attempt,
    LocatorNotFoundError,
    Resolution,
    resolve,
)

__all__ = ["Attempt", "LocatorNotFoundError", "Resolution", "resolve"]
