# -*- coding: utf-8 -*-
"""站长密码校验。

- 密码只以 PBKDF2 哈希形式保存在系统用户目录（不在项目目录，不会进备份包）；
- 验证状态只存在内存里，工具关闭即失效；
- 换电脑/重置系统后：删除本文件重新设置即可恢复。
"""
import hashlib
import hmac
import json
import os
import secrets

OWNER_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "TVBox地址工具")
OWNER_FILE = os.path.join(OWNER_DIR, "owner.dat")

_verified = {"ok": False}
_DEFAULT_ITERS = 200000


def exists():
    return os.path.exists(OWNER_FILE)


def _read():
    try:
        return json.load(open(OWNER_FILE, encoding="utf-8"))
    except Exception:
        return None


def _hash(pwd, salt, iters):
    return hashlib.pbkdf2_hmac("sha256", str(pwd).encode("utf-8"), salt, int(iters)).hex()


def set_password(pwd):
    salt = secrets.token_bytes(16)
    obj = {"alg": "pbkdf2_sha256", "iters": _DEFAULT_ITERS,
           "salt": salt.hex(), "hash": _hash(pwd, salt, _DEFAULT_ITERS)}
    os.makedirs(OWNER_DIR, exist_ok=True)
    with open(OWNER_FILE, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    try:
        os.chmod(OWNER_FILE, 0o600)
    except Exception:
        pass
    _verified["ok"] = True
    return True


def verify(pwd):
    obj = _read()
    if not obj:
        return False
    try:
        salt = bytes.fromhex(str(obj.get("salt") or ""))
        iters = int(obj.get("iters") or _DEFAULT_ITERS)
        ok = hmac.compare_digest(_hash(pwd, salt, iters), str(obj.get("hash") or ""))
    except Exception:
        ok = False
    _verified["ok"] = bool(ok)
    return bool(ok)


def is_verified():
    return bool(_verified["ok"])


def lock():
    _verified["ok"] = False


def reset():
    """忘记密码时：删除密码文件，下次进入会要求重新设置。"""
    try:
        os.remove(OWNER_FILE)
    except Exception:
        pass
    _verified["ok"] = False
