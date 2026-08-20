"""Gemini vision calls — the only component that reads images.

Two jobs, both structured-output:
  * a photo of a Costco receipt -> line items with 7-digit item numbers
  * a page of the monthly savings book -> the deals printed on it

Item numbers are the join key between the two, which is why the schemas below
insist on them and why anything without one is dropped rather than guessed at.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import threading
import time
from pathlib import Path

import httpx

from . import config

# The free tier caps requests per MINUTE (15 at the time of writing), and a
# coupon book is ~22 pages — firing them in a burst trips the cap partway
# through the import. Space calls out and retry on 429 rather than losing the
# pages already paid for.
_MIN_INTERVAL = 4.5
_RETRIES = 4
_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Prefer the server's own RetryInfo; fall back to exponential backoff."""
    try:
        for detail in resp.json()["error"].get("details", []):
            raw = detail.get("retryDelay")
            if raw:
                return float(str(raw).rstrip("s")) + 1.0
    except Exception:  # noqa: BLE001
        pass
    return min(60.0, _MIN_INTERVAL * (2 ** attempt))


class NotConfigured(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


RECEIPT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "purchased_on": {
            "type": "STRING",
            "description": "Transaction date on the receipt, as YYYY-MM-DD. Empty string if unreadable.",
        },
        "warehouse": {"type": "STRING", "description": "Warehouse name or number, or empty string."},
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "item_number": {"type": "STRING", "description": "The 6-8 digit item number."},
                    "description": {"type": "STRING", "description": "Abbreviated item name as printed."},
                    "price_paid": {"type": "NUMBER", "description": "Price of ONE unit, after any instant savings applied to it. Divide if the line shows a total for several units."},
                    "quantity": {"type": "INTEGER", "description": "Units bought. Only from an explicit quantity column or Qty label; a bare digit beside the item number or price is a department/tax code, not a quantity. Default 1."},
                },
                "required": ["item_number", "description", "price_paid", "quantity"],
            },
        },
    },
    "required": ["purchased_on", "items"],
}

RECEIPT_PROMPT = """\
This is a photo of a Costco warehouse receipt. Extract every purchased line item.

Rules:
- The item number is the 6-8 digit number at the START of each item line. It is
  required. If you cannot read it confidently, omit that line entirely rather
  than guessing.
- price_paid is what was actually paid for that line. Costco prints instant
  savings/coupon discounts as SEPARATE lines that reference the item above them
  (often with a "/" or "-" and the item number). Subtract those from the item
  they belong to and report the net price. Do not emit the discount lines as
  items of their own.
- Skip non-merchandise lines: SUBTOTAL, TAX, TOTAL, payment/tender lines,
  membership number, change due.
- Return prices as positive numbers with no currency symbol.

QUANTITY — read this carefully, it is the most common mistake:
- quantity comes ONLY from an explicit quantity column or a "Qty" label.
- Costco lines also carry small standalone digits that are department, tax or
  category codes, NOT quantities. A bare digit sitting next to the item number
  or the price is one of those. Ignore it.
- If no explicit quantity is labelled for a line, quantity is 1.
- A sanity check: it is normal for every line on a receipt to be quantity 1.
  It is NOT normal for most lines to share the same quantity above 1. If you
  find yourself assigning the same number above 1 to many lines, you are
  reading a code, not a quantity -- use 1.

PRICE — price_paid is the price of a SINGLE UNIT, net of instant savings.
- If the line shows an extended total for several units (e.g. "2 @ 5.99" or a
  quantity of 2 against 11.98), divide to get the per-unit price.
- Downstream this gets multiplied back by quantity, so reporting a line total
  as price_paid double-counts and inflates any refund.
"""

BOOK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "valid_from": {"type": "STRING", "description": "Offer period start as YYYY-MM-DD, from the page footer. Empty string if absent."},
        "valid_to": {"type": "STRING", "description": "Offer period end as YYYY-MM-DD. Empty string if absent."},
        "deals": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "item_numbers": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Every 6-8 digit item number listed for this offer. One coupon often covers several.",
                    },
                    "description": {"type": "STRING"},
                    "discount": {"type": "NUMBER", "description": "Instant savings, in dollars off. 0 if not stated."},
                    "sale_price": {"type": "NUMBER", "description": "YOUR COST after savings. 0 if not stated."},
                    "regular_price": {"type": "NUMBER", "description": "Warehouse Price before savings. 0 if not stated."},
                },
                "required": ["item_numbers", "description", "discount", "sale_price", "regular_price"],
            },
        },
    },
    "required": ["deals", "valid_from", "valid_to"],
}

