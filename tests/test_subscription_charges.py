"""首期扣款的 gwsr 佔位符與升級。

綠界的定期定額**首期回呼不帶 gwsr**（實測的真實回呼欄位只有
MerchantTradeNo / TradeNo / TradeAmt / PaymentType / RtnCode …），
所以只能先塞 `first:<單號>` 佔位。真實的 gwsr 要等對帳才拿得到 ——
那時必須把佔位那列升級，不能插入第二列。
"""
import app.store.subscriptions as store


class FakeDB:
    def __init__(self, placeholder=None):
        self.placeholder = placeholder
        self.calls = []

    def query(self, sql, args=(), fetch="all"):
        self.calls.append(sql.split()[0] + " " + ("UPDATE" if "UPDATE" in sql else "INSERT"))
        if "UPDATE subscription_charges" in sql:
            return {"id": 1} if self.placeholder else None
        return {"id": 2}


def test_real_gwsr_upgrades_the_placeholder(monkeypatch):
    db = FakeDB(placeholder=True)
    monkeypatch.setattr(store, "db", db)
    got = store.record_charge("s1", gwsr="14563860", amount=5, rtn_code="1",
                              auth_code="777777", process_date="2026/08/24 23:18:02")
    assert got == 1
    assert not any("INSERT" in c for c in db.calls), "升級成功就不該再插入一列"


def test_real_gwsr_inserts_when_no_placeholder(monkeypatch):
    db = FakeDB(placeholder=False)
    monkeypatch.setattr(store, "db", db)
    assert store.record_charge("s1", gwsr="14563999", amount=5, rtn_code="1",
                               auth_code="x", process_date="2026/09/24 12:00:00") == 2


def test_placeholder_itself_never_tries_to_upgrade(monkeypatch):
    """佔位符不能拿去升級別的佔位符，否則首期會覆蓋掉自己。"""
    db = FakeDB(placeholder=True)
    monkeypatch.setattr(store, "db", db)
    store.record_charge("s1", gwsr="first:S1", amount=5, rtn_code="1",
                        auth_code=None, process_date="2026/08/24 23:18:02")
    assert not any("UPDATE" in c for c in db.calls)
