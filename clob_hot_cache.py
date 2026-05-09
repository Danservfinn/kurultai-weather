#!/usr/bin/env python3
"""In-memory public CLOB book cache for hot-token paper research."""

from __future__ import annotations

import datetime as dt
from typing import Any

import features
import scanner


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(value: Any) -> dt.datetime | None:
    parsed = features.parse_time(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


class HotBookCache:
    """Small cache facade; network subscription is intentionally out of scope."""

    def __init__(self, *, now_fn=utc_now) -> None:
        self._token_ids: set[str] = set()
        self._books: dict[str, dict[str, Any]] = {}
        self._now_fn = now_fn

    @property
    def token_ids(self) -> set[str]:
        return set(self._token_ids)

    def subscribe(self, token_ids: Any) -> None:
        if isinstance(token_ids, str):
            token_ids = [token_ids]
        for token_id in token_ids or []:
            if token_id:
                self._token_ids.add(str(token_id))

    def unsubscribe(self, token_ids: Any) -> None:
        if isinstance(token_ids, str):
            token_ids = [token_ids]
        for token_id in token_ids or []:
            self._token_ids.discard(str(token_id))

    def update_from_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        token_id = message.get("token_id") or message.get("asset_id") or message.get("market")
        if not token_id:
            return None
        token = str(token_id)
        timestamp = message.get("timestamp") or message.get("updated_at") or self._now_fn().isoformat()
        raw_book = {
            "bids": message.get("bids") or message.get("buys") or [],
            "asks": message.get("asks") or message.get("sells") or [],
            "timestamp": timestamp,
        }
        quote = scanner.quote_from_book(raw_book, None, float(message.get("paper_size") or 1.0))
        asks = scanner.parse_book_side(raw_book["asks"], reverse=False)
        ask_depth = asks[0][1] if asks else 0.0
        book = {
            "token_id": token,
            "bids": raw_book["bids"],
            "asks": raw_book["asks"],
            "updated_at": timestamp,
            "best_bid": quote.get("bid"),
            "best_ask": quote.get("ask"),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "spread": quote.get("spread"),
            "ask_depth": ask_depth,
            "depth": ask_depth,
            "depth_sufficient": quote.get("depth_sufficient"),
            "midpoint": quote.get("midpoint"),
            "quote_age_seconds": self.book_age_seconds({"updated_at": timestamp}),
            "stale_book_flag": quote.get("stale_book_flag"),
            "execution_source": quote.get("execution_source"),
            "raw_status": quote.get("raw_status"),
        }
        self._books[token] = book
        self._token_ids.add(token)
        return dict(book)

    def get_book(self, token_id: str | None) -> dict[str, Any] | None:
        if not token_id:
            return None
        book = self._books.get(str(token_id))
        if book is None:
            return None
        out = dict(book)
        out["quote_age_seconds"] = self.book_age_seconds(out)
        return out

    def book_age_seconds(self, book_or_token: dict[str, Any] | str | None) -> float | None:
        if book_or_token is None:
            return None
        if isinstance(book_or_token, str):
            book = self._books.get(book_or_token)
        else:
            book = book_or_token
        if not book:
            return None
        timestamp = book.get("updated_at") or book.get("timestamp") or book.get("created_at")
        parsed = _parse_time(timestamp)
        if parsed is None:
            return None
        return max(0.0, (self._now_fn() - parsed).total_seconds())
