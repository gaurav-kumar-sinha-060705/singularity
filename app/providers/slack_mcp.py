"""First-party Slack REST provider (credential-backed).

The previous `slack-mcp` entry pointed at WayStation's hosted Slack MCP server,
which returns 402 "Payment required / DEPLOYMENT_DISABLED" (host-side, not
something we can toggle). This provider speaks directly to the Slack Web API
(api.slack.com) using the user's stored workspace token (`xoxb-` bot token or
`xoxp-` user token), so Slack works from Claude with just a token stored on the
dashboard — no third-party MCP host.

Argument shapes (all accepted):
  remote-style  {"tool": "post_message", "arguments": {...}}
  action-style  {"action": "post_message", "channel": "#dev", "text": "hi"}
  bare-style    {"list_channels": {}}

Supported actions:
  get_workspace_info  {}                                 -> auth.test
  list_channels       {}                                 -> conversations.list
  list_messages       {channel, limit?}                  -> conversations.history
  post_message        {channel, text, thread_ts?}        -> chat.postMessage
  list_users          {}                                 -> users.list
  search_messages     {query, limit?}                    -> search.messages

credential: {"access_token": "xoxb-..."} | {"token": ...} | {"api_key": ...}
"""

from app.providers.base import Provider, ProviderError

SLACK_API = "https://slack.com/api"


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


