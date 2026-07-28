"""osrs-assets: read-only index construction over the OSRS Grand Exchange.

Phase 0 of a feasibility study into "OSRS ETFs". This package computes and
publishes indices. It holds no gold, custodies nothing, and accepts no
deposits -- see docs/feasibility.md for why that boundary is where it is.
"""

__version__ = "0.1.0"

from .models import Bar, IndexLevel, Item, PriceObservation, Quality  # noqa: F401
