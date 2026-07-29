"""推送层测试：不碰真实网络，mock requests，锁定"回执→PushResult"的映射契约，
以及 store.push_health 落库。确保"推送成不成功"被如实回报、可观测。
"""
import sqlite3

import pytest

import push
import store
from domain.delivery import summarize_push


class _FakeResp:
    def __init__(self, payload, text=""):
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    # 保证测试不发真实请求
    monkeypatch.setenv("PUSH_KEY", "")
    monkeypatch.setenv("WECOM_KEY", "")
    monkeypatch.setenv("FEISHU_WEBHOOK", "")


class TestServerchanMapping:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(push.requests, "post", lambda *a, **k: _FakeResp(
            {"code": 0, "data": {"error": "SUCCESS", "pushid": "PID123"}}))
        r = push._push_serverchan("k", "Server酱", "t", "c")
        assert r.success and r.code == 0 and r.pushid == "PID123"

    def test_non_success_code(self, monkeypatch):
        monkeypatch.setattr(push.requests, "post", lambda *a, **k: _FakeResp(
            {"code": 40001, "data": {"error": "BAD_KEY"}}, text="bad"))
        r = push._push_serverchan("k", "Server酱", "t", "c")
        assert not r.success and r.code == 40001 and "BAD_KEY" in r.error

    def test_exception_is_failure(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(push.requests, "post", boom)
        r = push._push_serverchan("k", "Server酱", "t", "c")
        assert not r.success and "network down" in r.error


class TestFeishuMapping:
    def test_success_card_contains_security_keyword(self, monkeypatch):
        captured = {}

        def fake_post(*args, **kwargs):
            captured.update(kwargs)
            return _FakeResp({"code": 0, "msg": "success"})

        monkeypatch.setattr(push.requests, "post", fake_post)
        r = push._push_feishu("https://example.test/hook", "飞书",
                               "秋招雷达初始化完成", "今日简报")

        body = captured["data"]
        payload = __import__("json").loads(body.decode("utf-8"))
        assert r.success and r.code == 0
        assert "岗位" in payload["card"]["header"]["title"]["content"]
        assert payload["card"]["elements"][0]["content"] == "今日简报"
        assert captured["headers"]["Content-Type"].endswith("utf-8")

    def test_legacy_success_response(self, monkeypatch):
        monkeypatch.setattr(push.requests, "post", lambda *a, **k: _FakeResp(
            {"StatusCode": 0, "StatusMessage": "success"}))
        r = push._push_feishu("https://example.test/hook", "飞书", "t", "c")
        assert r.success and r.code == 0

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(push.requests, "post", lambda *a, **k: _FakeResp(
            {"code": 19024, "msg": "Key Words Not Found"}))
        r = push._push_feishu("https://example.test/hook", "飞书", "t", "c")
        assert not r.success and r.code == 19024
        assert "Key Words Not Found" in r.error

    def test_utf8_content_is_safely_truncated(self):
        text = "岗" * 100
        result = push._truncate_utf8(text, 10)
        assert result == "岗" * 3
        assert len(result.encode("utf-8")) <= 10


class TestSendBriefStructured:
    def test_multi_key_returns_per_channel(self, monkeypatch):
        monkeypatch.setenv("PUSH_KEY", "k1,k2")
        monkeypatch.setenv("WECOM_KEY", "")
        monkeypatch.setattr(push.requests, "post", lambda *a, **k: _FakeResp(
            {"code": 0, "data": {"error": "SUCCESS", "pushid": "P"}}))
        results = push.send_brief("hello", title="t")
        assert len(results) == 2
        assert [r.channel for r in results] == ["Server酱#1", "Server酱#2"]
        assert summarize_push(results).succeeded == 2

    def test_no_channels_returns_empty(self, monkeypatch):
        monkeypatch.setenv("PUSH_KEY", "")
        monkeypatch.setenv("WECOM_KEY", "")
        assert push.send_brief("x") == []

    def test_feishu_webhook_is_a_channel(self, monkeypatch):
        monkeypatch.setenv("FEISHU_WEBHOOK", "https://example.test/hook")
        monkeypatch.setattr(push.requests, "post", lambda *a, **k: _FakeResp(
            {"code": 0, "msg": "success"}))
        results = push.send_brief("hello", title="今日岗位")
        assert len(results) == 1
        assert results[0].channel == "飞书"
        assert results[0].success

    def test_all_failed_detected(self, monkeypatch):
        monkeypatch.setenv("PUSH_KEY", "k1,k2")
        monkeypatch.setenv("WECOM_KEY", "")
        monkeypatch.setattr(push.requests, "post", lambda *a, **k: _FakeResp(
            {"code": 40001, "data": {"error": "X"}}, text="x"))
        results = push.send_brief("hello")
        assert summarize_push(results).all_failed


class TestPushHealthPersistence:
    def test_save_push_health(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t.db"))
        from domain.delivery import PushResult
        store.save_push_health([
            PushResult("Server酱#1", True, code=0, pushid="P1"),
            PushResult("Server酱#2", False, code=40001, error="bad")])
        conn = sqlite3.connect(store.DB_PATH)
        rows = conn.execute(
            "SELECT channel, success, code, pushid, error FROM push_health "
            "ORDER BY channel").fetchall()
        conn.close()
        assert rows == [("Server酱#1", 1, 0, "P1", ""),
                        ("Server酱#2", 0, 40001, "", "bad")]

    def test_empty_results_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t.db"))
        store.save_push_health([])   # 不建库、不报错
