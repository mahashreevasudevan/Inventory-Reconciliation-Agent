"""
scripts/generate_fixtures.py

Builds three independent, schema-divergent stub inventory sources plus a
historical-accuracy log and a reservations feed. This script is run ONCE,
its output is committed to the repo, and src/agent.py never imports or
calls it -- the agent only ever reads data/wms.db, data/ecommerce_feed.json,
and data/tpl_feed.csv as if they were three real external systems it has
no control over. 

Two layers of data:

  1. CURATED SCENARIOS (SKU-1001 - SKU-1013) -- specific, documented cases
     that exercise every branch of the decision logic (timing lag, inventory
     semantics, corroboration, stale evidence, data-quality faults, missing
     sources). These are the cases discussed in the README.

  2. BULK CATALOG (SKU-2001 onwards, ~68 items, seeded random) -- ordinary
     inventory. The overwhelming majority are simply consistent, because
     that's what a real warehouse catalog looks like: most SKUs agree
     across systems most of the time. A handful get timing lag, a genuine
     conflict, or a missing source, generated the same way the curated
     cases are (not hand-authored one at a time) so the dataset reads as
     inventory rather than a pile of test cases. Reproducible via
     random.seed(FIXTURE_SEED) -- re-running this script produces byte-
     identical output.

Run once (or any time you want to regenerate deterministically):
    python scripts/generate_fixtures.py
"""
import sqlite3
import json
import csv
import os
import random
from datetime import datetime, timedelta

NOW = datetime(2026, 8, 14, 15, 0, 0)  # fixed "now" so the demo is reproducible
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
FIXTURE_SEED = 42


def ts(minutes_ago=0, hours_ago=0):
    return (NOW - timedelta(minutes=minutes_ago, hours=hours_ago)).isoformat()


# LAYER 1: Curated scenarios -- documented, specific, one per behaviour.
# Each row: internal_sku, wms_qty, wms_age_min, ecom_qty, ecom_age_min,
#           tpl_qty, tpl_age_min, note
# tpl_qty of None => missing from that source's feed entirely.
CURATED_SCENARIOS = [
    ("SKU-1001", 40, 5, 41, 20, 40, 90, "baseline agreement, 1-unit noise is within tolerance"),
    ("SKU-1002", 60, 1, 60, 4, 45, 26 * 60, "3PL stale (daily batch), WMS+ecom agree post-shipment"),
    ("SKU-1003", 25, 6, 24, 40, 27, 45, "small genuine spread, all sources fresh -> auto-reconcile"),
    ("SKU-1004", 30, 8, 18, 35, 32, 50, "ecom materially undercounts vs WMS+3PL agreement"),
    ("SKU-1005", 12, 5, 45, 110, 13, 60, "ecom wildly overstates stock, high unit cost item"),
    ("SKU-1006", 15, 10, 16, 30, None, None, "3PL has no record at all for this SKU"),
    ("SKU-1007", 20, 7, 19, 25, -5, 15, "3PL reports negative stock: invalid, not a stock conflict"),
    ("SKU-1008", 50, 2, 38, 130, 49, 40, "ecom sync job simply hasn't run yet, WMS+3PL agree"),
    ("SKU-1009", 33, 15, 34, 70, 33, 300, "all within tolerance though ages differ"),
    ("SKU-1010", 5, 4, 40, 20, 42, 35, "WMS lags a large ecom+3PL agreed increase (corroboration case)"),
    ("SKU-1011", 50, 5, 42, 15, 49, 40, "raw spread looks like conflict; fully explained by reservations"),
    ("SKU-1012", 30, 6, 24, 20, 29, 45, "explained by reservations, but that evidence is itself stale"),
    ("SKU-1013", 8, 5000, 25, 3000, 9, 3000, "every source is stale AND they disagree (STALE_EVIDENCE)"),
]

