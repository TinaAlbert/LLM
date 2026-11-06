"""真实 loopback HTTP 个人路径及独立 SQL 核验；仅开发库，不重置或输出 token。"""

import argparse
import json
from pathlib import Path
from uuid import uuid4

import httpx

from services.business.config import load_settings
from services.business.db import connect
from services.business.manage import check_marker, database_purpose

RESULT = Path(__file__).resolve().parents[3] / ".local" / "personal-http-result.json"


def login(client, account):
    response = client.post("/demo/sessions", json={"account": account})
    assert response.status_code == 201, f"登录失败，HTTP {response.status_code}"
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "verify"])
    args = parser.parse_args()
    settings = load_settings()
    with connect(settings.database_url) as conn:
        assert database_purpose(conn) == "demo" and settings.workspace_id == "demo"
        check_marker(conn, "demo")
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=10) as client:
        alice, bob = login(client, "personal_alice"), login(client, "personal_bob")
        try:
            if args.action == "start":
                if RESULT.exists():
                    raise ValueError("已有个人路径记录；按记录推进/verify，不重复消费开发库库存")
                run_id = uuid4().hex

                def write(path, body, suffix, method="post"):
                    response = getattr(client, method)(path, json=body,
                        headers={**alice, "Idempotency-Key": run_id + suffix})
                    assert response.status_code in (200, 201, 202), response.text
                    return response.json()

                profile = client.get("/payment-profile", headers=alice).json()
                if profile["id"] is None:
                    write("/payment-profile", {"source_ref": "demo_personal_alice", "expected_revision": 0}, "bind", "put")
                trip = write("/trips", {"flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-01", "passengers": 1}, "trip")
                quote = write(f"/trips/{trip['id']}/quotes", {"expected_revision": 1}, "quote")
                assert quote["amount_minor"] == 110000 and quote["currency"] == "DEMO"
                lock = write(f"/quotes/{quote['id']}/lock", {}, "lock")
                payment = write(f"/locks/{lock['id']}/payments", {"amount_minor": 110000}, "pay")
                assert payment["status"] == "processing" and payment["ticket_task_id"] is None
                result = {"trip_id": trip["id"], "payment_id": payment["id"], "revision": 1}
                RESULT.write_text(json.dumps(result, indent=2), encoding="utf-8")
                print(json.dumps({"http_acceptance": "passed", **result}))
            else:
                result = json.loads(RESULT.read_text(encoding="utf-8"))
                response = client.get(f"/payments/{result['payment_id']}", headers=alice)
                assert response.status_code == 200
                payment = response.json()
                assert payment["status"] == "succeeded" and payment["amount_minor"] == 110000
                task = client.get(f"/ticket-tasks/{payment['ticket_task_id']}", headers=alice).json()
                assert task["status"] == "succeeded"
                ticket = client.get(f"/tickets/{task['ticket_id']}", headers=alice).json()
                assert ticket["status"] == "valid" and ticket["trip_revision"] == result["revision"]
                denied = client.get(f"/tickets/{ticket['id']}", headers={**bob, "X-Role": "admin"})
                assert denied.status_code == 404 and denied.json() == {"error": "not_visible"}
                with connect(settings.database_url) as conn:
                    row = conn.execute("""SELECT t.revision,p.status,p.amount_minor,
                        l.trip_revision,l.status AS lock_status,l.quantity,k.certificate,
                        (SELECT count(*) FROM payments WHERE trip_id=t.id AND status='succeeded') AS payments,
                        (SELECT count(*) FROM ticket_tasks WHERE payment_id=p.id) AS tasks,
                        (SELECT count(*) FROM tickets WHERE payment_id=p.id) AS tickets,
                        (SELECT sum(captured_minor) FROM provider_payments WHERE operation_id=p.id) AS captured
                        FROM trips t JOIN payments p ON p.trip_id=t.id AND p.workspace_id=t.workspace_id
                        JOIN booking_locks l ON l.id=p.lock_id AND l.workspace_id=p.workspace_id
                        JOIN tickets k ON k.payment_id=p.id AND k.workspace_id=p.workspace_id
                        WHERE t.workspace_id='demo' AND t.id=%s AND p.id=%s""",
                        (result["trip_id"], result["payment_id"])).fetchone()
                    assert row is not None and row["certificate"]
                    assert row["revision"] == row["trip_revision"] == 1
                    assert row["status"] == "succeeded" and row["lock_status"] == "consumed"
                    assert row["payments"] == row["tasks"] == row["tickets"] == row["quantity"] == 1
                    assert row["amount_minor"] == row["captured"] == 110000
                    bad = conn.execute("""SELECT f.id FROM flights f WHERE f.workspace_id='demo' AND
                        (f.reserved <> (SELECT coalesce(sum(quantity),0) FROM booking_locks l
                         WHERE (l.workspace_id,l.flight_id,l.travel_date)=(f.workspace_id,f.id,f.travel_date)
                         AND l.status IN ('held','protected')) OR
                         f.sold <> (SELECT coalesce(sum(quantity),0) FROM booking_locks l
                         WHERE (l.workspace_id,l.flight_id,l.travel_date)=(f.workspace_id,f.id,f.travel_date)
                         AND l.status='consumed') OR f.reserved+f.sold>f.capacity)""").fetchall()
                    assert not bad
                print(json.dumps({"http_and_independent_pg": "passed", "valid_tickets": 1,
                                  "successful_payments": 1, "amount_minor": 110000, "revision": 1}))
        finally:
            for headers in (alice, bob):
                assert client.delete("/session", headers=headers).status_code == 204


if __name__ == "__main__":
    main()
