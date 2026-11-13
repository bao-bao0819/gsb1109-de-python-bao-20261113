"""Public, float-facing API backed by the exact rational engine.

The engine performs every feasibility, tie and strict-inequality check with
:class:`fractions.Fraction`; only the values handed back to callers are
converted to floats for display and serialization.
"""

from . import exact_engine as engine

TOP_K = engine.TOP_K


def weighted_score(relevance, weights):
    return float(engine.weighted_score(relevance, weights))


def rank_table(records, weights):
    return engine.rank_table(records, weights)


def rank_of(records, weights, number):
    return engine.rank_of(records, weights, number)


def classify(records, lows, highs):
    verdicts = engine.classify(records, lows, highs)
    out = []
    for entry in verdicts:
        converted = dict(entry)
        if entry["weights"] is not None:
            converted["weights"] = tuple(float(w) for w in entry["weights"])
        out.append(converted)
    return out
