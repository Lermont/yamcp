"""Audience-group append tests. All external reads/writes are in-memory fakes."""

from copy import deepcopy

import pytest

from yadirect_mcp import audience_group as mod
from yadirect_mcp import repair, verification


def plan():
    return repair.normalize(
        {
            "audience_group": {
                "campaign_id": 1,
                "source_ad_id": 2,
                "segment_id": 4,
                "name": "Contact audience",
            }
        },
        "client",
    )


class Api:
    def __init__(self):
        self.campaign = {
            "id": 1,
            "type": "UNIFIED_CAMPAIGN",
            "state": "ON",
            "status": "MODERATION",
            "tracking_params": "utm_source=yandex",
            "bidding_strategy": {
                "Search": {"BiddingStrategyType": "SERVING_OFF"},
                "Network": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS", "weekly_budget": 1000},
            },
        }
        self.ad = {
            "id": 2,
            "campaign_id": 1,
            "ad_group_id": 3,
            "type": "RESPONSIVE_AD",
            "state": "OFF",
            "status": "MODERATION",
            "href": "https://example.org/",
            "titles": [
                "Дополните свой хелпдеск аналитикой обращений клиентов",
                "Отчётность поддержки: темы обращений и их динамика",
                "Подключите отчётность к вашему хелпдеску через API",
            ],
            "texts": [
                "Анализируйте обращения клиентов и их категории.",
                "Узнайте о возможностях аналитики для хелпдеска.",
                "Подключение через API. Изучите тарифы сервиса.",
            ],
            "ad_image_hashes": ["image1", "image2", "image3"],
            "ad_extension_ids": [6],
            "sitelink_set_id": 5,
            "price_extension": {
                "Price": 5900000000,
                "PriceQualifier": "FROM",
                "PriceCurrency": "RUB",
            },
        }
        self.inventory = {
            "groups": [
                {
                    "id": 3,
                    "campaign_id": 1,
                    "name": "Existing",
                    "offer_retargeting": "NO",
                    "region_ids": [225],
                    "tracking_params": "",
                }
            ],
            "ads": [self.ad],
            "keywords": [],
        }
        self.lists, self.targets, self.writes = [], [], []
        self.fail, self.active_auto = None, False
        self.catalog = [{"GoalID": 4, "Type": "audience_segment", "Login": "client"}]

    async def call_v4(self, method, param):
        assert method == "GetRetargetingGoals" and param == {"Logins": ["client"]}
        return deepcopy(self.catalog)

    async def call_v501(self, service, method, params, *, client_login):
        assert client_login == "client"
        if method == "get":
            values = {
                "retargetinglists": ("RetargetingLists", self.lists),
                "audiencetargets": ("AudienceTargets", self.targets),
                "keywords": (
                    "Keywords",
                    [
                        {"Id": r["id"], "Keyword": r["keyword"], "State": r["state"]}
                        for r in self.inventory["keywords"]
                        if r["ad_group_id"] == 11
                    ],
                ),
            }
            key, data = values[service]
            return {key: deepcopy(data)} if data else {}
        self.writes.append((service, method, deepcopy(params)))
        if service == self.fail:
            return {"AddResults": [{"Errors": [{"Code": 6000}]}]}
        if method == "suspend":
            assert service == "keywords"
            for row in self.inventory["keywords"]:
                if row["id"] in params["SelectionCriteria"]["Ids"]:
                    row["state"] = "SUSPENDED"
            return {"SuspendResults": [{"Id": i} for i in params["SelectionCriteria"]["Ids"]]}
        assert method == "add"
        if service == "retargetinglists":
            row = deepcopy(params["RetargetingLists"][0])
            self.lists.append(
                {"Id": 10, "IsAvailable": "YES", "Scope": "FOR_TARGETS_AND_ADJUSTMENTS", **row}
            )
            identifier = 10
        elif service == "adgroups":
            row = params["AdGroups"][0]
            self.inventory["groups"].append(
                {
                    "id": 11,
                    "campaign_id": 1,
                    "name": row["Name"],
                    "region_ids": row["RegionIds"],
                    "offer_retargeting": "NO",
                    "tracking_params": "",
                }
            )
            self.inventory["keywords"].append(
                {
                    "id": 15,
                    "campaign_id": 1,
                    "ad_group_id": 11,
                    "keyword": "---autotargeting",
                    "autotargeting": True,
                    "state": "ON" if self.active_auto else "SUSPENDED",
                }
            )
            identifier = 11
        elif service == "audiencetargets":
            self.targets.append({"Id": 12, "State": "ON", **params["AudienceTargets"][0]})
            identifier = 12
        elif service == "ads":
            row = deepcopy(self.ad)
            row.update(id=13, ad_group_id=11, status="DRAFT", state="OFF")
            self.inventory["ads"].append(row)
            identifier = 13
        else:
            raise AssertionError("Unexpected write")
        return {"AddResults": [{"Id": identifier}]}


