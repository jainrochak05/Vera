"""Persistence layer: MongoDB Atlas when configured, in-memory otherwise."""

from __future__ import annotations

import copy
import os
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

DB_NAME = os.getenv("DB_NAME", "vera_db")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def mongo_uri_usable(uri: str) -> bool:
    if not uri:
        return False
    lowered = uri.lower()
    if "<username>" in lowered or "<password>" in lowered:
        return False
    if "your_gemini" in lowered or "your_" in lowered:
        return False
    return lowered.startswith("mongodb")


class Store:
    def __init__(self) -> None:
        self.backend = "memory"
        self.db = None
        self.client = None
        self._mem: dict[str, dict[str, dict]] = {
            "contexts": {},
            "conversations": {},
            "merchant_flags": {},
            "sent_suppressions": {},
            "merchants": {},
            "customers": {},
            "triggers": {},
            "categories": {},
        }
        self._metrics = {
            "_id": "global_metrics",
            "contexts_ingested": 0,
            "active_conversations": 0,
            "ticks_processed": 0,
            "total_replies_generated": 0,
        }

    def preload_seed_data(self) -> None:
        """Pre-populate the in-memory store from local JSON files.

        Priority (same as seed_db.py):
          expanded/merchants/*.json  > dataset/merchants_seed.json
          expanded/customers/*.json  > dataset/customers_seed.json
          expanded/triggers/*.json   > dataset/triggers_seed.json
          expanded/categories/*.json > dataset/categories/*.json
        """
        try:
            import json
            from pathlib import Path

            root = Path(__file__).parent
            dataset = root / "dataset"
            expanded = root / "expanded"
            now = utc_now_iso()

            def _store(scope: str, key: str, payload: dict) -> None:
                doc = {
                    "id": key,
                    "scope": scope,
                    "context_id": key,
                    "version": 1,
                    "payload": payload,
                    "data": payload,
                    "delivered_at": now,
                    "stored_at": now,
                    **payload
                }
                self._mem["contexts"][self._ctx_key(scope, key)] = doc
                coll_name = scope + "s" if scope != "category" else "categories"
                self._mem.setdefault(coll_name, {})[key] = doc

            # ---- Categories ----
            cat_expanded = expanded / "categories"
            cat_seed = dataset / "categories"
            cat_dir = cat_expanded if cat_expanded.is_dir() and any(cat_expanded.glob("*.json")) else cat_seed
            for f in sorted(cat_dir.glob("*.json")):
                data = json.loads(f.read_text(encoding="utf-8"))
                slug = data.get("slug") or f.stem
                _store("category", slug, data)

            # ---- Merchants / Customers / Triggers ----
            for scope, expanded_subdir, seed_file, id_field, container in [
                ("merchant", "merchants", "merchants_seed.json", "merchant_id", "merchants"),
                ("customer", "customers", "customers_seed.json", "customer_id", "customers"),
                ("trigger",  "triggers",  "triggers_seed.json",  "id",          "triggers"),
            ]:
                exp_dir = expanded / expanded_subdir
                if exp_dir.is_dir():
                    for f in sorted(exp_dir.glob("*.json")):
                        item = json.loads(f.read_text(encoding="utf-8"))
                        if not isinstance(item, dict):
                            continue
                        key = item.get(id_field) or item.get(f"{scope}_id") or item.get("id")
                        if key:
                            _store(scope, key, item)
                else:
                    seed_path = dataset / seed_file
                    if seed_path.exists():
                        data = json.loads(seed_path.read_text(encoding="utf-8"))
                        for item in data.get(container, []):
                            key = item.get(id_field) or item.get(f"{scope}_id") or item.get("id")
                            if key:
                                _store(scope, key, item)
        except Exception:
            pass

    async def connect(self) -> None:
        uri = os.getenv("MONGO_URI", "")
        if not mongo_uri_usable(uri):
            self.backend = "memory"
            self.preload_seed_data()
            return
        try:
            from motor.motor_asyncio import AsyncIOMotorClient

            self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=4000)
            await self.client.admin.command("ping")
            self.db = self.client[DB_NAME]
            await self._ensure_indexes()
            existing = await self.db.system_metrics.find_one({"_id": "global_metrics"})
            if existing:
                self._metrics.update(existing)
            else:
                await self.db.system_metrics.update_one(
                    {"_id": "global_metrics"}, {"$setOnInsert": self._metrics}, upsert=True
                )
            self.backend = "mongo"
        except Exception:
            self.backend = "memory"
            self.client = None
            self.db = None
            self.preload_seed_data()

    async def _ensure_indexes(self) -> None:
        if self.db is None:
            return
        await self.db.merchants.create_index("id", unique=True)
        await self.db.merchants.create_index("merchant_id", unique=True)
        await self.db.customers.create_index("id", unique=True)
        await self.db.customers.create_index("customer_id", unique=True)
        await self.db.triggers.create_index("id", unique=True)
        await self.db.triggers.create_index("trigger_id", unique=True)
        await self.db.categories.create_index("id", unique=True)
        await self.db.categories.create_index("category_name", unique=True)
        await self.db.conversations.create_index("conversation_id", unique=True)
        await self.db.contexts.create_index([("scope", 1), ("context_id", 1)], unique=True)
        await self.db.contexts.create_index("id")
        await self.db.merchant_flags.create_index("merchant_id", unique=True)

    async def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def ping_ok(self) -> bool:
        return self.backend == "mongo"

    async def context_counts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        if self.backend == "mongo" and self.db is not None:
            for scope in counts:
                counts[scope] = await self.db.contexts.count_documents({"scope": scope})
            return counts
        for key, doc in self._mem["contexts"].items():
            scope = key.split("|", 1)[0]
            if scope in counts:
                counts[scope] += 1
        return counts

    async def metrics(self) -> dict[str, Any]:
        if self.backend == "mongo" and self.db is not None:
            doc = await self.db.system_metrics.find_one({"_id": "global_metrics"})
            if doc:
                return doc
        return dict(self._metrics)

    async def bump(self, field: str, n: int = 1) -> None:
        if self.backend == "mongo" and self.db is not None:
            await self.db.system_metrics.update_one(
                {"_id": "global_metrics"}, {"$inc": {field: n}}, upsert=True
            )
        self._metrics[field] = int(self._metrics.get(field, 0)) + n

    def _ctx_key(self, scope: str, context_id: str) -> str:
        return f"{scope}|{context_id}"

    async def find_doc(self, scope: str, item_id: str) -> Optional[dict]:
        """Unified document lookup by normalized 'id' or secondary fields."""
        if not item_id:
            return None
        coll_name = scope + "s" if scope != "category" else "categories"
        if self.backend == "mongo" and self.db is not None:
            coll = getattr(self.db, coll_name, None)
            if coll is not None:
                id_fields = ["id", f"{scope}_id"]
                if scope == "category":
                    id_fields.extend(["category_name", "slug"])
                doc = await coll.find_one({"$or": [{k: item_id} for k in id_fields]})
                if doc:
                    return doc
            doc = await self.db.contexts.find_one({"$or": [{"id": item_id}, {"context_id": item_id}]})
            if doc:
                return doc
            return None
        else:
            mem_coll = self._mem.get(coll_name, {})
            if item_id in mem_coll:
                return mem_coll[item_id]
            for key, doc in self._mem.get("contexts", {}).items():
                if doc.get("id") == item_id or doc.get("context_id") == item_id:
                    return doc
                if key == f"{scope}|{item_id}":
                    return doc
            return None

    async def save_doc(self, scope: str, context_id: str, payload: dict) -> dict:
        """Unified document save normalizing by 'id' and 'data'."""
        doc = {
            "id": context_id,
            "scope": scope,
            "data": payload,
            "payload": payload,
            **payload
        }
        coll_name = scope + "s" if scope != "category" else "categories"
        if self.backend == "mongo" and self.db is not None:
            coll = getattr(self.db, coll_name, None)
            if coll is not None:
                await coll.update_one({"id": context_id}, {"$set": doc}, upsert=True)
            await self.db.contexts.update_one({"id": context_id}, {"$set": doc}, upsert=True)
        else:
            self._mem.setdefault(coll_name, {})[context_id] = doc
            self._mem["contexts"][self._ctx_key(scope, context_id)] = doc
        await self.bump("contexts_ingested", 1)
        return {"status": "success", "accepted": True}

    async def get_context(self, scope: str, context_id: str) -> Optional[dict]:
        return await self.find_doc(scope, context_id)

    async def upsert_context(
        self, scope: str, context_id: str, version: int, payload: dict, delivered_at: str
    ) -> dict:
        current = await self.get_context(scope, context_id)
        if current and int(current.get("version", 0)) > int(version):
            return {
                "accepted": False,
                "reason": "stale_version",
                "current_version": int(current.get("version", 0)),
            }

        stored_at = utc_now_iso()
        record = {
            "id": context_id,
            "scope": scope,
            "context_id": context_id,
            "version": int(version),
            "payload": payload,
            "data": payload,
            "delivered_at": delivered_at,
            "stored_at": stored_at,
            **payload
        }
        coll_name = scope + "s" if scope != "category" else "categories"
        if self.backend == "mongo" and self.db is not None:
            await self.db.contexts.update_one(
                {"id": context_id}, {"$set": record}, upsert=True
            )
            coll = getattr(self.db, coll_name, None)
            if coll is not None:
                await coll.update_one({"id": context_id}, {"$set": record}, upsert=True)
        else:
            self._mem["contexts"][self._ctx_key(scope, context_id)] = record
            self._mem.setdefault(coll_name, {})[context_id] = record
        await self.bump("contexts_ingested", 1)
        return {
            "accepted": True,
            "status": "success",
            "ack_id": f"ack_{context_id}_v{version}",
            "stored_at": stored_at,
        }

    async def get_payload(self, scope: str, context_id: str) -> Optional[dict]:
        doc = await self.get_context(scope, context_id)
        if not doc:
            return None
        if "data" in doc and isinstance(doc["data"], dict):
            return copy.deepcopy(doc["data"])
        if "payload" in doc and isinstance(doc["payload"], dict):
            return copy.deepcopy(doc["payload"])
        return copy.deepcopy(doc)

    async def get_conversation(self, conversation_id: str) -> Optional[dict]:
        if self.backend == "mongo" and self.db is not None:
            return await self.db.conversations.find_one({"conversation_id": conversation_id})
        return self._mem["conversations"].get(conversation_id)

    async def save_conversation(self, conv: dict) -> None:
        cid = conv["conversation_id"]
        if self.backend == "mongo" and self.db is not None:
            await self.db.conversations.update_one(
                {"conversation_id": cid}, {"$set": conv}, upsert=True
            )
        else:
            self._mem["conversations"][cid] = conv

    async def ensure_conversation(
        self, conversation_id: str, merchant_id: Optional[str], customer_id: Optional[str]
    ) -> dict:
        existing = await self.get_conversation(conversation_id)
        if existing:
            return existing
        conv = {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "state": "qualifying",
            "canned_reply_count": 0,
            "turns": [],
            "sent_bodies": [],
            "created_at": utc_now_iso(),
        }
        await self.save_conversation(conv)
        await self.bump("active_conversations", 1)
        return conv

    async def mark_suppression(self, key: str) -> None:
        if not key:
            return
        if self.backend == "mongo" and self.db is not None:
            await self.db.sent_suppressions.update_one(
                {"_id": key}, {"$set": {"_id": key, "at": utc_now_iso()}}, upsert=True
            )
        else:
            self._mem["sent_suppressions"][key] = True

    async def was_suppressed(self, key: str) -> bool:
        if not key:
            return False
        if self.backend == "mongo" and self.db is not None:
            return await self.db.sent_suppressions.find_one({"_id": key}) is not None
        return key in self._mem["sent_suppressions"]

    async def bump_merchant_canned(self, merchant_id: str) -> int:
        if not merchant_id:
            return 1
        if self.backend == "mongo" and self.db is not None:
            doc = await self.db.merchant_flags.find_one_and_update(
                {"merchant_id": merchant_id},
                {"$inc": {"canned_reply_count": 1}},
                upsert=True,
                return_document=True,
            )
            return int((doc or {}).get("canned_reply_count") or 1)
        flags = self._mem["merchant_flags"].setdefault(merchant_id, {"canned_reply_count": 0})
        flags["canned_reply_count"] = int(flags.get("canned_reply_count", 0)) + 1
        return flags["canned_reply_count"]

    async def teardown(self) -> None:
        if self.backend == "mongo" and self.db is not None:
            for name in (
                "contexts",
                "conversations",
                "merchant_flags",
                "sent_suppressions",
                "merchants",
                "customers",
                "triggers",
                "categories",
            ):
                await self.db[name].delete_many({})
            await self.db.system_metrics.update_one(
                {"_id": "global_metrics"},
                {
                    "$set": {
                        "contexts_ingested": 0,
                        "active_conversations": 0,
                        "ticks_processed": 0,
                        "total_replies_generated": 0,
                    }
                },
                upsert=True,
            )
        self._mem = {
            "contexts": {},
            "conversations": {},
            "merchant_flags": {},
            "sent_suppressions": {},
        }
        self._metrics = {
            "_id": "global_metrics",
            "contexts_ingested": 0,
            "active_conversations": 0,
            "ticks_processed": 0,
            "total_replies_generated": 0,
        }


store = Store()
