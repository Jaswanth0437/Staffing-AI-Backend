"""
Outreach email sending via Microsoft Graph — pipeline step 6.

Auth flow: Azure App Registration -> Microsoft Entra ID OAuth2 client
credentials flow -> access token -> Graph API POST /users/{sender}/sendMail.

The access token (~1hr TTL) is cached process-wide and only refetched once
expired, so a burst of sends doesn't hit the token endpoint per-call.
"""

import time

import requests

from backend.config import settings

TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
GRAPH_SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Refresh a bit before actual expiry to avoid racing a token that dies mid-request.
TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 60

_cached_token: str | None = None
_cached_token_expires_at: float = 0.0


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


def send_email(sender: str, recipient: str, subject: str, body: str) -> dict:
    """Sends an email via Graph's sendMail on behalf of `sender`. Raises
    GraphEmailError with the actual Graph API error detail on failure.
    Returns a small summary dict on success (Graph's sendMail returns 202
    with an empty body on success, so there's nothing else to report)."""
    if not sender:
        raise GraphEmailError("GRAPH_SENDER_EMAIL is not configured")
    if not recipient:
        raise GraphEmailError("Email has no recipient set")

    token = get_access_token()

    payload = {
        "message": {
            "subject": subject or "",
            "body": {"contentType": "Text", "content": body or ""},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        },
        "saveToSentItems": True,
    }

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
