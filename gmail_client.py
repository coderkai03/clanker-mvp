import base64
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailClient:
    def __init__(self) -> None:
        self._service = None

    @property
    def service(self):
        if self._service is None:
            self._service = self._authenticate()
        return self._service

    def _authenticate(self):
        token_path = os.getenv("GMAIL_TOKEN_PATH", "token.json")
        creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
        creds: Credentials | None = None

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(creds_path):
                    raise FileNotFoundError(
                        f"credentials.json not found at {creds_path}. "
                        "Download it from Google Cloud Console > APIs & Services > Credentials."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_unread_messages(self, max_results: int = 20) -> list[dict]:
        """Fetch full message objects for all unread inbox messages."""
        result = (
            self.service.users()
            .messages()
            .list(userId="me", q="is:unread in:inbox", maxResults=max_results)
            .execute()
        )
        raw_list = result.get("messages", [])
        full_messages = []
        for msg in raw_list:
            full = (
                self.service.users()
                .messages()
                .get(userId="me", id=msg["id"], format="full")
                .execute()
            )
            full_messages.append(full)
        return full_messages

    def mark_as_read(self, message_id: str) -> None:
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()

    def start_watch(
        self,
        topic_name: str,
        label_ids: list[str] | None = None,
    ) -> dict:
        """Register a Gmail push-notification watch on the user's mailbox.

        Returns the API response containing `historyId` and `expiration`
        (ms epoch, ~7 days out). Callers must renew before expiration.
        """
        body: dict = {
            "topicName": topic_name,
            "labelIds": label_ids or ["INBOX"],
        }
        return self.service.users().watch(userId="me", body=body).execute()

    def stop_watch(self) -> None:
        """Cancel any active Gmail push-notification watch for this user."""
        self.service.users().stop(userId="me").execute()

    def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
    ) -> dict:
        """Send a plain-text email, optionally within an existing thread."""
        msg = MIMEMultipart("alternative")
        msg["to"] = to
        msg["subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        sent = self.service.users().messages().send(userId="me", body=payload).execute()
        return {"message_id": sent["id"], "thread_id": sent.get("threadId")}
