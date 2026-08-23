"""Operator-supplied venue adapters, loaded at runtime.

Trading adapters for specific venues are deliberately **not** in this repository
(docs/04-exchanges-iran.md §1). Instead the operator installs a package that
exposes an entry point in the ``simin.adapters`` group, and Simin discovers it by
name from configuration. The core stays venue-agnostic and the repository ships
no integration with any designated entity.

    # in the operator's own package
    [project.entry-points."simin.adapters"]
    my_venue = "my_package.adapter:MyVenueAdapter"

Nothing is auto-loaded: a plugin runs only when named in ``SIMIN_VENUE_PLUGIN``.
Silent discovery of code that can place orders is not a feature.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from simin.exchanges.base import ExchangeAdapter

ENTRY_POINT_GROUP = "simin.adapters"


def available_plugins() -> dict[str, str]:
    """Installed adapter plugins, as ``{name: target}``. Discovery only, no import."""
    return {ep.name: ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)}


def load_adapter(name: str, **kwargs: Any) -> ExchangeAdapter:
    """Instantiate a named plugin adapter, verifying the contract before use.

    The isinstance check is the point: an object that merely looks adapter-shaped
    would fail somewhere deep in the execution path, at which point it is holding
    real money.
    """
    matches = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if not matches:
        installed = ", ".join(sorted(available_plugins())) or "none installed"
        raise LookupError(f"no adapter plugin named {name!r} (available: {installed})")
    factory = matches[0].load()
    adapter = factory(**kwargs)
    if not isinstance(adapter, ExchangeAdapter):
        raise TypeError(
            f"plugin {name!r} produced {type(adapter).__name__}, "
            "which does not implement ExchangeAdapter"
        )
    return adapter
