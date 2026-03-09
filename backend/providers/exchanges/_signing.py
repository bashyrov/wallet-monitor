import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode


def ms() -> str:
    return str(int(time.time() * 1000))


def s() -> str:
    return str(int(time.time()))


def b64_hmac_sha256(secret: str, msg: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def hex_hmac_sha256(secret: str, msg: str) -> str:
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def hex_hmac_sha512(secret: str, msg: str) -> str:
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha512).hexdigest()


def sha512_hex(payload: str) -> str:
    return hashlib.sha512(payload.encode("utf-8")).hexdigest()


def json_dumps_compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)