"""auth.py —— 含 bug 的认证模块(故意写错)。

bug: ``login`` 调用 ``hash(password)`` 不加 salt,而 test 期望
``hash_with_salt(password, salt)``。修复方法:从 password.py import
``hash_with_salt`` 并用它替代 ``hash``。
"""

from __future__ import annotations

_USERS: dict[str, str] = {
    "alice": "5f4dcc3b5aa765d61d8327deb882cf99",  # hash("password")
    "bob": "098f6bcd4621d373cade4e832627b4f6",    # hash("test")
}


def hash(password: str) -> str:
    """不安全的 hash —— 缺 salt。这是 bug 根源。"""
    import hashlib
    return hashlib.md5(password.encode()).hexdigest()


def login(username: str, password: str) -> bool:
    """验证用户凭据。

    bug: 这里用 ``hash(password)`` 不加 salt,但用户表里存的是
    ``hash_with_salt(password, salt)`` 的结果,永远匹配不上。
    """
    hashed = hash(password)
    return _USERS.get(username) == hashed
