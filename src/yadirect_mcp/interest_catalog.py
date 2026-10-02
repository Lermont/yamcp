"""Complete read-only audience-interest dictionary for a scoped client."""

from typing import Any


async def read(api: Any, client_login: str, query: str | None = None) -> dict[str, Any]:
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise ValueError("query must be a non-empty string")
    result = await api.call_v501(
        "dictionaries", "get", {"DictionaryNames": ["AudienceInterests"]},
        client_login=client_login,
    )
    rows = result.get("AudienceInterests")
    if result.get("LimitedBy") is not None or not isinstance(rows, list):
        raise ValueError("AudienceInterests dictionary is incomplete")
    ids = [row.get("Id") for row in rows]
    if any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("AudienceInterests dictionary has invalid or duplicate IDs")
    selected = [row for row in rows if row.get("InterestType") == "SHORT_TERM"
                and (query is None or query.casefold() in str(row.get("Name", "")).casefold())]
    return {"client_login": client_login, "interests": selected, "count": len(selected),
            "catalog_count": len(rows), "complete": True, "api_version": "v501",
            "query": query}
