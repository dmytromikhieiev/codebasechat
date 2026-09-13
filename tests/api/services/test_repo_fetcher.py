import io
import tarfile

import httpx
import pytest
import respx

from api.services import repo_fetcher


def _build_tarball(files: dict[str, bytes], root: str = "octocat-hello-world-abc123") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relative_path, content in files.items():
            info = tarfile.TarInfo(name=f"{root}/{relative_path}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _fake_installation_token(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_installation_token(installation_id: int) -> str:
        return "fake-installation-token"

    monkeypatch.setattr(repo_fetcher, "get_installation_token", fake_get_installation_token)


@respx.mock
async def test_fetch_repo_files_filters_excluded_dirs_and_binaries() -> None:
    tarball = _build_tarball(
        {
            "src/main.py": b"def foo():\n    return 1\n",
            "node_modules/pkg/index.js": b"module.exports = {};\n",
            ".git/config": b"[core]\n",
            "vendor/lib.go": b"package vendor\n",
            "assets/logo.png": b"\x89PNG\r\n\x1a\nnotarealpng",
        }
    )
    route = respx.get("https://api.github.com/repos/octocat/hello-world/tarball/main").mock(
        return_value=httpx.Response(200, content=tarball)
    )

    files = await repo_fetcher.fetch_repo_files(42, "octocat/hello-world", "main")

    assert route.called
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer fake-installation-token"

    paths = {f.path for f in files}
    assert paths == {"src/main.py"}
    assert files[0].language == "python"
    assert files[0].content == "def foo():\n    return 1\n"


@respx.mock
async def test_fetch_repo_files_includes_docs_and_config() -> None:
    tarball = _build_tarball(
        {
            "README.md": b"# hello\n",
            "docker-compose.yml": b"services:\n  api: {}\n",
            "package.json": b'{"name": "x"}\n',
            "Dockerfile.api": b"FROM python:3.12-slim\n",
            ".gitignore": b"node_modules/\n",
            "assets/logo.png": b"\x89PNG\r\n\x1a\nnotarealpng",
        }
    )
    respx.get("https://api.github.com/repos/octocat/hello-world/tarball/main").mock(
        return_value=httpx.Response(200, content=tarball)
    )

    files = await repo_fetcher.fetch_repo_files(42, "octocat/hello-world", "main")

    paths = {f.path for f in files}
    assert paths == {"README.md", "docker-compose.yml", "package.json", "Dockerfile.api", ".gitignore"}
    assert "assets/logo.png" not in paths
    languages = {f.path: f.language for f in files}
    assert languages["README.md"] == "text"


@respx.mock
async def test_fetch_repo_files_excludes_generated_lockfiles() -> None:
    tarball = _build_tarball(
        {
            "package-lock.json": b'{"lockfileVersion": 3}\n',
            "yarn.lock": b"# yarn lockfile\n",
            "go.sum": b"example.com/pkg v1.0.0\n",
        }
    )
    respx.get("https://api.github.com/repos/octocat/hello-world/tarball/main").mock(
        return_value=httpx.Response(200, content=tarball)
    )

    files = await repo_fetcher.fetch_repo_files(42, "octocat/hello-world", "main")

    assert files == []


@respx.mock
async def test_fetch_repo_files_skips_undecodable_files() -> None:
    tarball = _build_tarball({"broken.py": b"\xff\xfe not valid utf-8 \x00\x01"})
    respx.get("https://api.github.com/repos/octocat/hello-world/tarball/main").mock(
        return_value=httpx.Response(200, content=tarball)
    )

    files = await repo_fetcher.fetch_repo_files(42, "octocat/hello-world", "main")

    assert files == []


@respx.mock
async def test_fetch_repo_files_skips_oversized_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo_fetcher, "MAX_FILE_SIZE_BYTES", 10)
    tarball = _build_tarball({"big.py": b"x = 1\n" * 10})

    respx.get("https://api.github.com/repos/octocat/hello-world/tarball/main").mock(
        return_value=httpx.Response(200, content=tarball)
    )

    files = await repo_fetcher.fetch_repo_files(42, "octocat/hello-world", "main")

    assert files == []


@respx.mock
async def test_fetch_repo_files_strips_archive_root_directory() -> None:
    tarball = _build_tarball({"pkg/util.py": b"CONST = 1\n"}, root="myorg-myrepo-deadbeef")
    respx.get("https://api.github.com/repos/myorg/myrepo/tarball/deadbeef").mock(
        return_value=httpx.Response(200, content=tarball)
    )

    files = await repo_fetcher.fetch_repo_files(42, "myorg/myrepo", "deadbeef")

    assert len(files) == 1
    assert files[0].path == "pkg/util.py"
