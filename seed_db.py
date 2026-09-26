#!/usr/bin/env python3
"""Seed MongoDB from dataset/ with expanded/ override support.

Priority order for each collection:
  Merchants  → expanded/merchants/*.json   (50 files)  → dataset/merchants_seed.json
  Customers  → expanded/customers/*.json   (200 files) → dataset/customers_seed.json
  Triggers   → expanded/triggers/*.json    (100 files) → dataset/triggers_seed.json
  Categories → expanded/categories/*.json              → dataset/categories/*.json
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
EXPANDED = ROOT / "expanded"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def mongo_uri_usable(uri: str) -> bool:
    if not uri:
        return False
    lowered = uri.lower()
    if "<username>" in lowered or "<password>" in lowered:
        return False
    return lowered.startswith("mongodb")


def upsert_many(coll, items: list[dict], id_field: str, extra: dict | None = None) -> int:
    n = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        key = item.get(id_field) or item.get("id")
        if not key:
            continue
        doc = dict(item)
        doc["id"] = key
        doc["data"] = dict(item)
        if extra:
            doc.update(extra)
        coll.update_one({"id": key}, {"$set": doc}, upsert=True)
        n += 1
    return n



# ---------------------------------------------------------------------------
# Dataset resolution: expanded/ directory takes priority over seed files
# ---------------------------------------------------------------------------

def load_merchants() -> list[dict]:
    """Load all merchant dicts. Prefers expanded/merchants/*.json (one file per merchant)."""
    expanded_dir = EXPANDED / "merchants"
    if expanded_dir.is_dir():
        items = []
        for f in sorted(expanded_dir.glob("*.json")):
            try:
                data = load_json(f)
                if isinstance(data, dict):
                    items.append(data)
                elif isinstance(data, list):
                    items.extend(data)
            except Exception as e:
                print(f"  [WARN] Skipping {f.name}: {e}")
        if items:
            print(f"  [expanded] merchants: {len(items)} files from expanded/merchants/")
            return items

    # Fallback to seed file
    seed = DATASET / "merchants_seed.json"
    if seed.exists():
        items = load_json(seed).get("merchants", [])
        print(f"  [seed] merchants: {len(items)} records from dataset/merchants_seed.json")
        return items

    print("  [WARN] No merchant data found.")
    return []


def load_customers() -> list[dict]:
    """Load all customer dicts. Prefers expanded/customers/*.json."""
    expanded_dir = EXPANDED / "customers"
    if expanded_dir.is_dir():
        items = []
        for f in sorted(expanded_dir.glob("*.json")):
            try:
                data = load_json(f)
                if isinstance(data, dict):
                    items.append(data)
                elif isinstance(data, list):
                    items.extend(data)
            except Exception as e:
                print(f"  [WARN] Skipping {f.name}: {e}")
        if items:
            print(f"  [expanded] customers: {len(items)} files from expanded/customers/")
            return items

    seed = DATASET / "customers_seed.json"
    if seed.exists():
        items = load_json(seed).get("customers", [])
        print(f"  [seed] customers: {len(items)} records from dataset/customers_seed.json")
        return items

    print("  [WARN] No customer data found.")
    return []


def load_triggers() -> list[dict]:
    """Load all trigger dicts. Prefers expanded/triggers/*.json."""
    expanded_dir = EXPANDED / "triggers"
    if expanded_dir.is_dir():
        items = []
        for f in sorted(expanded_dir.glob("*.json")):
            try:
                data = load_json(f)
                if isinstance(data, dict):
                    items.append(data)
                elif isinstance(data, list):
                    items.extend(data)
            except Exception as e:
                print(f"  [WARN] Skipping {f.name}: {e}")
        if items:
            print(f"  [expanded] triggers: {len(items)} files from expanded/triggers/")
            return items

    seed = DATASET / "triggers_seed.json"
    if seed.exists():
        items = load_json(seed).get("triggers", [])
        print(f"  [seed] triggers: {len(items)} records from dataset/triggers_seed.json")
        return items

    print("  [WARN] No trigger data found.")
    return []


def load_category_paths() -> list[Path]:
    """Return category JSON paths. Prefers expanded/categories/, falls back to dataset/categories/."""
    expanded_cats = EXPANDED / "categories"
    if expanded_cats.is_dir():
        paths = sorted(expanded_cats.glob("*.json"))
        if paths:
            print(f"  [expanded] categories: {len(paths)} files from expanded/categories/")
            return paths

    paths = sorted((DATASET / "categories").glob("*.json"))
    print(f"  [seed] categories: {len(paths)} files from dataset/categories/")
    return paths


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    uri = os.getenv("MONGO_URI", "")
    db_name = os.getenv("DB_NAME", "vera_db")

    if not mongo_uri_usable(uri):
        print("MONGO_URI is not set to a real Atlas connection string.")
        print("1. Create a free cluster at https://cloud.mongodb.com")
        print("2. Database Access → add a user")
        print("3. Network Access → allow 0.0.0.0/0 (dev) or your IP")
        print("4. Connect → Drivers → paste URI into .env as MONGO_URI")
        print("The API still runs with in-memory storage if Mongo is skipped.")
        return 1

    from pymongo import MongoClient

    print(f"\nConnecting to MongoDB ({db_name})…")
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    db = client[db_name]
    print("Connected.\n")

    # Ensure indexes
    db.merchants.create_index("id", unique=True)
    db.merchants.create_index("merchant_id", unique=True)
    db.customers.create_index("id", unique=True)
    db.customers.create_index("customer_id", unique=True)
    db.triggers.create_index("id", unique=True)
    db.triggers.create_index("trigger_id", unique=True)
    db.categories.create_index("id", unique=True)
    db.categories.create_index("category_name", unique=True)
    db.conversations.create_index("conversation_id", unique=True)
    db.contexts.create_index([("scope", 1), ("context_id", 1)], unique=True)
    db.contexts.create_index("id")

    print("Loading datasets…")

    # --- Merchants ---
    merchants = load_merchants()
    n_m = upsert_many(db.merchants, merchants, "merchant_id")

    # --- Customers ---
    customers = load_customers()
    n_c = upsert_many(db.customers, customers, "customer_id")

    # --- Triggers ---
    triggers = load_triggers()
    trig_docs = []
    for t in triggers:
        doc = dict(t)
        tid = t.get("id") or t.get("trigger_id")
        doc["id"] = tid
        doc["trigger_id"] = tid
        trig_docs.append(doc)
    n_t = upsert_many(db.triggers, trig_docs, "id")

    # --- Categories ---
    n_cat = 0
    for path in load_category_paths():
        try:
            payload = load_json(path)
            slug = payload.get("slug") or path.stem
            cat_doc = {**payload, "id": slug, "category_name": slug, "slug": slug, "data": payload}
            db.categories.update_one(
                {"id": slug},
                {"$set": cat_doc},
                upsert=True,
            )
            n_cat += 1
        except Exception as e:
            print(f"  [WARN] Skipping category {path.name}: {e}")
        except Exception as e:
            print(f"  [WARN] Skipping category {path.name}: {e}")

    # --- System metrics (idempotent init) ---
    db.system_metrics.update_one(
        {"_id": "global_metrics"},
        {
            "$setOnInsert": {
                "_id": "global_metrics",
                "contexts_ingested": 0,
                "active_conversations": 0,
                "ticks_processed": 0,
                "total_replies_generated": 0,
            }
        },
        upsert=True,
    )

    print(f"\n✓ Seeded {db_name}:")
    print(f"  merchants  = {n_m}")
    print(f"  customers  = {n_c}")
    print(f"  triggers   = {n_t}")
    print(f"  categories = {n_cat}")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
