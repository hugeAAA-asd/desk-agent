"""测试夹具：每个用例一个独立的临时库，互不污染。"""

import pytest
from fastapi.testclient import TestClient

from biz import app as app_module
from biz import seed, store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把数据层指到临时文件，再起一个应用实例。

    必须用 `with TestClient(...)`：lifespan 里才建表和写种子，
    直接 `TestClient(app)` 拿到的是一个空库。
    """
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "desk.db")
    seed.ensure_ready()
    with TestClient(app_module.app) as test_client:
        yield test_client
