import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from api.services import github_app


def _generate_rsa_keypair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def rsa_keys() -> tuple[str, str]:
    return _generate_rsa_keypair()


@pytest.fixture(autouse=True)
def _patch_private_key(monkeypatch: pytest.MonkeyPatch, rsa_keys: tuple[str, str]):
    private_pem, _ = rsa_keys

    async def fake_get_private_key() -> str:
        return private_pem

    monkeypatch.setattr(github_app, "get_github_app_private_key", fake_get_private_key)
    monkeypatch.setenv("GITHUB_APP_ID", "123456")


async def test_generate_app_jwt_has_expected_claims(rsa_keys: tuple[str, str]) -> None:
    _, public_pem = rsa_keys

    before = int(time.time())
    token = await github_app.generate_app_jwt()
    after = int(time.time())

    payload = jwt.decode(token, public_pem, algorithms=["RS256"])
    assert payload["iss"] == "123456"
    # backdated for clock drift, per GitHub's recommendation
    assert payload["iat"] <= before
    # exp must be no more than 10 minutes from actual now, not from iat
    assert payload["exp"] <= after + github_app.APP_JWT_TTL_SECONDS
    assert payload["exp"] >= before + github_app.APP_JWT_TTL_SECONDS - 5


async def test_generate_app_jwt_rejects_wrong_key(rsa_keys: tuple[str, str]) -> None:
    token = await github_app.generate_app_jwt()
    _, other_public_pem = _generate_rsa_keypair()

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, other_public_pem, algorithms=["RS256"])


def _mock_installation_token() -> None:
    respx.post("https://api.github.com/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(200, json={"token": "installation-token-abc"})
    )


@respx.mock
async def test_resolve_ref_to_sha_returns_commit_sha() -> None:
    _mock_installation_token()
    respx.get("https://api.github.com/repos/octocat/hello-world/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "abcdef1234567890"})
    )

    sha = await github_app.resolve_ref_to_sha(42, "octocat/hello-world", "main")

    assert sha == "abcdef1234567890"


@respx.mock
async def test_compare_commits_returns_changed_files() -> None:
    _mock_installation_token()
    respx.get("https://api.github.com/repos/octocat/hello-world/compare/base-sha...head-sha").mock(
        return_value=httpx.Response(
            200,
            json={"files": [{"filename": "a.py", "status": "modified"}, {"filename": "b.py", "status": "added"}]},
        )
    )

    files = await github_app.compare_commits(42, "octocat/hello-world", "base-sha", "head-sha")

    assert [f["filename"] for f in files] == ["a.py", "b.py"]


@respx.mock
async def test_compare_commits_returns_empty_list_when_no_files_key() -> None:
    _mock_installation_token()
    respx.get("https://api.github.com/repos/octocat/hello-world/compare/base-sha...head-sha").mock(
        return_value=httpx.Response(200, json={})
    )

    files = await github_app.compare_commits(42, "octocat/hello-world", "base-sha", "head-sha")

    assert files == []