# internal_sku -> (units currently reserved against pending/unshipped
# orders, minutes ago the reservations snapshot was taken).
RESERVATIONS = {
    "SKU-1004": (2, 10),
    "SKU-1011": (8, 10),
    "SKU-1012": (6, 5 * 60),
}

# LAYER 2: Bulk catalog -- ordinary inventory, generated.
# Product names are illustrative filler (this is synthetic operational data,
# not real retail data -- see README "Data" note); quantities, ages, and
# which of the 4 buckets each item falls into come from a seeded RNG so the
# dataset is dense and reproducible without being individually scripted.
BULK_CATALOG = [
    ("Packing Tape Roll", 3.20), ("Bubble Wrap 10m", 6.75), ("Cardboard Box Small", 1.10),
    ("Cardboard Box Medium", 1.85), ("Cardboard Box Large", 2.60), ("Shipping Label Roll", 4.40),
    ("Zip Ties 100pk", 2.95), ("Furniture Blanket", 14.50), ("Pallet Wrap Roll", 9.20),
    ("Warehouse Gloves (pair)", 3.75), ("Hi-Vis Vest", 8.90), ("Barcode Scanner Battery", 11.40),
    ("Clipboard A4", 5.60), ("Marker Pen Black", 0.95), ("Box Cutter", 2.30),
    ("Foam Padding Sheet", 1.65), ("Corner Protector 4pk", 3.10), ("Strapping Tape Roll", 5.85),
    ("Pallet Jack Wheel", 22.00), ("Loading Dock Bumper", 34.50), ("Forklift Fuse", 6.20),
    ("Safety Cone", 12.75), ("Warning Sign - Wet Floor", 9.40), ("First Aid Kit Small", 15.20),
    ("Fire Extinguisher 2kg", 28.90), ("Dust Mask 20pk", 4.10), ("Ear Plugs 50pk", 3.35),
    ("Trolley Wheel Set", 18.60), ("Storage Bin Small", 2.75), ("Storage Bin Large", 5.90),
    ("Shelf Label Holder", 0.65), ("Inventory Tag 100pk", 3.50), ("Stretch Film Roll", 7.80),
    ("Address Label Sheet", 1.20), ("Packing Peanuts Bag", 6.40), ("Kraft Paper Roll", 8.15),
    ("Tape Dispenser", 4.95), ("Utility Cart Wheel", 16.30), ("Pallet Corner Post", 3.85),
    ("Warehouse Broom", 7.25), ("Wireless Mouse - Grey", 11.80), ("USB-C Cable 2m", 5.40),
    ("HDMI Cable 3m", 7.90), ("Laptop Stand Aluminium", 24.50), ("Wireless Charger Pad", 16.90),
    ("Bluetooth Earbuds", 34.00), ("Phone Case - Universal", 6.20), ("Screen Protector 3pk", 4.75),
    ("Power Bank 10000mAh", 19.90), ("Extension Lead 4-way", 8.60), ("Surge Protector 6-way", 12.40),
    ("Desk Organiser Tray", 9.15), ("Whiteboard Marker 4pk", 3.95), ("Sticky Notes 12pk", 4.30),
    ("Stapler Heavy Duty", 8.75), ("Hole Punch 2-hole", 6.50), ("Ring Binder A4", 2.10),
    ("Lever Arch File", 3.60), ("Envelope C4 250pk", 11.20), ("Bubble Mailer 100pk", 14.80),
    ("Thermal Label Roll", 6.90), ("Barcode Label Sheet", 2.45), ("Handheld Scanner Grip", 9.80),
    ("Conveyor Belt Section", 145.00), ("Roller Skate Wheel", 4.20), ("Pallet Truck Handle", 28.00),
    ("Warehouse Ladder Step", 42.00), ("Anti-Fatigue Mat", 22.60), ("Cable Tidy Clips 20pk", 1.95),
    ("USB Hub 4-port", 13.40), ("Ethernet Cable 5m", 4.85), ("Keyboard Wrist Rest", 7.10),
    ("Monitor Cleaning Kit", 5.75), ("Label Printer Ribbon", 9.30), ("Void Fill Paper Roll", 5.50),
]


