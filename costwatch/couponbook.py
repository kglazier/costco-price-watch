"""The monthly Member-Only Savings book — the price source.

Costco's coupon book is published as page images by several aggregators about a
week before it takes effect. That lead time is what makes the forward-looking
half work: we know an item goes on sale before you'd otherwise buy it.

Auto-fetch is best-effort. These are ordinary blogs with no API and no
stability guarantee, so a failed fetch falls back to whatever page images are
sitting in data/books/ — you can always photograph the mailed book, or save the
scans by hand, and re-run `import-book`.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import httpx

from . import config, db, llm

# Page scans on these sites are large JPEGs/PNGs; thumbnails and site chrome
# are small. Filtering by URL alone is unreliable, so we filter by byte size
# after fetching the headers.
IMG_SRC = re.compile(r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|webp))', re.IGNORECASE)
MIN_PAGE_BYTES = 60_000

ITEM_NUMBER = re.compile(r"^\d{6,8}$")


MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"], start=1)}

# e.g. /costco-august-2026-coupon-book/  or  /costco-june-and-july-2026-coupon-book/
BOOK_POST = re.compile(
    r"https?://[^\s\"']*?/costco-([a-z]+(?:-and-[a-z]+)?)-(\d{4})-coupon-book/?",
    re.IGNORECASE,
)

INDEX_PAGES = [
    "https://www.costcoinsider.com/category/coupons/",
    "https://www.costcoinsider.com/",
]


def discover_latest(indexes: list[str] | None = None) -> str | None:
    """Find the most recent coupon-book post URL.

    A hardcoded month URL goes stale in weeks, and an archive index mixes books
    from many months together — so resolve the newest post each run instead.
    """
    headers = {"User-Agent": config.USER_AGENT, "Accept": "text/html,*/*"}
    best: tuple[tuple[int, int], str] | None = None

    for index in indexes or INDEX_PAGES:
        try:
            resp = httpx.get(index, headers=headers, timeout=45, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError:
            continue

        for match in BOOK_POST.finditer(resp.text):
            month_part, year = match.group(1).lower(), int(match.group(2))
            # "june-and-july" -> sort by the later month
            month = max(
                (MONTHS[p] for p in month_part.split("-and-") if p in MONTHS),
                default=0,
            )
            if not month:
                continue
            key = (year, month)
            if best is None or key > best[0]:
                best = (key, match.group(0).rstrip("/") + "/")

        if best:
            break

    return best[1] if best else None


def _absolute(url: str, base: str) -> str:
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return "https:" + url
    root = "/".join(base.split("/")[:3])
    return root + ("" if url.startswith("/") else "/") + url


def fetch_pages(source: str | None = None, limit: int = 40) -> list[Path]:
    """Download coupon book page images into data/books/. Best effort."""
    config.ensure_dirs()
    source = source or config.COUPON_BOOK_SOURCE or discover_latest()
    if not source:
        raise RuntimeError(
            "Could not find a current coupon book post. Set COUPON_BOOK_SOURCE "
            f"in .env to a specific book URL, or drop page images into {config.BOOKS}."
        )
    headers = {"User-Agent": config.USER_AGENT, "Accept": "text/html,*/*"}

    try:
        page = httpx.get(source, headers=headers, timeout=45, follow_redirects=True)
        page.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Could not fetch {source}: {exc}. Drop page images into "
            f"{config.BOOKS} by hand and re-run import-book."
        ) from exc

    urls, seen = [], set()
    for raw in IMG_SRC.findall(page.text):
        url = _absolute(raw, source)
        if url not in seen:
            seen.add(url)
            urls.append(url)

    saved: list[Path] = []
    for idx, url in enumerate(urls[:limit]):
        try:
            img = httpx.get(url, headers=headers, timeout=60, follow_redirects=True)
            img.raise_for_status()
        except httpx.HTTPError:
            continue
        if len(img.content) < MIN_PAGE_BYTES:
            continue  # thumbnail, logo, or site chrome
        suffix = Path(url.split("?")[0]).suffix or ".jpg"
        dest = config.BOOKS / f"page-{idx:02d}{suffix}"
        dest.write_bytes(img.content)
        saved.append(dest)

    return saved


def local_pages() -> list[Path]:
    """Page images already sitting in data/books/."""
    config.ensure_dirs()
    return sorted(
        p for p in config.BOOKS.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )


def _clean_date(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def import_pages(pages: list[Path]) -> tuple[int, int, int]:
    """Read each page with the LLM and store the deals.

    A single coupon can cover several item numbers, so one offer may produce
    several rows — each item number needs its own row to be matchable.

    Returns (offers_seen, rows_stored, rows_new).
    """
    # Read every page first. The validity period belongs to the BOOK, not the
    # page: plenty of interior pages (brand blocks, for instance) print no
    # dates at all, so a per-page reading leaves most deals undated — and an
    # undated deal would otherwise look valid forever and match stale books.
    parsed: list[tuple[Path, dict]] = []
    votes: dict[tuple[str, str], int] = {}
    for page in pages:
        result = llm.read_book_page(page)
        parsed.append((page, result))
        pf = _clean_date(result.get("valid_from", ""))
        pt = _clean_date(result.get("valid_to", ""))
        if pf and pt:
            votes[(pf, pt)] = votes.get((pf, pt), 0) + 1

    # Most-agreed period across the pages that did print one.
    book_from = book_to = None
    if votes:
        book_from, book_to = max(votes.items(), key=lambda kv: kv[1])[0]

    seen = stored = new = 0
    with db.connect() as conn:
        for page, result in parsed:
            page_from = _clean_date(result.get("valid_from", "")) or book_from
            page_to = _clean_date(result.get("valid_to", "")) or book_to

            for deal in result.get("deals", []):
                seen += 1
                sale = float(deal.get("sale_price") or 0)
                discount = float(deal.get("discount") or 0)
                regular = float(deal.get("regular_price") or 0)

                # Derive whichever figure the page left out.
                if discount <= 0 and regular > 0 and sale > 0:
                    discount = round(regular - sale, 2)
                if sale <= 0 and regular > 0 and discount > 0:
                    sale = round(regular - discount, 2)
                if sale <= 0 and discount <= 0:
                    continue

                numbers = [
                    n for n in (str(x).strip() for x in deal.get("item_numbers", []))
                    if ITEM_NUMBER.match(n)
                ]
                if not numbers:
                    continue  # "item numbers vary" -- no join key, drop it

                for item in numbers:
                    stored += 1
                    if db.add_deal(
                        conn,
                        item_number=item,
                        description=str(deal.get("description", ""))[:120],
                        discount=discount,
                        sale_price=sale,
                        regular_price=regular,
                        valid_from=page_from,
                        valid_to=page_to,
                        source=page.name,
                    ):
                        new += 1
        db.set_meta(conn, "last_book_import", date.today().isoformat())
        if book_from and book_to:
            db.set_meta(conn, "book_period", f"{book_from}..{book_to}")
    return seen, stored, new
