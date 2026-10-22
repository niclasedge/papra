"""Fetch mail attachments from Gmail into Papra's ingestion folder.

Every INTERVAL seconds: each message matching GMAIL_QUERY (default: the label
`Papra`) that does not yet carry GMAIL_DONE_LABEL has its attachments written
to /ingestion/<PAPRA_ORG_ID>/, then gets GMAIL_DONE_LABEL added. A message
without attachments (order confirmation, receipt) is written as one .md file
instead: headers plus body text, which Papra indexes as text. No label is
removed, so a query on `label:Rechnungen` leaves the tax export
(~/GITHUB/gmail/export_invoices.py) untouched. A message whose files could not
be written stays unlabelled and is retried on the next round, so a sleeping Mac
loses nothing.

Uses the Gmail REST API with the OAuth client of ~/GITHUB/gmail (scope
gmail.modify): no app password, no IMAP. Gmail's IMAP would need the broader
https://mail.google.com/ scope.

Files are written to /ingestion/.staging first (outside every org folder, so
Papra ignores it) and renamed into place, so the watcher never sees half a file.

Stdlib only; runs in python:3.13-alpine. Config via environment.
"""

import base64
import email
import email.policy
import email.utils
import json
import os
import re
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
SECRET_ENV = "GMAIL_CLIENT_SECRET"
CLIENT_SECRET = os.environ.get(SECRET_ENV, "")
REFRESH_ENV = "GMAIL_REFRESH_TOKEN"
REFRESH_TOKEN = os.environ.get(REFRESH_ENV, "")
QUERY = os.environ.get("GMAIL_QUERY", "label:Papra")
DONE_LABEL = os.environ.get("GMAIL_DONE_LABEL", "Papra/Done")
ORG_ID = os.environ.get("PAPRA_ORG_ID", "")
INTERVAL = int(os.environ.get("INTERVAL", "300"))
ROOT = Path(os.environ.get("INGESTION_ROOT", "/ingestion"))

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
AUTH_SCHEME = "Bearer"

# Calendar invites, signatures and contact cards are never documents.
SKIP_SUFFIXES = {".ics", ".p7s", ".asc", ".sig", ".vcf"}


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def access_token() -> str:
    body = urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    with urlopen(Request(TOKEN_URL, data=body), timeout=30) as resp:
        return json.load(resp)["access_token"]


def api(token: str, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    url = f"{API}/{path}" + (f"?{urlencode(params)}" if params else "")
    headers = {"Authorization": " ".join((AUTH_SCHEME, token)), "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(Request(url, data=data, method=method, headers=headers), timeout=60) as resp:
        return json.load(resp)


def done_label_id(token: str) -> str:
    for label in api(token, "GET", "labels").get("labels", []):
        if label["name"] == DONE_LABEL:
            return label["id"]
    return api(token, "POST", "labels", body={"name": DONE_LABEL})["id"]


def message_ids(token: str) -> list[str]:
    # Gmail search spells nested labels with dashes: Papra/Done -> papra-done.
    done = re.sub(r"[/\s]", "-", DONE_LABEL.lower())
    params = {"q": f"({QUERY}) -label:{done}", "maxResults": 500}
    ids = []
    while True:
        page = api(token, "GET", "messages", params)
        ids += [m["id"] for m in page.get("messages", [])]
        if "nextPageToken" not in page:
            return ids
        params["pageToken"] = page["nextPageToken"]


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or "attachment"


def attachments(msg: email.message.EmailMessage):
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        # Inline images are logos and signatures, not documents.
        if part.get_content_disposition() == "inline" and part.get_content_maintype() == "image":
            continue
        if Path(filename).suffix.lower() in SKIP_SUFFIXES:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            yield filename, payload


class HtmlText(HTMLParser):
    """Visible text of an HTML mail, one line per block element."""

    BLOCK = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def body_text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):  # unknown or broken charset
        text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_subtype() == "html":
        parser = HtmlText()
        parser.feed(text)
        text = "".join(parser.parts)
    # Newsletter templates pad the preview line with invisible joiners.
    text = re.sub(r"[­͏​-‏⁠﻿]", "", text)
    lines = (" ".join(line.split()) for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def as_markdown(msg: email.message.EmailMessage) -> str:
    head = [f"# {msg.get('Subject', '')}", ""]
    head += [f"- {name}: {msg[header]}" for name, header in (("Von", "From"), ("An", "To"), ("Datum", "Date")) if msg[header]]
    return "\n".join(head) + "\n\n" + body_text(msg) + "\n"


def prefix(msg: email.message.EmailMessage) -> str:
    try:
        day = email.utils.parsedate_to_datetime(msg["Date"]).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        day = datetime.now().strftime("%Y-%m-%d")
    sender = email.utils.parseaddr(str(msg.get("From", "")))[1]
    domain = sender.rpartition("@")[2] or "unbekannt"
    # "Mail" is matched by the Papra tagging rule "Mail" (name contains Mail).
    return f"{day} Mail {domain}"


def unique(path: Path) -> Path:
    n = 1
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def write(target_dir: Path, staging: Path, name: str, data: bytes) -> Path:
    tmp = staging / f"{os.getpid()}-{time.time_ns()}"
    tmp.write_bytes(data)
    final = unique(target_dir / safe_name(name))
    os.rename(tmp, final)
    return final


def run_once() -> None:
    target_dir = ROOT / ORG_ID
    staging = ROOT / ".staging"
    target_dir.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)

    token = access_token()
    done_id = done_label_id(token)
    for msg_id in message_ids(token):
        raw = api(token, "GET", f"messages/{quote(msg_id)}", {"format": "raw"})["raw"]
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
        subject = str(msg.get("Subject", ""))[:80]
        written = [
            write(target_dir, staging, f"{prefix(msg)} - {filename}", payload)
            for filename, payload in attachments(msg)
        ] or [write(target_dir, staging, f"{prefix(msg)} - {subject or 'Mail'}.md", as_markdown(msg).encode())]
        api(token, "POST", f"messages/{quote(msg_id)}/modify", body={"addLabelIds": [done_id]})
        log(f"{len(written)} file(s) from {subject!r}: " + ", ".join(p.name for p in written))


def main() -> None:
    config = {"GMAIL_CLIENT_ID": CLIENT_ID, SECRET_ENV: CLIENT_SECRET, REFRESH_ENV: REFRESH_TOKEN, "PAPRA_ORG_ID": ORG_ID}
    missing = [k for k, v in config.items() if not v]
    if missing:
        log(f"idle: {', '.join(missing)} not set")
        while True:
            time.sleep(3600)
    log(f"fetching {QUERY!r} -> {ROOT / ORG_ID} every {INTERVAL}s")
    while True:
        try:
            run_once()
        except (OSError, KeyError, ValueError) as exc:
            # Offline after sleep, Gmail hiccup: the mail stays put, next round retries.
            # urllib's HTTPError/URLError are OSErrors.
            log(f"round failed: {exc}")
        sys.stdout.flush()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
