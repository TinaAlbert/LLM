"""通过实际本地 HTTP 服务验收；不打印令牌，不重置任何数据库。"""

import argparse
import json
from pathlib import Path
from uuid import uuid4

import httpx

RESULT = Path(__file__).resolve().parents[3] / ".local" / "http-smoke-result.json"


def login(client, account):
    response = client.post("/demo/sessions", json={"account": account})
    assert response.status_code == 201, f"登录失败，HTTP {response.status_code}"
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-result", action="store_true", help="重启后重读上次保存的合成草稿")
    args = parser.parse_args()
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=10) as client:
        alice = login(client, "personal_alice")
        bob = login(client, "personal_bob")
        try:
            if args.read_result:
                expected = json.loads(RESULT.read_text(encoding="utf-8"))
            else:
                key = uuid4().hex
                body = {"flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-01", "passengers": 1}
                response = client.post("/trips", json=body, headers={**alice, "Idempotency-Key": key})
                assert response.status_code == 201
                trip = response.json()
                assert trip["price"] is None and trip["revision"] == 1
                repeated = client.post("/trips", json=body, headers={**alice, "Idempotency-Key": key})
                assert repeated.status_code == 201 and repeated.json() == trip
                modified = client.patch(
                    f"/trips/{trip['id']}", json={"expected_revision": 1, "passengers": 2},
                    headers={**alice, "Idempotency-Key": key + "-edit"},
                )
                assert modified.status_code == 200
                expected = {**trip, "passengers": 2, "revision": 2}
                assert modified.json() == expected
                stale = client.patch(
                    f"/trips/{trip['id']}", json={"expected_revision": 1, "passengers": 3},
                    headers={**alice, "Idempotency-Key": key + "-stale"},
                )
                assert stale.status_code == 409 and stale.json() == {"error": "state_changed"}
                RESULT.parent.mkdir(exist_ok=True)
                RESULT.write_text(json.dumps(expected, indent=2), encoding="utf-8")
            path = f"/trips/{expected['id']}"
            actual = client.get(path, headers=alice)
            assert actual.status_code == 200
            # 个人资料可在两次烟测之间绑定；草稿无报价，价格仍未知。
            stable = {key: value for key, value in expected.items() if key != "price_status"}
            assert {key: value for key, value in actual.json().items() if key != "price_status"} == stable
            assert actual.json()["price_status"] in {"requires_payment_profile", "requires_quote"}
            denied = client.get(path, headers={**bob, "X-Role": "admin"})
            assert denied.status_code == 404 and denied.json() == {"error": "not_visible"}
            print(json.dumps({"http_smoke": "passed", "restarted_read": args.read_result,
                              "revision": expected["revision"], "price": expected["price"]}))
        finally:
            for headers in (alice, bob):
                assert client.delete("/session", headers=headers).status_code == 204


if __name__ == "__main__":
    main()
