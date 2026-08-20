"""Telegram push. Your phone's Telegram app is the whole client."""

import httpx

from . import config


class NotConfigured(RuntimeError):
    pass


def _api(method: str) -> str:
    if not config.TELEGRAM_BOT_TOKEN:
        raise NotConfigured(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"


def send(text: str) -> None:
    # Token first: a missing token is the more fundamental problem, and
    # reporting the chat id instead sends you chasing the wrong thing.
    if not config.TELEGRAM_BOT_TOKEN:
        raise NotConfigured(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    if not config.TELEGRAM_CHAT_ID:
        raise NotConfigured(
            "TELEGRAM_CHAT_ID is not set. Message your bot once, then run: "
            "python -m costwatch chat-id"
        )
    resp = httpx.post(
        _api("sendMessage"),
        json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    resp.raise_for_status()


def discover_chat_id() -> str | None:
    """Read the chat id off the most recent message you sent your bot."""
    resp = httpx.get(_api("getUpdates"), timeout=20)
    resp.raise_for_status()
    for update in reversed(resp.json().get("result", [])):
        msg = update.get("message") or update.get("edited_message")
        if msg and "chat" in msg:
            return str(msg["chat"]["id"])
    return None


def _period(row) -> str:
    lo, hi = row.get("sale_from"), row.get("sale_to")
    if lo and hi:
        return f"{lo} to {hi}"
    return (lo and f"from {lo}") or (hi and f"through {hi}") or "dates unknown"


def format_adjustments(rows) -> str:
    """Everything needed to verify a claim before driving to the warehouse.

    Prints the purchase date, what was paid, the sale's own start and end, the
    new price, and the claim deadline. The sale window is the figure to check:
    a refund is only honoured while the lower price is actually in effect, so
    those dates are what separate a real claim from a wasted trip.
    """
    """rows: list of dicts with description, item_number, price_paid, price_now,
    savings, days_left."""
    if not rows:
        return "No price adjustments available right now."

    total = sum(r["savings"] * r.get("quantity", 1) for r in rows)
    lines = [f"*Claim now — ${total:.2f} available*", ""]

    for r in sorted(rows, key=lambda x: x["days_left"]):
        qty = f"  (x{r['quantity']})" if r.get("quantity", 1) > 1 else ""
        urgency = "  ⚠️ *last chance*" if r["days_left"] <= 3 else ""
        lines += [
            f"*{(r['description'] or r['item_number'])[:44]}*  `{r['item_number']}`",
            f"  bought   {r['purchased_on']}  for ${r['price_paid']:.2f}{qty}",
            f"  now      ${r['price_now']:.2f}   → *${r['savings']:.2f} back*",
            f"  sale     {_period(r)}  (active today)",
            f"  claim by {r['claim_by']}  ({r['days_left']}d left){urgency}",
            "",
        ]

    lines.append(
        "_Warehouse purchases: membership counter. Online: costco.com order details._"
    )
    return "\n".join(lines)


def format_upcoming_claimable(rows) -> str:
    """Bought it, sale hasn't started, but it starts before your window closes."""
    if not rows:
        return ""

    lines = ["*Will be claimable — don't go yet*", ""]
    for r in sorted(rows, key=lambda x: str(x.get("sale_from") or "")):
        lines += [
            f"*{(r['description'] or r['item_number'])[:44]}*  `{r['item_number']}`",
            f"  bought   {r['purchased_on']}  for ${r['price_paid']:.2f}",
            f"  drops to ${r['price_now']:.2f}   → ${r['savings']:.2f} back",
            f"  sale     {_period(r)}",
            f"  claim between {r['sale_from']} and {r['claim_by']}",
            "",
        ]
    lines.append("_Going before the sale starts gets you turned away._")
    return "\n".join(lines)


def format_on_sale_now(deals) -> str:
    """Items you buy that are on sale today — restock, nothing to claim."""
    if not deals:
        return ""

    lines = ["*On sale now — stuff you buy*", ""]
    for d in deals:
        price = f"${d['sale_price']:.2f}" if d["sale_price"] else f"${d['discount']:.2f} off"
        ends = f"  _ends {d['valid_to']}_" if d["valid_to"] else ""
        lines.append(f"• *{d['description'][:44]}*  `{d['item_number']}`\n  {price}{ends}")
    return "\n".join(lines)


def format_upcoming(deals) -> str:
    """The forward-looking half: don't buy it today, it's on sale Monday.

    Needs no receipt, so it works from the moment a coupon book is imported.
    """
    if not deals:
        return "No upcoming deals in the next few days."

    lines = ["*Coming up in the next coupon book — consider waiting*", ""]
    for d in deals:
        if d["sale_price"]:
            price = f"${d['sale_price']:.2f}"
        else:
            price = f"${d['discount']:.2f} off"
        when = f" (from {d['valid_from']})" if d["valid_from"] else ""
        lines.append(f"• *{d['description'][:44]}*  `{d['item_number']}`\n  {price}{when}")
    return "\n".join(lines)
