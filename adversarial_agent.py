"""
adversarial_review.py

Not part of the test suite -- a one-off adversarial pass, try to break the reasoning with uncomfortable cases rather
than confirming the happy path. Each case prints what the agent actually
decided so it can be judged honestly rather than assumed to be fine.

Run:
    python adversarial_review.py
"""
import sys
import os
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from agent import Reading, classify_and_decide, NOW  # noqa: E402


def mk(source, qty, minutes_ago):
    return Reading(source, f"raw-{source}", qty, NOW - timedelta(minutes=minutes_ago))


hist = {"WMS": 0.93, "ECOMMERCE": 0.70, "TPL": 0.81}
counts = {"WMS": [27, 29], "ECOMMERCE": [19, 27], "TPL": [22, 27]}


def show(label, d):
    print(f"\n{'='*70}\n{label}\n{'='*70}")
    print(f"classification={d['classification']}  action={d['action']}  "
          f"trusted={d.get('trusted_source')}  impact={d.get('financial_impact_gbp')}")
    if d.get("corrections"):
        print("corrections:", d["corrections"])
    print("reasoning:", d.get("reasoning", "")[:400])


# 1. ALL THREE SOURCES STALE, but agreeing. Does the agent notice none of
#    its evidence is actually current, or does agreement alone satisfy it?
d = classify_and_decide("SKU-A", {
    "WMS": mk("WMS", 20, 500),        # 500min old, freshness window 30min
    "ECOMMERCE": mk("ECOMMERCE", 21, 1000),  # window 120min
    "TPL": mk("TPL", 20, 2000),       # window 1500min
}, 10.0, "All-stale, agreeing", hist, counts)
show("1. All three sources stale but agreeing", d)

# 2. ALL THREE SOURCES STALE, and disagreeing. This is the dangerous one:
#    will the agent confidently pick a "trusted" source from data that's
#    entirely out of date?
d = classify_and_decide("SKU-B", {
    "WMS": mk("WMS", 10, 500),
    "ECOMMERCE": mk("ECOMMERCE", 40, 1000),
    "TPL": mk("TPL", 42, 2000),
}, 50.0, "All-stale, disagreeing", hist, counts)
show("2. All three sources stale AND disagreeing", d)

# 3. Reservation translation going negative: e-commerce is NOT trusted, and
#    the reserved-units figure is bigger than the trusted physical value --
#    does the write-back produce a negative "corrected" quantity, breaking
#    the same rule the agent enforces elsewhere?
d = classify_and_decide("SKU-C", {
    "WMS": mk("WMS", 5, 3),
    "ECOMMERCE": mk("ECOMMERCE", 30, 5),
    "TPL": mk("TPL", 5, 4),
}, 0.5, "Reservation bigger than trusted stock", hist, counts,
    reservations={"SKU-C": (40, NOW)})  # 40 reserved against a trusted stock of ~5
show("3. Reservations bigger than physical stock (correction could go negative)", d)

# 4. Exact tie at the tolerance boundary: spread == tolerance exactly.
d = classify_and_decide("SKU-D", {
    "WMS": mk("WMS", 40, 5),
    "ECOMMERCE": mk("ECOMMERCE", 42, 10),
    "TPL": mk("TPL", 40, 8),
}, 10.0, "Spread exactly at tolerance boundary", hist, counts)
show("4. Spread exactly equal to tolerance (boundary behaviour)", d)

# 5. Two sources tie exactly on composite score -- who wins, and is the
#    tie-break deterministic/defensible?
d = classify_and_decide("SKU-E", {
    "WMS": mk("WMS", 10, 5),
    "ECOMMERCE": mk("ECOMMERCE", 50, 5),
}, 10.0, "Two-source conflict, no third source to corroborate either", hist, counts)
show("5. Only 2 sources report -- corroboration structurally can't fire", d)

# 6. Extremely cheap SKU with a huge quantity gap: does financial-impact
#    routing let a big discrepancy slip through as auto_reconcile purely
#    because unit cost is tiny? (fixed: quantity-deviation guard)
d = classify_and_decide("SKU-F", {
    "WMS": mk("WMS", 10, 5),
    "ECOMMERCE": mk("ECOMMERCE", 5000, 10),
    "TPL": mk("TPL", 12, 8),
}, 0.01, "Cheap SKU, massive quantity gap", hist, counts)
show("6. £0.01 unit cost, 5000-unit gap -- quantity-deviation guard should block auto_reconcile", d)

# 7. High business-rule trust (WMS) is stale, while ecommerce+3PL are fresh
#    and agree -- does staleness alone (without corroboration doing the
#    work) correctly demote WMS?
d = classify_and_decide("SKU-G", {
    "WMS": mk("WMS", 5, 45),   # stale: window is 30min
    "ECOMMERCE": mk("ECOMMERCE", 40, 10),
    "TPL": mk("TPL", 41, 15),
}, 10.0, "WMS stale, ecom+3PL fresh and agree", hist, counts)
show("7. WMS (business favourite) stale vs fresh corroborating pair", d)

# 8. Reservations present but ZERO -- does explicitly-zero reservations
#    behave the same as no reservations row at all?
d = classify_and_decide("SKU-H", {
    "WMS": mk("WMS", 30, 5),
    "ECOMMERCE": mk("ECOMMERCE", 24, 10),
    "TPL": mk("TPL", 29, 8),
}, 10.0, "Reservations explicitly zero", hist, counts,
    reservations={"SKU-H": (0, NOW)})
show("8. Reservations explicitly present but zero", d)

# 9. Three-way total disagreement, nobody corroborates anybody -- does the
#    system still confidently pick a "winner" with no supporting evidence
#    at all, or does the composite score reflect the low confidence?
d = classify_and_decide("SKU-I", {
    "WMS": mk("WMS", 10, 5),
    "ECOMMERCE": mk("ECOMMERCE", 30, 10),
    "TPL": mk("TPL", 50, 8),
}, 20.0, "Total 3-way disagreement, no corroboration anywhere", hist, counts)
show("9. Three-way disagreement -- nobody corroborates anybody", d)

# 10. A source with NO historical track record at all (not in the accuracy
#     file), AND not registered in the agent's static config dicts (e.g. a
#     new source wired into the fetchers without updating agent.py) --
#     does this crash, or degrade gracefully?
try:
    d = classify_and_decide("SKU-J", {
        "WMS": mk("WMS", 10, 20),
        "NEWSOURCE": Reading("NEWSOURCE", "raw", 30, NOW - timedelta(minutes=5)),
    }, 10.0, "Unregistered source, no historical track record", hist, counts)
    show("10. Unregistered source (not in FRESHNESS_WINDOW_MIN/SOURCE_MEASURES/BUSINESS_RULE_BONUS)", d)
except Exception as e:
    print(f"\n{'='*70}\n10. Unregistered source\n{'='*70}\nCRASHED: {type(e).__name__}: {e}")
