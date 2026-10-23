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
    lookup.cache_path.write_text('{"search:v2:parsnip": {"query":"parsnip","source":"Stardew Valley Wiki","sourceUrl":"https://stardewvalleywiki.com/","results":[],"cachedAt":99999999999}}', encoding="utf-8")
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


def test_exact_page_sections_reuse_one_read_and_reach_later_tables(tmp_path, monkeypatch):
    calls = []
    body = "Intro " * 2200 + "\n==Growth==\n6 days\n==Prices==\nSeed cost 40; base value 90"
    def reply(request, **kwargs):
        calls.append(request.full_url)
        return _Resp(json.dumps({"parse": {"title": "Radish", "wikitext": {"*": body}}}).encode())
    monkeypatch.setattr("stardew_ai_runtime.wiki.urllib.request.urlopen", reply)
    wiki = WikiLookup(tmp_path / "wiki.json")
    first = wiki.lookup("Radish", exact_page=True)
    assert first["results"][0]["excerptTruncated"]
    prices = wiki.lookup("radish", exact_page=True, section="Prices")
    assert prices["cached"] and prices["results"][0]["sectionFound"]
    assert "Seed cost 40" in prices["results"][0]["pageExcerpt"]
    missing = wiki.lookup("Radish", exact_page=True, section="Unknown")
    assert not missing["results"][0]["sectionFound"]
    assert len(calls) == 1


def test_blocked_english_falls_back_to_attributed_chinese_body(tmp_path, monkeypatch):
    import urllib.error
    import urllib.parse
    calls = []
    def reply(request, **kwargs):
        calls.append(request.full_url)
        if request.host == 'stardewvalleywiki.com':
            raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden', {}, None)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        if params['action'] == ['query']:
            return _Resp(json.dumps({'query': {'search': [
                {'title': 'Potato', 'snippet': 'redirect'}, {'title': '土豆', 'snippet': 'crop'}]}}).encode())
        return _Resp(json.dumps({'parse': {'title': '土豆', 'wikitext': {'*': '正文\n==生长阶段==\n6 天'}}}).encode())
    monkeypatch.setattr('stardew_ai_runtime.wiki.urllib.request.urlopen', reply)
    wiki = WikiLookup(tmp_path / 'wiki.json')
    result = wiki.lookup('Potato')
    assert result['contentLanguage'] == 'zh' and 'BWIKI' in result['source']
    assert result['sourceErrors'][0]['error'] == 'HTTP Error 403: Forbidden'
    assert len(result['results']) == 1
    entry = result['results'][0]
    assert entry['title'] == '土豆' and entry['evidenceType'] == 'page-excerpt'
    assert entry['url'] == 'https://wiki.biligame.com/stardewvalley/%E5%9C%9F%E8%B1%86'
    assert len(calls) == 3  # failed English search, Chinese search, canonical page read
    for title in ('Potato', '土豆'):
        page = wiki.lookup(title, exact_page=True, section='生长阶段')
        assert page['cached'] and page['contentLanguage'] == 'zh'
        assert page['results'][0]['sectionFound'] and '6 天' in page['results'][0]['pageExcerpt']
    assert wiki.lookup('Potato')['cached']
    assert len(calls) == 3


def test_chinese_query_goes_directly_to_chinese_source(tmp_path, monkeypatch):
    def reply(request, **kwargs):
        assert request.host == 'wiki.biligame.com'
        return _Resp(json.dumps({'parse': {'title': '土豆', 'wikitext': {'*': '正文'}}}).encode())
    monkeypatch.setattr('stardew_ai_runtime.wiki.urllib.request.urlopen', reply)
    result = WikiLookup(tmp_path / 'wiki.json').lookup('土豆', exact_page=True)
    assert result['contentLanguage'] == 'zh'
    assert result['results'][0]['sourceUrl'] == result['sourceUrl']


def test_missing_exact_page_does_not_become_an_unrelated_search_result(tmp_path, monkeypatch):
    monkeypatch.setattr('stardew_ai_runtime.wiki.urllib.request.urlopen',
        lambda *a, **k: _Resp(b'{"error":{"code":"missingtitle"}}'))
    with pytest.raises(OSError, match='missingtitle'):
        WikiLookup(tmp_path / 'wiki.json').lookup('Not a page', exact_page=True)
    assert not (tmp_path / 'wiki.json').exists()


def test_api_error_is_unavailable_not_successful_empty_search(tmp_path, monkeypatch):
    monkeypatch.setattr('stardew_ai_runtime.wiki.urllib.request.urlopen',
        lambda *a, **k: _Resp(b'{"error":{"code":"ratelimited"}}'))
    with pytest.raises(OSError, match='ratelimited'):
        WikiLookup(tmp_path / 'wiki.json').lookup('Potato')
    assert not (tmp_path / 'wiki.json').exists()


def test_empty_search_is_not_cached_and_old_empty_cache_is_ignored(tmp_path, monkeypatch):
    calls = []
    def reply(request, **kwargs):
        calls.append(request.full_url)
        return _Resp(b'{"query":{"search":[]}}')
    monkeypatch.setattr('stardew_ai_runtime.wiki.urllib.request.urlopen', reply)
    path = tmp_path / 'wiki.json'
    path.write_text(json.dumps({'potato': {'results': [], 'cachedAt': 99999999999}}))
    wiki = WikiLookup(path)
    assert wiki.lookup('Potato')['results'] == []
    assert wiki.lookup('Potato')['cached'] is False
    assert len(calls) == 4


def test_failed_page_reads_remain_snippets_and_are_retried(tmp_path, monkeypatch):
    import urllib.parse
    calls = []
    def reply(request, **kwargs):
        calls.append(request.full_url)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        if params['action'] == ['parse']:
            raise OSError('blocked')
        return _Resp(_api_payload())
    monkeypatch.setattr('stardew_ai_runtime.wiki.urllib.request.urlopen', reply)
    wiki = WikiLookup(tmp_path / 'wiki.json')
    result = wiki.lookup('egg festival')
    assert all(e['evidenceType'] == 'search-snippet' and 'pageExcerpt' not in e for e in result['results'])
    assert all('blocked' in e['pageReadError'] for e in result['results'])
    first_calls = len(calls)
    assert wiki.lookup('egg festival')['cached'] is False
    assert len(calls) == first_calls * 2
