"""Configuration, loaded from .env with sane defaults."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INBOX = DATA / "inbox"          # receipt photos pulled off Telegram
BOOKS = DATA / "books"          # coupon book page images
DB_PATH = DATA / "costwatch.db"

load_dotenv(ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# GEMINI_KEY is accepted as an alias — it's the name AI Studio's own snippets
# use, and getting this wrong fails as "key not set" rather than anything that
# points at the real problem.
GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_KEY") or ""
).strip()
# Flash-Lite is the cheapest vision-capable tier and has the most generous
# free-tier request-per-day allowance; ~40 images/month is a rounding error
# against that quota.
#
# The "-latest" alias rather than a pinned version on purpose: Google retires
# old models to new users (gemini-2.5-flash-lite already 404s that way), and
# this job runs unattended, so a pinned id turns into a silent outage on their
# schedule. The strict responseSchema pins the output shape regardless of which
# model serves it. Pin a version here if you'd rather control upgrades.
#
# `or` rather than a getenv default: CI sets GEMINI_MODEL to an empty string
# when the repo variable is unset, and an empty string is not a missing key —
# a getenv default would be skipped and the model name would end up blank.
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "").strip() or "gemini-flash-lite-latest"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

ADJUSTMENT_WINDOW_DAYS = int(os.getenv("ADJUSTMENT_WINDOW_DAYS", "30"))
ALERT_LEAD_DAYS = int(os.getenv("ALERT_LEAD_DAYS", "7"))

# Where to pull the monthly Member-Only Savings book from. Leave blank to
# auto-discover the newest book post each run — a pinned month URL goes stale
# in weeks, and archive index pages mix several months' books together.
COUPON_BOOK_SOURCE = os.getenv("COUPON_BOOK_SOURCE", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


def ensure_dirs() -> None:
    for d in (DATA, INBOX, BOOKS):
        d.mkdir(parents=True, exist_ok=True)
