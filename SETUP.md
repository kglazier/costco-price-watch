# CostWatch setup

Three keys to paste, about 10 minutes. Telegram is already done if you set it up
earlier — skip to Part 2.

Run everything from the project folder:

```powershell
cd path\to\costwatch
```

Every command starts with `.\.venv\Scripts\python` — plain `python` fails with
`ModuleNotFoundError`.

> Using the `!` prefix to run these yourself? That's Bash, where `\` is an escape
> character. Use forward slashes: `./.venv/Scripts/python`

---

## Part 1 — Telegram (~5 min)

**1.** `copy .env.example .env`

**2.** In Telegram, message **@BotFather** → `/newbot` → name it → pick a username
ending in `bot`. Paste the token it gives you into `.env` as `TELEGRAM_BOT_TOKEN`.

**3.** Open your new bot, tap **Start**, send it `hi`. *(Required — bots can't
message you first.)* Then:

```powershell
.\.venv\Scripts\python -m costwatch chat-id
```

Paste the printed `TELEGRAM_CHAT_ID=...` line into `.env`.

**4.** ```powershell
.\.venv\Scripts\python -m costwatch ping
```

Don't move on until your phone buzzes.

---

## Part 2 — Gemini (~2 min, free)

**5.** Go to **https://aistudio.google.com/apikey** → *Create API key*. No credit
card. Paste it into `.env` as `GEMINI_API_KEY` (or `GEMINI_KEY` — both work).

> Copy the **key** itself, not the `projects/…` value shown beside it — that's
> the Google Cloud project the key lives in, not a credential. Current keys
> start with `AQ.`; older ones start with `AIza`.

**6.** ```powershell
.\.venv\Scripts\python -m costwatch check-llm
```

Expected: `Gemini OK (gemini-flash-lite-latest) -> 'ok'`

> The default model is the `-latest` alias on purpose. Google closes older
> models to new users (`gemini-2.5-flash-lite` already 404s that way), and this
> job runs unattended, so a pinned id becomes a silent outage on their schedule.
> Pin `GEMINI_MODEL` in `.env` if you'd rather control upgrades.

Your volume is ~22 coupon pages once a month plus a few receipts. That's far
inside the free tier. The binding limit is **15 requests per minute**, so a book
import deliberately paces itself and takes about two minutes — that's the
throttle working, not a hang. Note free-tier content may be used to improve
Google's products: the coupon book is public, but your receipts are your
grocery list.

---

## Part 3 — First run

**7. Import the current coupon book.** This is the price source:

```powershell
.\.venv\Scripts\python -m costwatch import-book
```

It finds the newest book post automatically, downloads ~22 page images, and
reads each one. Takes about two minutes.

**You only do this once per book.** The deals are stored in `data/costwatch.db`
and every future receipt is matched against that stored data — no rescan. The
weekly job only checks whether a *new* book has been published, and re-importing
the same one is a near no-op because rows dedupe on a `UNIQUE` constraint.

**8. Send a receipt.** Photograph a Costco receipt and send the photo to your bot
in Telegram.

Once the schedule is running (Part 4), that's the entire workflow — the bot
replies within the hour with everything it read:

```
Receipt read — 14 items, $184.22   2026-08-19

`  121128`  HAAGEN DAZS BAR       $16.99
` 1451835`  AMYLU ANDOUILLE       $14.49 x2
...
Now watching 14 new item(s) for 30 days.
```

**Check that reply against the paper the first time** — item numbers, and prices
*after* any instant savings. If a line is wrong, tell me and I'll tune the
extraction prompt. You never need a terminal for this.

**Before the schedule exists**, you can process it immediately from here:

```powershell
.\.venv\Scripts\python -m costwatch run --upcoming
```

---

## Part 4 — Put it on a schedule

Push to a **private** GitHub repo, then set:

**Secrets** (Settings → Secrets and variables → Actions → Secrets)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GEMINI_API_KEY`

**Variables** (same page → Variables), optional
- `GEMINI_MODEL`, `ALERT_LEAD_DAYS`

The workflow runs **hourly** — picks up any receipt you sent, reads it, replies
with what it found, and pushes anything claimable. On **Mondays** it also
re-imports the book and tells you what's about to go on sale. It commits
`data/costwatch.db` back to the repo — that's the persistence layer, since
Actions has no disk.

Hourly costs roughly 750 of the 2,000 free private-repo minutes a month; a quiet
run is just a Telegram poll, and the LLM only fires when a photo is waiting.
Every 30 minutes would not fit in the free tier.

> `.env` is gitignored and your keys live in GitHub Secrets. Don't commit `.env`.

---

## Daily use

Shop → photograph the receipt → send it to the bot. **That's the whole
workflow — no terminal, ever.**

Within the hour the bot replies with what it read. After that it pushes you when
something you bought drops in price, reminds you before anything ages out of the
30-day window, and on Mondays flags items you buy that are about to go on sale.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'costwatch'` | You used plain `python`. Use `.\.venv\Scripts\python` |
| `Config error: TELEGRAM_BOT_TOKEN is not set` | `.env` missing or blank. It's `.env`, not `.env.example` |
| `chat-id` says "No messages found" | Message *your* bot, not BotFather. Send an actual message, don't just tap Start |
| `ping` fails 401 / 400 | Token wrong / chat id wrong — re-run `chat-id` |
| `check-llm` 404 | The error quotes Google's own replacement model — put that in `GEMINI_MODEL` |
| `check-llm` 400 or 403 | Key wrong, or the Gemini API isn't enabled for it |
| `check-llm` "key is not set" but you pasted one | Variable must be `GEMINI_API_KEY` or `GEMINI_KEY`, and the value must be the key itself, not `projects/…` |
| 429 during `import-book` | Free tier is 15 req/min. It retries automatically; if it still fails, wait a minute and re-run — imported pages are skipped |
| `import-book` finds no pages | Site layout changed. Save page images into `data/books/` and run `import-book --local` |
| Receipt parsed with wrong prices | Tell me which lines — the extraction prompt is tunable |
| Nothing claimable but you expect some | Run `status` — check the book imported (`Deals known` > 0) and your purchase is inside 30 days |
