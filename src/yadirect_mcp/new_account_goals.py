"""Read-only goal verification before a client's first measured campaign."""

from urllib.parse import urlsplit

from . import goals
from .identifiers import parse_id


async def verify(api, client_login, goal_ids, hrefs):
    """Require real goals from this client's complete catalog and advertised domains.

    GetStatGoals needs an existing measured campaign. The client-scoped Live
    catalog can verify availability before one exists. It does not prove firing,
    business quality, or the mapping to a specific counter; those remain manual.
    """
    domains = {urlsplit(href).hostname.lower().removeprefix("www.")
               for href in hrefs if isinstance(href, str) and urlsplit(href).hostname}
    if not domains:
        raise ValueError("New-account goals require verified landing domains")
    catalog = await goals.read_retargeting(api, client_login)
    if catalog.get("complete") is not True or catalog.get("client_login") != client_login:
        raise ValueError("New-account goals require a complete client-scoped catalog")
    available = {}
    for row in catalog["goals"]:
        identifier = parse_id(row["id"], "goal_id")
        if identifier in available:
            raise ValueError("New-account goal catalog contains duplicate IDs")
        available[identifier] = row
    for identifier in goal_ids:
        row = available.get(identifier)
        if not row or row.get("type") != "goal":
            raise ValueError("Selected conversion goal is unavailable or is a segment")
        domain = str(row.get("domain") or "").lower().removeprefix("www.")
        if domain not in domains:
            raise ValueError("Selected conversion goal belongs to another landing domain")
    return {"status": "PASS", "catalog_source": "GetRetargetingGoals",
            "client_login": client_login, "goal_ids": sorted(goal_ids),
            "domains": sorted(domains), "counter_mapping": "caller_reviewed",
            "note": "Доступность целей подтверждена в полном каталоге клиента; "
                    "принадлежность счётчику и срабатывание проверяет специалист."}
