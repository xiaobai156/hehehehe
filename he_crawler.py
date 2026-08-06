"""Single-period command entry and backwards-compatible public facade."""

# ruff: noqa: F403

from he_app.api import *
from he_app.services.runner import main


if __name__ == "__main__":
    main()
