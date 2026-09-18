from pathlib import Path

from stardew_ai_runtime.wiki import WikiLookup


def test_wiki_cache_is_short_and_source_attributed(tmp_path: Path, monkeypatch):
    lookup = WikiLookup(tmp_path / "wiki-cache.json")
    monkeypatch.setattr("stardew_ai_runtime.wiki.urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    lookup.cache_path.parent.mkdir(parents=True, exist_ok=True)
    lookup.cache_path.write_text('{"parsnip": {"query":"parsnip","source":"Stardew Valley Wiki","sourceUrl":"https://stardewvalleywiki.com/","results":[],"cachedAt":99999999999}}', encoding="utf-8")
    result = lookup.lookup("parsnip")
    assert result["cached"] is True
    assert result["source"] == "Stardew Valley Wiki"