def _generate_bulk_scenarios(seed=FIXTURE_SEED, start_index=2001):
    """
    Returns (scenarios, crosswalk_rows) for the bulk catalog. Deterministic
    given `seed` -- re-running this produces identical output every time.

    Bucketing (roughly matches what a real warehouse catalog looks like):
      - ~80% CONSISTENT: all three sources agree, fresh
      - ~9% TIMING_LAG: one source stale, others agree
      - ~7% GENUINE_CONFLICT: all fresh, but a real spread
      - ~3% MISSING_SOURCE: 3PL never onboarded this SKU
    """
    rng = random.Random(seed)
    scenarios = []
    crosswalk_rows = []
    n = len(BULK_CATALOG)

    for i, (name, cost) in enumerate(BULK_CATALOG):
        sku = f"SKU-{start_index + i}"
        product_id = f"PID-{90000 + i}"
        upc = f"{9000000000000 + start_index + i:013d}"
        crosswalk_rows.append((sku, product_id, upc, name, cost))

        base_qty = rng.randint(12, 300)
        bucket_roll = rng.random()

        if bucket_roll < 0.03:
            # missing source
            wms_qty = base_qty
            wms_age = rng.randint(1, 25)
            ecom_qty = base_qty + rng.choice([-1, 0, 1])
            ecom_age = rng.randint(5, 100)
            tpl_qty = None
            tpl_age = None
            note = "bulk: 3PL never onboarded"
        elif bucket_roll < 0.12:
            # timing lag: 3PL stale, WMS+ecom agree
            wms_qty = base_qty
            wms_age = rng.randint(1, 20)
            ecom_qty = base_qty + rng.choice([-1, 0, 1])
            ecom_age = rng.randint(5, 90)
            tpl_qty = base_qty - rng.randint(3, 15)
            tpl_age = rng.randint(1600, 2200)  # beyond 25h freshness window
            note = "bulk: timing lag, 3PL behind a recent movement"
        elif bucket_roll < 0.19:
            # genuine conflict, all fresh, real spread
            wms_qty = base_qty
            wms_age = rng.randint(1, 20)
            drift = rng.randint(8, 40) * rng.choice([-1, 1])
            ecom_qty = max(0, base_qty + drift)
            ecom_age = rng.randint(5, 90)
            tpl_qty = base_qty + rng.choice([-2, -1, 0, 1, 2])
            tpl_age = rng.randint(30, 600)
            note = "bulk: genuine spread, all sources fresh"
        else:
            # ordinary consistent agreement -- the overwhelming majority
            wms_qty = base_qty
            wms_age = rng.randint(1, 20)
            ecom_qty = base_qty + rng.choice([-1, 0, 0, 1])
            ecom_age = rng.randint(5, 90)
            tpl_qty = base_qty + rng.choice([-1, 0, 0, 1])
            tpl_age = rng.randint(30, 800)
            note = "bulk: ordinary consistent stock"

        scenarios.append((sku, wms_qty, wms_age, ecom_qty, ecom_age, tpl_qty, tpl_age, note))

    return scenarios, crosswalk_rows


def load_curated_crosswalk():
    crosswalk = {}
    with open(os.path.join(DATA_DIR, "sku_crosswalk.csv")) as f:
        for row in csv.DictReader(f):
            crosswalk[row["internal_sku"]] = row
    return crosswalk


