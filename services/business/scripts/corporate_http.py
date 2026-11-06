"""真实 loopback HTTP 企业 V1 路径及独立 SQL 核验；仅开发库，不重置或输出凭证。"""

import argparse
import json
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

from services.business.config import load_settings
from services.business.db import connect, read_snapshot
from services.business.manage import check_marker, database_purpose


RESULT = Path(__file__).resolve().parents[3] / ".local" / "corporate-http-result.json"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def request_json(client, method, path, headers, *, body=None, statuses=(200,)):
    arguments = {"headers": headers}
    if body is not None:
        arguments["json"] = body
    response = client.request(method, path, **arguments)
    # 不转发响应正文：服务故障时也不把内部信息写进终端或报告。
    require(response.status_code in statuses,
            f"HTTP 路径核验失败：{method.upper()} {path}，状态 {response.status_code}")
    return response.json()


def login(client, account):
    body = request_json(client, "post", "/demo/sessions", {},
                        body={"account": account}, statuses=(201,))
    return {"Authorization": "Bearer " + body["access_token"]}


def save_progress(result, *, initial=False):
    # 先持久化非秘密运行标识；中途失败也禁止再次 start 造成重复资源占用。
    with RESULT.open("x" if initial else "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)


def start(client, chen, lin):
    require(not RESULT.exists(), "已有企业路径记录；按记录推进或 verify，不重复消费开发库库存")
    result = {"run_id": uuid4().hex, "stage": "starting", "revision": 1}
    save_progress(result, initial=True)

    def write(path, body, suffix, *, method="post", headers=chen):
        return request_json(client, method, path,
                            {**headers, "Idempotency-Key": result["run_id"] + suffix},
                            body=body, statuses=(200, 201, 202))

    trip = write("/trips", {"flight_id": "DEMO-PEK-SHA", "travel_date": "2026-10-02",
                            "passengers": 1}, "trip")
    require(trip["revision"] == 1 and trip["organization_id"] == "org-A",
            "企业行程的可信组织或修订不符")
    result.update(trip_id=trip["id"], stage="trip_created")
    save_progress(result)
    profile = request_json(client, "get", "/payment-profile", chen)
    if profile["source_ref"] != "demo_corporate_chen":
        write("/payment-profile", {"source_ref": "demo_corporate_chen",
                                   "expected_revision": profile["revision"]}, "bind", method="put")
    quote = write(f"/trips/{trip['id']}/quotes", {"expected_revision": 1}, "quote")
    require(quote["amount_minor"] == 100000 and quote["currency"] == "DEMO",
            "企业报价须为 100000 minor DEMO")
    lock = write(f"/quotes/{quote['id']}/lock", {}, "lock")
    result.update(lock_id=lock["id"], stage="locked")
    save_progress(result)
    write(f"/trips/{trip['id']}/budget", {"project_id": "A-eng", "expected_revision": 1,
                                        "expected_selection_revision": 0}, "budget", method="put")
    projects = request_json(client, "get", "/budget-projects", chen)["projects"]
    selected = next((row for row in projects if row["id"] == "A-eng"), None)
    require(selected is not None and selected["reserved_minor"] == 0
            and selected["used_minor"] == 0 and selected["policy_revision"] == 1,
            "A-eng 初选项目不应占预算，且本核验需要未消费的 single-trip 预算夹具")
    approval = write(f"/locks/{lock['id']}/approvals", {"expected_revision": 1}, "submit")
    require(approval["status"] == "pending", "提交未产生待审批申请")
    result.update(approval_id=approval["id"], stage="submitted")
    save_progress(result)
    approved = write(f"/approvals/{approval['id']}/decision",
                     {"expected_revision": 1, "decision": "approved"}, "approve", headers=lin)
    require(approved["status"] == "approved", "指定审批人未形成有效批准")
    payment = write(f"/locks/{lock['id']}/payments", {"amount_minor": 100000}, "pay")
    require(payment["status"] == "processing" and payment["ticket_task_id"] is None,
            "付款应已受理但尚未产生出票任务")
    result.update(payment_id=payment["id"], stage="payment_accepted")
    save_progress(result)
    print(json.dumps({"http_acceptance": "passed", **result}, ensure_ascii=False))


def verify(client, chen, lin, ding, settings):
    result = json.loads(RESULT.read_text(encoding="utf-8"))
    require(result.get("stage") == "payment_accepted",
            "企业运行尚未受理付款；检查已保存的阶段与对象，用原操作标识恢复，勿重复 start")
    payment = request_json(client, "get", f"/payments/{result['payment_id']}", chen)
    require(payment["status"] == "succeeded" and payment["amount_minor"] == 100000,
            "企业付款尚未成功或金额不符")
    task = request_json(client, "get", f"/ticket-tasks/{payment['ticket_task_id']}", chen)
    require(task["status"] == "succeeded", "出票任务尚未成功")
    ticket = request_json(client, "get", f"/tickets/{task['ticket_id']}", chen)
    require(ticket["status"] == "valid" and ticket["trip_revision"] == result["revision"],
            "票据无效或行程修订不符")
    approval = request_json(client, "get", f"/approvals/{result['approval_id']}", lin)
    require(approval["status"] == "approved" and approval["trip_revision"] == result["revision"]
            and approval["lock_id"] == result["lock_id"] and approval["valid"],
            "批准未绑定同一行程修订与锁")
    projects = request_json(client, "get", "/budget-projects", chen)["projects"]
    selected = next((row for row in projects if row["id"] == "A-eng"), None)
    require(selected is not None and selected["used_minor"] == 100000
            and selected["reserved_minor"] == 0 and selected["available_minor"] == 100000
            and selected["policy_revision"] == 1, "HTTP 预算终态不符")
    for headers in (lin, ding):
        denied = request_json(client, "get", f"/tickets/{ticket['id']}",
                              {**headers, "X-Role": "admin"}, statuses=(404,))
        require(denied == {"error": "not_visible"}, "越权票据读取未采用一致不可见响应")
    verify_sql(settings, result)
    print(json.dumps({"http_and_independent_pg": "passed", "valid_tickets": 1,
                      "successful_payments": 1, "amount_minor": 100000, "revision": 1,
                      "budget_used_minor": 100000, "budget_reserved_minor": 0,
                      "budget_available_minor": 100000}, ensure_ascii=False))


def verify_sql(settings, result):
    with connect(settings.database_url) as conn:
        read_snapshot(conn)
        row = conn.execute("""SELECT t.revision,t.status AS trip_status,t.owner_id,
            t.organization_id,t.department_id,p.status AS payment_status,p.amount_minor,
            p.approval_id AS payment_approval_id,p.currency,l.id AS lock_id,l.trip_revision,l.status AS lock_status,
            l.quantity,l.payment_kind,a.id AS approval_id,a.status AS approval_status,
            a.resource_status,a.trip_revision AS approval_trip_revision,a.lock_id AS approval_lock_id,
            a.applicant_id,a.approver_id,a.project_id,a.policy_revision,a.budget_total_minor,
            a.amount_minor AS approval_amount_minor,k.trip_revision AS ticket_revision,k.certificate,
            (SELECT count(*) FROM payments x WHERE x.workspace_id=t.workspace_id AND x.trip_id=t.id) AS payments,
            (SELECT count(*) FROM payments x WHERE x.workspace_id=t.workspace_id AND x.trip_id=t.id
                AND x.status='succeeded') AS successful_payments,
            (SELECT count(*) FROM payment_jobs x WHERE x.workspace_id=p.workspace_id AND x.payment_id=p.id) AS jobs,
            (SELECT count(*) FROM ticket_tasks x WHERE x.workspace_id=p.workspace_id AND x.payment_id=p.id) AS tasks,
            (SELECT count(*) FROM tickets x WHERE x.workspace_id=p.workspace_id AND x.payment_id=p.id) AS tickets,
            (SELECT count(*) FROM provider_payments x WHERE x.workspace_id=p.workspace_id AND x.operation_id=p.id) AS provider_operations,
            (SELECT sum(captured_minor) FROM provider_payments x WHERE x.workspace_id=p.workspace_id
                AND x.operation_id=p.id AND x.outcome='succeeded') AS captured
            FROM trips t JOIN payments p ON (p.workspace_id,p.trip_id)=(t.workspace_id,t.id)
            JOIN booking_locks l ON (l.workspace_id,l.id)=(p.workspace_id,p.lock_id)
            JOIN approval_requests a ON (a.workspace_id,a.id)=(p.workspace_id,p.approval_id)
            JOIN tickets k ON (k.workspace_id,k.payment_id)=(p.workspace_id,p.id)
            WHERE t.workspace_id='demo' AND t.id=%s AND p.id=%s""",
            (result["trip_id"], result["payment_id"])).fetchone()
        require(row is not None and bool(row["certificate"]), "独立 SQL 未找到有效企业票据")
        require(row["revision"] == row["trip_revision"] == row["approval_trip_revision"]
                == row["ticket_revision"] == result["revision"] == 1,
                "独立 SQL 行程、锁、批准和票据修订不一致")
        require(str(row["lock_id"]) == str(row["approval_lock_id"]) == result["lock_id"]
                and str(row["payment_approval_id"]) == str(row["approval_id"]) == result["approval_id"],
                "独立 SQL 付款未冻结对应锁与批准")
        require(row["trip_status"] == "paid" and row["payment_status"] == "succeeded"
                and row["lock_status"] == "consumed" and row["approval_status"] == "approved"
                and row["resource_status"] == "consumed", "独立 SQL 企业对象未达到预期终态")
        require(row["owner_id"] == row["applicant_id"] == "employee_chen"
                and row["approver_id"] == "approver_lin" and row["organization_id"] == "org-A"
                and row["department_id"] == "eng" and row["project_id"] == "A-eng"
                and row["payment_kind"] == "corporate", "独立 SQL 企业身份或付款类别不符")
        require(row["payments"] == row["successful_payments"] == row["jobs"] == row["tasks"]
                == row["tickets"] == row["quantity"] == row["provider_operations"] == 1,
                "独立 SQL 付款、供应商操作、任务或票据不唯一")
        require(row["amount_minor"] == row["approval_amount_minor"] == row["captured"] == 100000
                and row["currency"] == "DEMO" and row["policy_revision"] == 1
                and row["budget_total_minor"] == 200000, "独立 SQL 金额或预算快照不符")
        budget = conn.execute("""SELECT total_minor,used_minor,reserved_minor,
            total_minor-used_minor-reserved_minor AS available_minor,policy_revision
            FROM budget_projects WHERE workspace_id='demo' AND id='A-eng'""").fetchone()
        require(budget == {"total_minor": 200000, "used_minor": 100000, "reserved_minor": 0,
                           "available_minor": 100000, "policy_revision": 1},
                "独立 SQL 预算已用、占用或可用额度不符")
        bad_budgets = conn.execute("""SELECT p.id FROM budget_projects p
            WHERE p.workspace_id='demo' AND (
                p.reserved_minor <> (SELECT coalesce(sum(a.amount_minor),0) FROM approval_requests a
                    WHERE (a.workspace_id,a.project_id)=(p.workspace_id,p.id)
                    AND a.resource_status IN ('held','protected')) OR
                p.used_minor <> (SELECT coalesce(sum(a.amount_minor),0) FROM approval_requests a
                    WHERE (a.workspace_id,a.project_id)=(p.workspace_id,p.id)
                    AND a.resource_status='consumed') OR
                p.reserved_minor<0 OR p.used_minor<0 OR p.reserved_minor+p.used_minor>p.total_minor)
            """).fetchall()
        require(not bad_budgets, "独立 SQL 预算账目与占用状态不守恒")
        bad_inventory = conn.execute("""SELECT f.id FROM flights f WHERE f.workspace_id='demo' AND
            (f.reserved <> (SELECT coalesce(sum(quantity),0) FROM booking_locks l
                WHERE (l.workspace_id,l.flight_id,l.travel_date)=(f.workspace_id,f.id,f.travel_date)
                AND l.status IN ('held','protected')) OR
             f.sold <> (SELECT coalesce(sum(quantity),0) FROM booking_locks l
                WHERE (l.workspace_id,l.flight_id,l.travel_date)=(f.workspace_id,f.id,f.travel_date)
                AND l.status='consumed') OR
             f.reserved<0 OR f.sold<0 OR f.reserved+f.sold>f.capacity)""").fetchall()
        require(not bad_inventory, "独立 SQL 全局库存不守恒")


def run(action):
    if action == "start":
        require(not RESULT.exists(), "已有企业路径记录；按记录推进或 verify，不重复消费开发库库存")
    settings = load_settings()
    with connect(settings.database_url) as conn:
        require(database_purpose(conn) == "demo" and settings.workspace_id == "demo",
                "此核验脚本仅允许项目开发库和 demo 工作空间")
        check_marker(conn, "demo")
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=10) as client:
        sessions = []
        try:
            chen = login(client, "employee_chen")
            sessions.append(chen)
            lin = login(client, "approver_lin")
            sessions.append(lin)
            if action == "start":
                start(client, chen, lin)
            else:
                ding = login(client, "employee_ding")
                sessions.append(ding)
                verify(client, chen, lin, ding, settings)
        finally:
            for headers in sessions:
                response = client.delete("/session", headers=headers)
                require(response.status_code == 204, "退出合成账户会话失败")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "verify"])
    args = parser.parse_args()
    try:
        run(args.action)
    except psycopg.Error:
        parser.exit(1, "PostgreSQL 核验失败；检查项目配置、实例状态与迁移。\n")
    except httpx.HTTPError:
        parser.exit(1, "Loopback HTTP 调用失败；检查 8000 端口的本地 API。\n")
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        parser.exit(1, "核验记录或响应结构不可用；检查 .local 企业运行记录及接口版本。\n")
    except ValueError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
