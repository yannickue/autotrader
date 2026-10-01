"""Market Structure Observer: observation-only, shadow-only, NOT alpha validated. See schema.py for the contract."""

from market_observer.schema import (  # noqa: F401
    GROUP_VERSIONS,
    OBSERVER_VERSION,
    SCHEMA_VERSION,
    CausalityError,
    DecisionFeatures,
    FeatureResult,
    LevelRef,
    LevelRole,
    LevelSource,
    ObserverBars,
    ObserverRecord,
    PostEventLabels,
    SessionSpec,
    SwingLabel,
    SwingSequence,
)
