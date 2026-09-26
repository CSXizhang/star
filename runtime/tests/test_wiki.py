"""Wiki lookup enrichment (contract §3.3): per-result page URLs, query
metadata (language/version/queriedAt), HTML snippet cleanup, and the clear
"Wiki lookup unavailable" failure wording at the tool layer.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from stardew_ai_runtime.mcp_server import create_mcp_server
from stardew_ai_runtime.wiki import WikiLookup


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, limit: int = -1) -> bytes:
        return self._data

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *args) -> bool:
        return False


def _api_payload() -> bytes:
    return json.dumps({
        "query": {
            "search": [
                {
                    "title": "Egg Festival",
                    "snippet": "The <span class=\"searchmatch\">Egg Festival</span> takes place on the 13th of Spring &amp; costs 100g",
                },
                {"title": "Strawberry", "snippet": "A <span class=\"searchmatch\">Strawberry</span> is a fruit"},
            ]
        }
    }).encode("utf-8")


def test_wiki_cache_is_short_and_source_attributed(tmp_path: Path, monkeypatch):
    lookup = WikiLookup(tmp_path / "wiki-cache.json")
    monkeypatch.setattr("stardew_ai_runtime.wiki.urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    lookup.cache_path.parent.mkdir(parents=True, exist_ok=True)
    lookup.cache_path.write_text('{"parsnip": {"query":"parsnip","source":"Stardew Valley Wiki","sourceUrl":"https://stardewvalleywiki.com/","results":[],"cachedAt":99999999999}}', encoding="utf-8")
    result = lookup.lookup("parsnip")
    assert result["cached"] is True
    assert result["source"] == "Stardew Valley Wiki"


def test_wiki_lookup_adds_url_language_and_query_time(tmp_path: Path, monkeypatch):
    lookup = WikiLookup(tmp_path / "wiki-cache.json")
    monkeypatch.setattr(
        "stardew_ai_runtime.wiki.urllib.request.urlopen",
        lambda *a, **k: _Resp(_api_payload()),
    )
    result = lookup.lookup("egg festival")
    assert result["cached"] is False
    assert result["contentLanguage"] == "en"
    assert result["sourceUrl"] == "https://stardewvalleywiki.com/"
    assert "queriedAt" in result and result["queriedAt"] > 0
    assert "版本" in result["versionNote"]
    titles = [r["title"] for r in result["results"]]
    assert titles == ["Egg Festival", "Strawberry"]
    assert result["results"][0]["url"] == "https://stardewvalleywiki.com/Egg_Festival"
    assert result["results"][1]["url"] == "https://stardewvalleywiki.com/Strawberry"


def test_wiki_lookup_strips_html_from_snippets(tmp_path: Path, monkeypatch):
    lookup = WikiLookup(tmp_path / "wiki-cache.json")
    monkeypatch.setattr(
        "stardew_ai_runtime.wiki.urllib.request.urlopen",
        lambda *a, **k: _Resp(_api_payload()),
    )
    result = lookup.lookup("egg festival")
    first, second = result["results"]
    assert "<span" not in first["snippet"]
    assert "Egg Festival" in first["snippet"]
    assert "&amp;" not in first["snippet"] and "&" in first["snippet"]
    assert "<span" not in second["snippet"]


def test_wiki_lookup_failure_is_clear_at_tool_layer(tmp_path: Path, monkeypatch):
    def _boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr("stardew_ai_runtime.wiki.urllib.request.urlopen", _boom)
    scheduler = MagicMock()
    scheduler.run_dir = None
    server = create_mcp_server(run_dir=tmp_path, scheduler=scheduler)

    async def run() -> None:
        with pytest.raises(ToolError, match="Wiki lookup unavailable"):
            await server.call_tool("query_wiki", {"query": "egg festival"})

    asyncio.run(run())


def test_wiki_distinguishes_page_evidence_and_refreshes_query_time(tmp_path, monkeypatch):
    import urllib.parse
    def reply(request, **kwargs):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        if params["action"] == ["parse"]:
            return _Resp(json.dumps({"parse": {"wikitext": {"*": "Official page body " * 900}}}).encode())
        return _Resp(_api_payload())
    monkeypatch.setattr("stardew_ai_runtime.wiki.urllib.request.urlopen", reply)
    lookup = WikiLookup(tmp_path / "wiki.json")
    fresh = lookup.lookup("Egg Festival")
    assert fresh["results"][0]["evidenceType"] == "page-excerpt"
    assert len(fresh["results"][0]["pageExcerpt"]) == 12000
    assert fresh["results"][0]["excerptTruncated"] is True
    cached = lookup.lookup("Egg Festival")
    assert cached["cached"] is True and cached["cachedAt"] == fresh["cachedAt"]
    assert cached["queriedAt"] >= fresh["queriedAt"]
