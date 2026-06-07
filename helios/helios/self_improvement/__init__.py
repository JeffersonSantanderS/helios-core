"""Helios Self-Improvement — closed-loop learning substrate.

Records learning events, outcomes, and policy proposals. Proposes
config/policy changes based on outcome evidence but never auto-promotes
without explicit safety gate checks.

Modes: shadow (observe only), apprentice (propose, no auto-apply), active (apply approved).
"""

from .models import (
    LearningEvent,
    OutcomeEvent,
    OutcomeType,
    PrivacyClass,
    PolicyProposal,
    ProposalTarget,
    ProposalStatus,
    PromotionDecision,
)
from .store import SelfImprovementStore
from .integration import SelfImprovementIntegration

__all__ = [
    "LearningEvent",
    "OutcomeEvent",
    "OutcomeType",
    "PrivacyClass",
    "PolicyProposal",
    "ProposalTarget",
    "ProposalStatus",
    "PromotionDecision",
    "SelfImprovementStore",
    "SelfImprovementIntegration",
]