@pytest.fixture
def api(monkeypatch):
    value = Api()

    async def settings(*args, **kwargs):
        return {"campaigns": [deepcopy(value.campaign)]}

    async def inventory(*args, **kwargs):
        return deepcopy(value.inventory)

    async def ad_read(*args, **kwargs):
        return {"ads": deepcopy([a for a in value.inventory["ads"] if a["id"] in kwargs["ad_ids"]])}

    async def sets(*args, **kwargs):
        return {5: {"Sitelinks": [{"Href": "https://example.org/"}] * 4}}

    async def refs(*args, **kwargs):
        return {"status": "PASS"}

    async def pages(*args, **kwargs):
        return [{"site_check": {"ok": True}, "device": "desktop"}]

    monkeypatch.setattr(mod.campaigns, "read_settings", settings)
    monkeypatch.setattr(mod.group_append, "inventory", inventory)
    monkeypatch.setattr(mod.ads, "read", ad_read)
    monkeypatch.setattr(mod.assets, "read_sets", sets)
    monkeypatch.setattr(mod.preflight_refs, "assets", refs)
    monkeypatch.setattr(mod.link_checks, "check_live", pages)
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("active", [False, True])
async def test_exact_group_clone_and_campaign_preserved(api, reuse, active):
    api.active_auto = active
    if reuse:
        api.lists = [
            {
                "Id": 10,
                "Name": "Existing list",
                "Type": "RETARGETING",
                "IsAvailable": "YES",
                "Scope": "FOR_TARGETS_AND_ADJUSTMENTS",
                "Rules": [
                    {"Operator": "ANY", "Arguments": [{"ExternalId": 4, "MembershipLifeSpan": 540}]}
                ],
            }
        ]
    p = plan()
    checked = await repair.preflight(api, p)
    result = await repair.apply(api, p, expected_preflight=checked)
    assert result["status"] == "complete" and result["activated"] is False
    writes_before = deepcopy(api.writes)
    actual = await repair.readback(
        verification.ReadOnlyAPI(api), p, before=checked["before"], added=result["added"])
    assert actual["verified"] and actual["campaign_state"] == "ON"
    assert api.writes == writes_before
    assert all(method not in {"resume", "moderate"} for _, method, _ in api.writes)
    assert all(service != "campaigns" for service, _, _ in api.writes)
    assert sum(s == "retargetinglists" for s, _, _ in api.writes) == (0 if reuse else 1)
    with pytest.raises(ValueError, match="Duplicate"):
        await repair.apply(api, p, expected_preflight=checked)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["foreign", "source", "search", "budget", "goal", "name", "url"])
async def test_changed_or_invalid_snapshot_blocks_before_any_write(api, bad):
    p = plan()
    checked = await repair.preflight(api, p)
    if bad == "foreign":
        api.inventory["groups"][0]["campaign_id"] = 20
    elif bad == "source":
        api.inventory["ads"][0]["id"] = 99
    elif bad == "search":
        api.campaign["bidding_strategy"]["Search"]["BiddingStrategyType"] = "WB_MAXIMUM_CLICKS"
    elif bad == "budget":
        api.campaign["bidding_strategy"]["Network"]["weekly_budget"] = 9000
    elif bad == "goal":
        api.catalog[0]["Type"] = "goal"
    elif bad == "name":
        api.inventory["groups"][0]["name"] = "Contact audience"
    else:
        api.ad["href"] = "https://elsewhere.example/"
    with pytest.raises(ValueError):
        await repair.apply(api, p, expected_preflight=checked)
    assert not api.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["retargetinglists", "adgroups", "audiencetargets", "ads"])
async def test_partial_write_never_retried_and_ids_preserved(api, stage):
    p = plan()
    api.fail = stage
    result = await repair.apply(api, p, expected_preflight=await repair.preflight(api, p))
    assert result["status"] == "partial"
    assert api.writes[-1][0] == stage
    assert sum(s == stage for s, _, _ in api.writes) == 1
    if stage != "retargetinglists":
        assert result["added"]["list_id"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["running_ad", "moderated", "target", "existing", "active_auto"])
async def test_readback_rejects_bad_new_or_changed_existing_state(api, bad):
    p = plan()
    checked = await repair.preflight(api, p)
    result = await repair.apply(api, p, expected_preflight=checked)
    if bad == "running_ad":
        api.inventory["ads"][-1]["state"] = "ON"
    elif bad == "moderated":
        api.inventory["ads"][-1]["status"] = "MODERATION"
    elif bad == "target":
        api.targets[0]["RetargetingListId"] = 99
    elif bad == "existing":
        api.ad["texts"][0] = "Changed existing"
    else:
        api.inventory["keywords"][-1]["state"] = "ON"
    assert not (await repair.readback(api, p, before=checked["before"], added=result["added"]))[
        "verified"
    ]


def test_mixed_actions_and_id_hash_binding():
    p = plan()
    with pytest.raises(ValueError):
        repair.normalize({"audience_group": p["audience_group"], "campaigns": []}, "client")
    other = {**p["audience_group"], "segment_id": 99}
    assert mod.normalize(other, "client")["plan_hash"] != p["plan_hash"]
