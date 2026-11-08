"""The product version: the one file the package build and the web build read."""

__version__ = "0.2.0"

# The dev designation inside the current release line: the current dev
# milestone, as the app shows it. It is deliberately
# separate from ``__version__``, which stays on the three-segment release line
# because packaging metadata, npm, and the eval baselines are pinned to it.
# It must start with ``__version__`` — tests/test_routes.py pins that.
__display_version__ = "0.2.0.12"
