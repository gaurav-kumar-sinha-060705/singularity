"""First-party GitHub REST provider (credential-backed).

The previous `github-mcp` entry pointed at a Smithery-hosted MCP endpoint that
returns 404 regardless of credentials, and Smithery's auth model never accepted
a plain GitHub PAT anyway. This provider replaces it: it speaks directly to the
GitHub REST API using the user's stored Personal Access Token, so Claude's
`call_tool("github-mcp", ...)` (and the dashboard's connect + verify flow) works
with nothing more than a GitHub token — no third-party host, no OAuth app.

Argument shapes (all accepted):
  remote-style  {"tool": "list_repositories", "arguments": {...}}
  action-style  {"action": "search_repositories", "q": "mcp", "per_page": 5}
  bare-style    {"list_repositories": {}}  /  {"get_repository": {"repo": "a/b"}}

Supported actions:
  list_repositories    {}                          -> GET /user/repos
  get_repository       {repo: "owner/name"}        -> GET /repos/{repo}
  search_repositories  {q, per_page}               -> GET /search/repositories
  list_issues          {repo, state, per_page}     -> GET /repos/{repo}/issues
  get_user             {username?}                 -> GET /user (or /users/{u})
  create_gist          {description, files: {name: content}} -> POST /gists
  star_repository      {repo}                      -> PUT /user/starred/{repo}

credential: {"access_token": "ghp_..."} | {"token": ...} | {"api_key": ...}
"""

from urllib.parse import quote

import httpx

from app.providers.base import Provider, ProviderError

GITHUB_API = "https://api.github.com"

ACTIONS: dict[str, str] = {
    "list_repositories": "List the authenticated user's repositories (name, private, html_url, default_branch, updated_at).",
    "get_repository": "Get a repository by name (owner/name): description, stars, default_branch, pushed_at, archived, license.",
    "search_repositories": "Search public repositories by query q (stars, language, name).",
    "list_issues": "List issues for a repository (owner/name), optional state=open/closed/all, per_page (default 10).",
    "get_user": "Get a GitHub user profile (username optional -> the token's own account).",
    "create_gist": "Create a gist: description + files map of {filename: content}.",
    "star_repository": "Star a repository (owner/name) as the token's user.",
}

SCOPE = "public:read"


def _token(credential: dict | None) -> str | None:
    if not credential:
        return None
    value = (
        credential.get("access_token")
        or credential.get("token")
        or credential.get("api_key")
        or ""
    ).strip()
    return value or None


def _mask(token: str) -> str:
    if len(token) <= 12:
        return "…".join((token[:3], token[-2:]))
    return f"{token[:6]}…{token[-4:]}"


