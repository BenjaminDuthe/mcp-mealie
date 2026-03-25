"""Scrapers multi-sources pour recettes francophones (Marmiton, 750g, Cuisine AZ)."""

import asyncio
import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
DELAY_SECONDS = 2.0
MAX_RETRIES = 3


@dataclass
class ScrapedRecipe:
    """Resultat de recherche d'une recette scrapee."""

    title: str
    url: str
    source: str
    description: str = ""
    rating: float = 0.0
    rating_count: int = 0
    prep_time: str = ""
    cook_time: str = ""
    total_time: str = ""
    servings: int = 0
    difficulty: str = ""
    cost: str = ""
    image_url: str = ""
    ingredients: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()[:12]


class RecipeScraper(ABC):
    """Classe de base pour les scrapers de recettes."""

    source_name: str = ""
    base_url: str = ""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._last_request_time: float = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    async def _fetch(self, url: str) -> str | None:
        """Fetch une URL avec rate limiting et retry."""
        # Rate limiting
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < DELAY_SECONDS:
            await asyncio.sleep(DELAY_SECONDS - elapsed)

        client = await self._get_client()
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._last_request_time = asyncio.get_event_loop().time()
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.text
            except httpx.HTTPStatusError as e:
                logger.warning(f"[{self.source_name}] HTTP {e.response.status_code} on {url} (attempt {attempt})")
                if e.response.status_code == 429:
                    await asyncio.sleep(DELAY_SECONDS * attempt * 2)
                elif e.response.status_code >= 500:
                    await asyncio.sleep(DELAY_SECONDS * attempt)
                else:
                    return None
            except httpx.RequestError as e:
                logger.warning(f"[{self.source_name}] Request error on {url}: {e} (attempt {attempt})")
                await asyncio.sleep(DELAY_SECONDS * attempt)
        logger.error(f"[{self.source_name}] Failed after {MAX_RETRIES} attempts: {url}")
        return None

    def _extract_json_ld(self, soup: BeautifulSoup) -> dict | None:
        """Extrait le JSON-LD schema.org/Recipe d'une page."""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                # JSON-LD peut etre un objet ou un tableau
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "Recipe":
                            return item
                elif isinstance(data, dict):
                    if data.get("@type") == "Recipe":
                        return data
                    # Parfois imbrique dans @graph
                    for item in data.get("@graph", []):
                        if isinstance(item, dict) and item.get("@type") == "Recipe":
                            return item
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    def _parse_duration(self, iso_duration: str | None) -> str:
        """Convertit une duree ISO 8601 (PT1H30M) en texte lisible."""
        if not iso_duration:
            return ""
        match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(iso_duration))
        if not match:
            return str(iso_duration)
        hours, minutes, seconds = match.groups()
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}min")
        if seconds and not hours and not minutes:
            parts.append(f"{seconds}s")
        return " ".join(parts) if parts else ""

    def _parse_duration_minutes(self, iso_duration: str | None) -> int:
        """Convertit une duree ISO 8601 en minutes."""
        if not iso_duration:
            return 0
        match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(iso_duration))
        if not match:
            return 0
        hours, minutes = match.groups()
        return int(hours or 0) * 60 + int(minutes or 0)

    def _recipe_from_json_ld(self, data: dict, url: str) -> ScrapedRecipe:
        """Cree un ScrapedRecipe a partir de JSON-LD."""
        # Ingredients
        ingredients = data.get("recipeIngredient", [])
        if isinstance(ingredients, str):
            ingredients = [ingredients]

        # Rating
        rating = 0.0
        rating_count = 0
        agg = data.get("aggregateRating")
        if isinstance(agg, dict):
            rating = float(agg.get("ratingValue", 0))
            rating_count = int(agg.get("ratingCount", agg.get("reviewCount", 0)))

        # Image
        image_url = ""
        img = data.get("image")
        if isinstance(img, str):
            image_url = img
        elif isinstance(img, list) and img:
            image_url = img[0] if isinstance(img[0], str) else img[0].get("url", "")
        elif isinstance(img, dict):
            image_url = img.get("url", "")

        # Servings
        servings = 0
        recipe_yield = data.get("recipeYield")
        if isinstance(recipe_yield, list) and recipe_yield:
            recipe_yield = recipe_yield[0]
        if recipe_yield:
            match = re.search(r"(\d+)", str(recipe_yield))
            if match:
                servings = int(match.group(1))

        # Tags
        tags = []
        for key in ("recipeCategory", "recipeCuisine", "keywords"):
            val = data.get(key)
            if isinstance(val, str):
                tags.extend(t.strip() for t in val.split(",") if t.strip())
            elif isinstance(val, list):
                tags.extend(str(t).strip() for t in val if t)

        return ScrapedRecipe(
            title=data.get("name", ""),
            url=url,
            source=self.source_name,
            description=data.get("description", ""),
            rating=rating,
            rating_count=rating_count,
            prep_time=self._parse_duration(data.get("prepTime")),
            cook_time=self._parse_duration(data.get("cookTime")),
            total_time=self._parse_duration(data.get("totalTime")),
            servings=servings,
            image_url=image_url,
            ingredients=ingredients,
            tags=list(set(tags)),
        )

    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> list[ScrapedRecipe]:
        """Recherche des recettes. A implementer par chaque scraper."""
        ...

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class MarmitonScraper(RecipeScraper):
    """Scraper pour Marmiton.org."""

    source_name = "marmiton"
    base_url = "https://www.marmiton.org"

    async def search(self, query: str, max_results: int = 5) -> list[ScrapedRecipe]:
        search_url = f"{self.base_url}/recettes/recherche.aspx?aqt={query}"
        html = await self._fetch(search_url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        results: list[ScrapedRecipe] = []

        # Methode 1: JSON-LD ItemList sur la page de recherche
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "ItemList":
                    for item in data.get("itemListElement", []):
                        if len(results) >= max_results:
                            break
                        url = item.get("url", "")
                        if url:
                            results.append(ScrapedRecipe(
                                title=item.get("name", ""),
                                url=url,
                                source=self.source_name,
                            ))
            except (json.JSONDecodeError, TypeError):
                continue

        # Methode 2: fallback HTML — parser les cartes de recette
        if not results:
            cards = soup.select("a.MRTN__sc-1gofnyi-2, a[href*='/recettes/recette_']")
            seen_urls = set()
            for card in cards:
                if len(results) >= max_results:
                    break
                href = card.get("href", "")
                if not href or "/recettes/recette_" not in href:
                    continue
                url = href if href.startswith("http") else f"{self.base_url}{href}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                title_el = card.select_one("h4, .MRTN__sc-1gofnyi-3, [class*='title']")
                title = title_el.get_text(strip=True) if title_el else card.get_text(strip=True)[:80]

                results.append(ScrapedRecipe(
                    title=title,
                    url=url,
                    source=self.source_name,
                ))

        # Enrichir les resultats avec les details JSON-LD de chaque recette
        enriched = []
        for recipe in results[:max_results]:
            detail = await self._enrich_recipe(recipe.url)
            enriched.append(detail if detail else recipe)

        return enriched

    async def _enrich_recipe(self, url: str) -> ScrapedRecipe | None:
        """Recupere les details JSON-LD d'une recette individuelle."""
        html = await self._fetch(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        json_ld = self._extract_json_ld(soup)
        if json_ld:
            return self._recipe_from_json_ld(json_ld, url)

        # Fallback HTML basique
        title_el = soup.select_one("h1")
        title = title_el.get_text(strip=True) if title_el else ""
        return ScrapedRecipe(title=title, url=url, source=self.source_name)


class SevenFiftyGScraper(RecipeScraper):
    """Scraper pour 750g.com."""

    source_name = "750g"
    base_url = "https://www.750g.com"

    async def search(self, query: str, max_results: int = 5) -> list[ScrapedRecipe]:
        search_url = f"{self.base_url}/recettes/recherche.aspx?q={query}"
        html = await self._fetch(search_url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        results: list[ScrapedRecipe] = []

        # Chercher les liens de recettes
        cards = soup.select("a[href*='/recettes/']")
        seen_urls = set()
        for card in cards:
            if len(results) >= max_results:
                break
            href = card.get("href", "")
            if not href or not re.search(r"/recettes/[\w-]+-\d+", href):
                continue
            url = href if href.startswith("http") else f"{self.base_url}{href}"
            if url in seen_urls:
                continue
            seen_urls.add(url)

            title_el = card.select_one("h2, h3, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else card.get_text(strip=True)[:80]
            if not title or len(title) < 3:
                continue

            results.append(ScrapedRecipe(
                title=title,
                url=url,
                source=self.source_name,
            ))

        # Enrichir avec JSON-LD
        enriched = []
        for recipe in results[:max_results]:
            detail = await self._enrich_recipe(recipe.url)
            enriched.append(detail if detail else recipe)
        return enriched

    async def _enrich_recipe(self, url: str) -> ScrapedRecipe | None:
        html = await self._fetch(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        json_ld = self._extract_json_ld(soup)
        if json_ld:
            return self._recipe_from_json_ld(json_ld, url)
        title_el = soup.select_one("h1")
        title = title_el.get_text(strip=True) if title_el else ""
        return ScrapedRecipe(title=title, url=url, source=self.source_name)


class CuisineAZScraper(RecipeScraper):
    """Scraper pour cuisineaz.com."""

    source_name = "cuisineaz"
    base_url = "https://www.cuisineaz.com"

    async def search(self, query: str, max_results: int = 5) -> list[ScrapedRecipe]:
        search_url = f"{self.base_url}/recettes/recherche_terme.aspx?recherche={query}"
        html = await self._fetch(search_url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        results: list[ScrapedRecipe] = []

        # Chercher les liens de recettes
        cards = soup.select("a[href*='/recettes/']")
        seen_urls = set()
        for card in cards:
            if len(results) >= max_results:
                break
            href = card.get("href", "")
            if not href or not re.search(r"/recettes/[\w-]+\.aspx", href):
                continue
            url = href if href.startswith("http") else f"{self.base_url}{href}"
            if url in seen_urls:
                continue
            seen_urls.add(url)

            title_el = card.select_one("h2, h3, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else card.get_text(strip=True)[:80]
            if not title or len(title) < 3:
                continue

            results.append(ScrapedRecipe(
                title=title,
                url=url,
                source=self.source_name,
            ))

        # Enrichir avec JSON-LD
        enriched = []
        for recipe in results[:max_results]:
            detail = await self._enrich_recipe(recipe.url)
            enriched.append(detail if detail else recipe)
        return enriched

    async def _enrich_recipe(self, url: str) -> ScrapedRecipe | None:
        html = await self._fetch(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        json_ld = self._extract_json_ld(soup)
        if json_ld:
            return self._recipe_from_json_ld(json_ld, url)
        title_el = soup.select_one("h1")
        title = title_el.get_text(strip=True) if title_el else ""
        return ScrapedRecipe(title=title, url=url, source=self.source_name)


# Registry des scrapers disponibles
SCRAPERS: dict[str, type[RecipeScraper]] = {
    "marmiton": MarmitonScraper,
    "750g": SevenFiftyGScraper,
    "cuisineaz": CuisineAZScraper,
}


async def search_all_sources(
    query: str,
    sources: list[str] | None = None,
    max_results: int = 5,
) -> list[ScrapedRecipe]:
    """Recherche sur plusieurs sources en parallele."""
    source_names = sources or list(SCRAPERS.keys())
    all_results: list[ScrapedRecipe] = []

    for source_name in source_names:
        scraper_cls = SCRAPERS.get(source_name)
        if not scraper_cls:
            logger.warning(f"Source inconnue: {source_name}")
            continue
        scraper = scraper_cls()
        try:
            results = await scraper.search(query, max_results=max_results)
            all_results.extend(results)
        except Exception as e:
            logger.error(f"Erreur scraping {source_name}: {e}")
        finally:
            await scraper.close()

    return all_results
