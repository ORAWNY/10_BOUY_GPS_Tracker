"""
utils/Email_parser/email_parser_outlook.py
==========================================
Outlook / MAPI access helpers.

Wraps win32com so callers never import it directly.  Gracefully raises
RuntimeError when pywin32 is not installed.

Previously inlined in email_parser_core.py.
"""
from __future__ import annotations

import time
from typing import List

try:
    import win32com.client  # type: ignore
except Exception:
    win32com = None  # type: ignore


def _get_namespace():
    if win32com is None:
        raise RuntimeError("pywin32 is not available. Install 'pywin32' to use the Outlook parser.")
    return win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")


def get_sender_email(msg) -> str:
    """Extract the SMTP sender address from an Outlook mail item; falls back to SenderName."""
    try:
        addr = getattr(msg, "SenderEmailAddress", "") or ""
        if addr:
            return addr.strip()
    except Exception:
        pass
    try:
        sender = getattr(msg, "Sender", None)
        if sender:
            ex_user = sender.GetExchangeUser()
            if ex_user:
                smtp = ex_user.PrimarySmtpAddress
                if smtp:
                    return smtp.strip()
    except Exception:
        pass
    try:
        return (getattr(msg, "SenderName", "") or "").strip()
    except Exception:
        return ""


def resolve_mailbox(mailbox_name: str):
    """Return the Outlook Folder object for *mailbox_name*, retrying up to ~3 s."""
    ns = _get_namespace()
    last_err = None
    for _ in range(6):
        try:
            return ns.Folders.Item(mailbox_name)
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"Mailbox '{mailbox_name}' not found or busy:\n{last_err}")


def _resolve_child(mailbox, parent, name: str):
    """Case-insensitive, Inbox-safe child folder resolution."""
    target = (name or "").strip()
    if not target:
        return parent
    try:
        if target.lower() == "inbox":
            return mailbox.GetDefaultFolder(6)
    except Exception:
        pass
    try:
        return parent.Folders[target]
    except Exception:
        try:
            for i in range(1, parent.Folders.Count + 1):
                f = parent.Folders.Item(i)
                if f.Name.strip().lower() == target.lower():
                    return f
        except Exception:
            pass
        raise


def resolve_folder_path(mailbox, path: List[str]):
    """Walk *path* segments from *mailbox* root and return the target folder."""
    f = mailbox
    for seg in path:
        f = _resolve_child(mailbox, f, seg)
    return f


def list_outlook_folder_paths(mailbox_name: str,
                              max_depth: int = 6,
                              max_count: int = 2000) -> List[List[str]]:
    """Return all folder paths under *mailbox_name* up to *max_depth* levels deep."""
    m = resolve_mailbox(mailbox_name)
    paths: List[List[str]] = []

    def walk(folder, prefix: List[str], depth: int):
        if depth > max_depth:
            return
        try:
            count = folder.Folders.Count
        except Exception:
            return
        for i in range(1, count + 1):
            if len(paths) >= max_count:
                return
            child = folder.Folders.Item(i)
            name  = (child.Name or "").strip()
            cur   = prefix + [name]
            paths.append(cur)
            walk(child, cur, depth + 1)

    walk(m, [], 0)
    return paths
