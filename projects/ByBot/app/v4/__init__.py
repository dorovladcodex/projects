"""ByBot V4 transaction-cost-aware alpha research components.

V4 is deliberately research/shadow only.  It consumes the existing V2
market-feature abstraction and never owns an execution or risk path.
"""

from app.v4.models import V4ForwardLabel, V4Opportunity

__all__ = ["V4ForwardLabel", "V4Opportunity"]
