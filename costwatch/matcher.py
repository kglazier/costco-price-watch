"""Decide what's claimable.

Deliberately dumb, because the join key is exact: Costco prints the same
7-digit item number on your receipt and on the coupon. No fuzzy name matching,
so no false positives sending you to the membership counter for nothing.

The central rule: **a price adjustment can only be claimed while the lower
price is actually in effect.** Two independent conditions have to hold on the
day you walk in —

    1. the sale is live today   (valid_from <= today <= valid_to)
    2. the purchase is still inside Costco's 30-day window

Checking only that the sale overlaps the window somewhere is not enough: a sale
starting next week satisfies that and still gets you turned away at the
counter. Those are surfaced separately, as `upcoming_claimable`.
"""

from datetime import date, datetime, timedelta

from . import config, db

# Ignore sub-dollar noise: rounding, deposit fees, unit-price wobble.
MIN_SAVINGS = 0.50


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _effective(deal, price_paid: float) -> float | None:
    """What the item costs under this deal, or None if it isn't a saving."""
    sale = float(deal["sale_price"] or 0)
    discount = float(deal["discount"] or 0)
    if sale > 0:
        return sale
    if discount > 0:
        return round(price_paid - discount, 2)
    return None


def _active_on(deal, day: date) -> bool:
    """Is this deal in effect on `day`?

    An undated deal counts as active — the book period is normally filled in
    at import, so a missing date means we couldn't read it, and suppressing
    those silently would be worse than a rare unverifiable alert. The
    notification prints the dates it has, so an undated one is visibly odd.
    """
    start, end = _parse(deal["valid_from"]), _parse(deal["valid_to"])
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


def _best_deal(conn, purchase, predicate):
    """Cheapest deal for this item that satisfies `predicate`."""
    best = None
    for deal in db.deals_for(conn, purchase["item_number"]):
        if not predicate(deal):
            continue
        price = _effective(deal, purchase["price_paid"])
        if price is None or price >= purchase["price_paid"]:
            continue
        if best is None or price < best[0]:
            best = (price, deal)
    return best


def _row(purchase, price_now, deal, window_days):
    purchased = _parse(purchase["purchased_on"])
    expires = purchased + timedelta(days=window_days)
    return {
        "purchase_id": purchase["id"],
        "item_number": purchase["item_number"],
        "description": purchase["description"] or deal["description"],
        "price_paid": purchase["price_paid"],
        "price_now": price_now,
        "quantity": purchase["quantity"],
        "savings": round(purchase["price_paid"] - price_now, 2),
        "purchased_on": purchase["purchased_on"],
        "claim_by": expires.isoformat(),
        "days_left": (expires - date.today()).days,
        "sale_from": deal["valid_from"],
        "sale_to": deal["valid_to"],
    }


def find_adjustments(conn, window_days=None, min_savings=MIN_SAVINGS):
    """Claimable RIGHT NOW: sale live today, purchase still inside the window."""
    window_days = window_days or config.ADJUSTMENT_WINDOW_DAYS
    today = date.today()
    out = []

    for p in db.open_purchases(conn, window_days):
        purchased = _parse(p["purchased_on"])
        if not purchased:
            continue
        if (purchased + timedelta(days=window_days) - today).days < 0:
            continue

        best = _best_deal(conn, p, lambda d: _active_on(d, today))
        if best is None:
            continue
        row = _row(p, best[0], best[1], window_days)
        if row["savings"] >= min_savings:
            out.append(row)

    return out


def upcoming_claimable(conn, window_days=None, min_savings=MIN_SAVINGS):
    """Bought it, the sale hasn't started yet, but it starts before your 30 days
    run out — so it WILL be claimable, on a date we can name.

    Without this, an item bought a week before a book drops looks like nothing
    is happening right up until the window quietly closes.
    """
    window_days = window_days or config.ADJUSTMENT_WINDOW_DAYS
    today = date.today()
    out = []

    for p in db.open_purchases(conn, window_days):
        purchased = _parse(p["purchased_on"])
        if not purchased:
            continue
        expires = purchased + timedelta(days=window_days)
        if expires < today:
            continue

        def not_yet_but_in_time(deal):
            start = _parse(deal["valid_from"])
            return bool(start) and today < start <= expires

        best = _best_deal(conn, p, not_yet_but_in_time)
        if best is None:
            continue
        row = _row(p, best[0], best[1], window_days)
        if row["savings"] >= min_savings:
            out.append(row)

    return out


def new_adjustments(conn, **kwargs):
    """Claimable now, and not already pushed at this price or lower."""
    return [
        a for a in find_adjustments(conn, **kwargs)
        if not db.already_alerted(conn, a["purchase_id"], a["price_now"], "claim")
    ]


def new_upcoming_claimable(conn, **kwargs):
    """Pending claims we haven't announced yet.

    Deduped under its own `kind`: this fires on an hourly schedule, so without
    it you get the same "a sale is coming" message every hour for weeks. The
    separate kind means announcing the upcoming sale does not suppress the
    alert when it actually opens.
    """
    return [
        a for a in upcoming_claimable(conn, **kwargs)
        if not db.already_alerted(conn, a["purchase_id"], a["price_now"], "pending")
    ]


def mark_alerted(conn, adjustments, kind="claim") -> None:
    for a in adjustments:
        db.record_alert(conn, a["purchase_id"], a["price_now"], kind)


def expiring_soon(conn, lead_days=None):
    lead_days = lead_days or config.ALERT_LEAD_DAYS
    return [a for a in find_adjustments(conn) if a["days_left"] <= lead_days]


def _familiar(conn) -> set[str]:
    """Every item number you've ever bought, not just those still in window."""
    return {
        r["item_number"]
        for r in conn.execute("SELECT DISTINCT item_number FROM purchases")
    }


def on_sale_now(conn, limit: int = 20):
    """Items you buy that are on sale TODAY — the restock signal.

    Distinct from a refund: no purchase window involved, nothing to claim. This
    is "you buy this, it's cheap right now, grab it this trip."
    """
    today = date.today()
    known = _familiar(conn)
    if not known:
        return []

    best: dict[str, dict] = {}
    for deal in db.upcoming_deals(conn):
        if deal["item_number"] not in known or not _active_on(deal, today):
            continue
        prior = best.get(deal["item_number"])
        if prior is None or (deal["discount"] or 0) > (prior["discount"] or 0):
            best[deal["item_number"]] = deal

    return sorted(best.values(), key=lambda d: -(d["discount"] or 0))[:limit]


def starting_soon(conn, horizon_days: int = 10, familiar_only: bool = True,
                  limit: int = 15):
    """Deals that begin within `horizon_days` — the 'wait, don't buy it yet' list.

    A book carries 150+ offers that all start the same day, so an unfiltered
    list is a wall of text nobody reads. Default to items you have actually
    bought before. Falls back to the unfiltered list while there's no purchase
    history yet, so it's useful from day one.
    """
    today = date.today()
    soon = []
    for deal in db.upcoming_deals(conn):
        start = _parse(deal["valid_from"])
        if start and today < start <= today + timedelta(days=horizon_days):
            soon.append(deal)

    if familiar_only:
        known = _familiar(conn)
        if known:
            soon = [d for d in soon if d["item_number"] in known]

    soon.sort(key=lambda d: -(d["discount"] or 0))
    return soon[:limit]
