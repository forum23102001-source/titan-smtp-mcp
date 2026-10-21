"""
Titan SMTP connector (MCP server) for Render.

Adds the missing "send" ability next to the Titan Mail connector:
  - titan_smtp_list_drafts   : list drafts in the Titan Drafts folder
  - titan_smtp_send_drafts   : send drafts over SMTP (dry_run is ON by default)
Each sent draft is copied to Sent and removed from Drafts.

Environment variables (set in the Render dashboard, never in code):
  TITAN_USER   mailbox address, e.g. karan@saviturlifescience.com
  TITAN_PASS   mailbox password (or Titan app password)
  MCP_SECRET   long random string; it becomes part of the URL and acts as the key
Optional: TITAN_IMAP_HOST, TITAN_SMTP_HOST, TITAN_SMTP_PORT (465 SSL or 587 STARTTLS)
"""
import email
import imaplib
import os
import smtplib
import time
from email import policy
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime

from mcp.server.fastmcp import FastMCP
from starlette.responses import PlainTextResponse

USER = os.environ["TITAN_USER"]
PASSWORD = os.environ["TITAN_PASS"]
SECRET = os.environ["MCP_SECRET"]
IMAP_HOST = os.getenv("TITAN_IMAP_HOST", "imap.titan.email")
SMTP_HOST = os.getenv("TITAN_SMTP_HOST", "smtp.titan.email")
SMTP_PORT = int(os.getenv("TITAN_SMTP_PORT", "465"))
DRAFTS, SENT = "Drafts", "Sent"
PAUSE = 3  # seconds between emails inside one call

mcp = FastMCP(
    "titan-smtp",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "10000")),
    stateless_http=True,
    streamable_http_path=f"/{SECRET}/mcp",
)


@mcp.custom_route("/healthz", methods=["GET"])
async def health(_request):
    return PlainTextResponse("ok")


def _imap():
    m = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    m.login(USER, PASSWORD)
    m.select(DRAFTS)
    return m


def _passes(msg, subject_prefix, required_cc, since):
    if not [a for _, a in getaddresses([str(msg["To"] or ""), str(msg["Cc"] or ""), str(msg["Bcc"] or "")]) if a]:
        return False
    if subject_prefix and not str(msg["Subject"] or "").startswith(subject_prefix):
        return False
    if required_cc and required_cc.lower() not in str(msg["Cc"] or "").lower():
        return False
    if since:
        try:
            if parsedate_to_datetime(msg["Date"]).date().isoformat() < since:
                return False
        except Exception:
            return False
    return True


def _drafts(imap, subject_prefix, required_cc, since, headers_only):
    _, data = imap.uid("search", None, "UNDELETED")
    out = []
    part = "(BODY.PEEK[HEADER])" if headers_only else "(RFC822)"
    for uid in data[0].split():
        _, d = imap.uid("fetch", uid, part)
        if not d or not d[0]:
            continue
        msg = email.message_from_bytes(d[0][1], policy=policy.default)
        if _passes(msg, subject_prefix, required_cc, since):
            out.append((uid.decode(), msg))
    return out


@mcp.tool()
def titan_smtp_list_drafts(subject_prefix: str = "", required_cc: str = "", since: str = "") -> dict:
    """List drafts that have at least one recipient. Optional filters: subject_prefix (e.g. 'RFQ:'),
    required_cc (an address that must be in CC), since (YYYY-MM-DD, draft date on/after)."""
    imap = _imap()
    try:
        rows = _drafts(imap, subject_prefix, required_cc, since, headers_only=True)
    finally:
        imap.logout()
    return {"count": len(rows),
            "drafts": [{"uid": u, "to": str(m["To"]), "cc": str(m["Cc"]), "subject": str(m["Subject"])} for u, m in rows]}


@mcp.tool()
def titan_smtp_send_drafts(dry_run: bool = True, limit: int = 10, subject_prefix: str = "",
                           required_cc: str = "", since: str = "", uids: list[str] | None = None) -> dict:
    """Send drafts from the Titan Drafts folder over SMTP. dry_run=True only previews (default).
    limit = max emails this call (keep <= 25); call again until remaining is 0.
    Filters as in titan_smtp_list_drafts. uids = send only these draft ids."""
    imap = _imap()
    try:
        rows = _drafts(imap, subject_prefix, required_cc, since, headers_only=dry_run)
        if uids:
            keep = set(map(str, uids))
            rows = [r for r in rows if r[0] in keep]
        total = len(rows)
        batch = rows[: max(1, min(limit, 25))]
        if dry_run:
            return {"dry_run": True, "matching_total": total, "would_send_now": len(batch),
                    "items": [{"to": str(m["To"]), "subject": str(m["Subject"])} for _, m in batch]}
        if SMTP_PORT == 465:
            smtp = smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=60)
        else:
            smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)
            smtp.starttls()
        smtp.login(USER, PASSWORD)
        results = []
        for i, (uid, msg) in enumerate(batch):
            try:
                for h in ("Date", "Message-ID", "X-Unsent"):
                    if msg[h]:
                        del msg[h]
                msg["Date"] = formatdate(localtime=True)
                msg["Message-ID"] = make_msgid(domain=USER.split("@")[-1])
                if not msg["From"]:
                    msg["From"] = USER
                rcpts = [a for _, a in getaddresses([str(msg["To"] or ""), str(msg["Cc"] or ""), str(msg["Bcc"] or "")]) if a]
                smtp.send_message(msg, from_addr=USER, to_addrs=rcpts)
                try:
                    imap.append(SENT, "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
                except Exception:
                    pass
                imap.uid("store", uid, "+FLAGS", "\\Deleted")
                results.append({"to": str(msg["To"]), "subject": str(msg["Subject"]), "status": "sent"})
            except Exception as e:
                results.append({"to": str(msg["To"]), "subject": str(msg["Subject"]), "status": f"FAILED: {e}"})
            if i < len(batch) - 1:
                time.sleep(PAUSE)
        imap.expunge()
        try:
            smtp.quit()
        except Exception:
            pass
        sent = sum(1 for r in results if r["status"] == "sent")
        return {"dry_run": False, "sent": sent, "failed": len(results) - sent,
                "remaining": total - sent, "items": results}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
