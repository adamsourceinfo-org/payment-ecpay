"""API key 驗證與 scope 檢查。

服務在 Cloud Run 上是公開的（綠界的回呼必須打得到），
所以這一層是唯一一道門。因此：只存 hash、可停用、每次呼叫留痕。
"""
import hashlib
from dataclasses import dataclass
from typing import Callable

from fastapi import Header, HTTPException

from app.store import api_keys


@dataclass(frozen=True)
class Caller:
    caller_id: str
    scopes: frozenset


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def require(*needed: str) -> Callable:
    """回傳 FastAPI dependency，驗證 key 並檢查 scope。"""

    def dependency(x_api_key: str = Header(default=None)) -> Caller:
        if not x_api_key:
            # 沒帶、錯的、停用的 —— 一律同一個回應，不幫攻擊者縮小範圍
            raise HTTPException(status_code=401, detail="invalid api key")

        row = api_keys.lookup(hash_key(x_api_key))
        if not row or not row["active"]:
            raise HTTPException(status_code=401, detail="invalid api key")

        scopes = frozenset(row["scopes"] or [])
        missing = [s for s in needed if s not in scopes]
        if missing:
            raise HTTPException(
                status_code=403, detail=f"需要 scope: {', '.join(missing)}")

        api_keys.touch(row["id"])
        return Caller(caller_id=row["caller_id"], scopes=scopes)

    return dependency