def write_full_crosswalk(curated_crosswalk, bulk_crosswalk_rows):
    """Rewrites data/sku_crosswalk.csv with curated + bulk rows combined,
    so the committed file is always the complete, self-consistent catalog."""
    path = os.path.join(DATA_DIR, "sku_crosswalk.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["internal_sku", "product_id", "upc", "description", "unit_cost_gbp"])
        for sku, row in curated_crosswalk.items():
            w.writerow([sku, row["product_id"], row["upc"], row["description"], row["unit_cost_gbp"]])
        for sku, product_id, upc, name, cost in bulk_crosswalk_rows:
            w.writerow([sku, product_id, upc, name, f"{cost:.2f}"])
    print(f"wrote {path}")


def build_wms(scenarios):
    db_path = os.path.join(DATA_DIR, "wms.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE inventory (
            internal_sku TEXT PRIMARY KEY,
            qty_on_hand INTEGER NOT NULL,
            last_updated TEXT NOT NULL
        )
    """)
    for sku, wms_qty, wms_age, *_ in scenarios:
        conn.execute(
            "INSERT INTO inventory VALUES (?, ?, ?)",
            (sku, wms_qty, ts(minutes_ago=wms_age)),
        )
    conn.commit()
    conn.close()
    print(f"wrote {db_path}  ({len(scenarios)} records)")


def build_ecommerce(scenarios, crosswalk):
    items = []
    for sku, _, _, ecom_qty, ecom_age, *_ in scenarios:
        row = crosswalk[sku]
        items.append({
            "product_id": row["product_id"],
            "stock_level": ecom_qty,
            "last_synced": ts(minutes_ago=ecom_age),
        })
    path = os.path.join(DATA_DIR, "ecommerce_feed.json")
    with open(path, "w") as f:
        json.dump({"generated_at": ts(), "items": items}, f, indent=2)
    print(f"wrote {path}  ({len(items)} records)")


def build_tpl(scenarios, crosswalk):
    path = os.path.join(DATA_DIR, "tpl_feed.csv")
    count = 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["upc", "quantity_on_hand", "snapshot_time"])
        for sku, _, _, _, _, tpl_qty, tpl_age, _ in scenarios:
            if tpl_qty is None:
                continue  # simulate a SKU the 3PL has never reported
            row = crosswalk[sku]
            w.writerow([row["upc"], tpl_qty, ts(minutes_ago=tpl_age)])
            count += 1
    print(f"wrote {path}  ({count} records)")


def build_history():
    """
    Simulated log of past discrepancies where a physical stocktake later
    established ground truth. The agent computes each source's historical
    accuracy from this file at startup rather than having it hardcoded.
    """
    path = os.path.join(DATA_DIR, "historical_reconciliations.csv")
    records = (
        [("WMS", True)] * 27 + [("WMS", False)] * 2 +
        [("ECOMMERCE", True)] * 19 + [("ECOMMERCE", False)] * 8 +
        [("TPL", True)] * 22 + [("TPL", False)] * 5
    )
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event_id", "source", "was_correct"])
        for i, (source, correct) in enumerate(records, start=1):
            w.writerow([f"HIST-{i:04d}", source, correct])
    print(f"wrote {path}")


def build_reservations():
    path = os.path.join(DATA_DIR, "reservations.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["internal_sku", "reserved_units", "as_of"])
        for sku, (reserved, age_min) in RESERVATIONS.items():
            w.writerow([sku, reserved, ts(minutes_ago=age_min)])
    print(f"wrote {path}")


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)

    curated_crosswalk = load_curated_crosswalk()
    bulk_scenarios, bulk_crosswalk_rows = _generate_bulk_scenarios()
    write_full_crosswalk(curated_crosswalk, bulk_crosswalk_rows)

    full_crosswalk = load_curated_crosswalk()  # re-read now that it includes bulk rows
    all_scenarios = CURATED_SCENARIOS + bulk_scenarios

    build_wms(all_scenarios)
    build_ecommerce(all_scenarios, full_crosswalk)
    build_tpl(all_scenarios, full_crosswalk)
    build_history()
    build_reservations()

    print(f"\nDone. {len(all_scenarios)} SKUs total "
          f"({len(CURATED_SCENARIOS)} curated scenarios + {len(bulk_scenarios)} bulk catalog items).")
    print("Curated scenarios (see README for the full walkthrough of each):")
    for sku, *_, note in CURATED_SCENARIOS:
        print(f"  {sku}: {note}")
