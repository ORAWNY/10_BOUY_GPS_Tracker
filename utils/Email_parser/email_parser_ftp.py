# utils/Email_parser/email_parser_ftp.py
from __future__ import annotations

import os
import ftplib
from typing import Optional, Callable, Any, Tuple, List


def split_remote_dir(p: str) -> List[str]:
    """Normalize a remote path to segments (used to cwd/mkd along the path)."""
    p = (p or "").strip().replace("\\", "/")
    p = p.strip("/")
    return [seg for seg in p.split("/") if seg and seg != "."]


class FTPSession:
    """
    Simple FTP/FTPS session helper. Lazily connects on first upload.

    `cfg` is expected to have:
      ftp_host, ftp_port, ftp_username, ftp_password, ftp_remote_dir,
      ftp_use_tls, ftp_passive, ftp_timeout, quiet
    """

    def __init__(self, cfg: Any, logger: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.logger = logger
        self.ftp: Optional[ftplib.FTP] = None

    def _log(self, msg: str):
        if self.logger and not getattr(self.cfg, "quiet", True):
            self.logger(msg)

    def _ensure_connected(self):
        if self.ftp is not None:
            return
        if not (self.cfg.ftp_host and self.cfg.ftp_username):
            raise RuntimeError("FTP settings are incomplete. Host and username are required.")
        cls = ftplib.FTP_TLS if getattr(self.cfg, "ftp_use_tls", False) else ftplib.FTP
        ftp = cls()
        ftp.connect(
            self.cfg.ftp_host,
            int(getattr(self.cfg, "ftp_port", 21) or 21),
            timeout=int(getattr(self.cfg, "ftp_timeout", 20) or 20),
        )
        ftp.login(self.cfg.ftp_username, getattr(self.cfg, "ftp_password", "") or "")
        if isinstance(ftp, ftplib.FTP_TLS):
            try:
                ftp.prot_p()  # secure data channel
            except Exception:
                pass
        try:
            ftp.set_pasv(bool(getattr(self.cfg, "ftp_passive", True)))
        except Exception:
            pass
        # CWD / ensure remote dir
        remote_dir = getattr(self.cfg, "ftp_remote_dir", "") or ""
        if remote_dir:
            for seg in split_remote_dir(remote_dir):
                try:
                    ftp.cwd(seg)
                except Exception:
                    try:
                        ftp.mkd(seg)
                    except Exception:
                        # race or permissions; attempt cwd anyway
                        pass
                    ftp.cwd(seg)
        self.ftp = ftp
        self._log(
            f"FTP connected: {self.cfg.ftp_host}:{getattr(self.cfg, 'ftp_port', 21)} "
            f"(TLS={'YES' if getattr(self.cfg, 'ftp_use_tls', False) else 'NO'})"
        )

    def test_connection(self) -> Tuple[bool, str]:
        try:
            self._ensure_connected()
            try:
                self.ftp.voidcmd("NOOP")
            except Exception:
                pass
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def upload(self, local_path: str, remote_name: Optional[str] = None):
        self._ensure_connected()
        rn = remote_name or os.path.basename(local_path)
        with open(local_path, "rb") as f:
            self.ftp.storbinary(f"STOR {rn}", f)
        self._log(f"FTP uploaded: {rn}")

    def close(self):
        try:
            if self.ftp is not None:
                try:
                    self.ftp.quit()
                except Exception:
                    try:
                        self.ftp.close()
                    except Exception:
                        pass
        finally:
            self.ftp = None

    def __enter__(self) -> "FTPSession":
        # Do not connect here; connect lazily or via test_connection
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
