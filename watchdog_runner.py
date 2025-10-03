# watchdog_runner.py
import argparse
import os
import subprocess
import sys
import time
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

def send_email(smtp_host, smtp_port, use_tls, user, password, from_addr, to_addr, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    if use_tls:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
        server.starttls()
    else:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)

    try:
        if user:
            server.login(user, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())
    finally:
        server.quit()

def tail_text(path, n=300):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return "(no log available)"

def main():
    p = argparse.ArgumentParser(description="Restart the GUI on crash and email an alert.")
    p.add_argument("--target", required=True, help="Path to your GUI script (the file with main()).")
    p.add_argument("--project", default="", help="Optional project file to force-open.")
    # Email settings (use env vars if you prefer)
    p.add_argument("--email-to", default=os.getenv("WATCHDOG_EMAIL_TO", ""))
    p.add_argument("--email-from", default=os.getenv("WATCHDOG_EMAIL_FROM", ""))
    p.add_argument("--smtp-host", default=os.getenv("WATCHDOG_SMTP_HOST", ""))
    p.add_argument("--smtp-port", type=int, default=int(os.getenv("WATCHDOG_SMTP_PORT", "587")))
    p.add_argument("--smtp-user", default=os.getenv("WATCHDOG_SMTP_USER", ""))
    p.add_argument("--smtp-pass", default=os.getenv("WATCHDOG_SMTP_PASS", ""))
    p.add_argument("--smtp-tls", action="store_true", default=os.getenv("WATCHDOG_SMTP_TLS", "1") not in ("0", "false", "False"))
    args, unknown = p.parse_known_args()

    cmd = [sys.executable, args.target]
    if args.project:
        cmd += ["--project", args.project]
    # pass through any extra flags after our known args
    cmd += unknown

    backoff = 5     # seconds, exponential up to 5 min
    max_backoff = 300

    # If a project was given, try to read its per-project log
    proj_log = ""
    if args.project and os.path.isfile(args.project):
        proj_dir = os.path.dirname(os.path.abspath(args.project))
        log_path = os.path.join(proj_dir, "logs", "app.log")
        proj_log = log_path if os.path.isfile(log_path) else ""

    while True:
        start = time.time()
        result = subprocess.run(cmd, capture_output=True, text=True)
        rc = result.returncode

        if rc == 0:
            # Clean exit — user closed the app. Stop watchdog.
            sys.exit(0)

        # Crash detected
        when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tail = tail_text(proj_log) if proj_log else (result.stderr or "")[-5000:]

        subject = f"[WATCHDOG] App crashed (rc={rc}) at {when}"
        body = (
            f"Target: {args.target}\n"
            f"Project: {args.project or '(none)'}\n"
            f"Return code: {rc}\n"
            f"Restarting in {backoff} seconds.\n\n"
            f"==== Log tail ====\n{tail}\n"
        )

        # Email if configured
        if args.email_to and args.email_from and args.smtp_host:
            try:
                send_email(
                    smtp_host=args.smtp_host,
                    smtp_port=args.smtp_port,
                    use_tls=args.smtp_tls,
                    user=args.smtp_user,
                    password=args.smtp_pass,
                    from_addr=args.email_from,
                    to_addr=args.email_to,
                    subject=subject,
                    body=body,
                )
            except Exception as e:
                # Don't block restart on mail errors
                print(f"[WATCHDOG] Email failed: {e}", file=sys.stderr)

        # Exponential backoff to avoid restart storms
        time.sleep(backoff)
        backoff = min(max_backoff, backoff * 2 if (time.time() - start) < 30 else 5)

if __name__ == "__main__":
    main()
