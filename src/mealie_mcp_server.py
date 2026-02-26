"""MCP Mealie — Recipe manager integration for Claude Code."""

import json
import logging
import os

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

mcp = FastMCP("mealie")

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
