"""推送层：把简报送到用户手机，并如实回报每个渠道的成败（送达检测的第一手数据）。
渠道由环境变量开关，不配置就静默跳过（简报始终落盘 reports/，推送只是加急通道）：

  - PUSH_KEY   Server酱(sct.ftqq.com) SendKey，微信服务号推送。
               支持逗号分隔多个 key —— 一个 key 对应一个微信，逐个投递。
  - WECOM_KEY  企业微信群机器人 webhook key（markdown 摘要，4K 字节上限）。
  - FEISHU_WEBHOOK  飞书群自定义机器人完整 webhook URL（卡片消息，20KB 请求体上限）。

单一职责：只发请求、只校验回执、只返回结构化 PushResult（供 main 判断是否告警、供 store 落库）。
不判断"全失败要不要标红"（那是编排层的事）、不写数据库。
"""
import json
import os
import requests

from domain.delivery import PushResult

_SCT_ENDPOINT = "https://sctapi.ftqq.com/{key}.send"
_WECOM_ENDPOINT = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
_SCT_BODY_LIMIT = 30000     # Server酱正文上限约 32KB，留余量
_WECOM_BODY_LIMIT = 4000
_FEISHU_CONTENT_LIMIT = 16000  # 飞书请求体上限 20KB，给卡片 JSON 结构留余量


def _keys_from_env(var):
    return [k.strip() for k in os.environ.get(var, "").split(",") if k.strip()]


def _truncate_utf8(text, max_bytes):
    """按 UTF-8 字节安全截断，避免中文字符把渠道请求体顶过上限。"""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _push_serverchan(key, label, title, content) -> PushResult:
    try:
        r = requests.post(_SCT_ENDPOINT.format(key=key),
                          data={"title": title, "desp": content[:_SCT_BODY_LIMIT]},
                          timeout=20)
        payload = r.json()
        detail = payload.get("data") or {}
        if payload.get("code") == 0 and detail.get("error") == "SUCCESS":
            return PushResult(channel=label, success=True,
                              code=0, pushid=str(detail.get("pushid") or ""))
        return PushResult(channel=label, success=False, code=payload.get("code"),
                          error=f"error={detail.get('error', '')} resp={r.text[:200]}")
    except Exception as e:
        return PushResult(channel=label, success=False, error=f"异常 {e}")


def _push_wecom(key, label, title, content) -> PushResult:
    try:
        r = requests.post(_WECOM_ENDPOINT.format(key=key),
                          json={"msgtype": "markdown",
                                "markdown": {"content": content[:_WECOM_BODY_LIMIT]}},
                          timeout=15)
        errcode = r.json().get("errcode")
        if errcode == 0:
            return PushResult(channel=label, success=True, code=0)
        return PushResult(channel=label, success=False, code=errcode,
                          error=r.text[:100])
    except Exception as e:
        return PushResult(channel=label, success=False, error=f"异常 {e}")


def _push_feishu(webhook, label, title, content) -> PushResult:
    """通过飞书群自定义机器人发送 Markdown 卡片。

    标题固定包含“岗位”，确保满足用户在机器人安全设置中配置的关键词。
    """
    try:
        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "blue",
                    "title": {
                        "tag": "plain_text",
                        "content": f"岗位推送｜{title}",
                    },
                },
                "elements": [{
                    "tag": "markdown",
                    "content": _truncate_utf8(content, _FEISHU_CONTENT_LIMIT),
                }],
            },
        }
        # ensure_ascii=False 让 16KB 内容预算与实际请求字节数一致。
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        r = requests.post(
            webhook,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
        response = r.json()
        # 飞书自定义机器人目前返回 code/msg；同时兼容旧版 StatusCode 格式。
        code = response.get("code")
        if code is None:
            code = response.get("StatusCode")
        if code == 0:
            return PushResult(channel=label, success=True, code=0)
        error = response.get("msg") or response.get("StatusMessage") or r.text[:200]
        return PushResult(channel=label, success=False, code=code, error=str(error))
    except Exception as e:
        return PushResult(channel=label, success=False, error=f"异常 {e}")


def send_brief(content: str, title: str = "秋招雷达日报"):
    """把简报推到所有已配置渠道，返回逐渠道 PushResult 列表（并打印一行日志）。
    没配任何渠道 → 返回空列表（编排层据此判定"非失败，只是没配"）。"""
    results = []

    sct_keys = _keys_from_env("PUSH_KEY")
    for i, key in enumerate(sct_keys, 1):
        label = f"Server酱#{i}" if len(sct_keys) > 1 else "Server酱"
        results.append(_push_serverchan(key, label, title, content))

    wecom_keys = _keys_from_env("WECOM_KEY")
    for i, key in enumerate(wecom_keys, 1):
        label = f"企业微信#{i}" if len(wecom_keys) > 1 else "企业微信"
        results.append(_push_wecom(key, label, title, content))

    feishu_webhooks = _keys_from_env("FEISHU_WEBHOOK")
    for i, webhook in enumerate(feishu_webhooks, 1):
        label = f"飞书#{i}" if len(feishu_webhooks) > 1 else "飞书"
        results.append(_push_feishu(webhook, label, title, content))

    if not results:
        print("未配置推送渠道（PUSH_KEY / WECOM_KEY / FEISHU_WEBHOOK），"
              "跳过推送。简报已保存到 reports/ 目录。")
    for r in results:
        print(f"  推送 {r.describe()}")
    return results
