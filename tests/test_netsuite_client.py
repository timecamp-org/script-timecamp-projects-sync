import tempfile
import unittest
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.netsuite_client import (
    NETSUITE_REQUEST_TIMEOUT,
    NetSuiteClient,
    OAuth2ClientCredentialsTokenProvider,
    StaticAccessTokenProvider,
    netsuite_base_url,
    normalize_account_subdomain,
)


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.content = b"" if payload is None else b"json"

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


class NetSuiteClientTest(unittest.TestCase):
    def test_normalizes_sandbox_account_for_account_specific_url(self):
        self.assertEqual(normalize_account_subdomain("1234567_SB1"), "1234567-sb1")
        self.assertEqual(
            netsuite_base_url("1234567_SB1"),
            "https://1234567-sb1.suitetalk.api.netsuite.com/services/rest",
        )

    def test_static_access_token_provider_rejects_empty_token(self):
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            StaticAccessTokenProvider(" ")

    def test_suiteql_paginates_and_sends_required_prefer_header(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "items": [{"id": "1"}, {"id": "2"}],
                        "count": 2,
                        "offset": 0,
                        "hasMore": True,
                    }
                ),
                FakeResponse(
                    {
                        "items": [{"id": "3"}],
                        "count": 1,
                        "offset": 2,
                        "hasMore": False,
                    }
                ),
            ]
        )
        client = NetSuiteClient(
            "1234567",
            access_token_provider=lambda: "access-token",
            session=session,
        )

        rows = client.suiteql("SELECT id FROM job ORDER BY id", page_size=2)

        self.assertEqual(rows, [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        self.assertEqual(
            [call["params"]["offset"] for call in session.calls],
            [0, 2],
        )
        self.assertEqual(session.calls[0]["headers"]["Prefer"], "transient")
        self.assertEqual(
            session.calls[0]["headers"]["Authorization"],
            "Bearer access-token",
        )
        self.assertEqual(session.calls[0]["timeout"], NETSUITE_REQUEST_TIMEOUT)

    def test_upsert_uses_put_and_external_id_url(self):
        session = FakeSession([FakeResponse(status_code=204)])
        client = NetSuiteClient(
            "1234567",
            access_token_provider=lambda: "access-token",
            session=session,
        )

        response = client.upsert_record(
            "timebill",
            "timecamp-12:34",
            {"hours": "1:30"},
        )

        self.assertEqual(response, {})
        self.assertEqual(session.calls[0]["method"], "PUT")
        self.assertTrue(
            session.calls[0]["url"].endswith("/record/v1/timebill/eid:timecamp-12%3A34")
        )
        self.assertEqual(session.calls[0]["json"], {"hours": "1:30"})

    def test_oauth2_m2m_assertion_has_required_netsuite_claims(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            key_path = Path(temp_dir) / "private-key.pem"
            key_path.write_bytes(private_key_pem)
            provider = OAuth2ClientCredentialsTokenProvider(
                account_id="1234567",
                client_id="client-id",
                certificate_id="certificate-id",
                private_key_file=str(key_path),
                now=lambda: 1_800_000_000,
            )

            assertion = provider._create_client_assertion(1_800_000_000)

        header = jwt.get_unverified_header(assertion)
        claims = jwt.decode(
            assertion,
            options={"verify_signature": False, "verify_exp": False},
        )
        self.assertEqual(header["alg"], "PS256")
        self.assertEqual(header["kid"], "certificate-id")
        self.assertEqual(claims["iss"], "client-id")
        self.assertEqual(claims["scope"], ["rest_webservices"])
        self.assertEqual(claims["aud"], provider.token_url)
        self.assertEqual(claims["iat"], 1_800_000_000)
        self.assertEqual(claims["exp"], 1_800_000_300)


if __name__ == "__main__":
    unittest.main()
