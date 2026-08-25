# backend/system_tools/email.py
# 邮件外发（P3）：通过 SMTP 发送邮件。账号信息从 app_config.yaml 的 email 节读取。
#
# 配置示例（app_config.yaml）：
#   email:
#     enabled: true
#     smtp_host: "smtp.qq.com"      # QQ 邮箱示例
#     smtp_port: 465
#     smtp_user: "你的QQ邮箱"
#     smtp_pass: "授权码"            # SMTP 授权码，非登录密码
#     from_addr: ""                  # 留空则用 smtp_user
#     use_ssl: true                  # true=SSL(465)；false=STARTTLS(587)
from __future__ import annotations

import asyncio
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Dict


def _send_blocking(host: str, port: int, user: str, password: str,
                   from_addr: str, to: str, subject: str, body: str, use_ssl: bool) -> None:
    """（阻塞）连接 SMTP 并发送一封纯文本邮件。"""
    import smtplib

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject or "", "utf-8")
    msg["From"] = from_addr
    msg["To"] = to

    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        server.starttls()
    try:
        server.login(user, password)
        server.sendmail(from_addr, [to], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send_email(to: str, subject: str, body: str) -> Dict[str, Any]:
    """通过配置的 SMTP 账户发送一封邮件给指定收件人。"""
    from config_loader import config

    to = (to or "").strip()
    subject = (subject or "").strip()
    body = body or ""

    if not config.email_enabled:
        return {"success": False, "content": "邮件功能未启用，请先在 app_config.yaml 的 email 节配置并开启"}
    if not to:
        return {"success": False, "content": "收件人（to）不能为空"}
    if not config.email_smtp_host or not config.email_smtp_user or not config.email_smtp_pass:
        return {"success": False, "content": "SMTP 配置不完整（需 smtp_host / smtp_user / smtp_pass）"}

    from_addr = (config.email_from or config.email_smtp_user).strip()
    try:
        await asyncio.to_thread(
            _send_blocking,
            config.email_smtp_host,
            int(config.email_smtp_port or 465),
            config.email_smtp_user,
            config.email_smtp_pass,
            from_addr,
            to,
            subject,
            body,
            bool(config.email_use_ssl),
        )
        return {"success": True, "content": f"邮件已发送给 {to}"}
    except Exception as e:
        return {"success": False, "content": f"邮件发送失败：{e}"}
