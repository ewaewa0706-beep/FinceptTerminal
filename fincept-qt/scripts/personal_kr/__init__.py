"""Personal Korean-market research engine for Fincept Terminal.

The package deliberately has no Qt dependency.  Fincept's C++ layer invokes it
through ``PythonRunner`` and exchanges JSON, which keeps provider/network logic
testable without building the desktop application.
"""

from .models import Instrument, QuantCandidate, ResearchPacket, ResearchResult
from .ranking import select_top_candidates

__all__ = [
    "Instrument",
    "QuantCandidate",
    "ResearchPacket",
    "ResearchResult",
    "select_top_candidates",
]
