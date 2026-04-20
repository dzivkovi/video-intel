"""Retrieval eval package marker.

Sets DEEPEVAL_TELEMETRY_OPT_OUT before any submodule import triggers
deepeval. DeepEval's telemetry module fires at import time and captures
the developer's public IP + an anonymous unique ID for every
metric.measure() call via PostHog + Sentry. Opt-out is read from this
env var via pydantic settings; setting it here guarantees it resolves
before conftest.py / test_search_quality.py load metrics.py / _helpers.py.
"""

import os

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
