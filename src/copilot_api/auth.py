from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx


class GoogleOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


class GoogleOAuth:
    authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    userinfo_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"

    def __init__(self, config: GoogleOAuthConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls) -> GoogleOAuth:
        app_base_url = os.getenv("APP_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
        return cls(
            GoogleOAuthConfig(
                client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""),
                client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", ""),
                redirect_uri=os.getenv(
                    "GOOGLE_OAUTH_REDIRECT_URI",
                    f"{app_base_url}/auth/google/callback",
                ),
            )
        )

    def authorization_url(self, state: str, redirect_uri: str | None = None) -> str:
        self._require_config()
        effective_redirect_uri = redirect_uri or self.config.redirect_uri
        return f"{self.authorization_endpoint}?{urlencode({
            'client_id': self.config.client_id,
            'redirect_uri': effective_redirect_uri,
            'response_type': 'code',
            'scope': 'openid profile email',
            'state': state,
            'prompt': 'select_account',
        })}"

    def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, str]:
        self._require_config()
        effective_redirect_uri = redirect_uri or self.config.redirect_uri
        try:
            with httpx.Client(timeout=20) as client:
                token_response = client.post(
                    self.token_endpoint,
                    data={
                        "code": code,
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                        "redirect_uri": effective_redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )
                token_response.raise_for_status()
                access_token = token_response.json().get("access_token")
                if not access_token:
                    raise GoogleOAuthError("Google did not return an access token.")
                user_response = client.get(
                    self.userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                user_response.raise_for_status()
                profile = user_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GoogleOAuthError("Google sign-in could not be completed. Please try again.") from exc

        if not profile.get("sub") or not profile.get("email"):
            raise GoogleOAuthError("Google did not return the required account information.")
        if profile.get("email_verified") is False:
            raise GoogleOAuthError("Your Google email address must be verified.")
        return {
            "sub": str(profile["sub"]),
            "email": str(profile["email"]),
            "name": str(profile.get("name") or ""),
            "picture": str(profile.get("picture") or ""),
        }

    def new_state(self) -> str:
        return secrets.token_urlsafe(32)

    def _require_config(self) -> None:
        if not self.config.configured:
            raise GoogleOAuthError(
                "Google sign-in needs GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET."
            )
