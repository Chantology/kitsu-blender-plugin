"""Minimal shim so vendored gazu's ``from typing_extensions import Literal``
works without installing the real typing_extensions package into Krita's
bundled Python. Krita ships Python 3.9+, where ``typing.Literal`` already
exists, so re-exporting it is sufficient for gazu's needs.
"""

from typing import Literal

__all__ = ["Literal"]
