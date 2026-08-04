"""password.py —— 正确的带 salt hash 实现。

修复 auth.py 时应该 import 这个模块的 ``hash_with_salt``。
"""

from __future__ import annotations

import hashlib

# 演示用 —— 生产环境每个用户一个独立 salt,存用户表里。
_DEFAULT_SALT = "s3cr3t-s4lt"


def hash_with_salt(password: str, salt: str = _DEFAULT_SALT) -> str:
    """带 salt 的 hash。用户表里存的是这个函数的结果。"""
    return hashlib.md5((salt + password).encode()).hexdigest()


def verify(password: str, stored_hash: str, salt: str = _DEFAULT_SALT) -> bool:
    """验证 password 是否匹配 stored_hash。"""
    return hash_with_salt(password, salt) == stored_hash
