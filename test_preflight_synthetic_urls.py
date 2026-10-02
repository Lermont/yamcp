from yadirect_mcp import link_checks


def sample(synthetic):
    campaigns = [{"id": 21, "name": "Test", "synthetic_object_ids": synthetic,
                  "tracking_params": "utm_campaign={campaign_id}&ad={ad_id}&group={gbid}"}]
    groups = [{"id": i, "campaign_id": 21, "region_ids": [213]} for i in (10, 20)]
    ads = [{"id": i * 10, "campaign_id": 21, "ad_group_id": i,
            "href": "https://example.test/"} for i in (10, 20)]
    return link_checks.inventory(campaigns, groups, ads, {})


def test_synthetic_ids_share_http_evidence_and_keep_all_object_references():
    pages = sample(True)
    assert len(pages) == 2  # desktop and mobile are still independent
    assert all(row["ad_groups"] == [10, 20] and row["ad_ids"] == [100, 200]
               for row in pages)
    assert all("ad=1&group=1" in row["url"] for row in pages)


def test_live_ids_are_never_collapsed():
    pages = sample(False)
    assert len(pages) == 4
    assert any("ad=100&group=10" in row["url"] for row in pages)
    assert any("ad=200&group=20" in row["url"] for row in pages)
