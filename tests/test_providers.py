import pytest

from app.providers import registry, weather
from app.providers.base import ProviderError


def _mock_weather_http(monkeypatch):
    def fake_get_json(url, params=None, timeout=None):
        if "geocoding-api" in url:
            return {"results": [
                {"name": "London", "country": "United Kingdom",
                 "latitude": 51.5074, "longitude": -0.1278, "timezone": "Europe/London"},
            ]}
        return {
            "current": {"temperature_2m": 18.2, "weather_code": 61,
                        "relative_humidity_2m": 76, "apparent_temperature": 17.5,
                        "wind_speed_10m": 14.0, "is_day": 1, "time": "2026-09-16T12:00"},
            "current_units": {"temperature_2m": "°C", "wind_speed_10m": "km/h"},
        }
    monkeypatch.setattr("app.providers.weather.http_get_json", fake_get_json)


def test_weather_flow(monkeypatch):
    _mock_weather_http(monkeypatch)
    out = registry.get_provider("weather").run({"location": "London"}, "public:read")
    assert out["ok"] is True
    result = out["result"]
    assert result["location"] == "London, United Kingdom"
    assert result["temperature"] == 18.2
    assert result["condition"] == "Light rain"


def test_weather_missing_location():
    out = registry.get_provider("weather").run({}, "public:read")
    assert out["ok"] is False
    assert "location" in out["error"]


def test_weather_bad_units():
    out = registry.get_provider("weather").run({"location": "London", "units": "kelvin"}, "public:read")
    assert out["ok"] is False
    assert "units" in out["error"]


def test_weather_geocode_miss(monkeypatch):
    monkeypatch.setattr("app.providers.weather.http_get_json",
                        lambda url, params=None, timeout=None: {"results": []})
    out = registry.get_provider("weather").run({"location": "Atlantis"}, "public:read")
    assert out["ok"] is False
    assert "geocode" in out["error"]


def test_weather_wrong_scope():
    out = registry.get_provider("weather").run({"location": "London"}, "github:write")
    assert out["ok"] is False
    assert "scope" in out["error"]


def test_npms_lookup(monkeypatch):
    monkeypatch.setattr("app.providers.npms_lookup.http_get_json", lambda url, params=None, timeout=None: {
        "collected": {"metadata": {
            "name": "express", "version": "4.19.2", "description": "Fast, unopinionated web framework",
            "license": "MIT",
            "links": {"homepage": "https://expressjs.com", "repository": "https://github.com/expressjs/express"},
            "maintainers": [{"username": "dougwilson"}],
        }},
        "score": {"final": 0.949, "detail": {"quality": 0.979, "popularity": 0.935, "maintenance": 0.939}},
    })
    out = registry.get_provider("npms_lookup").run({"package": "express"}, "public:read")
    assert out["ok"] is True
    assert out["result"]["version"] == "4.19.2"
    assert out["result"]["license"] == "MIT"
    assert out["result"]["score_final"] == 0.949


def test_npms_missing_package():
    out = registry.get_provider("npms_lookup").run({}, "public:read")
    assert out["ok"] is False
    assert "package" in out["error"]


def test_pypi_lookup(monkeypatch):
    monkeypatch.setattr("app.providers.pypi_lookup.http_get_json", lambda url, params=None, timeout=None: {
        "info": {"name": "requests", "version": "2.32.3", "summary": "Python HTTP for Humans.",
                 "requires_python": ">=3.8", "license": "Apache-2.0",
                 "home_page": "https://requests.readthedocs.io",
                 "project_urls": {"Source": "https://github.com/psf/requests"}},
        "releases": {"2.32.3": [], "2.31.0": []},
    })
    out = registry.get_provider("pypi_lookup").run({"package": "requests"}, "public:read")
    assert out["ok"] is True
    assert out["result"]["version"] == "2.32.3"
    assert out["result"]["num_versions"] == 2


def test_pypi_missing_package():
    out = registry.get_provider("pypi_lookup").run({}, "public:read")
    assert out["ok"] is False
    assert "package" in out["error"]


def test_web_search(monkeypatch):
    monkeypatch.setattr("app.providers.web_search.http_get_json", lambda url, params=None, timeout=None: {
        "AbstractText": "London is the capital and largest city of England and the United Kingdom.",
        "AbstractURL": "https://en.wikipedia.org/wiki/London",
        "Heading": "London",
        "RelatedTopics": [
            {"Text": "London Eye - Ferris wheel", "FirstURL": "https://en.wikipedia.org/wiki/London_Eye"},
        ],
    })
    out = registry.get_provider("web_search").run({"query": "London", "max_results": 2}, "public:read")
    assert out["ok"] is True
    assert out["result"]["count"] == 2
    assert "capital" in out["result"]["results"][0]["snippet"]


def test_web_search_no_results(monkeypatch):
    monkeypatch.setattr("app.providers.web_search.http_get_json",
                        lambda url, params=None, timeout=None: {"RelatedTopics": []})
    out = registry.get_provider("web_search").run({"query": "zzzznonexistent"}, "public:read")
    assert out["ok"] is False


def test_web_search_bad_max_results():
    out = registry.get_provider("web_search").run({"query": "London", "max_results": 10}, "public:read")
    assert out["ok"] is False
    assert "max_results" in out["error"]


def test_all_providers_registered():
    slugs = {entry["slug"] for entry in registry.list_public_tools()}
    assert {"weather", "npms_lookup", "pypi_lookup", "web_search"} <= slugs
    for entry in registry.list_public_tools():
        assert entry["scopes"] == ["public:read"]


def test_unregistered_provider_none():
    assert registry.get_provider("github-mcp") is None