def _api(method_name: str, token: str, payload: dict | None = None) -> dict:
    """Call one Slack Web API method. Module-level so tests can stub it.

    Slack always returns HTTP 200 with {"ok": false, "error": ...} on failure,
    so success is decided by the `ok` flag, not the status code.
    """
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    resp = httpx.post(f"{SLACK_API}/{method_name}", data=payload or {},
                      headers=headers, timeout=15.0)
    if resp.status_code >= 400:
        raise ProviderError(f"Slack API HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("ok"):
        raise ProviderError(f"Slack API {method_name}: {data.get('error', 'unknown error')}")
    return data


def _channel_id(token: str, channel: str) -> str:
    channel = str(channel or "").strip()
    if not channel:
        raise ProviderError("channel is required (name like '#dev' or a channel id)")
    if channel.startswith("C") and len(channel) == 9:
        return channel
    name = channel.lstrip("#")
    data = _api("conversations.list", token,
                {"types": "public_channel,private_channel", "limit": 1000})
    for c in data.get("channels", []):
        if c.get("name") == name:
            return c["id"]
    raise ProviderError(f"channel '{channel}' not found in workspace")


def _message_rows(messages: list) -> list:
    rows = []
    for m in messages:
        if not isinstance(m, dict) or m.get("type") != "message":
            continue
        rows.append({
            "ts": m.get("ts"), "user": m.get("user"), "text": m.get("text"),
            "subtype": m.get("subtype"), "thread_ts": m.get("thread_ts"),
        })
    return rows


class SlackMcpProvider(Provider):
    slug = "slack-mcp"
    name = "Slack (first-party REST)"
    version = "1.0.0"
    category = "communication"
    description = (
        "First-party gateway to the Slack Web API using the user's stored "
        "workspace token (bot xoxb-* or user xoxp-*). Actions: "
        "get_workspace_info, list_channels, list_messages, post_message, "
        "list_users, search_messages. Authenticated per user."
    )
    scopes = {"public:read"}

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
                    "no action given — pass one of: get_workspace_info, "
                    "list_channels, list_messages, post_message, list_users, "
                    "search_messages (as {\"tool\": \"<action>\", \"arguments\": {...}})")
            action, params = str(candidates[0]), args[candidates[0]]

        actions = {"get_workspace_info", "list_channels", "list_messages",
                   "post_message", "list_users", "search_messages"}
        if action not in actions:
            raise ProviderError(
                f"unknown Slack action '{action}' — supported: "
                f"{', '.join(sorted(actions))}")

        if action in ("list_messages", "post_message"):
            if not str(params.get("channel") or "").strip():
                raise ProviderError("channel is required (name like '#dev' or a channel id)")
        if action == "post_message" and not str(params.get("text") or "").strip():
            raise ProviderError("text is required")
        if action == "search_messages" and not str(params.get("query") or "").strip():
            raise ProviderError("query is required")
        return {"action": action, "params": params}

    def execute(self, args: dict, credential: dict | None = None) -> dict:
        token = _token(credential)
        if not token:
            raise ProviderError(
                "no Slack token connected — connect your Slack workspace or "
                "store a bot/user token on the dashboard first")

        action = args["action"]
        p = args["params"]

        if action == "get_workspace_info":
            data = _api("auth.test", token)
            return {"action": action, "team": data.get("team"),
                    "team_id": data.get("team_id"), "user": data.get("user"),
                    "user_id": data.get("user_id"), "url": data.get("url")}
        if action == "list_channels":
            data = _api("conversations.list", token,
                        {"types": "public_channel,private_channel",
                         "exclude_archived": "true", "limit": 200})
            rows = [
                {"id": c.get("id"), "name": c.get("name"),
                 "is_private": c.get("is_private"), "is_member": c.get("is_member"),
                 "is_archived": c.get("is_archived"),
                 "topic": ((c.get("topic") or {}).get("value") or "")}
                for c in data.get("channels", []) if isinstance(c, dict)
            ]
            return {"action": action, "count": len(rows), "channels": rows}
        if action == "list_messages":
            channel_id = _channel_id(token, p["channel"])
            data = _api("conversations.history", token,
                        {"channel": channel_id, "limit": p.get("limit", 20)})
            return {"action": action, "channel": p["channel"], "channel_id": channel_id,
                    "count": len(data.get("messages", [])),
                    "messages": _message_rows(data.get("messages", []))}
        if action == "post_message":
            channel_id = _channel_id(token, p["channel"])
            payload = {"channel": channel_id, "text": p["text"]}
            if p.get("thread_ts"):
                payload["thread_ts"] = str(p["thread_ts"])
            data = _api("chat.postMessage", token, payload)
            return {"action": action, "channel": p["channel"], "ok": True,
                    "ts": data.get("ts"), "message": (data.get("message") or {}).get("text")}
        if action == "list_users":
            data = _api("users.list", token, {"limit": 200})
            rows = [
                {"id": u.get("id"), "name": u.get("name"), "real_name": u.get("real_name"),
                 "is_bot": u.get("is_bot")}
                for u in data.get("members", []) if isinstance(u, dict)
            ]
            return {"action": action, "count": len(rows), "users": rows}
        if action == "search_messages":
            data = _api("search.messages", token,
                        {"query": p["query"], "count": p.get("limit", 20)})
            matches = (data.get("messages") or {}).get("matches", [])
            rows = [
                {"ts": m.get("ts"), "user": m.get("user"), "text": m.get("text"),
                 "channel": m.get("channel"), "permalink": m.get("permalink")}
                for m in matches if isinstance(m, dict)
            ]
            return {"action": action, "query": p["query"], "count": len(rows),
                    "messages": rows}

        raise ProviderError(f"unhandled action '{action}'")  # pragma: no cover

    async def _async_list_tools(self, headers: dict[str, str] | None = None) -> list:
        """Verify the stored token against auth.test and list available actions
        (used by the dashboard verify endpoint)."""
        token = None
        if headers and headers.get("Authorization"):
            token = headers["Authorization"].split(" ", 1)[-1]
        if not token:
            raise ProviderError("no token provided")
        _api("auth.test", token)  # probe auth
        return [
            {"name": name, "description": description}
            for name, description in sorted({
                "get_workspace_info": "Identify the workspace and bot/user the token belongs to.",
                "list_channels": "List public and private channels in the workspace.",
                "list_messages": "Read recent messages in a channel (name like #dev or id).",
                "post_message": "Post a message to a channel (and optionally a thread_ts).",
                "list_users": "List workspace members (id, name, real_name, is_bot).",
                "search_messages": "Search messages across the workspace by query.",
            }.items())
        ]


provider = SlackMcpProvider()