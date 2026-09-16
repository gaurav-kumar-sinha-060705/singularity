from app.providers.base import Provider, ProviderError, http_get_json

DDG_URL = "https://api.duckduckgo.com/"


class WebSearchProvider(Provider):
    slug = "web_search"
    name = "Web Search (DuckDuckGo Instant Answers)"
    version = "1.0.0"
    category = "research"
    description = (
        "Keyless web search via the DuckDuckGo Instant Answer API: a headline "
        "answer, definition, and related links for a query. Read-only and "
        "does not scrape web pages."
    )
    scopes = {"public:read"}

    def validate(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ProviderError("query is required")
        if len(query) > 300:
            raise ProviderError("query is too long")
        max_results = int(args.get("max_results", 3))
        if not 1 <= max_results <= 5:
            raise ProviderError("max_results must be between 1 and 5")
        return {"query": query, "max_results": max_results}

    def _flatten(self, topics: list, results: list) -> None:
        for topic in topics or []:
            if topic.get("Topics"):
                self._flatten(topic["Topics"], results)
            elif topic.get("Text") or topic.get("FirstURL"):
                text = topic.get("Text") or ""
                title = text.split(" - ")[0].strip() or "Related topic"
                results.append({
                    "title": title,
                    "url": topic.get("FirstURL"),
                    "snippet": text,
                })

    def execute(self, args: dict) -> dict:
        body = http_get_json(DDG_URL, {
            "q": args["query"], "format": "json",
            "no_html": 1, "skip_disambig": 1,
        })
        results: list = []
        if body.get("AbstractText"):
            results.append({
                "title": body.get("Heading") or "Definition",
                "url": body.get("AbstractURL"),
                "snippet": body["AbstractText"],
            })
        self._flatten(body.get("RelatedTopics") or [], results)
        if not results:
            raise ProviderError(
                f"DuckDuckGo returned no instant answers for '{args['query']}' "
                "(Instant-Answer API only covers defined/short-answer queries)"
            )
        capped = results[: args["max_results"]]
        return {
            "query": args["query"],
            "count": len(capped),
            "results": capped,
            "source": "DuckDuckGo Instant Answer API",
        }


provider = WebSearchProvider()