"""Bounded, on-demand Stardew Valley Wiki lookup with a small source cache."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class WikiLookup:
    def __init__(self, cache_path: Path | str, ttl_seconds: int = 7 * 24 * 3600):
        self.cache_path = Path(cache_path)
        self.ttl_seconds = ttl_seconds

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def lookup(self, query: str) -> dict[str, Any]:
        query = query.strip()
        if not query or len(query) > 120:
            raise ValueError("query must contain 1-120 characters")
        key = query.casefold()
        cache = self._load()
        cached = cache.get(key)
        if isinstance(cached, dict) and time.time() - float(cached.get("cachedAt", 0)) < self.ttl_seconds:
            return {**cached, "cached": True}

        params = urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": 3, "format": "json", "utf8": 1,
        })
        url = "https://stardewvalleywiki.com/mediawiki/api.php?" + params
        request = urllib.request.Request(url, headers={"User-Agent": "StardewAICompanion/0.1 (on-demand wiki lookup)"})
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        hits = data.get("query", {}).get("search", []) if isinstance(data, dict) else []
        result = {
            "query": query,
            "source": "Stardew Valley Wiki",
            "sourceUrl": "https://stardewvalleywiki.com/",
            "results": [
                {"title": h.get("title", ""), "snippet": h.get("snippet", "")[:300]}
                for h in hits if isinstance(h, dict)
            ],
            "cached": False,
            "cachedAt": time.time(),
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache[key] = result
        temp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.cache_path)
        return result
