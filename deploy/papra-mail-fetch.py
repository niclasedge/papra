"""Fetch mail attachments from an IMAP folder into Papra's ingestion folder.

Every INTERVAL seconds: each message in IMAP_FOLDER (Gmail: the label `Papra`)
has its attachments written to /ingestion/<PAPRA_ORG_ID>/, then the message is
moved to IMAP_DONE_FOLDER (Gmail: label `Papra/Done`). A message whose files
could not be written stays in IMAP_FOLDER and is retried on the next round, so
a sleeping Mac loses nothing: the mail simply waits in the mailbox.

Files are written to /ingestion/.staging first (outside every org folder, so
Papra ignores it) and renamed into place, so the watcher never sees half a file.

Stdlib only; runs in python:3.13-alpine. Config via environment.
"""

import email
import email.policy
import email.utils
import imaplib
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
USER = os.environ.get("IMAP_USER", "")
PASSWORD_ENV = "IMAP_PASSWORD"
PASSWORD = os.environ.get(PASSWORD_ENV, "")
FOLDER = os.environ.get("IMAP_FOLDER", "Papra")
DONE_FOLDER = os.environ.get("IMAP_DONE_FOLDER", "Papra/Done")
ORG_ID = os.environ.get("PAPRA_ORG_ID", "")
INTERVAL = int(os.environ.get("INTERVAL", "300"))
ROOT = Path(os.environ.get("INGESTION_ROOT", "/ingestion"))

# Calendar invites, signatures and contact cards are never documents.
SKIP_SUFFIXES = {".ics", ".p7s", ".asc", ".sig", ".vcf"}


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def quote(folder: str) -> str:
    return '"' + folder.replace('"', '\\"') + '"'


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


def prefix(msg: email.message.EmailMessage) -> str:
    try:
        day = email.utils.parsedate_to_datetime(msg["Date"]).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        day = datetime.now().strftime("%Y-%m-%d")
    sender = email.utils.parseaddr(str(msg.get("From", "")))[1]
    domain = sender.rpartition("@")[2] or "unbekannt"
    return f"{day} {domain}"


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

    with imaplib.IMAP4_SSL(HOST) as imap:
        imap.login(USER, PASSWORD)
        imap.create(quote(DONE_FOLDER))  # NO when it exists, which is fine
        status, _ = imap.select(quote(FOLDER))
        if status != "OK":
            log(f"folder {FOLDER!r} not found, create the label in the mailbox")
            return
        _, data = imap.uid("SEARCH", None, "ALL")
        uids = data[0].split()
        for uid in uids:
            _, fetched = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((item[1] for item in fetched if isinstance(item, tuple)), None)
            if raw is None:
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            subject = str(msg.get("Subject", ""))[:80]
            written = [
                write(target_dir, staging, f"{prefix(msg)} - {filename}", payload)
                for filename, payload in attachments(msg)
            ]
            status, _ = imap.uid("MOVE", uid, quote(DONE_FOLDER))
            if status != "OK":
                log(f"move failed for {subject!r}, files already written: {len(written)}")
                continue
            if written:
                log(f"{len(written)} file(s) from {subject!r}: " + ", ".join(p.name for p in written))
            else:
                log(f"no attachment in {subject!r}, moved to {DONE_FOLDER}")


def main() -> None:
    missing = [k for k, v in {"IMAP_USER": USER, "IMAP_PASSWORD": PASSWORD, "PAPRA_ORG_ID": ORG_ID}.items() if not v]
    if missing:
        log(f"idle: {', '.join(missing)} not set")
        while True:
            time.sleep(3600)
    log(f"fetching {USER} {FOLDER!r} -> {ROOT / ORG_ID} every {INTERVAL}s")
    while True:
        try:
            run_once()
        except (OSError, imaplib.IMAP4.error) as exc:
            # Offline after sleep, Gmail hiccup: the mail stays put, next round retries.
            log(f"round failed: {exc}")
        sys.stdout.flush()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
