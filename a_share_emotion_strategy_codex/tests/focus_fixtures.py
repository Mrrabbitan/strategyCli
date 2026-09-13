"""Fictional point-in-time pools for strategy tests; never production data."""
from emotion_monitor import load_emotion_config
from hot_sector_leaders import build_hot_sector_context


def focus_context(pool, *, near=None, peers=False, as_of="2026-08-25"):
    pool = list(pool)
    if peers:
        pool += [
            {"c": code, "n": "虚构同板块", "hybk": "算力", "lbc": 1,
             "amount": 100_000_000, "fund": 10_000_000, "zbc": 0}
            for code in ("600090", "600091")
        ]
    return build_hot_sector_context(pool, near or [], as_of=as_of,
                                    source_verified=True, config=load_emotion_config())
