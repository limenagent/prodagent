from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
from auth import login
from password import hash_with_salt


def test_login():
    auth._USERS["alice"] = hash_with_salt("password")
    assert login("alice", "password") is True
    assert login("alice", "wrong") is False
    assert login("nobody", "password") is False


if __name__ == "__main__":
    test_login()
    print("test_login PASSED")
