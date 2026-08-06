"""Compatibility entry point for multi-period crawling."""

# ruff: noqa: F401,F403

from he_app import api as he_crawler
from he_app.services.multi_period import *
from he_app.services.multi_period import main as _main


if __name__ == "__main__":
    _main()
