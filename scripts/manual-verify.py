#!/usr/bin/env python3
"""把僅剩的人工驗證步驟壓到最小：你只要點兩個連結、刷兩次測試卡。

為什麼需要人：綠界的收銀台是 Vue SPA，信用卡的表單由前端動態產生
（HTML 原始碼裡只有 `<div id="PayForm"></div>`），所以伺服器端重放不可能；
而自動化瀏覽器在本機跑到最後一步不會送出。ATM 與超商代碼那類「留在綠界站內」
的流程都自動驗過了，只有需要交棒給銀行／3D 驗證的信用卡這條要人。

這支會：建立一筆信用卡訂單與一個月訂閱 → 印出兩個付款連結 →
盯著資料庫等回呼進來 → 回呼到齊後把該驗的都驗一遍。

用法：BASE=... KEY=... python3 scripts/manual-verify.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ["BASE"].rstrip("/")
KEY = os.environ["KEY"]
TIMEOUT_MINUTES = int(os.environ.get("WAIT_MINUTES", "15"))

_passed, _failed = [], []


def check(name, cond, detail=""):
    (_passed if cond else _failed).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{'  ' + str(detail) if detail and not cond else ''}")


def api(method, path, body=None):
    req = urllib.request.Request(
        f"{BASE}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={k: v for k, v in {
            "Content-Type": "application/json" if body is not None else None,
            "X-API-Key": KEY}.items() if v})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_for(path, done, label):
    """盯著某個資源直到條件成立。回 (成功?, 最後一次的內容)。"""
    deadline = time.time() + TIMEOUT_MINUTES * 60
    last = None
    shown = None
    while time.time() < deadline:
        _, last = api("GET", path)
        state = last.get("status")
        if state != shown:
            print(f"     {label}: {state}", flush=True)
            shown = state
        if done(last):
            return True, last
        time.sleep(5)
    return False, last


def main():
    stamp = int(time.time())
    print(f"═══ 人工驗證 @ {BASE}\n")

    s, order = api("POST", "/v1/orders", {
        "reference_id": f"manual-{stamp}", "amount": 7,
        "item_name": "人工驗證-信用卡", "choose_payment": "Credit",
        "return_url": "https://example.com/paid"})
    check("建立信用卡訂單", s == 201, (s, order))
    s, sub = api("POST", "/v1/subscriptions", {
        "reference_id": f"manualsub-{stamp}", "amount": 5,
        "item_name": "人工驗證-月訂閱", "period_type": "M",
        "frequency": 1, "exec_times": 12})
    check("建立定期定額", s == 201, (s, sub))
    if _failed:
        sys.exit(1)

    print(f"""
┌───────────────────────────────────────────────────────────────┐
│  請用一般瀏覽器打開下面兩個連結，各刷一次測試卡              │
│                                                               │
│    卡號      4311-9511-1111-1111                              │
│    有效期限  12 / 30      安全碼  123                          │
│    持卡人    TEST USER    手機    0987654321                   │
│    3D 驗證簡訊碼  1234                                        │
└───────────────────────────────────────────────────────────────┘

  1) 一次性付款  {order['checkout_url']}

  2) 月訂閱      {sub['checkout_url']}

  刷完不用回報，這支會自己等（最多 {TIMEOUT_MINUTES} 分鐘）。
""", flush=True)

    print("【等待訂單的付款回呼】")
    ok_o, order = wait_for(f"/v1/orders/{order['id']}",
                           lambda d: d.get("status") in ("paid", "failed"), "訂單")
    print("\n【等待訂閱的首期扣款回呼】")
    ok_s, sub = wait_for(f"/v1/subscriptions/{sub['id']}",
                         lambda d: d.get("status") in ("active", "failed"), "訂閱")

    print("\n【訂單】")
    check("回呼把狀態翻成 paid", order.get("status") == "paid", order.get("status"))
    check("綠界交易編號已寫入", bool(order.get("ecpay_trade_no")))
    check("付款方式是信用卡",
          str(order.get("payment_type") or "").lower().startswith("credit"),
          order.get("payment_type"))
    check("付款後 checkout_url 失效", "checkout_url" not in order)

    s, refreshed = api("GET", f"/v1/orders/{order['id']}?refresh=true")
    check("QueryTradeInfo 對帳一致（含回應驗簽）",
          s == 200 and refreshed.get("status") == "paid", (s, refreshed.get("status")))

    print("\n【訂閱】")
    check("首期成功後轉 active", sub.get("status") == "active", sub.get("status"))
    check("首期扣款已記錄", (sub.get("total_success_times") or 0) >= 1, sub)
    check("扣款明細有一筆", len(sub.get("charges") or []) >= 1)

    s, rs = api("GET", f"/v1/subscriptions/{sub['id']}?refresh=true")
    check("QueryCreditCardPeriodInfo 確認這真的是定期定額",
          s == 200 and (rs.get("total_success_times") or 0) >= 1, (s, rs))

    print("\n【事件：首期必須被標成 subscription 而不是 order】")
    s, ev = api("GET", "/v1/events?after=0&limit=200")
    kinds = {(e["event_type"], e["subject_kind"]) for e in ev.get("items", [])
             if e["subject_id"] in (order["id"], sub["id"])}
    check("訂單的付款事件標成 order", ("payment.return", "order") in kinds, kinds)
    check("訂閱的首期事件標成 subscription",
          ("payment.return", "subscription") in kinds, kinds)

    print("\n【終止訂閱（真實 API，不可復原）】")
    s, c = api("POST", f"/v1/subscriptions/{sub['id']}/cancel", {})
    check("終止成功", s == 200 and c.get("status") == "cancelled", (s, c))
    check("記錄了終止時間", bool(c.get("cancelled_at")))
    s2, c2 = api("POST", f"/v1/subscriptions/{sub['id']}/cancel", {})
    check("重複終止是冪等而非錯誤", s2 == 200, s2)

    print(f"\n═══ 通過 {len(_passed)} / 失敗 {len(_failed)}")
    for f in _failed:
        print(f"    ✗ {f}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
