#!/usr/bin/env python3
"""
Watches an OLX.ua search and pushes new listings to Telegram.

Bypasses OLX's TLS-fingerprint (JA3) antibot by impersonating Chrome via curl_cffi.
State (already-seen offer ids) is kept in seen.json so only *new* listings are sent.

Config comes from environment variables (see README.md):
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_CHAT_ID     required
  OLX_CATEGORY_ID      default 108   (легкові автомобілі)
  OLX_REGION_ID        default 17    (Запорізька область)
  OLX_EXTRA_PARAMS     optional, e.g. "filter_enum_cleared_customs[0]=no"
"""
from __future__ import annotations

import html
import json
import os
import sys
import time
from pathlib import Path

from curl_cffi import requests

API = "https://www.olx.ua/api/v1/offers/"
SEEN_FILE = Path(__file__).with_name("seen.json")
MAX_SEEN = 2000          # how many ids to remember (FIFO)
SEED_DEPTH = 300         # on first run, how many current offers to pre-mark as seen
PAGE_LIMIT = 50          # offers per API page
POLL_PAGES = 1           # pages to scan on each normal run (50 new in 30min is plenty)


def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing required env var: {name}")
    return val or ""


def fetch_offers(category_id: str, region_id: str, extra: str, offset: int, limit: int):
    params = {
        "offset": str(offset),
        "limit": str(limit),
        "category_id": category_id,
        "region_id": region_id,
        "sort_by": "created_at:desc",
    }
    # extra params like "filter_enum_cleared_customs[0]=no&foo=bar"
    for pair in filter(None, extra.split("&")):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k] = v
    r = requests.get(API, params=params, impersonate="chrome", timeout=30)
    r.raise_for_status()
    return r.json().get("data", [])


def _fmt(n) -> str:
    return f"{n:,.0f}".replace(",", " ")


def get_price(offer: dict) -> str:
    for p in offer.get("params", []):
        if p.get("key") != "price":
            continue
        v = p.get("value", {})
        amount = v.get("value")
        cur = v.get("currency", "")
        if not amount:
            return "Договірна"
        out = f"{_fmt(amount)} {cur}"
        # OLX gives a UAH-converted value for foreign currencies — show it too
        conv = v.get("converted_value")
        if cur != "UAH" and conv:
            out += f" (~{_fmt(conv)} грн)"
        if v.get("negotiable"):
            out += " · торг"
        return out
    return "—"


def get_city(offer: dict) -> str:
    loc = offer.get("location", {}) or {}
    return (loc.get("city") or {}).get("name", "") or ""


def send_telegram(token: str, chat_id: str, offer: dict) -> bool:
    title = html.escape(offer.get("title", "Без назви"))
    price = html.escape(get_price(offer))
    city = html.escape(get_city(offer))
    url = offer.get("url", "")
    text = (
        f"🚗 <b>{title}</b>\n"
        f"💰 {price}\n"
        f"📍 {city}\n"
        f'🔗 <a href="{html.escape(url)}">Дивитись на OLX</a>'
    )
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  !! Telegram error {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def load_seen() -> list[int]:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except json.JSONDecodeError:
            return []
    return []


def save_seen(ids: list[int]) -> None:
    SEEN_FILE.write_text(json.dumps(ids[-MAX_SEEN:], ensure_ascii=False))


def main() -> None:
    token = env("TELEGRAM_BOT_TOKEN", required=True)
    chat_id = env("TELEGRAM_CHAT_ID", required=True)
    category_id = env("OLX_CATEGORY_ID", "108")
    region_id = env("OLX_REGION_ID", "17")
    extra = env(
        "OLX_EXTRA_PARAMS",
        "filter_enum_cleared_customs[0]=no&filter_float_price:to=200000",
    )

    seen = load_seen()
    seen_set = set(seen)
    first_run = not seen  # empty/missing state -> seed silently

    if first_run:
        print("First run: seeding seen.json without sending messages.")
        collected: list[int] = []
        for offset in range(0, SEED_DEPTH, PAGE_LIMIT):
            batch = fetch_offers(category_id, region_id, extra, offset, PAGE_LIMIT)
            if not batch:
                break
            collected.extend(o["id"] for o in batch)
        save_seen(collected)
        print(f"Seeded {len(collected)} offers. Next runs will notify on new ones.")
        return

    new_offers = []
    for page in range(POLL_PAGES):
        batch = fetch_offers(category_id, region_id, extra, page * PAGE_LIMIT, PAGE_LIMIT)
        for o in batch:
            if o["id"] not in seen_set:
                new_offers.append(o)
                seen_set.add(o["id"])

    # oldest first so Telegram order reads chronologically
    new_offers.sort(key=lambda o: o.get("created_time", ""))

    print(f"Found {len(new_offers)} new offer(s).")
    sent_any = False
    for o in new_offers:
        print(f"  -> {o['id']} {o.get('title','')[:50]}")
        if send_telegram(token, chat_id, o):
            seen.append(o["id"])  # only mark seen if delivery succeeded (auto-retry otherwise)
            sent_any = True
        time.sleep(1)  # stay under Telegram rate limits

    if sent_any:
        save_seen(seen)


if __name__ == "__main__":
    main()
