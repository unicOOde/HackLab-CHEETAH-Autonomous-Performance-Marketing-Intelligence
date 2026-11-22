"""Non-colliding synthetic ID allocation.

Real Click IDs observed up to ~1.163B; real Transaction IDs up to ~2.605M. Synthetic IDs start
well above both ranges so a synthetic row is visually distinguishable from a real one if the two
are ever mixed during ETL testing. One synthetic click == one journey (real data has no
multi-click-per-user concept), so a single Click ID also serves as the journey identifier —
no separate journey_id is allocated.
"""
from __future__ import annotations

import numpy as np

SYNTH_CLICK_ID_START = 9_000_000_000
SYNTH_TRANSACTION_ID_START = 9_000_000


class IdAllocator:
    def __init__(self, start: int):
        self._next = start

    def next_batch(self, n: int) -> np.ndarray:
        ids = np.arange(self._next, self._next + n, dtype=np.int64)
        self._next += n
        return ids
