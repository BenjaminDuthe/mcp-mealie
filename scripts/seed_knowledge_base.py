#!/usr/bin/env python3
"""Seed the chef_knowledge MongoDB database with curated culinary data."""

import asyncio
import json
import os
import sys
import unicodedata
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

SEEDS_DIR = Path(__file__).parent.parent / "data" / "seeds"
MONGODB_URI = os.environ.get(
    "MONGODB_URI",
    "mongodb://chef_assistant:J9BRqDkH3ALEUbeJSa0TgNCn@192.168.1.123:27017/chef_knowledge?authSource=chef_knowledge",
)


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


def add_normalized_fields(doc: dict) -> dict:
    """Add normalized fields for indexing."""
    if "name" in doc:
        doc["name_normalized"] = normalize(doc["name"])
    if "ingredient" in doc:
        doc["ingredient_normalized"] = normalize(doc["ingredient"])
    if "original" in doc:
        doc["original_normalized"] = normalize(doc["original"])
    if "ingredient_a" in doc:
        doc["ingredient_a_normalized"] = normalize(doc["ingredient_a"])
    if "ingredient_b" in doc:
        doc["ingredient_b_normalized"] = normalize(doc["ingredient_b"])
    doc["added_by"] = "seed"
    return doc


async def seed_collection(db, collection_name: str, filename: str) -> int:
    """Seed a single collection from a JSON file."""
    filepath = SEEDS_DIR / filename
    if not filepath.exists():
        print(f"  SKIP {filename} (fichier non trouve)")
        return 0

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    coll = db[collection_name]
    existing = await coll.count_documents({})
    if existing > 0:
        print(f"  {collection_name}: {existing} documents existants, nettoyage des seeds...")
        await coll.delete_many({"added_by": "seed"})

    docs = [add_normalized_fields(doc) for doc in data]

    if docs:
        result = await coll.insert_many(docs)
        count = len(result.inserted_ids)
        print(f"  {collection_name}: {count} documents inseres")
        return count
    return 0


async def main():
    print(f"Connexion a MongoDB: {MONGODB_URI.split('@')[1] if '@' in MONGODB_URI else MONGODB_URI}")
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client.get_default_database()

    # Verify connection
    await db.command("ping")
    print("Connexion OK\n")

    collections = {
        "techniques": "techniques.json",
        "ingredient_substitutions": "substitutions.json",
        "seasonal_ingredients": "seasonal_france.json",
        "flavor_pairings": "flavor_pairings.json",
    }

    total = 0
    for coll_name, filename in collections.items():
        count = await seed_collection(db, coll_name, filename)
        total += count

    print(f"\nTotal: {total} documents inseres dans {len(collections)} collections")

    # Summary
    print("\nResume:")
    for coll_name in collections:
        count = await db[coll_name].count_documents({})
        print(f"  {coll_name}: {count} documents")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
