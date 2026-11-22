from __future__ import annotations

import csv
from html.parser import HTMLParser
import logging
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# vibe coded and not based on kaijchang/worldathletics project 


logger = logging.getLogger(__name__)

RANKINGS_URL = "https://worldathletics.org/world-rankings/overall-ranking"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
BASE_DELAY = 2.0
PAGE_DELAY = 0.3
TOTAL_PAGES = 250

_CATEGORY_SLUGS = {"male": "men", "female": "women"}
_CELL_FIELDS = {
    "Rank": "rank",
    "Competitor": "competitor_name",
    "DOB": "birth_date",
    "Nat": "country_code",
    "score": "ranking_score",
    "EventList": "events",
}
_CSV_FIELDS = ["athlete_id", "athlete_url", *_CELL_FIELDS.values()]


class _RankingTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rankings: list[dict[str, str | int | None]] = []
        self._row: dict[str, str | int | None] | None = None
        self._cell_label: str | None = None
        self._cell_text: list[str] = []
        self._country_code: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr" and "table-row--hover" in (attributes.get("class") or "").split():
            self._row = {
                "athlete_id": attributes.get("data-id"),
                "athlete_url": attributes.get("data-athlete-url"),
            }
        elif self._row is not None and tag == "td":
            self._cell_label = attributes.get("data-th")
            self._cell_text = []
            self._country_code = None
        elif self._row is not None and self._cell_label == "Nat" and tag == "img":
            self._country_code = attributes.get("alt")

    def handle_data(self, data: str) -> None:
        if self._row is not None and self._cell_label is not None:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._cell_label is not None:
            field = _CELL_FIELDS.get(self._cell_label)
            if field is not None:
                value = " ".join("".join(self._cell_text).split())
                if field == "country_code":
                    value = self._country_code or value
                if field in {"rank", "ranking_score"}:
                    try:
                        self._row[field] = int(value)
                    except ValueError as exc:
                        raise ValueError(
                            f"Invalid {self._cell_label} value in ranking row: {value!r}"
                        ) from exc
                else:
                    self._row[field] = value or None
            self._cell_label = None
        elif tag == "tr" and self._row is not None:
            required_fields = (
                "rank",
                "competitor_name",
                "country_code",
                "ranking_score",
            )
            missing_fields = [field for field in required_fields if not self._row.get(field)]
            if missing_fields:
                raise ValueError(
                    "Ranking row is missing required fields: "
                    + ", ".join(missing_fields)
                )
            self.rankings.append(self._row)
            self._row = None


def _download_page(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def _download_page_with_retry(url: str) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return _download_page(url)
        except HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            if not retryable or attempt == MAX_RETRIES:
                raise
            error = exc
        except (URLError, TimeoutError) as exc:
            if attempt == MAX_RETRIES:
                raise
            error = exc

        delay = BASE_DELAY * (2 ** (attempt - 1))
        logger.warning(
            "Rankings request failed (%s). Retrying in %.1f seconds (%d/%d)...",
            error,
            delay,
            attempt,
            MAX_RETRIES,
        )
        time.sleep(delay)

    raise RuntimeError("Rankings request exhausted retries without a response.")


def get_latest_rankings(
    category: str,
    total_pages: int = TOTAL_PAGES,
    output_csv: str | Path | None = None,
) -> list[dict[str, str | int | None]]:
    """Fetch and save the latest overall rankings for male or female athletes."""
    if not isinstance(category, str) or category.strip().lower() not in _CATEGORY_SLUGS:
        raise ValueError("category must be either 'male' or 'female'")
    if isinstance(total_pages, bool) or not isinstance(total_pages, int) or total_pages < 1:
        raise ValueError("total_pages must be a positive integer")

    category_name = category.strip().lower()
    category_slug = _CATEGORY_SLUGS[category_name]
    rankings: list[dict[str, str | int | None]] = []
    for page in range(1, total_pages + 1):
        query = urlencode(
            {"regionType": "world", "page": page, "limitByCountry": 0}
        )
        url = f"{RANKINGS_URL}/{category_slug}?{query}"
        html = _download_page_with_retry(url)

        parser = _RankingTableParser()
        parser.feed(html)
        if not parser.rankings:
            raise ValueError(
                f"No ranking rows found on page {page} of the World Athletics response: {url}"
            )
        rankings.extend(parser.rankings)
        logger.info(
            "Fetched rankings page %d/%d (%d rows).",
            page,
            total_pages,
            len(parser.rankings),
        )
        if page < total_pages:
            time.sleep(PAGE_DELAY)

    output_path = Path(output_csv) if output_csv is not None else Path(
        f"world_athletics_rankings_{category_name}.csv"
    )
    try:
        with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rankings)
    except OSError:
        logger.exception("Could not save rankings to %s", output_path)
        raise

    return rankings