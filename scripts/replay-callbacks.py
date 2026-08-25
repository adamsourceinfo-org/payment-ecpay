#!/usr/bin/env python3
"""用**正確簽章**的回呼重放，驗證訂閱的狀態機。

為什麼需要這支：綠界是導轉模型，訂閱的首期扣款必須有人在瀏覽器上真的刷一張卡。
當那條路走不通時（例如刷卡頁的前端在某些環境跑不完），訂閱的狀態機就完全沒被驗到。

**這支驗得到什麼**：訂閱以 MerchantTradeNo 從自己的表解析出來、首期成功轉 active、
每期扣款以 gwsr 落地與去重、累計次數金額、事件的 subject_kind 標成 subscription、
以及扣款失敗不會讓訂閱被取消。全部打的是**已部署的服務與真實資料庫**。

**這支驗不到什麼**：綠界是否真的會那樣送 —— 簽章是我們自己算的。
簽章演算法本身由 tests/test_checkmac.py 的官方向量鎖定，
而「綠界實際送出的回呼能被我們驗過」已由 ATM 那條真實流程證明。

用法：BASE=... KEY=... python3 scripts/replay-callbacks.py
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.ecpay.checkmac import generate                      # noqa: E402

BASE = os.environ["BASE"].rstrip("/")
KEY = os.environ["KEY"]
# 綠界公開的測試特店金鑰（文件上就有，不是機密）
HASH_KEY = os.environ.get("ECPAY_HASH_KEY", "pwFHCqoQZGmho4w6")
HASH_IV = os.environ.get("ECPAY_HASH_IV", "EkRm7iFT261dpevs")

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


def post_callback(path, params):
    """照綠界的方式送：application/x-www-form-urlencoded + CheckMacValue。"""
    signed = dict(params)
    signed["CheckMacValue"] = generate(params, HASH_KEY, HASH_IV)
    data = urllib.parse.urlencode(signed).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}", method="POST", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    stamp = int(time.time())
    ref = f"replay-{stamp}"
    # gwsr 是綠界每次授權的**全域唯一**交易號，去重就靠它 ——
    # 重放腳本每次都要換一組，不然會被上一次跑的紀錄正確地擋掉。
    gw = lambda n: f"{stamp}{n}"
    print(f"═══ 訂閱狀態機重放 @ {BASE}")

    print("\n【建立訂閱】")
    s, sub = api("POST", "/v1/subscriptions", {
        "reference_id": ref, "amount": 199, "item_name": "重放測試-月訂閱",
        "period_type": "M", "frequency": 1})
    check("建立成功", s == 201, (s, sub))
    if s != 201:
        return
    sid, trade_no = sub["id"], sub["merchant_trade_no"]
    check("初始狀態 created", sub["status"] == "created", sub["status"])
    print(f"     id={sid} trade_no={trade_no}")

    print("\n【首期扣款：回呼與一次性付款逐欄位相同】")
    first = {
        "MerchantID": "3002607", "MerchantTradeNo": trade_no,
        "RtnCode": "1", "RtnMsg": "付款成功",          # 中文，會驗到 UTF-8 解碼
        "TradeNo": f"T{stamp}1", "TradeAmt": "199",
        "PaymentDate": "2026/08/24 12:00:00",
        "PaymentType": "Credit_CreditCard", "PaymentTypeChargeFee": "5",
        "TradeDate": "2026/08/24 11:59:00", "SimulatePaid": "0",
        "StoreID": "", "CustomField1": "", "CustomField2": "",
        "CustomField3": "", "CustomField4": "",
    }
    check("首期 payload 不含任何 Period 欄位",
          not any(k.startswith(("Period", "TotalSuccess")) or k in ("Frequency", "ExecTimes")
                  for k in first))
    st, body = post_callback("/ecpay/return", first)
    check("回呼被接受並回 1|OK", st == 200 and body == "1|OK", (st, body))

    s, sub = api("GET", f"/v1/subscriptions/{sid}")
    check("狀態轉為 active", sub["status"] == "active", sub["status"])
    check("首期扣款時間已寫入", bool(sub["first_charged_at"]), sub)
    check("累計成功次數 = 1", sub["total_success_times"] == 1, sub["total_success_times"])
    check("扣款明細有一筆", len(sub["charges"]) == 1, sub["charges"])

    print("\n【首期重送：綠界沒收到 1|OK 會重發四次】")
    st, body = post_callback("/ecpay/return", first)
    check("重送仍回 1|OK", st == 200 and body == "1|OK", (st, body))
    s, sub2 = api("GET", f"/v1/subscriptions/{sid}")
    check("重送沒有變成兩筆扣款", len(sub2["charges"]) == 1, sub2["charges"])

    print("\n【第二期扣款：走 PeriodReturnURL，欄位長得不一樣】")
    second = {
        "MerchantID": "3002607", "MerchantTradeNo": trade_no,
        "RtnCode": "1", "RtnMsg": "刷卡成功", "gwsr": gw(1),
        "amount": "199", "process_date": "2026/09/24 12:00:00",
        "auth_code": "R05013", "PeriodType": "M", "Frequency": "1",
        "ExecTimes": "12", "TotalSuccessTimes": "2",
        "TotalSuccessAmount": "398", "TradeNo": f"T{stamp}2",
    }
    check("續期 payload 才有 Period 欄位",
          second["PeriodType"] == "M" and second["TotalSuccessTimes"] == "2")
    st, body = post_callback("/ecpay/period-return", second)
    check("回呼被接受並回 1|OK", st == 200 and body == "1|OK", (st, body))

    s, sub = api("GET", f"/v1/subscriptions/{sid}")
    check("累計次數更新為 2", sub["total_success_times"] == 2, sub["total_success_times"])
    check("累計金額更新為 398", sub["total_success_amount"] == 398, sub["total_success_amount"])
    check("扣款明細變兩筆", len(sub["charges"]) == 2, len(sub["charges"]))

    print("\n【第二期重送：以 gwsr 去重】")
    st, body = post_callback("/ecpay/period-return", second)
    check("重送仍回 1|OK", st == 200 and body == "1|OK", (st, body))
    s, sub = api("GET", f"/v1/subscriptions/{sid}")
    check("沒有變成三筆", len(sub["charges"]) == 2, len(sub["charges"]))

    print("\n【第三期扣款失敗：不該把訂閱標成結束】")
    third = dict(second, gwsr=gw(2), RtnCode="10100058",
                 RtnMsg="銀行拒絕交易", TotalSuccessTimes="2")
    st, body = post_callback("/ecpay/period-return", third)
    check("回呼被接受並回 1|OK", st == 200 and body == "1|OK", (st, body))
    s, sub = api("GET", f"/v1/subscriptions/{sid}")
    check("訂閱仍是 active（綠界連續六期失敗才自動終止）",
          sub["status"] == "active", sub["status"])
    check("失敗的那期也留下紀錄", len(sub["charges"]) == 3, len(sub["charges"]))

    print("\n【事件流：訂閱首期必須被標成 subscription 而不是 order】")
    s, ev = api("GET", "/v1/events?after=0&limit=200")
    mine = [e for e in ev["items"] if e["subject_id"] == sid]
    kinds = {(e["event_type"], e["subject_kind"]) for e in mine}
    check("首期事件標成 subscription",
          ("payment.return", "subscription") in kinds, kinds)
    check("續期事件標成 subscription",
          ("subscription.charge", "subscription") in kinds, kinds)
    # 首期走 /ecpay/return，第二、三期走 /ecpay/period-return —— 共 3 筆。
    # 重送的那兩次不算（去重成功才是對的）。
    check("事件正好 3 筆：首期 1 + 續期 2，重送不重複落地", len(mine) == 3,
          [(e["id"], e["event_type"]) for e in mine])

    print("\n【驗簽把關：竄改金額必須被擋下且不落地】")
    tampered = dict(second, gwsr=gw(3))
    signed = dict(tampered)
    signed["CheckMacValue"] = generate(tampered, HASH_KEY, HASH_IV)
    signed["amount"] = "99999"                      # 簽完再改
    data = urllib.parse.urlencode(signed).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/ecpay/period-return", method="POST", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            st, body = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        st, body = e.code, e.read().decode()
    check("竄改的回呼被拒絕", st == 400, (st, body))
    s, sub = api("GET", f"/v1/subscriptions/{sid}")
    check("竄改的回呼沒有落地", len(sub["charges"]) == 3, len(sub["charges"]))

    print(f"\n═══ 通過 {len(_passed)} / 失敗 {len(_failed)}")
    for f in _failed:
        print(f"    ✗ {f}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
