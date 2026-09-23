"""邮件发送：标准库 smtplib，支持 SSL / STARTTLS / 明文。

常见服务商（发信地址在前端“邮件设置”里配）：
  Gmail   smtp.gmail.com     465 SSL      密码用「应用专用密码」
  QQ 邮箱 smtp.qq.com        465 SSL      密码用「授权码」
  163     smtp.163.com       465 SSL      密码用「授权码」
  Outlook smtp.office365.com 587 STARTTLS
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate

log = logging.getLogger(__name__)

TIMEOUT = 25


def _recipients(email_cfg: dict, override: list[str] | None) -> list[str]:
    addrs = override if override is not None else email_cfg.get("to_addrs") or []
    if isinstance(addrs, str):
        addrs = [x.strip() for x in addrs.replace(";", ",").split(",")]
    return [a.strip() for a in addrs if a and a.strip()]


def validate(email_cfg: dict, override_to: list[str] | None = None) -> str | None:
    """返回错误信息，None 表示配置看起来可用。"""
    if not (email_cfg.get("smtp_host") or "").strip():
        return "没有填写 SMTP 服务器地址"
    if not _recipients(email_cfg, override_to):
        return "没有填写收件地址"
    if not (email_cfg.get("from_addr") or email_cfg.get("username") or "").strip():
        return "没有填写发件地址或用户名"
    return None


def send_mail(
    email_cfg: dict,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    override_to: list[str] | None = None,
) -> tuple[bool, str]:
    """发送一封邮件，返回 (是否成功, 说明)。不抛异常。"""
    problem = validate(email_cfg, override_to)
    if problem:
        return False, problem

    host = email_cfg["smtp_host"].strip()
    port = int(email_cfg.get("smtp_port") or 465)
    security = email_cfg.get("security") or "ssl"
    username = (email_cfg.get("username") or "").strip()
    password = email_cfg.get("password") or ""
    from_addr = (email_cfg.get("from_addr") or username).strip()
    to_addrs = _recipients(email_cfg, override_to)
    prefix = (email_cfg.get("subject_prefix") or "").strip()
    if prefix and not subject.startswith(prefix):
        subject = f"{prefix} {subject}"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("StockWatch", from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        if security == "ssl":
            context = ssl.create_default_context()
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if username and password:
                server.login(username, password)
            server.send_message(msg, from_addr=from_addr, to_addrs=to_addrs)
        return True, f"已发送给 {', '.join(to_addrs)}"
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"SMTP 认证失败（检查用户名/授权码）：{exc}"
    except Exception as exc:  # 网络、DNS、TLS 等各种问题统一回报给前端
        log.warning("发送邮件失败: %s", exc)
        return False, f"{type(exc).__name__}: {exc}"


def send_test(email_cfg: dict, override_to: list[str] | None = None) -> tuple[bool, str]:
    return send_mail(
        email_cfg,
        "测试邮件 / Test",
        "这是一封来自 StockWatch 的测试邮件，说明 SMTP 配置可用。",
        "<p>这是一封来自 <b>StockWatch</b> 的测试邮件，说明 SMTP 配置可用。</p>",
        override_to=override_to,
    )
