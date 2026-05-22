"""Compatibility import for the MAX adapter."""

import sys

from .max import adapter as _adapter

sys.modules[__name__] = _adapter
