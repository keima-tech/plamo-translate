"""接続設定 API / config.json のテスト（TestClient 使用、実ネットワーク不要）"""
import os
import stat

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def clean_config():
    """各テストで config.json を初期化"""
    main.CONFIG_FILE.unlink(missing_ok=True)
    main._cfg_cache["key"] = None
    yield
    main.CONFIG_FILE.unlink(missing_ok=True)
    main._cfg_cache["key"] = None


def test_get_config_defaults_from_env():
    d = client.get("/api/config").json()
    assert d["ollama_url"] == os.environ["OLLAMA_URL"]  # config.json 無し → 環境変数既定
    assert d["api_key_set"] is False
    assert d["api_key_masked"] == ""
    assert d["model"] == ""


def test_put_and_get_masks_key():
    r = client.put("/api/config", json={"ollama_url": "http://remote:11434",
                                        "api_key": "super-secret-abcd", "model": "m1"})
    assert r.status_code == 200
    d = r.json()
    assert d["ollama_url"] == "http://remote:11434"
    assert d["api_key_set"] is True
    assert d["api_key_masked"] == "****abcd"
    assert d["model"] == "m1"
    # 平文がレスポンスに混入していないこと
    assert "super-secret-abcd" not in r.text
    g = client.get("/api/config")
    assert "super-secret-abcd" not in g.text
    assert g.json()["api_key_masked"] == "****abcd"


def test_config_file_mode_0600():
    client.put("/api/config", json={"api_key": "k12345678"})
    mode = stat.S_IMODE(main.CONFIG_FILE.stat().st_mode)
    assert mode == 0o600


def test_empty_api_key_keeps_existing():
    client.put("/api/config", json={"api_key": "keepme-9999"})
    d = client.put("/api/config", json={"ollama_url": "http://x:1", "api_key": ""}).json()
    assert d["api_key_set"] is True
    assert d["api_key_masked"] == "****9999"


def test_clear_api_key():
    client.put("/api/config", json={"api_key": "gone-1234"})
    d = client.put("/api/config", json={"api_key": "__CLEAR__"}).json()
    assert d["api_key_set"] is False
    assert d["api_key_masked"] == ""


def test_url_scheme_validation():
    assert client.put("/api/config", json={"ollama_url": "ftp://evil"}).status_code == 400
    assert client.put("/api/config", json={"ollama_url": "javascript:alert(1)"}).status_code == 400
    assert client.put("/api/config", json={"ollama_url": "https://ok.example"}).status_code == 200


def test_trailing_slash_normalized():
    d = client.put("/api/config", json={"ollama_url": "http://h:11434/"}).json()
    assert d["ollama_url"] == "http://h:11434"


def test_empty_url_falls_back_to_env_default():
    client.put("/api/config", json={"ollama_url": "http://custom:1"})
    d = client.put("/api/config", json={"ollama_url": ""}).json()
    assert d["ollama_url"] == os.environ["OLLAMA_URL"]


def test_model_override_and_reset():
    d = client.put("/api/config", json={"model": "remote-plamo"}).json()
    assert d["model"] == "remote-plamo"
    assert main.load_config()["model"] == "remote-plamo"
    d = client.put("/api/config", json={"model": ""}).json()
    assert d["model"] == ""


def test_config_test_connection_failure_reason():
    # conftest の OLLAMA_URL は閉じたポート 59999 → 接続失敗理由が返る
    d = client.post("/api/config/test").json()
    assert d["ok"] is False
    assert "接続失敗" in d["error"]


def test_save_config_reflects_without_reload():
    """mtime キャッシュ: save_config 直後の load_config が新値を返す（再起動不要の根拠）"""
    main.save_config({"ollama_url": "http://a:1"})
    assert main.load_config()["ollama_url"] == "http://a:1"
    main.save_config({"ollama_url": "http://b:2"})
    assert main.load_config()["ollama_url"] == "http://b:2"


def test_resolve_model_uses_config_override():
    import asyncio
    main.save_config({"model": "my-model"})
    assert asyncio.run(main.resolve_model(None)) == "my-model"
    assert asyncio.run(main.resolve_model("explicit")) == "explicit"
