"""Command line entry point.

Setup, once:
    python -m costwatch chat-id      find your Telegram chat id
    python -m costwatch ping         prove the push loop works
    python -m costwatch check-llm    prove the Gemini key and model work

Routine (the scheduled job runs `run`):
    python -m costwatch receipts     pull receipt photos from Telegram, parse them
    python -m costwatch import-book  fetch + read the monthly savings book
    python -m costwatch run          receipts, then match, then push
    python -m costwatch status       what's tracked and what's claimable
    python -m costwatch add          record a purchase by hand
"""

import argparse
import sys
from datetime import date

import httpx

from . import config, couponbook, db, llm, matcher, notify, telegram


def cmd_chat_id(_args) -> int:
    chat_id = notify.discover_chat_id()
    if not chat_id:
        print("No messages found. Open Telegram, send your bot any message, "
              "then run this again.")
        return 1
    print(f"TELEGRAM_CHAT_ID={chat_id}\n\nPaste that line into your .env file.")
    return 0


def cmd_ping(_args) -> int:
    notify.send("*CostWatch is wired up.* 🛒\nSend me a photo of a Costco "
                "receipt and I'll watch those items for price drops.")
    print("Sent. Check your phone.")
    return 0


def cmd_check_llm(_args) -> int:
    reply = llm.check()
    print(f"Gemini OK ({config.GEMINI_MODEL}) -> {reply!r}")
    return 0


def cmd_receipts(args) -> int:
    """Pull receipt photos off Telegram and turn them into tracked purchases."""
    with db.connect() as conn:
        offset = db.get_meta(conn, "telegram_offset")
    offset = int(offset) if offset else None

    photos, next_offset, skipped = telegram.fetch_photos(offset)

    if skipped and not args.quiet:
        # Advancing past an unusable message is irreversible — Telegram drops
        # the update once acknowledged — so say so instead of failing quietly.
        notify.send(
            f"I saw {skipped} message(s) with no readable image and skipped "
            "them. If that was a receipt, send it again as a *photo* rather "
            "than a file."
        )

    if not photos:
        print(f"No new receipt photos. ({skipped} message(s) skipped)")
        # Still advance so plain text messages don't replay forever.
        if next_offset != offset:
            with db.connect() as conn:
                db.set_meta(conn, "telegram_offset", next_offset)
        return 0

    total_new = 0
    for photo in photos:
        try:
            parsed = llm.read_receipt(photo)
        except llm.LLMError as exc:
            print(f"  {photo.name}: could not read ({exc})", file=sys.stderr)
            if not args.quiet:
                notify.send("I couldn't read that receipt photo. A flatter, "
                            "better-lit shot usually fixes it.")
            continue

        when = (parsed.get("purchased_on") or "").strip() or date.today().isoformat()
        items = parsed.get("items", [])
        added = 0
        with db.connect() as conn:
            for item in items:
                if db.add_purchase(
                    conn,
                    item_number=item["item_number"],
                    description=item.get("description", ""),
                    price_paid=item["price_paid"],
                    quantity=item.get("quantity", 1) or 1,
                    purchased_on=when,
                    warehouse=parsed.get("warehouse", ""),
                    receipt=photo.name,
                ):
                    added += 1
        total_new += added
        print(f"  {photo.name}: {len(items)} line items, {added} new ({when})")

        if items and not args.quiet:
            notify.send(_receipt_summary(items, when, added))

    # Only advance the cursor once the photos are parsed and stored — Telegram
    # drops acknowledged updates, so an early advance loses the receipt.
    with db.connect() as conn:
        db.set_meta(conn, "telegram_offset", next_offset)
        db.touch_checkin(conn)

    return 0


def _receipt_summary(items, when, added) -> str:
    """Echo back what was read, so you can check it against the paper receipt
    on your phone instead of at a terminal."""
    total = sum(i["price_paid"] * (i.get("quantity") or 1) for i in items)
    lines = [f"*Receipt read — {len(items)} items, ${total:.2f}*  _{when}_", ""]
    for i in items[:40]:
        qty = f" x{i['quantity']}" if (i.get("quantity") or 1) > 1 else ""
        lines.append(f"`{i['item_number']:>8}`  {i.get('description','')[:26]:<26} "
                     f"${i['price_paid']:.2f}{qty}")
    if len(items) > 40:
        lines.append(f"…and {len(items) - 40} more")
    lines += ["", f"_Now watching {added} new item(s) for 30 days._"]
    return "\n".join(lines)


def cmd_import_book(args) -> int:
    if args.local:
        pages = couponbook.local_pages()
        if not pages:
            print(f"No images in {config.BOOKS}. Put coupon book page images "
                  "there, or drop --local to try fetching them.")
            return 1
    else:
        try:
            pages = couponbook.fetch_pages()
        except RuntimeError as exc:
            print(f"{exc}", file=sys.stderr)
            pages = couponbook.local_pages()
            if not pages:
                return 1
            print(f"Falling back to {len(pages)} local page(s).")

    if not pages:
        print("No page images found — nothing to import.")
        return 1

    print(f"Reading {len(pages)} page(s)...")
    seen, stored, new = couponbook.import_pages(pages)
    print(f"{seen} offer(s) on the pages -> {stored} item row(s), {new} new.")
    return 0


