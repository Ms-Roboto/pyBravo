"""Headless geometry checks, loaded only when physical rehearsal is requested.

Read-only planning and the control panel remain usable without the optional
native engine. A requested physical rehearsal must never silently fall back.
"""


def __getattr__(name):
    if name in {"CollisionContact", "SuperDexCollisionBackend", "ensure_physics_context"}:
        from . import superdex_backend

        return getattr(superdex_backend, name)
    raise AttributeError(name)


__all__ = ["CollisionContact", "SuperDexCollisionBackend", "ensure_physics_context"]
