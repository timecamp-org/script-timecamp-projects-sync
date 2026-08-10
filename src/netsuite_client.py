import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import requests

NETSUITE_REQUEST_TIMEOUT = 30
NETSUITE_SUITEQL_PAGE_SIZE = 1000
NETSUITE_MAX_SUITEQL_RESULTS = 100_000
NETSUITE_RECORD_TYPE = re.compile(r"^[A-Za-z0-9_]+$")


def normalize_account_subdomain(account_id: str) -> str:
    """Return the account-specific subdomain used by SuiteTalk."""
    normalized = str(account_id or "").strip().lower().replace("_", "-")
    if not normalized:
        raise ValueError("NETSUITE_ACCOUNT_ID must not be empty")
    return normalized


def netsuite_base_url(account_id: str) -> str:
    account = normalize_account_subdomain(account_id)
    return f"https://{account}.suitetalk.api.netsuite.com/services/rest"


class StaticAccessTokenProvider:
    def __init__(self, access_token: str):
        self.access_token = str(access_token or "").strip()
        if not self.access_token:
            raise ValueError("NETSUITE_ACCESS_TOKEN must not be empty")

    def __call__(self) -> str:
        return self.access_token


class OAuth2ClientCredentialsTokenProvider:
    """Get and cache a NetSuite OAuth 2.0 M2M access token."""

    def __init__(
        self,
        account_id: str,
        client_id: str,
        certificate_id: str,
        private_key_file: str,
        private_key_passphrase: Optional[str] = None,
        algorithm: str = "PS256",
        base_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
        now: Callable[[], float] = time.time,
    ):
        self.account_id = account_id
        self.client_id = str(client_id or "").strip()
        self.certificate_id = str(certificate_id or "").strip()
        self.private_key_file = Path(private_key_file)
        self.private_key_passphrase = private_key_passphrase
        self.algorithm = str(algorithm or "PS256").strip().upper()
        self.base_url = (base_url or netsuite_base_url(account_id)).rstrip("/")
        self.session = session or requests.Session()
        self.now = now
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

        if not self.client_id:
            raise ValueError("NETSUITE_CLIENT_ID must not be empty")
        if not self.certificate_id:
            raise ValueError("NETSUITE_CERTIFICATE_ID must not be empty")
        if self.algorithm not in {"PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}:
            raise ValueError(f"Unsupported NETSUITE_JWT_ALGORITHM: {self.algorithm}")

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/auth/oauth2/v1/token"

    def __call__(self) -> str:
        now = self.now()
        if self._access_token and now < self._expires_at - 60:
            return self._access_token

        assertion = self._create_client_assertion(int(now))
        response = self.session.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=NETSUITE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise ValueError("NetSuite OAuth token response has no access_token")

        try:
            expires_in = int(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600

        self._access_token = access_token
        self._expires_at = now + max(1, expires_in)
        return access_token

    def _create_client_assertion(self, now: int) -> str:
        try:
            import jwt
            from cryptography.hazmat.primitives import serialization
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "OAuth 2.0 M2M requires PyJWT[crypto]; install requirements.txt"
            ) from exc

        try:
            private_key_pem = self.private_key_file.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"Cannot read NETSUITE_PRIVATE_KEY_FILE: {self.private_key_file}"
            ) from exc

        password = None
        if self.private_key_passphrase:
            password = self.private_key_passphrase.encode("utf-8")

        private_key = serialization.load_pem_private_key(
            private_key_pem,
            password=password,
        )
        claims = {
            "iss": self.client_id,
            "scope": ["rest_webservices"],
            "aud": self.token_url,
            "iat": now,
            "exp": now + 300,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(
            claims,
            private_key,
            algorithm=self.algorithm,
            headers={"typ": "JWT", "kid": self.certificate_id},
        )


def token_provider_from_env(
    account_id: str,
    base_url: Optional[str] = None,
) -> Callable[[], str]:
    access_token = os.getenv("NETSUITE_ACCESS_TOKEN")
    if access_token:
        return StaticAccessTokenProvider(access_token)

    required_values = {
        "NETSUITE_CLIENT_ID": os.getenv("NETSUITE_CLIENT_ID"),
        "NETSUITE_CERTIFICATE_ID": os.getenv("NETSUITE_CERTIFICATE_ID"),
        "NETSUITE_PRIVATE_KEY_FILE": os.getenv("NETSUITE_PRIVATE_KEY_FILE"),
    }
    missing = [name for name, value in required_values.items() if not value]
    if missing:
        raise ValueError(
            "Set NETSUITE_ACCESS_TOKEN or all OAuth 2.0 M2M variables: "
            + ", ".join(missing)
        )

    return OAuth2ClientCredentialsTokenProvider(
        account_id=account_id,
        client_id=required_values["NETSUITE_CLIENT_ID"],
        certificate_id=required_values["NETSUITE_CERTIFICATE_ID"],
        private_key_file=required_values["NETSUITE_PRIVATE_KEY_FILE"],
        private_key_passphrase=os.getenv("NETSUITE_PRIVATE_KEY_PASSPHRASE"),
        algorithm=os.getenv("NETSUITE_JWT_ALGORITHM", "PS256"),
        base_url=base_url,
    )


class NetSuiteClient:
    """Small SuiteTalk REST client for SuiteQL and record upserts."""

    def __init__(
        self,
        account_id: str,
        access_token_provider: Optional[Callable[[], str]] = None,
        base_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.account_id = str(account_id or "").strip()
        if not self.account_id:
            raise ValueError("NETSUITE_ACCOUNT_ID must be set")

        self.base_url = (
            base_url
            or os.getenv("NETSUITE_BASE_URL")
            or netsuite_base_url(self.account_id)
        ).rstrip("/")
        self.access_token_provider = access_token_provider or token_provider_from_env(
            self.account_id,
            self.base_url,
        )
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "TimeCamp-NetSuite-Sync",
            }
        )

    @classmethod
    def from_env(cls) -> "NetSuiteClient":
        account_id = os.getenv("NETSUITE_ACCOUNT_ID")
        if not account_id:
            raise ValueError("NETSUITE_ACCOUNT_ID must be set in .env")
        return cls(account_id)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        request_headers = {
            "Authorization": f"Bearer {self.access_token_provider()}",
        }
        if headers:
            request_headers.update(headers)

        response = self.session.request(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
            json=json,
            params=params,
            headers=request_headers,
            timeout=NETSUITE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def suiteql(
        self,
        query: str,
        page_size: int = NETSUITE_SUITEQL_PAGE_SIZE,
    ) -> List[Dict[str, Any]]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("SuiteQL query must not be empty")
        if page_size < 1 or page_size > NETSUITE_SUITEQL_PAGE_SIZE:
            raise ValueError(
                f"SuiteQL page_size must be between 1 and {NETSUITE_SUITEQL_PAGE_SIZE}"
            )

        rows: List[Dict[str, Any]] = []
        offset = 0
        while True:
            payload = self._request(
                "POST",
                "query/v1/suiteql",
                json={"q": normalized_query},
                params={"limit": page_size, "offset": offset},
                headers={"Prefer": "transient"},
            )
            if not isinstance(payload, dict):
                raise ValueError("Unexpected NetSuite SuiteQL response")
            page = payload.get("items", [])
            if not isinstance(page, list):
                raise ValueError("Unexpected NetSuite SuiteQL items response")
            rows.extend(page)

            if len(rows) > NETSUITE_MAX_SUITEQL_RESULTS:
                raise ValueError(
                    "SuiteQL result exceeds NetSuite's 100,000 row REST limit"
                )
            if not payload.get("hasMore"):
                break

            try:
                next_offset = int(payload.get("offset", offset)) + int(
                    payload.get("count", len(page))
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Invalid NetSuite SuiteQL pagination response"
                ) from exc
            if next_offset <= offset:
                raise ValueError("NetSuite SuiteQL pagination did not advance")
            offset = next_offset

        return rows

    def get_metadata(self, record_types: Optional[List[str]] = None) -> Dict[str, Any]:
        params = None
        if record_types:
            params = {"select": ",".join(record_types)}
        payload = self._request(
            "GET",
            "record/v1/metadata-catalog",
            params=params,
            headers={"Accept": "application/swagger+json"},
        )
        if not isinstance(payload, dict):
            raise ValueError("Unexpected NetSuite metadata response")
        return payload

    def upsert_record(
        self,
        record_type: str,
        external_id: str,
        payload: Dict[str, Any],
    ) -> Any:
        normalized_record_type = str(record_type or "").strip()
        normalized_external_id = str(external_id or "").strip()
        if not normalized_record_type:
            raise ValueError("NetSuite record_type must not be empty")
        if not NETSUITE_RECORD_TYPE.fullmatch(normalized_record_type):
            raise ValueError("NetSuite record_type contains unsupported characters")
        if not normalized_external_id:
            raise ValueError("NetSuite external_id must not be empty")

        encoded_external_id = quote(normalized_external_id, safe="-_")
        return self._request(
            "PUT",
            f"record/v1/{normalized_record_type}/eid:{encoded_external_id}",
            json=payload,
        )