BOOK_PROMPT = """\
This is a page from Costco's monthly Member-Only Savings book. Extract every
coupon/offer printed on it.

Rules:
- Each offer prints its item number(s) in small text, usually as "Item 1234567".
  A SINGLE offer often covers SEVERAL item numbers, printed as a comma-separated
  list (e.g. "Item 1806358, 1806329, 1806352"). Put every one of them in
  item_numbers. This matters — a missed number means a missed match.
- If the offer says "Item numbers vary" or shows no item number at all, return
  an empty item_numbers array for it. Do not invent or guess a number.
- Offers typically print three figures: "Warehouse Price" (regular_price),
  "Instant Savings" (discount), and "YOUR COST" (sale_price). Fill in whichever
  are shown and use 0 for the rest. A headline like "SAVE $10" with no other
  figure means discount=10, sale_price=0, regular_price=0.
- valid_from and valid_to come from the page footer, e.g. "Savings valid
  July 27 - Aug. 23, 2026". Convert to YYYY-MM-DD. If the year is only printed
  once, apply it to both dates.
- Ignore pure advertising with no price.
"""


def _require_key() -> str:
    if not config.GEMINI_API_KEY:
        raise NotConfigured(
            "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey "
            "and add it to .env"
        )
    return config.GEMINI_API_KEY


def _call(prompt: str, image: Path, schema: dict, timeout: float = 120.0) -> dict:
    key = _require_key()
    mime = mimetypes.guess_type(image.name)[0] or "image/jpeg"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime,
                            "data": base64.b64encode(image.read_bytes()).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }

    url = config.GEMINI_ENDPOINT.format(model=config.GEMINI_MODEL)
    headers = {"x-goog-api-key": key}

    for attempt in range(_RETRIES):
        _throttle()
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code == 429 and attempt < _RETRIES - 1:
            time.sleep(_retry_delay(resp, attempt))
            continue
        break

    if resp.status_code == 429:
        raise LLMError(
            "Gemini free-tier rate limit still hit after retries. Wait a minute "
            "and re-run — already-imported pages are skipped."
        )
    if resp.status_code != 200:
        raise LLMError(f"Gemini returned {resp.status_code}: {resp.text[:400]}")

    body = resp.json()
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        # Usually a safety block or an empty candidate list.
        raise LLMError(f"Unexpected Gemini response shape: {json.dumps(body)[:400]}")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Gemini did not return valid JSON: {exc}") from exc


def read_receipt(image: Path) -> dict:
    """Photo of a receipt -> {purchased_on, warehouse, items[]}."""
    return _call(RECEIPT_PROMPT, image, RECEIPT_SCHEMA)


def read_book_page(image: Path) -> dict:
    """Coupon book page image -> {deals[]}."""
    return _call(BOOK_PROMPT, image, BOOK_SCHEMA)


def check() -> str:
    """Verify the key and model are usable. Returns the model's reply."""
    key = _require_key()
    url = config.GEMINI_ENDPOINT.format(model=config.GEMINI_MODEL)
    resp = httpx.post(
        url,
        json={"contents": [{"parts": [{"text": "Reply with exactly: ok"}]}]},
        headers={"x-goog-api-key": key},
        timeout=30,
    )
    if resp.status_code == 404:
        # Google's own message names the replacement model, which is far more
        # useful than anything we can guess — surface it verbatim.
        try:
            detail = resp.json()["error"]["message"]
        except Exception:  # noqa: BLE001
            detail = resp.text[:300]
        raise LLMError(
            f"Model '{config.GEMINI_MODEL}' unavailable: {detail}\n"
            "Set GEMINI_MODEL in .env to a current id."
        )
    if resp.status_code != 200:
        raise LLMError(f"Gemini returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
