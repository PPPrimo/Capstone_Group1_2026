"""Issue coturn REST credentials; the shared secret never leaves the backend."""

import base64
import hmac
import os
import time

from aiortc import RTCConfiguration, RTCIceServer


def turn_settings() -> tuple[str, list[str], int]:
    secret = os.environ["TURN_SHARED_SECRET"]
    urls = [url.strip() for url in os.environ["TURN_URLS"].split(",") if url.strip()]
    if not secret or not urls or any(not url.startswith(("turn:", "turns:")) for url in urls):
        raise ValueError("TURN_SHARED_SECRET and valid TURN_URLS must both be configured")
    ttl = int(os.getenv("TURN_CREDENTIAL_TTL", "3600"))
    if not 300 <= ttl <= 86400:
        raise ValueError("TURN_CREDENTIAL_TTL must be between 300 and 86400 seconds")
    return secret, urls, ttl


def ice_credentials(subject: str) -> dict:
    secret, urls, ttl = turn_settings()
    expires_at = int(time.time()) + ttl
    username = f"{expires_at}:{subject}"
    credential = base64.b64encode(
        hmac.digest(secret.encode(), username.encode(), "sha1")
    ).decode("ascii")
    return {
        "iceServers": [{"urls": urls, "username": username, "credential": credential}],
        "expiresAt": expires_at,
    }


def peer_configuration(subject: str) -> RTCConfiguration:
    return RTCConfiguration(iceServers=[
        RTCIceServer(**item) for item in ice_credentials(subject)["iceServers"]
    ])
