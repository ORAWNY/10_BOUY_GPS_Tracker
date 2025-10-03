# utils/alerts/emailer.py
from __future__ import annotations
from typing import List, Optional
import os

# Try Outlook
try:
    import win32com.client as win32  # type: ignore
except Exception:
    win32 = None

OUTLOOK_ACCOUNT_DISPLAY_NAME = "Metocean Configuration"

def send_email_outlook(subject: str, body: str, recipients: List[str], attachment_path: Optional[str] = None):
    if win32 is None:
        raise RuntimeError("pywin32 is not installed. pip install pywin32")

    outlook = win32.Dispatch("Outlook.Application")
    session = outlook.GetNamespace("MAPI")
    mail = outlook.CreateItem(0)
    mail.Subject = subject
    mail.Body = body
    mail.To = "; ".join(recipients)
    mail.SentOnBehalfOfName = OUTLOOK_ACCOUNT_DISPLAY_NAME

    # Try bind to the configured account
    try:
        for acc in session.Accounts:
            if str(acc.DisplayName).strip().lower() == OUTLOOK_ACCOUNT_DISPLAY_NAME.strip().lower():
                try:
                    mail.SendUsingAccount = acc
                except Exception:
                    try:
                        mail._oleobj_.Invoke(*(64209, 0, 8, 0, acc))
                    except Exception:
                        pass
                break
    except Exception:
        pass

    if attachment_path and os.path.exists(attachment_path):
        mail.Attachments.Add(attachment_path)
    mail.Send()
