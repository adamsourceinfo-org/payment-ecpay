"""事件形狀只准有一份定義。

行為測試（body == item() 的輸出）抓不到「有第二份手抄的 dict」——
只要兩份內容當下剛好一樣，測試就是綠的。這正是實際發生過的事：
dispatch.py 裡躺著一份手抄的 event_payload()，而盯著它的測試寫成
`event_payload(row) == item(row)`，於是一路綠燈到 code review 才被指出來。

所以這一條直接掃原始碼：**除了 event_view.item()（定義形狀）與
dispatch.ping_row()（合成一列 row 給它取形狀）之外，
app/ 底下任何地方都不准出現同時列出這六個欄位的 dict。**
"""
import ast
import pathlib

SHAPE = {"id", "event_type", "subject_kind", "subject_id", "payload",
         "received_at"}

# (檔案, 所在函式) → 為什麼可以
ALLOWED = {
    ("app/event_view.py", "item"): "形狀的唯一定義",
    ("app/webhooks/dispatch.py", "ping_row"): "合成一列 row，提供值而不是重新描述形狀",
}


def _dict_literals_with_shape(tree, path):
    """回 [(函式名, 行號)] —— 所有鍵涵蓋整組形狀的 dict 字面值。"""
    out = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if SHAPE <= keys:
                out.append((func.name, node.lineno))
    return out


def test_事件形狀在app底下只有一份定義():
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "app").rglob("*.py")):
        rel = str(path.relative_to(root))
        tree = ast.parse(path.read_text())
        for func_name, lineno in _dict_literals_with_shape(tree, rel):
            if (rel, func_name) in ALLOWED:
                continue
            offenders.append(f"{rel}:{lineno} 的 {func_name}()")

    assert not offenders, (
        "又有人手抄了一份事件形狀：\n  " + "\n  ".join(offenders) +
        "\n\n形狀只在 app/event_view.py 的 item() 定義一次，兩條出口都用它。"
        "\n真的需要新增例外的話，連同理由寫進這個檔案的 ALLOWED。")


def test_這條守衛本身有效():
    """守衛如果掃不到東西就等於沒有 —— 用一段假的原始碼證明它會叫。"""
    fake = ast.parse(
        "def event_payload(row):\n"
        "    return {'id': 1, 'event_type': 'x', 'subject_kind': None,\n"
        "            'subject_id': None, 'payload': {}, 'received_at': None}\n")
    assert _dict_literals_with_shape(fake, "fake.py") == [("event_payload", 2)]


def test_允許清單裡的兩個位置都還存在():
    """例外會過期。允許清單指到不存在的函式時要當場說出來，
    而不是靜靜地放寬守衛。"""
    root = pathlib.Path(__file__).resolve().parent.parent
    for (rel, func_name), why in ALLOWED.items():
        tree = ast.parse((root / rel).read_text())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert func_name in names, f"{rel} 已經沒有 {func_name}()（例外理由：{why}）"