def _api(method: str, url: str, token: str, params: dict | None = None,
         body: dict | None = None) -> dict:
    """Single network entrypoint (module-level so tests can stub it)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "singularity-mcp-gateway",
    }
    resp = httpx.request(method, url, headers=headers, params=params, json=body,
                         timeout=15.0)
    if resp.status_code >= 400:
        try:
            message = resp.json().get("message", resp.text[:200])
        except Exception:
            message = resp.text[:200]
        raise ProviderError(f"GitHub API {resp.status_code}: {message}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def _owner_repo(repo: str) -> str:
    repo = str(repo or "").strip().strip("/")
    if not repo or "/" not in repo:
        raise ProviderError("repo must be of the form 'owner/name'")
    return quote(repo, safe="/")


class GithubProvider(Provider):
    slug = "github-mcp"
    name = "GitHub (first-party REST)"
    version = "1.0.0"
    token_hint = "ghp_... (fine-grained PAT) or gho_... (OAuth)"
    category = "developer-tools"
    description = (
        "First-party gateway to the GitHub REST API using the user's stored "
        "Personal Access Token. Actions: list_repositories, get_repository, "
        "search_repositories, list_issues, get_user, create_gist, "
        "star_repository. Authenticated per user; nothing leaves the gateway."
    )
    scopes = {SCOPE}

    @property
    def requires_auth(self) -> bool:
        return True

    @staticmethod
    def _auth_headers(credential: dict | None) -> dict[str, str] | None:
        token = _token(credential)
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    def validate(self, args: dict) -> dict:
        if not isinstance(args, dict):
            raise ProviderError("arguments must be an object")

        if args.get("tool"):
            action = str(args["tool"]).strip()
            params = args.get("arguments") or {}
            if not isinstance(params, dict):
                raise ProviderError("'arguments' must be an object")
        elif args.get("action"):
            action = str(args["action"]).strip()
            params = {k: v for k, v in args.items() if k != "action"}
        else:
            candidates = [k for k in args]
            if len(candidates) != 1 or not isinstance(args[candidates[0]], dict):
                raise ProviderError(
                    f"no action given — pass one of: {', '.join(sorted(ACTIONS))} "
                    '(as {"tool": "<action>", "arguments": {...}})')
            action, params = str(candidates[0]), args[candidates[0]]

        if action not in ACTIONS:
            raise ProviderError(
                f"unknown GitHub action '{action}' — supported: "
                f"{', '.join(sorted(ACTIONS))}")

        if action in ("get_repository", "star_repository", "list_issues"):
            _owner_repo(params.get("repo", ""))
        if action == "search_repositories" and not str(params.get("q") or "").strip():
            raise ProviderError("q is required (search query)")
        if action == "create_gist":
            files = params.get("files")
            if not isinstance(files, dict) or not files:
                raise ProviderError("files is required (map of {filename: content})")
        return {"action": action, "params": params}

    def execute(self, args: dict, credential: dict | None = None) -> dict:
        token = _token(credential)
        if not token:
            raise ProviderError(
                "no GitHub token connected — connect your GitHub account or "
                "store a Personal Access Token on the dashboard first")

        action = args["action"]
        p = args["params"]
        base = GITHUB_API

        if action == "list_repositories":
            data = _api("GET", f"{base}/user/repos", token,
                        params={"per_page": p.get("per_page", 30),
                                "sort": p.get("sort", "updated")})
            rows = [
                {"name": r.get("full_name"), "private": r.get("private"),
                 "html_url": r.get("html_url"),
                 "default_branch": r.get("default_branch"),
                 "updated_at": r.get("updated_at")}
                for r in data if isinstance(r, dict)
            ]
            return {"action": action, "count": len(rows), "repositories": rows,
                    "authenticated_as": _mask(token)}
        if action == "get_repository":
            data = _api("GET", f"{base}/repos/{_owner_repo(p['repo'])}", token)
            return {"action": action,
                    "repository": {
                        "name": data.get("full_name"), "description": data.get("description"),
                        "stars": data.get("stargazers_count"), "default_branch": data.get("default_branch"),
                        "pushed_at": data.get("pushed_at"), "archived": data.get("archived"),
                        "license": (data.get("license") or {}).get("spdx_id"),
                        "html_url": data.get("html_url"), "fork": data.get("fork")}}
        if action == "search_repositories":
            data = _api("GET", f"{base}/search/repositories", token,
                        params={"q": p["q"], "per_page": p.get("per_page", 10)})
            rows = [
                {"name": r.get("full_name"), "html_url": r.get("html_url"),
                 "stars": r.get("stargazers_count"), "description": r.get("description"),
                 "fork": r.get("fork"), "language": r.get("language")}
                for r in data.get("items", []) if isinstance(r, dict)
            ]
            return {"action": action, "total_count": data.get("total_count", len(rows)),
                    "results": rows}
        if action == "list_issues":
            data = _api("GET", f"{base}/repos/{_owner_repo(p['repo'])}/issues", token,
                        params={"state": p.get("state", "open"),
                                "per_page": p.get("per_page", 10)})
            rows = [
                {"number": i.get("number"), "title": i.get("title"),
                 "state": i.get("state"), "html_url": i.get("html_url"),
                 "pull_request": bool(i.get("pull_request"))}
                for i in data if isinstance(i, dict)
            ]
            return {"action": action, "repo": p["repo"], "count": len(rows), "issues": rows}
        if action == "get_user":
            username = str(p.get("username") or "").strip()
            url = f"{base}/user" if not username else f"{base}/users/{quote(username)}"
            data = _api("GET", url, token)
            return {"action": action, "login": data.get("login"),
                    "name": data.get("name"), "public_repos": data.get("public_repos"),
                    "html_url": data.get("html_url")}
        if action == "create_gist":
            files = {name: {"content": content}
                     for name, content in (p.get("files") or {}).items()}
            data = _api("POST", f"{base}/gists", token,
                        body={"description": p.get("description", ""), "files": files})
            return {"action": action, "id": data.get("id"), "html_url": data.get("html_url"),
                    "public": data.get("public")}
        if action == "star_repository":
            _api("PUT", f"{base}/user/starred/{_owner_repo(p['repo'])}", token)
            return {"action": action, "repo": p["repo"], "starred": True}

        raise ProviderError(f"unhandled action '{action}'")  # pragma: no cover

    async def _async_list_tools(self, headers: dict[str, str] | None = None) -> list:
        """Verify the stored token against the GitHub API and list available
        (gateway-native) actions. Used by the dashboard verify endpoint."""
        token = None
        if headers and headers.get("Authorization"):
            token = headers["Authorization"].split(" ", 1)[-1]
        if not token:
            raise ProviderError("no token provided")
        _api("GET", f"{GITHUB_API}/user", token)  # probe auth
        return [
            {"name": name, "description": description}
            for name, description in sorted(ACTIONS.items())
        ]


provider = GithubProvider()