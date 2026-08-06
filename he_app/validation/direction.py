from typing import TypeVar

from he_app.domain.policies import normalize_pick


T = TypeVar("T")


def directional_window(items: list[T], pick: str, size: int = 1) -> list[T]:
    """Return the single valid row at the configured direction edge.

    ``top`` and ``bottom`` are positional boundaries, not period-number
    windows. ``size`` remains in the signature for compatibility, but it
    never widens the accepted region.
    The caller is responsible for supplying rows from one authority/block.
    """
    if size < 1:
        raise ValueError("方向窗口必须大于0")
    if not items:
        return []
    return [items[0]] if normalize_pick(pick) == "top" else [items[-1]]
