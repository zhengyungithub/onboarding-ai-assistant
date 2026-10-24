"""邮件发送服务（SMTP）"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def _get_smtp_config():
    return {
        "host": os.getenv("SMTP_HOST", "smtp.qq.com"),
        "port": int(os.getenv("SMTP_PORT", "465")),
        "user": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from_addr": os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")),
    }


def send_email(to_email: str, subject: str, body: str) -> bool:
    """发送纯文本邮件，返回是否成功"""
    cfg = _get_smtp_config()
    if not cfg["user"] or not cfg["password"]:
        print("[邮件] SMTP 未配置，跳过发送")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["from_addr"]
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        use_ssl = cfg["port"] in (465,)
        if use_ssl:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
            server.starttls()

        server.login(cfg["user"], cfg["password"])
        server.sendmail(cfg["from_addr"], [to_email], msg.as_string())
        server.quit()
        print(f"[邮件] 已发送验证码到 {to_email}")
        return True
    except Exception as e:
        print(f"[邮件] 发送失败: {e}")
        return False


def send_reset_password_code(to_email: str, code: str) -> bool:
    """发送重置密码验证码邮件"""
    subject = "【入职AI业务问答助手】密码重置验证码"
    body = f"""您好！

您正在申请重置登录密码，验证码为：

    {code}

验证码 10 分钟内有效，请勿泄露给他人。

如非本人操作，请忽略此邮件。"""
    return send_email(to_email, subject, body)
