from __future__ import annotations
import asyncio
import logging
from pathlib import Path
import pandas as pd
from worldathletics import worldathletics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

OUTPUT_CSV = Path("world_athletics_results.csv")

# Colonnes fixes pour garantir un schéma cohérent à chaque écriture
COLUMNS = [
    "competition_id",
    "competition_name",
    "date_range",
    "event_title",
    "event_name",
    "race_name",
    "place",
    "competitor_name",
    "iaaf_id",
    "country",
    "mark",
    "wind",
]


def load_processed_competition_ids(csv_path: Path) -> set[str]:
    """Récupère les IDs déjà enregistrés pour permettre une reprise sans doublons."""
    if not csv_path.exists():
        return set()
    df_existing = pd.read_csv(csv_path, usecols=["competition_id"], dtype={"competition_id": str})
    return set(df_existing["competition_id"].dropna().unique())


def append_to_csv(rows: list[dict], csv_path: Path) -> None:
    """Convertit un lot de lignes en DataFrame et l'ajoute au CSV."""
    if not rows:
        return
    df = pd.DataFrame(rows, columns=COLUMNS)
    file_exists = csv_path.exists()
    df.to_csv(
        csv_path,
        mode="a",
        index=False,
        header=not file_exists,
        encoding="utf-8-sig",  # UTF-8 avec BOM pour une ouverture propre dans Excel
    )


async def fetch_with_retry(coro_fn, max_retries: int = 3, base_delay: float = 2.0):
    """Exécute un appel API asynchrone avec backoff exponentiel en cas d'erreur réseau."""
    for attempt in range(1, max_retries + 1):
        try:
            return await coro_fn()
        except Exception as e:
            if attempt == max_retries:
                raise
            wait = base_delay * (2 ** (attempt - 1))
            logger.warning(f"Erreur API ({e}). Nouvelle tentative dans {wait:.1f}s ({attempt}/{max_retries})...")
            await asyncio.sleep(wait)


def parse_competition_results(competition, comp_results_resp) -> list[dict]:
    """Aplatit la structure imbriquée d'une compétition en liste de dictionnaires."""
    rows = []
    comp_data = getattr(comp_results_resp, "get_calendar_competition_results", None)
    event_titles = getattr(comp_data, "event_titles", None) or []

    for event_title in event_titles:
        events = getattr(event_title, "events", None) or []
        for event in events:
            races = getattr(event, "races", None) or []
            for race in races:
                results = getattr(race, "results", None) or []
                for result in results:
                    competitor = getattr(result, "competitor", None)
                    rows.append({
                        "competition_id": str(getattr(competition, "id", "")),
                        "competition_name": getattr(competition, "name", None),
                        "date_range": getattr(competition, "date_range", None),
                        "event_title": getattr(event_title, "event_title", None),
                        "event_name": getattr(event, "event", None),
                        "race_name": getattr(race, "race", None),
                        "place": getattr(result, "place", None),
                        "competitor_name": getattr(competitor, "name", None),
                        "iaaf_id": getattr(competitor, "iaaf_id", None),
                        "country": getattr(competitor, "country", None),
                        "mark": getattr(result, "mark", None),
                        "wind": getattr(result, "wind", None),
                    })
    return rows


async def main():
    client = worldathletics.WorldAthletics()
    processed_ids = load_processed_competition_ids(OUTPUT_CSV)
    if processed_ids:
        logger.info(f"Reprise détectée : {len(processed_ids)} compétitions déjà présentes dans {OUTPUT_CSV}.")

    offset = 0
    limit = 100

    while True:
        logger.info(f"Récupération du calendrier (offset={offset}, limit={limit})...")
        calendar_resp = await fetch_with_retry(
            lambda: client.get_calendar_events(
                hide_competitions_with_no_results=True,
                limit=limit,
                region_type="world",
                offset=offset,
                show_options_with_no_hits=False,
                order_direction="Ascending",
                competition_group_id=None,
                competition_subgroup_id=None,
                discipline_id=5,
                end_date="2026-09-30",
                permit_level_id=2,
                query=None,
                ranking_category_id=None,
                region_id=None,
                start_date="1990-01-01",
            )
        )

        competitions = getattr(calendar_resp.get_calendar_events, "results", None) or []
        if not competitions:
            break

        for competition in competitions:
            comp_id = str(competition.id)
            if comp_id in processed_ids:
                continue

            logger.info(f"Extraction : {competition.name} [{competition.date_range}] (ID: {comp_id})")
            try:
                results_resp = await fetch_with_retry(
                    lambda cid=competition.id: client.get_calendar_competition_results(competition_id=cid)
                )
                rows = parse_competition_results(competition, results_resp)
                append_to_csv(rows, OUTPUT_CSV)
                processed_ids.add(comp_id)
                logger.info(f"  -> {len(rows)} lignes ajoutées.")
            except Exception as e:
                logger.error(f"Échec définitif sur la compétition {comp_id} ({competition.name}) : {e}")

            # Pause légère pour éviter le rate-limiting AWS AppSync
            await asyncio.sleep(0.3)

        if len(competitions) < limit:
            break
        offset += limit

    logger.info(f"Terminé. Fichier disponible : {OUTPUT_CSV.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())