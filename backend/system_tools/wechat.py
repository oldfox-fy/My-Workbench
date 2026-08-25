# backend/system_tools/wechat.py
# 微信外发（P3）：通过 Server酱 / PushPlus 免费 Webhook，把消息推送到用户自己的微信。
# 两者都是「自己给自己发通知」的服务（无需对方授权），适合提醒/告警推送。
#
# 配置示例（app_config.yaml）：
#   wechat:
#     enabled: true
#     service: "pushplus"             # pushplus | serverchan
#     pushplus_token: ""              # PushPlus 的 token
#     serverchan_sendkey: ""          # Server酱 的 SendKey（SCT...）
from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger("My Workbench")


async def send_wechat(title: str, content: str) -> Dict[str, Any]:
    """把一条消息推送到用户自己的微信（Server酱 或 PushPlus）。"""
    from config_loader import config

    title = (title or "").strip() or "通知"
    content = content or ""

    if not config.wechat_enabled:
        return {"success": False, "content": "微信推送未启用，请先在 app_config.yaml 的 wechat 节配置并开启"}

    service = (config.wechat_service or "pushplus").strip().lower()
    try:
        if service == "serverchan":
            sendkey = (config.wechat_serverchan_sendkey or "").strip()
            if not sendkey:
                return {"success": False, "content": "Server酱 SendKey 未配置（wechat.serverchan_sendkey）"}
            url = f"https://sctapi.ftqq.com/{sendkey}.send"
            data = {"title": title, "desp": content}
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, data=data)
            # Server酱：code=0 表示成功
            resp = r.json()
            ok = int(resp.get("code", -1)) == 0
            msg = resp.get("message") or resp.get("data") or ("发送成功" if ok else "发送失败")
        else:  # pushplus
            token = (config.wechat_pushplus_token or "").strip()
            if not token:
                return {"success": False, "content": "PushPlus token 未配置（wechat.pushplus_token）"}
            url = "https://www.pushplus.plus/send"
            payload = {"token": token, "title": title, "content": content, "template": "txt"}
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=payload)
            # PushPlus：code=200 表示成功
            resp = r.json()
            ok = int(resp.get("code", -1)) == 200
            msg = resp.get("msg") or ("发送成功" if ok else "发送失败")

        if ok:
            return {"success": True, "content": f"微信推送成功（{service}）：{msg}"}
        return {"success": False, "content": f"微信推送失败（{service}）：{msg}"}
    except Exception as e:
        logger.warning(f"[Wechat] 推送失败：{e}")
        return {"success": False, "content": f"微信推送异常：{e}"}