def cmd_run(args) -> int:
    """The scheduled job: ingest receipts, match, push anything claimable."""
    # Not quiet: the "logged N items" reply is the acknowledgement that your
    # photo was received and read. Without it there's no way to tell a working
    # bot from a broken one until something happens to go on sale.
    cmd_receipts(argparse.Namespace(quiet=args.dry_run))

    def deliver(text: str) -> None:
        if not text:
            return
        if args.dry_run:
            print(text + "\n")
        else:
            notify.send(text)

    with db.connect() as conn:
        # Claimable today. Only these are worth a trip.
        fresh = matcher.new_adjustments(conn)
        if fresh:
            deliver(notify.format_adjustments(fresh))
            if not args.dry_run:
                matcher.mark_alerted(conn, fresh)
            print(f"{len(fresh)} adjustment(s) claimable now.")
        else:
            print("Nothing claimable right now.")

        # Bought it, sale starts later but still inside the window. Announced
        # once -- this runs hourly, and a standing reminder becomes noise long
        # before the sale opens. The "claim now" alert covers the open itself.
        pending = matcher.new_upcoming_claimable(conn)
        if pending:
            deliver(notify.format_upcoming_claimable(pending))
            if not args.dry_run:
                matcher.mark_alerted(conn, pending, "pending")
            print(f"{len(pending)} will become claimable later.")

        if args.upcoming:
            restock = matcher.on_sale_now(conn)
            if restock:
                deliver(notify.format_on_sale_now(restock))
                print(f"{len(restock)} item(s) you buy are on sale now.")

            soon = matcher.starting_soon(conn)
            if soon:
                deliver(notify.format_upcoming(soon))
                print(f"{len(soon)} deal(s) starting soon.")

        db.touch_checkin(conn)
    return 0


def cmd_add(args) -> int:
    with db.connect() as conn:
        new = db.add_purchase(
            conn,
            item_number=args.item,
            description=args.description or "",
            price_paid=args.price,
            quantity=args.quantity,
            purchased_on=args.date or date.today().isoformat(),
            receipt="manual",
        )
    print("Added." if new else "Already tracked.")
    return 0


def cmd_status(_args) -> int:
    with db.connect() as conn:
        open_rows = db.open_purchases(conn)
        adjustments = matcher.find_adjustments(conn)
        deal_count = conn.execute("SELECT COUNT(*) c FROM deals").fetchone()["c"]
        last_run = db.get_meta(conn, "last_run", "never")
        last_book = db.get_meta(conn, "last_book_import", "never")
        soon = matcher.starting_soon(conn)

    print(f"Last run            : {last_run}")
    print(f"Last book import    : {last_book}")
    print(f"Deals known         : {deal_count}")
    print(f"Purchases in window : {len(open_rows)}")
    print(f"Claimable now       : {len(adjustments)}")
    if adjustments:
        total = sum(a["savings"] * a["quantity"] for a in adjustments)
        print(f"Total claimable     : ${total:.2f}\n")
        for a in sorted(adjustments, key=lambda x: x["days_left"]):
            print(f"  {a['item_number']}  {a['description'][:34]:<34} "
                  f"${a['price_paid']:>7.2f} -> ${a['price_now']:>7.2f}  "
                  f"(+${a['savings']:.2f}, {a['days_left']}d left)")
    if soon:
        print(f"\nStarting soon ({len(soon)}) — consider waiting:")
        for d in soon[:10]:
            print(f"  {d['item_number']}  {d['description'][:40]:<40} from {d['valid_from']}")
    return 0


def main(argv=None) -> int:
    # Messages carry arrows and currency glyphs for Telegram. A Windows console
    # defaults to cp1252 and raises UnicodeEncodeError printing them, which
    # takes down --dry-run on the machine most likely to run it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="costwatch", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("chat-id", help="find your Telegram chat id")
    sub.add_parser("ping", help="send a test push")
    sub.add_parser("check-llm", help="verify the Gemini key and model")
    sub.add_parser("status", help="what's tracked and claimable")

    p_rec = sub.add_parser("receipts", help="pull + parse receipt photos")
    p_rec.add_argument("--quiet", action="store_true", help="don't push a confirmation")
    p_rec.set_defaults(func=cmd_receipts)

    p_book = sub.add_parser("import-book", help="fetch + read the savings book")
    p_book.add_argument("--local", action="store_true",
                        help="use images already in data/books/ instead of fetching")
    p_book.set_defaults(func=cmd_import_book)

    p_run = sub.add_parser("run", help="receipts, match, push (the cron job)")
    p_run.add_argument("--dry-run", action="store_true", help="print instead of pushing")
    p_run.add_argument("--upcoming", action="store_true",
                       help="also push deals that start soon")
    p_run.set_defaults(func=cmd_run)

    p_add = sub.add_parser("add", help="record a purchase by hand")
    p_add.add_argument("item")
    p_add.add_argument("price", type=float)
    p_add.add_argument("--description", "-d")
    p_add.add_argument("--quantity", "-q", type=int, default=1)
    p_add.add_argument("--date", help="ISO date; defaults to today")
    p_add.set_defaults(func=cmd_add)

    args = parser.parse_args(argv)
    handlers = {
        "chat-id": cmd_chat_id, "ping": cmd_ping, "check-llm": cmd_check_llm,
        "status": cmd_status,
    }
    func = handlers.get(args.cmd) or args.func
    try:
        return func(args)
    except (notify.NotConfigured, llm.NotConfigured) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except llm.LLMError as exc:
        print(f"LLM error: {exc}", file=sys.stderr)
        return 3
    except httpx.HTTPError as exc:
        # Telegram or Gemini unreachable. Nothing is lost -- the read cursor
        # only advances after a successful fetch -- so report it plainly rather
        # than dumping a traceback, and let the next scheduled run retry.
        print(f"Network error ({type(exc).__name__}): {exc}. "
              "Nothing consumed; the next run will retry.", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
