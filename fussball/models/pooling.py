"""Log-lineares Pooling: p ∝ Π p_k^{w_k}. Kombiniert Modell und Markt.

Der Markt (ohne Marge) ist ein starker Prior. Das Modellgewicht wird im
Walk-Forward-Backtest nur auf vergangenen Saisons bestimmt.
"""

from __future__ import annotations

import numpy as np


def pool(sources: list[dict[str, float]], weights: list[float]) -> dict[str, float]:
    keys = list(sources[0])
    log_p = np.zeros(len(keys))
    for src, w in zip(sources, weights):
        log_p += w * np.log(np.clip([src[k] for k in keys], 1e-9, 1.0))
    p = np.exp(log_p - log_p.max())
    p /= p.sum()
    return dict(zip(keys, map(float, p)))
