"""MCP Mealie — Recipe manager + Chef Assistant knowledge base."""

import json
import logging
import os
import unicodedata
import re

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

MEALIE_URL = os.environ.get("MEALIE_URL", "http://192.168.1.124:9925").rstrip("/")
MEALIE_TOKEN = os.environ.get("MEALIE_TOKEN", "")
MONGODB_URI = os.environ.get("MONGODB_URI", "")

mcp = FastMCP("mealie")

# --- MongoDB client (knowledge base) ---

_mongo_db = None


async def _get_mongo_db():
    global _mongo_db
    if _mongo_db is None and MONGODB_URI:
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(MONGODB_URI)
        _mongo_db = client.get_default_database()
    return _mongo_db


def _normalize(text: str) -> str:
    """Normalise un texte: minuscule, sans accents."""
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text

_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=MEALIE_URL,
            headers={
                "Authorization": f"Bearer {MEALIE_TOKEN}",
                "Content-Type": "application/json",
                "Accept-Language": "fr-FR",
            },
            timeout=30.0,
        )
    return _client


async def _api(method: str, endpoint: str, **kwargs) -> dict:
    client = await _get_client()
    resp = await client.request(method, f"/api/{endpoint}", **kwargs)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
async def search_recipes(query: str, page: int = 1, per_page: int = 10) -> str:
    """Rechercher des recettes dans Mealie par mot-cle.

    Args:
        query: Terme de recherche (ex: "poulet", "dessert", "rapide")
        page: Numero de page (defaut: 1)
        per_page: Resultats par page (defaut: 10)
    """
    try:
        data = await _api("GET", "recipes", params={
            "search": query,
            "page": page,
            "perPage": per_page,
            "orderBy": "name",
            "orderDirection": "asc",
        })
        items = data.get("items", [])
        results = []
        for r in items:
            results.append({
                "id": r.get("id"),
                "slug": r.get("slug"),
                "name": r.get("name"),
                "description": r.get("description", ""),
                "total_time": r.get("totalTime", ""),
                "rating": r.get("rating"),
                "tags": [t.get("name", "") for t in r.get("tags", [])],
            })
        return json.dumps({
            "total": data.get("total", 0),
            "page": page,
            "results": results,
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"search_recipes error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_recipe(slug: str) -> str:
    """Obtenir les details complets d'une recette par son slug.

    Args:
        slug: Slug de la recette (obtenu via search_recipes)
    """
    try:
        r = await _api("GET", f"recipes/{slug}")
        return json.dumps({
            "id": r.get("id"),
            "name": r.get("name"),
            "slug": r.get("slug"),
            "description": r.get("description", ""),
            "prep_time": r.get("prepTime", ""),
            "cook_time": r.get("cookTime", ""),
            "total_time": r.get("totalTime", ""),
            "servings": r.get("recipeYield", ""),
            "rating": r.get("rating"),
            "tags": [t.get("name", "") for t in r.get("tags", [])],
            "categories": [c.get("name", "") for c in r.get("recipeCategory", [])],
            "ingredients": [
                i.get("display", i.get("note", ""))
                for i in r.get("recipeIngredient", [])
            ],
            "instructions": [
                {"step": idx + 1, "text": s.get("text", "")}
                for idx, s in enumerate(r.get("recipeInstructions", []))
            ],
            "notes": [n.get("text", "") for n in r.get("notes", [])],
            "nutrition": r.get("nutrition", {}),
        }, ensure_ascii=False)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return json.dumps({"error": f"Recette non trouvee: {slug}"})
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"get_recipe error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def create_recipe(
    name: str,
    description: str = "",
    ingredients: list[str] | None = None,
    instructions: list[str] | None = None,
    prep_time: str = "",
    cook_time: str = "",
    total_time: str = "",
    servings: str = "",
    tags: list[str] | None = None,
) -> str:
    """Creer une nouvelle recette dans Mealie.

    Args:
        name: Nom de la recette
        description: Description courte
        ingredients: Liste des ingredients (texte libre par ingredient)
        instructions: Liste des etapes de preparation
        prep_time: Temps de preparation (ex: "15 min")
        cook_time: Temps de cuisson (ex: "30 min")
        total_time: Temps total (ex: "45 min")
        servings: Nombre de portions (ex: "4 personnes")
        tags: Tags/categories (ex: ["dessert", "rapide"])
    """
    try:
        # Step 1: Create empty recipe
        r = await _api("POST", "recipes", json={"name": name})
        slug = r  # Mealie returns the slug as string

        # Step 2: Update with full details
        update_data = {
            "name": name,
            "description": description,
            "prepTime": prep_time,
            "cookTime": cook_time,
            "totalTime": total_time,
            "recipeYield": servings,
        }

        if ingredients:
            update_data["recipeIngredient"] = [
                {"note": ing, "display": ing} for ing in ingredients
            ]

        if instructions:
            update_data["recipeInstructions"] = [
                {"text": step} for step in instructions
            ]

        if tags:
            update_data["tags"] = [{"name": t} for t in tags]

        await _api("PATCH", f"recipes/{slug}", json=update_data)

        return json.dumps({
            "success": True,
            "slug": slug,
            "name": name,
            "url": f"{MEALIE_URL}/recipe/{slug}",
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"create_recipe error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_meal_plan(start_date: str = "", end_date: str = "") -> str:
    """Obtenir le plan de repas.

    Args:
        start_date: Date de debut (YYYY-MM-DD, defaut: aujourd'hui)
        end_date: Date de fin (YYYY-MM-DD, defaut: +7 jours)
    """
    try:
        from datetime import date, timedelta

        if not start_date:
            start_date = date.today().isoformat()
        if not end_date:
            end_date = (date.today() + timedelta(days=7)).isoformat()

        data = await _api("GET", "households/mealplans", params={
            "start_date": start_date,
            "end_date": end_date,
        })

        items = data.get("items", []) if isinstance(data, dict) else data
        plans = []
        for p in items:
            plans.append({
                "id": p.get("id"),
                "date": p.get("date"),
                "entry_type": p.get("entryType", ""),
                "title": p.get("title", ""),
                "recipe_id": p.get("recipeId"),
                "recipe": p.get("recipe", {}).get("name", "") if p.get("recipe") else "",
            })

        return json.dumps({
            "start_date": start_date,
            "end_date": end_date,
            "plans": sorted(plans, key=lambda x: x.get("date", "")),
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"get_meal_plan error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def add_to_meal_plan(
    date: str,
    recipe_slug: str = "",
    title: str = "",
    entry_type: str = "dinner",
) -> str:
    """Ajouter une recette ou un repas au plan de la semaine.

    Args:
        date: Date du repas (YYYY-MM-DD)
        recipe_slug: Slug de la recette (optionnel si title fourni)
        title: Titre libre du repas (si pas de recette associee)
        entry_type: Type de repas: "breakfast", "lunch", "dinner", "snack" (defaut: dinner)
    """
    try:
        payload = {
            "date": date,
            "entryType": entry_type,
        }

        if recipe_slug:
            # Get recipe ID from slug
            recipe = await _api("GET", f"recipes/{recipe_slug}")
            payload["recipeId"] = recipe.get("id")
            payload["title"] = recipe.get("name", title)
        elif title:
            payload["title"] = title
        else:
            return json.dumps({"error": "recipe_slug ou title requis"})

        result = await _api("POST", "households/mealplans", json=payload)
        return json.dumps({
            "success": True,
            "id": result.get("id"),
            "date": date,
            "entry_type": entry_type,
            "title": payload.get("title", ""),
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"add_to_meal_plan error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def generate_shopping_list(name: str = "Liste de courses", recipe_slugs: list[str] | None = None) -> str:
    """Generer une liste de courses a partir de recettes.

    Args:
        name: Nom de la liste (defaut: "Liste de courses")
        recipe_slugs: Slugs des recettes a inclure (optionnel)
    """
    try:
        # Create shopping list
        result = await _api("POST", "households/shopping/lists", json={"name": name})
        list_id = result.get("id")

        if recipe_slugs:
            for slug in recipe_slugs:
                try:
                    recipe = await _api("GET", f"recipes/{slug}")
                    for ing in recipe.get("recipeIngredient", []):
                        display = ing.get("display", ing.get("note", ""))
                        if display:
                            await _api(
                                "POST",
                                f"households/shopping/lists/{list_id}/items",
                                json={
                                    "note": display,
                                    "isFood": True,
                                },
                            )
                except Exception as e:
                    logger.warning(f"Could not add ingredients from {slug}: {e}")

        # Fetch the complete list
        shopping_list = await _api("GET", f"households/shopping/lists/{list_id}")
        items = shopping_list.get("listItems", [])

        return json.dumps({
            "success": True,
            "id": list_id,
            "name": name,
            "items": [
                {
                    "note": i.get("note", ""),
                    "checked": i.get("checked", False),
                }
                for i in items
            ],
            "total_items": len(items),
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"generate_shopping_list error: {e}")
        return json.dumps({"error": str(e)})


# --- Nouveaux outils: Import, Update, Scrapers, Knowledge Base ---


@mcp.tool()
async def import_recipe_from_url(url: str) -> str:
    """Importer une recette depuis une URL (Marmiton, 750g, Cuisine AZ, etc.).

    Utilise le scraper natif de Mealie qui parse le JSON-LD schema.org/Recipe.

    Args:
        url: URL de la page recette
    """
    try:
        result = await _api("POST", "recipes/create/url", json={"url": url, "includeTags": True})
        slug = result if isinstance(result, str) else result.get("slug", "")
        if slug:
            recipe = await _api("GET", f"recipes/{slug}")
            return json.dumps({
                "success": True,
                "slug": slug,
                "name": recipe.get("name", ""),
                "url": f"{MEALIE_URL}/recipe/{slug}",
                "ingredients_count": len(recipe.get("recipeIngredient", [])),
                "instructions_count": len(recipe.get("recipeInstructions", [])),
            }, ensure_ascii=False)
        return json.dumps({"success": True, "slug": str(result)})
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 422:
            return json.dumps({"error": f"Impossible de parser la recette depuis {url}"})
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"import_recipe_from_url error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def update_recipe(
    slug: str,
    name: str = "",
    description: str = "",
    ingredients: list[str] | None = None,
    instructions: list[str] | None = None,
    prep_time: str = "",
    cook_time: str = "",
    total_time: str = "",
    servings: str = "",
    tags: list[str] | None = None,
    notes: list[str] | None = None,
) -> str:
    """Modifier une recette existante dans Mealie.

    Seuls les champs fournis sont mis a jour (les autres restent inchanges).

    Args:
        slug: Slug de la recette a modifier
        name: Nouveau nom (optionnel)
        description: Nouvelle description (optionnel)
        ingredients: Nouvelle liste d'ingredients (remplace l'existante)
        instructions: Nouvelles etapes (remplace l'existante)
        prep_time: Temps de preparation
        cook_time: Temps de cuisson
        total_time: Temps total
        servings: Nombre de portions
        tags: Tags (remplace les existants)
        notes: Notes du chef (ajoutees aux existantes)
    """
    try:
        # Verifier que la recette existe
        existing = await _api("GET", f"recipes/{slug}")

        update_data = {}
        if name:
            update_data["name"] = name
        if description:
            update_data["description"] = description
        if prep_time:
            update_data["prepTime"] = prep_time
        if cook_time:
            update_data["cookTime"] = cook_time
        if total_time:
            update_data["totalTime"] = total_time
        if servings:
            update_data["recipeYield"] = servings
        if ingredients is not None:
            update_data["recipeIngredient"] = [
                {"note": ing, "display": ing} for ing in ingredients
            ]
        if instructions is not None:
            update_data["recipeInstructions"] = [
                {"text": step} for step in instructions
            ]
        if tags is not None:
            update_data["tags"] = [{"name": t} for t in tags]
        if notes is not None:
            existing_notes = existing.get("notes", [])
            new_notes = existing_notes + [{"text": n} for n in notes]
            update_data["notes"] = new_notes

        if not update_data:
            return json.dumps({"error": "Aucun champ a modifier"})

        await _api("PATCH", f"recipes/{slug}", json=update_data)
        return json.dumps({
            "success": True,
            "slug": slug,
            "updated_fields": list(update_data.keys()),
            "url": f"{MEALIE_URL}/recipe/{slug}",
        }, ensure_ascii=False)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return json.dumps({"error": f"Recette non trouvee: {slug}"})
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error(f"update_recipe error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def search_web_recipes(
    query: str,
    sources: list[str] | None = None,
    max_results: int = 5,
) -> str:
    """Rechercher des recettes sur plusieurs sites francophones.

    Sources disponibles: marmiton, 750g, cuisineaz.
    Par defaut, recherche sur toutes les sources.

    Args:
        query: Terme de recherche (ex: "tarte tatin", "risotto champignons")
        sources: Sites a interroger (defaut: tous). Ex: ["marmiton", "750g"]
        max_results: Resultats par source (defaut: 5, max: 10)
    """
    try:
        from scrapers import search_all_sources
        max_results = min(max_results, 10)
        results = await search_all_sources(query, sources=sources, max_results=max_results)
        return json.dumps({
            "query": query,
            "sources": sources or ["marmiton", "750g", "cuisineaz"],
            "total": len(results),
            "results": [r.to_dict() for r in results],
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"search_web_recipes error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def scrape_marmiton(query: str, max_results: int = 5) -> str:
    """Rechercher des recettes sur Marmiton.org.

    Retourne titre, URL, note, temps, difficulte et ingredients.
    Utiliser import_recipe_from_url() pour importer une recette dans Mealie.

    Args:
        query: Terme de recherche (ex: "blanquette de veau", "gateau chocolat")
        max_results: Nombre maximum de resultats (1-10, defaut: 5)
    """
    try:
        from scrapers import MarmitonScraper
        scraper = MarmitonScraper()
        try:
            results = await scraper.search(query, max_results=min(max_results, 10))
            return json.dumps({
                "query": query,
                "source": "marmiton",
                "total": len(results),
                "results": [r.to_dict() for r in results],
            }, ensure_ascii=False)
        finally:
            await scraper.close()
    except Exception as e:
        logger.error(f"scrape_marmiton error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def scrape_750g(query: str, max_results: int = 5) -> str:
    """Rechercher des recettes sur 750g.com.

    Retourne titre, URL, note, temps et ingredients.
    Utiliser import_recipe_from_url() pour importer une recette dans Mealie.

    Args:
        query: Terme de recherche (ex: "quiche lorraine", "tiramisu")
        max_results: Nombre maximum de resultats (1-10, defaut: 5)
    """
    try:
        from scrapers import SevenFiftyGScraper
        scraper = SevenFiftyGScraper()
        try:
            results = await scraper.search(query, max_results=min(max_results, 10))
            return json.dumps({
                "query": query,
                "source": "750g",
                "total": len(results),
                "results": [r.to_dict() for r in results],
            }, ensure_ascii=False)
        finally:
            await scraper.close()
    except Exception as e:
        logger.error(f"scrape_750g error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def query_knowledge_base(
    collection: str,
    query: str = "",
    category: str = "",
    month: int = 0,
) -> str:
    """Interroger la base de connaissances culinaires.

    Collections: techniques, ingredient_substitutions, seasonal_ingredients,
    flavor_pairings, recipe_inspirations.

    Args:
        collection: Collection a interroger
        query: Terme de recherche libre
        category: Filtrer par categorie (ex: "cuisson", "legume", "patisserie")
        month: Mois (1-12) pour les ingredients de saison
    """
    try:
        db = await _get_mongo_db()
        if db is None:
            return json.dumps({"error": "MongoDB non configure (MONGODB_URI manquant)"})

        coll = db[collection]
        mongo_filter: dict = {}

        # Filtre par texte
        if query:
            normalized = _normalize(query)
            if collection == "ingredient_substitutions":
                mongo_filter["original_normalized"] = {"$regex": normalized, "$options": "i"}
            elif collection == "flavor_pairings":
                mongo_filter["$or"] = [
                    {"ingredient_a_normalized": {"$regex": normalized, "$options": "i"}},
                    {"ingredient_b_normalized": {"$regex": normalized, "$options": "i"}},
                ]
            else:
                mongo_filter["$or"] = [
                    {"name_normalized": {"$regex": normalized, "$options": "i"}},
                    {"ingredient_normalized": {"$regex": normalized, "$options": "i"}},
                    {"name": {"$regex": query, "$options": "i"}},
                ]

        # Filtre par categorie
        if category:
            mongo_filter["category"] = {"$regex": _normalize(category), "$options": "i"}

        # Filtre par mois (saison)
        if month and 1 <= month <= 12:
            mongo_filter["months"] = month

        cursor = coll.find(mongo_filter, {"_id": 0}).limit(20)
        results = await cursor.to_list(length=20)

        return json.dumps({
            "collection": collection,
            "count": len(results),
            "results": results,
        }, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error(f"query_knowledge_base error: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def add_to_knowledge_base(
    collection: str,
    data: str,
) -> str:
    """Ajouter une entree a la base de connaissances culinaires.

    Args:
        collection: Collection cible (techniques, ingredient_substitutions,
                    seasonal_ingredients, flavor_pairings, recipe_inspirations)
        data: Document JSON a inserer (format specifique a la collection)
    """
    try:
        db = await _get_mongo_db()
        if db is None:
            return json.dumps({"error": "MongoDB non configure (MONGODB_URI manquant)"})

        doc = json.loads(data) if isinstance(data, str) else data

        # Auto-normalisation des champs
        if "name" in doc and "name_normalized" not in doc:
            doc["name_normalized"] = _normalize(doc["name"])
        if "ingredient" in doc and "ingredient_normalized" not in doc:
            doc["ingredient_normalized"] = _normalize(doc["ingredient"])
        if "original" in doc and "original_normalized" not in doc:
            doc["original_normalized"] = _normalize(doc["original"])
        if "ingredient_a" in doc and "ingredient_a_normalized" not in doc:
            doc["ingredient_a_normalized"] = _normalize(doc["ingredient_a"])
        if "ingredient_b" in doc and "ingredient_b_normalized" not in doc:
            doc["ingredient_b_normalized"] = _normalize(doc["ingredient_b"])

        # Ajout timestamp
        from datetime import datetime, timezone
        doc["added_at"] = datetime.now(timezone.utc).isoformat()

        coll = db[collection]
        result = await coll.insert_one(doc)

        return json.dumps({
            "success": True,
            "collection": collection,
            "inserted_id": str(result.inserted_id),
        }, ensure_ascii=False)
    except json.JSONDecodeError:
        return json.dumps({"error": "Format JSON invalide pour le champ data"})
    except Exception as e:
        logger.error(f"add_to_knowledge_base error: {e}")
        return json.dumps({"error": str(e)})


def main():
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport in ("sse", "http", "streamable-http"):
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8000"))
        mcp.run(transport=transport, host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
