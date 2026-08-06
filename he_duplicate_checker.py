"""Compatibility entry point for duplicate-site detection."""

# ruff: noqa: F401,F403

from he_app import api as he_crawler
from he_app.services.duplicate_check import *
from he_app.services.duplicate_check import main as _main


if __name__ == "__main__":
    _main()
