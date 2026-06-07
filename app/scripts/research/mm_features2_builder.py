#!/usr/bin/env python3
"""
Compatibility shim.

New canonical module:
  app.scripts.research.algomm_feature_sets

Keep this file only if older code still imports mm_features2_builder.
"""
from app.scripts.research.algomm_feature_sets import *  # noqa: F401,F403
