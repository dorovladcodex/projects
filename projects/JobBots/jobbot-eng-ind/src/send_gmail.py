from __future__ import annotations

import argparse
import base64
from email.message import EmailMessage
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def build_gmail_service(client_file: Path, token_file: Path):
    credentials = None
    if token_file.exists():
        credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
        credentials = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(credentials.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a JobBot Eng Ind report through Gmail API")
    parser.add_argument("--to", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body-file", required=True, type=Path)
    parser.add_argument("--attachment", action="append", default=[], type=Path)
    parser.add_argument("--google-client-file", type=Path, default=Path("credentials/google-oauth-client.json"))
    parser.add_argument("--gmail-token-file", type=Path, default=Path("credentials/gmail-token.json"))
    args = parser.parse_args()

    message = EmailMessage()
    message["To"] = args.to
    message["Subject"] = args.subject
    message.set_content(args.body_file.read_text(encoding="utf-8"))
    for attachment in args.attachment:
        data = attachment.read_bytes()
        message.add_attachment(
            data,
            maintype="application",
            subtype="octet-stream",
            filename=attachment.name,
        )

    service = build_gmail_service(args.google_client_file, args.gmail_token_file)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    result = service.users().messages().send(userId="me", body={"raw": encoded}).execute()
    print(f"Sent Gmail message: {result.get('id')}")


if __name__ == "__main__":
    main()
