#!/usr/bin/env python3
"""打已部署的服務，逐一驗證每個對外端點。

分成三段，因為**中間需要真的用瀏覽器在綠界付一筆款** ——
綠界是導轉模型，不實際走一次付款頁就沒有回呼，沒有回呼就等於
訂單狀態機、驗簽、去重全部沒被驗過。純 API 的煙霧測試在這裡是自欺。

  create  建立測試資料並印出 checkout_url（拿去瀏覽器付款）
  verify  付款後回來確認狀態、事件、訂閱
  checks  不需要付款的分支：驗證錯誤、冪等、權限、退款的各種拒絕

用法：
  BASE=https://... KEY=... python3 scripts/stage-smoke.py checks
  BASE=... KEY=... python3 scripts/stage-smoke.py create
  BASE=... KEY=... python3 scripts/stage-smoke.py verify <state.json>
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ["BASE"].rstrip("/")
KEY = os.environ["KEY"]

_passed, _failed = [], []


def call(method, path, body=None, key=KEY):
    req = urllib.request.Request(
        f"{BASE}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={k: v for k, v in {
            "Content-Type": "application/json" if body is not None else None,
            "X-API-Key": key}.items() if v})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, {"_raw": raw.decode(errors="replace")[:300]}


def check(name, cond, detail=""):
    (_passed if cond else _failed).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{'  ' + str(detail) if detail and not cond else ''}")


def ref(tag):
    return f"smoke-{tag}-{int(time.time())}"


# ─────────────────────────────────────────────────────────── checks
def run_checks():
    print("\n【健康檢查】")
    s, b = call("GET", "/health")
    check("/health 回 200", s == 200, (s, b))
    check("db 由 DB 自己回答 server_user",
          b.get("db", {}).get("server_user", "").startswith("run-runtime@"), b.get("db"))
    check("db.database 正確", b.get("db", {}).get("database") == "payment_ecpay")
    check("憑證已載入", b.get("ecpay", {}).get("credentials") == "loaded")
    check("健康檢查不外洩金鑰",
          "pwFHCqoQZGmho4w6" not in json.dumps(b) and "EkRm7iFT261dpevs" not in json.dumps(b))
    print(f"     env={b.get('env')} merchant={b.get('ecpay', {}).get('merchant_id')} "
          f"refund_api={b.get('ecpay', {}).get('refund_api')}")

    print("\n【認證】")
    s, b = call("GET", "/v1/orders", key=None)
    check("沒帶 key 回 401", s == 401, s)
    s, b = call("GET", "/v1/orders", key="totally-wrong")
    check("錯的 key 回 401", s == 401, s)
    check("401 不區分原因", b.get("detail") == "invalid api key", b)

    print("\n【金額驗證】")
    for amount, why in [(0, "零"), (-5, "負數")]:
        s, b = call("POST", "/v1/orders", {
            "reference_id": ref("bad"), "amount": amount, "item_name": "x"})
        check(f"拒絕{why}金額", s == 400 and b["detail"]["field"] == "amount", (s, b))
    s, b = call("POST", "/v1/orders", {
        "reference_id": ref("bad"), "amount": 10.5, "item_name": "x"})
    check("拒絕小數金額（schema 層）", s == 422, s)

    print("\n【付款方式白名單】")
    s, b = call("POST", "/v1/orders", {
        "reference_id": ref("bad"), "amount": 100, "item_name": "x",
        "choose_payment": "TWQR"})
    check("拒絕未開通的付款方式",
          s == 400 and b["detail"]["field"] == "choose_payment", (s, b))

    print("\n【冪等】")
    r = ref("idem")
    s1, b1 = call("POST", "/v1/orders", {
        "reference_id": r, "amount": 100, "item_name": "冪等測試"})
    s2, b2 = call("POST", "/v1/orders", {
        "reference_id": r, "amount": 100, "item_name": "冪等測試"})
    check("第一次建單 201", s1 == 201, s1)
    check("重複 reference_id 回 200 而非錯誤", s2 == 200, s2)
    check("回的是同一筆", b1.get("id") == b2.get("id"), (b1.get("id"), b2.get("id")))
    check("MerchantTradeNo 只有 20 碼英數",
          len(b1["merchant_trade_no"]) == 20 and b1["merchant_trade_no"].isalnum())
    check("回傳含 checkout_url", "checkout_url" in b1)
    check("回傳含可自行 render 的 form",
          b1.get("form", {}).get("action", "").endswith("/Cashier/AioCheckOut/V5"))
    check("表單已簽章", "CheckMacValue" in b1.get("form", {}).get("fields", {}))
    check("表單的回呼網址指向本服務",
          b1["form"]["fields"]["ReturnURL"].startswith(BASE), b1["form"]["fields"].get("ReturnURL"))

    print("\n【找不到 / 越權】")
    s, _ = call("GET", "/v1/orders/00000000-0000-0000-0000-000000000000")
    check("不存在的訂單回 404（不是 403）", s == 404, s)

    print("\n【退款的拒絕分支】")
    s, b = call("POST", f"/v1/orders/{b1['id']}/refund", {})
    check("未付款訂單不能退款",
          s == 400 and b["detail"]["field"] == "status", (s, b))

    print("\n【定期定額參數驗證】")
    for kw, field in [({"period_type": "W"}, "period_type"),
                      ({"frequency": 13}, "frequency"),
                      ({"exec_times": 1}, "exec_times")]:
        body = {"reference_id": ref("subbad"), "amount": 100,
                "item_name": "x", "period_type": "M", "frequency": 1,
                "exec_times": 12}
        body.update(kw)
        s, b = call("POST", "/v1/subscriptions", body)
        check(f"拒絕不合法的 {field}",
              s == 400 and b["detail"]["field"] == field, (s, b))

    print("\n【事件游標】")
    s, b = call("GET", "/v1/events?after=0&limit=5")
    check("events 可讀", s == 200 and "items" in b, (s, b))
    check("events 回 next_cursor", "next_cursor" in b, b)


# ─────────────────────────────────────────────────────────── create
def run_create():
    state = {}
    print("\n【建立信用卡訂單】")
    s, b = call("POST", "/v1/orders", {
        "reference_id": ref("credit"), "amount": 7,
        "item_name": "端對端測試-信用卡", "choose_payment": "Credit",
        "return_url": "https://example.com/paid", "custom1": "smoke"})
    check("建單 201", s == 201, (s, b))
    state["credit_order"] = {"id": b["id"], "url": b.get("checkout_url"),
                             "trade_no": b["merchant_trade_no"]}
    print(f"     checkout_url: {b.get('checkout_url')}")

    print("\n【建立 ATM 訂單】")
    s, b = call("POST", "/v1/orders", {
        "reference_id": ref("atm"), "amount": 9,
        "item_name": "端對端測試-ATM", "choose_payment": "ATM"})
    check("建單 201", s == 201, (s, b))
    state["atm_order"] = {"id": b["id"], "url": b.get("checkout_url"),
                          "trade_no": b["merchant_trade_no"]}
    print(f"     checkout_url: {b.get('checkout_url')}")

    print("\n【建立定期定額】")
    s, b = call("POST", "/v1/subscriptions", {
        "reference_id": ref("sub"), "amount": 5,
        "item_name": "端對端測試-月訂閱", "period_type": "M",
        "frequency": 1, "exec_times": 12,
        "return_url": "https://example.com/subscribed"})
    check("建訂閱 201", s == 201, (s, b))
    check("週期參數如實回報",
          (b.get("period_type"), b.get("frequency"), b.get("exec_times")) == ("M", 1, 12), b)
    state["subscription"] = {"id": b["id"], "url": b.get("checkout_url"),
                             "trade_no": b["merchant_trade_no"]}
    print(f"     checkout_url: {b.get('checkout_url')}")

    path = "/tmp/ecpay-smoke-state.json"
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\n  狀態寫入 {path}")
    return state


# ─────────────────────────────────────────────────────────── verify
def run_verify(state):
    co = state["credit_order"]
    print("\n【信用卡訂單付款後】")
    s, b = call("GET", f"/v1/orders/{co['id']}")
    check("回呼已把狀態翻成 paid", b.get("status") == "paid", b.get("status"))
    check("綠界交易編號已寫入", bool(b.get("ecpay_trade_no")), b.get("ecpay_trade_no"))
    check("付款方式已記錄", bool(b.get("payment_type")), b.get("payment_type"))
    check("付款後 checkout_url 失效（token 已清）", "checkout_url" not in b)

    print("\n【向綠界對帳（?refresh=true）】")
    s, b = call("GET", f"/v1/orders/{co['id']}?refresh=true")
    check("查詢 API 可用且驗簽通過", s == 200, (s, b))
    check("對帳後仍是 paid", b.get("status") == "paid", b.get("status"))

    print("\n【已付款訂單的退款（stage 應被擋下並說明原因）】")
    s, b = call("POST", f"/v1/orders/{co['id']}/refund", {"amount": 1})
    check("stage 明確拒絕退款且指出是環境限制",
          s == 400 and b["detail"]["field"] == "environment", (s, b))

    atm = state.get("atm_order")
    if atm:
        print("\n【ATM 取號】")
        s, b = call("GET", f"/v1/orders/{atm['id']}")
        check("狀態為 awaiting_payment",
              b.get("status") == "awaiting_payment", b.get("status"))
        info = b.get("payment_info") or {}
        check("虛擬帳號已落地", bool(info.get("v_account")), info)
        check("繳費期限已落地", bool(info.get("expire_date")), info)

    sub = state.get("subscription")
    if sub:
        print("\n【定期定額首期】")
        s, b = call("GET", f"/v1/subscriptions/{sub['id']}")
        check("狀態為 active", b.get("status") == "active", b.get("status"))
        check("首期扣款已記錄", b.get("total_success_times", 0) >= 1, b)
        check("扣款明細有一筆", len(b.get("charges") or []) >= 1, b.get("charges"))

        print("\n【向綠界對帳定期定額（唯一能分辨訂閱的途徑）】")
        s, b = call("GET", f"/v1/subscriptions/{sub['id']}?refresh=true")
        check("QueryCreditCardPeriodInfo 可用", s == 200, (s, b))
        check("綠界確認這是定期定額（回報成功次數）",
              b.get("total_success_times", 0) >= 1, b)

        print("\n【終止訂閱】")
        s, b = call("POST", f"/v1/subscriptions/{sub['id']}/cancel", {})
        check("終止成功", s == 200 and b.get("status") == "cancelled", (s, b))
        check("有記錄終止時間", bool(b.get("cancelled_at")), b)
        s, b = call("POST", f"/v1/subscriptions/{sub['id']}/cancel", {})
        check("重複終止是冪等而非錯誤", s == 200, s)

    print("\n【事件流】")
    s, b = call("GET", "/v1/events?after=0&limit=100")
    kinds = {(e["event_type"], e["subject_kind"]) for e in b.get("items", [])}
    check("收到 payment.return 事件",
          any(k[0] == "payment.return" for k in kinds), kinds)
    check("事件在落地時就標好了 subject_kind",
          all(k[1] in ("order", "subscription") for k in kinds), kinds)
    check("訂閱首期被正確標成 subscription 而非 order",
          ("payment.return", "subscription") in kinds, kinds)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "checks"
    print(f"═══ {mode} @ {BASE}")
    if mode == "checks":
        run_checks()
    elif mode == "create":
        run_create()
    elif mode == "verify":
        path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ecpay-smoke-state.json"
        run_verify(json.load(open(path)))
    else:
        sys.exit(f"不認得的模式 {mode!r}")

    print(f"\n═══ 通過 {len(_passed)} / 失敗 {len(_failed)}")
    if _failed:
        for f in _failed:
            print(f"    ✗ {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
