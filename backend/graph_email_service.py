"""
Outreach email sending via Microsoft Graph — pipeline step 6.

Auth flow: Azure App Registration -> Microsoft Entra ID OAuth2 client
credentials flow -> access token -> Graph API POST /users/{sender}/sendMail.

The access token (~1hr TTL) is cached process-wide and only refetched once
expired, so a burst of sends doesn't hit the token endpoint per-call.
"""

import base64
import html
import time
from pathlib import Path

import requests

from backend.config import settings

TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
GRAPH_SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Refresh a bit before actual expiry to avoid racing a token that dies mid-request.
TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 60

_cached_token: str | None = None
_cached_token_expires_at: float = 0.0

# Appended as an inline image below the text signature on the outreach send
# only (not the internal CEO notification) — a plain-text email can't embed
# an image, so this forces that one send into HTML with a CID attachment.
SIGNATURE_IMAGE_PATH = Path(__file__).parent / "assets" / "winfomi_signature.png"
SIGNATURE_IMAGE_CID = "winfomi-signature-logo"

_cached_signature_image_b64: str | None = None


def _get_signature_image_b64() -> str | None:
    global _cached_signature_image_b64
    if _cached_signature_image_b64 is None:
        try:
            _cached_signature_image_b64 = base64.b64encode(SIGNATURE_IMAGE_PATH.read_bytes()).decode("ascii")
        except OSError:
            return None
    return _cached_signature_image_b64


def _text_to_html(text: str) -> str:
    """Escapes a plain-text email body for safe embedding in an HTML
    message, preserving line breaks."""
    return html.escape(text or "").replace("\n", "<br>")


class GraphEmailError(Exception):
    pass


def get_access_token() -> str:
    """Returns a cached access token if still valid, otherwise fetches a new
    one via the OAuth2 client credentials flow. Raises GraphEmailError with
    the actual Entra ID error detail on auth failure."""
    global _cached_token, _cached_token_expires_at

    if _cached_token and time.time() < _cached_token_expires_at:
        return _cached_token

    if not (settings.AZURE_CLIENT_ID and settings.AZURE_CLIENT_SECRET and settings.AZURE_TENANT_ID):
        raise GraphEmailError(
            "Missing Azure credentials — set AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID in .env"
        )

    try:
        response = requests.post(
            TOKEN_URL.format(tenant_id=settings.AZURE_TENANT_ID),
            data={
                "client_id": settings.AZURE_CLIENT_ID,
                "client_secret": settings.AZURE_CLIENT_SECRET,
                "scope": GRAPH_SCOPE,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise GraphEmailError(f"Network error fetching Entra ID token: {exc}") from exc

    if response.status_code != 200:
        raise GraphEmailError(f"Entra ID token request failed: {response.status_code} {response.text}")

    data = response.json()
    token = data.get("access_token")
    expires_in = data.get("expires_in", 3600)
    if not token:
        raise GraphEmailError(f"Entra ID token response missing access_token: {data!r}")

    _cached_token = token
    _cached_token_expires_at = time.time() + expires_in - TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS
    return token


def send_email(sender: str, recipient: str, subject: str, body: str, include_signature_image: bool = False) -> dict:
    """Sends an email via Graph's sendMail on behalf of `sender`. Raises
    GraphEmailError with the actual Graph API error detail on failure.
    Returns a small summary dict on success (Graph's sendMail returns 202
    with an empty body on success, so there's nothing else to report).

    include_signature_image=True switches the message to HTML with the
    Winfomi/Salesforce-Partner logo attached inline below the text body —
    used for the actual outreach send, not the internal CEO notification,
    since that one's just a plain-text FYI copy."""
    if not sender:
        raise GraphEmailError("GRAPH_SENDER_EMAIL is not configured")
    if not recipient:
        raise GraphEmailError("Email has no recipient set")

    token = get_access_token()

    image_b64 = _get_signature_image_b64() if include_signature_image else None
    if image_b64:
        html_body = f'{_text_to_html(body)}<br><br><img src="cid:{SIGNATURE_IMAGE_CID}" alt="Winfomi - Salesforce CREST Partner">'
        message = {
            "subject": subject or "",
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
            "attachments": [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": "winfomi-signature.png",
                "contentType": "image/png",
                "contentBytes": image_b64,
                "isInline": True,
                "contentId": SIGNATURE_IMAGE_CID,
            }],
        }
    else:
        message = {
            "subject": subject or "",
            "body": {"contentType": "Text", "content": body or ""},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        }

    payload = {"message": message, "saveToSentItems": True}

    try:
        response = requests.post(
            GRAPH_SEND_MAIL_URL.format(sender=sender),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise GraphEmailError(f"Network error calling Graph sendMail: {exc}") from exc

    if response.status_code != 202:
        raise GraphEmailError(f"Graph sendMail failed: {response.status_code} {response.text}")

    return {"status_code": response.status_code, "sender": sender, "recipient": recipient}
