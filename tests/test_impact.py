"""静态依赖影响分析：引擎单测 + HTTP 接口测试。"""

from app.engine import compile_ruleset, dependency_impact


def _chain_payload():
    """base <- mid <- top，side 依赖 mid/top，top 与 peer 同冲突组。"""
    return {
        "name": "impact-chain",
        "rules": [
            {"code": "base", "condition": 'doc["x"] >= 10'},
            {"code": "mid", "condition": "base.hit", "depends_on": ["base"]},
            {
                "code": "top",
                "condition": "mid.hit",
                "depends_on": ["mid"],
                "conflict_group": "promo",
            },
            {
                "code": "side",
                "condition": "mid.hit and top.hit",
                "depends_on": ["mid", "top"],
            },
            {"code": "peer", "condition": "true", "conflict_group": "promo"},
            {"code": "alone", "condition": "false"},
        ],
    }


def _create_chain(client):
    resp = client.post("/api/rulesets", json=_chain_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()["ruleset_id"]


# ---------- 引擎层 ----------


def test_engine_impact_levels_via_and_coupling():
    compiled = compile_ruleset(
        "t", "", "collect_all",
        [
            {"code": "base", "condition": "", "depends_on": []},
            {"code": "mid", "condition": "base.hit", "depends_on": ["base"]},
            {
                "code": "top",
                "condition": "mid.hit",
                "depends_on": ["mid"],
                "conflict_group": "promo",
            },
            {
                "code": "side",
                "condition": "mid.hit and top.hit",
                "depends_on": ["mid", "top"],
            },
            {"code": "peer", "condition": "", "depends_on": [], "conflict_group": "promo"},
        ],
    )
    report = dependency_impact(compiled, ["base"])

    assert [r["rule_code"] for r in report["input_rules"]] == ["base"]
    assert report["input_rules"][0]["level"] == 0
    assert report["input_rules"][0]["coupled"] is False

    # 直接依赖者 level=1；传递依赖者按 evaluation_order 排列
    assert [r["rule_code"] for r in report["direct_dependents"]] == ["mid"]
    assert report["direct_dependents"][0]["via"] == ["base"]
    assert [r["rule_code"] for r in report["transitive_dependents"]] == ["top", "side"]
    assert all(r["level"] == 2 for r in report["transitive_dependents"])

    # via 只记录最短路径来源：side 经 mid（level1）到达，不经同层的 top
    side = next(r for r in report["transitive_dependents"] if r["rule_code"] == "side")
    assert side["via"] == ["mid"]

    # top 与 peer 同冲突组：peer 成为联动项，top 标记 coupled
    top = next(r for r in report["transitive_dependents"] if r["rule_code"] == "top")
    assert top["conflict_group"] == "promo"
    assert top["coupled"] is True
    assert report["coupled_rules"] == [
        {"rule_code": "peer", "conflict_group": "promo", "coupled_with": ["top"]}
    ]


def test_engine_impact_multiple_inputs_dedup_and_shortest_level():
    compiled = compile_ruleset(
        "t", "", "collect_all",
        [
            {"code": "a", "condition": "", "depends_on": []},
            {"code": "b", "condition": "a.hit", "depends_on": ["a"]},
            {"code": "c", "condition": "b.hit and a.hit", "depends_on": ["a", "b"]},
        ],
    )
    # 重复输入去重；c 距 a 的最短距离为 1（直接依赖 a），不是 2
    report = dependency_impact(compiled, ["a", "b", "a"])
    assert [r["rule_code"] for r in report["input_rules"]] == ["a", "b"]
    assert report["direct_dependents"] == [
        {
            "rule_code": "c",
            "level": 1,
            "via": ["a", "b"],
            "conflict_group": None,
            "coupled": False,
        }
    ]
    assert report["transitive_dependents"] == []
    assert report["coupled_rules"] == []


def test_engine_impact_leaf_rule_has_empty_closure():
    compiled = compile_ruleset(
        "t", "", "collect_all",
        [
            {"code": "a", "condition": "", "depends_on": []},
            {"code": "b", "condition": "a.hit", "depends_on": ["a"]},
        ],
    )
    report = dependency_impact(compiled, ["b"])
    assert report["direct_dependents"] == []
    assert report["transitive_dependents"] == []
    assert report["coupled_rules"] == []


# ---------- HTTP 接口 ----------


def test_impact_api_basic(client):
    rid = _create_chain(client)
    resp = client.post(f"/api/rulesets/{rid}/impact", json={"rule_codes": ["base"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ruleset_id"] == rid
    assert body["version"] == 1
    assert body["evaluation_order"] == ["base", "mid", "top", "side", "peer", "alone"]

    assert [r["rule_code"] for r in body["input_rules"]] == ["base"]
    assert [r["rule_code"] for r in body["direct_dependents"]] == ["mid"]
    assert [r["rule_code"] for r in body["transitive_dependents"]] == ["top", "side"]
    assert body["coupled_rules"] == [
        {"rule_code": "peer", "conflict_group": "promo", "coupled_with": ["top"]}
    ]
    # 无依赖、无冲突组的规则不受影响
    assert "alone" not in {
        r["rule_code"]
        for r in body["direct_dependents"] + body["transitive_dependents"]
    }


def test_impact_api_does_not_write_evaluations(client):
    rid = _create_chain(client)
    resp = client.post(f"/api/rulesets/{rid}/impact", json={"rule_codes": ["base"]})
    assert resp.status_code == 200
    # 纯静态分析：不产生任何决策记录
    assert client.get(f"/api/rulesets/{rid}/evaluations").json() == []


def test_impact_api_multiple_inputs(client):
    rid = _create_chain(client)
    resp = client.post(
        f"/api/rulesets/{rid}/impact", json={"rule_codes": ["mid", "peer"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [r["rule_code"] for r in body["input_rules"]] == ["mid", "peer"]
    assert [r["rule_code"] for r in body["direct_dependents"]] == ["top", "side"]
    assert body["transitive_dependents"] == []
    # peer 是输入规则、top 在闭包内：promo 组两个成员都在受影响集合中，无联动项
    assert body["coupled_rules"] == []
    peer = next(r for r in body["input_rules"] if r["rule_code"] == "peer")
    assert peer["coupled"] is True


def test_impact_api_version_pinning(client):
    rid = _create_chain(client)
    # v2：side 改为只依赖 top（不再直接依赖 mid）
    updated = _chain_payload()
    updated["rules"][3] = {
        "code": "side",
        "condition": "top.hit",
        "depends_on": ["top"],
    }
    resp = client.put(f"/api/rulesets/{rid}", json=updated)
    assert resp.status_code == 200

    # 缺省用最新版 v2：mid 的影响不再直接波及 side
    body = client.post(
        f"/api/rulesets/{rid}/impact", json={"rule_codes": ["mid"]}
    ).json()
    assert body["version"] == 2
    assert [r["rule_code"] for r in body["direct_dependents"]] == ["top"]
    assert [r["rule_code"] for r in body["transitive_dependents"]] == ["side"]
    assert body["transitive_dependents"][0]["level"] == 2

    # 显式指定 v1：side 仍是 mid 的直接依赖者
    body = client.post(
        f"/api/rulesets/{rid}/impact",
        json={"version": 1, "rule_codes": ["mid"]},
    ).json()
    assert body["version"] == 1
    assert [r["rule_code"] for r in body["direct_dependents"]] == ["top", "side"]


def test_impact_api_demo_ruleset(client, demo_id):
    resp = client.post(f"/api/rulesets/{demo_id}/impact", json={"rule_codes": ["is_vip"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["rule_code"] for r in body["direct_dependents"]] == ["vip_discount"]
    assert [r["rule_code"] for r in body["transitive_dependents"]] == ["manual_review"]
    # vip_discount 与 large_order 同属 discount 组 -> large_order 联动
    assert body["coupled_rules"] == [
        {
            "rule_code": "large_order",
            "conflict_group": "discount",
            "coupled_with": ["vip_discount"],
        }
    ]


def test_impact_api_unknown_rule_codes_422(client):
    rid = _create_chain(client)
    resp = client.post(
        f"/api/rulesets/{rid}/impact", json={"rule_codes": ["base", "nope"]}
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "UNKNOWN_RULE_CODES"
    assert error["details"]["unknown_rule_codes"] == ["nope"]


def test_impact_api_empty_rule_codes_422(client):
    rid = _create_chain(client)
    resp = client.post(f"/api/rulesets/{rid}/impact", json={"rule_codes": []})
    assert resp.status_code == 422


def test_impact_api_not_found(client):
    resp = client.post("/api/rulesets/nope/impact", json={"rule_codes": ["a"]})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_impact_api_version_not_found(client):
    rid = _create_chain(client)
    resp = client.post(
        f"/api/rulesets/{rid}/impact", json={"version": 99, "rule_codes": ["base"]}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERSION_NOT_FOUND"
