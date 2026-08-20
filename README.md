# CostWatch

Snap a photo of your Costco receipt. Get a push notification when something you
bought goes on sale inside the 30-day price-adjustment window — plus a heads-up
when something is about to go on sale, so you can just wait.

Personal use, one member. No always-on computer, no Costco account, no app store.

## How it works

```
  your phone                GitHub Actions (free)              sources
  ──────────                ─────────────────────              ───────
  photo of receipt  ──────►  Gemini reads it                   coupon book
       (Telegram)            into item numbers        ◄──────  page images
                                    │                          (public blogs)
                                    ▼
                            match on item number
                                    │
  push notification ◄───────────────┘
       (Telegram)
```

**It never touches costco.com.** No login, no session to expire, no bot
detection, nothing that can be blocked. The 7-digit item number printed on both
your receipt and every coupon is the join key, so matches are exact — no fuzzy
name matching and therefore no false trips to the membership counter.

| Piece | Cost |
| --- | --- |
| GitHub Actions | free |
| Telegram bot | free |
| Gemini Flash-Lite (~40 images/month) | free tier |

## The four things it tells you

| Alert | When | Meaning |
| --- | --- | --- |
| **Claim now** | hourly | You bought it, the sale is **live today**, and you're inside 30 days. Go to the counter. |
| **Will be claimable** | hourly | You bought it, the sale starts *later* but before your window closes. **Don't go yet** — it names the date. |
| **On sale now** | Mondays | Something you buy regularly is on sale today. Nothing to claim; restock this trip. |
| **Coming up** | Mondays | Something you buy goes on sale soon. Wait rather than buying now. |

The first two are the ones that matter for money back, and they are strictly
separated on purpose. A refund is only honoured **while the lower price is
actually in effect** — a sale that merely overlaps your 30-day window somewhere
is not claimable today, and treating it as claimable is exactly how you end up
arguing with a membership desk. Every "claim now" alert prints the purchase
date, the price paid, the sale's own start and end, the new price, and the
deadline, so you can verify it before leaving the house.

## What it covers, honestly

- **Monthly Member-Only Savings book** — 80–150 offers, grocery-heavy, published
  about a week before it takes effect. This is the discount that drives most
  warehouse price adjustments.
- **Not covered: unannounced shelf-price drops and manager markdowns.** There is
  no public source for those. Nothing outside Costco can see them reliably.

At ~10–15 grocery items a week, expect roughly **3–8 matches per book cycle at
$2–5 each**. Real, modest, and zero ongoing effort.

The forward-looking half may be worth more than the refunds: because the book is
published before it starts, the bot can tell you *"wait three days on the olive
oil."* That needs no receipt at all.

## ⚠️ Run this in a private repository

Actions runners have no persistent disk, so the workflow commits
`data/costwatch.db` back to the repo after each run. **That database is your
purchase history** — every item, price, date, and your home warehouse.

In a public repo you'd be publishing your shopping history on an hourly
schedule, and every past version would remain in git history afterwards. Fork
it private.

This repository is the code only; it carries no database and never has.

## Setup

See **[SETUP.md](SETUP.md)** — about 10 minutes, three keys to paste.

## Commands

```
chat-id       find your Telegram chat id
ping          send a test push
check-llm     verify the Gemini key and model
receipts      pull receipt photos from Telegram and parse them
import-book   fetch + read the current savings book
run           receipts, match, push          (what the schedule runs)
status        what's tracked and what's claimable
add           record a purchase by hand, no photo needed
```

## Design notes

- **State lives in `data/costwatch.db`, committed back to the repo** by the
  workflow. GitHub Actions has no persistent disk; the git history doubles as an
  audit trail.
- **The Telegram offset cursor is persisted and only advanced after photos are
  parsed and stored.** Telegram drops updates once acknowledged, so advancing
  early would silently lose a receipt.
- **One coupon can list several item numbers** (`Item 1806358, 1806329, ...`).
  Each gets its own row — collapsing them to one would silently miss matches.
- **A deal only counts if its sale period overlaps your claim window.** An
  adjustment has to be claimed while the lower price is actually live, so a sale
  that ended before you bought, or starts after day 30, is correctly ignored.
- **The book source auto-discovers the newest post each run.** A pinned month URL
  goes stale in weeks, and archive index pages mix several months together.
