import dataclasses
import io
import logging
import tarfile

import httpx

from api.services.chunking import detect_language
from api.services.github_app import get_installation_token

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

EXCLUDED_DIR_NAMES = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
    ".next",
    "target",
    "bin",
    "obj",
}

MAX_FILE_SIZE_BYTES = 1_000_000  # skip generated/binary-ish files this large

# Files with no recognized programming language are still worth indexing if
# they're prose/config a question could plausibly be about (README, project
# docs, Docker/CI config) — chunking.py already has a line-based fallback
# chunker for exactly this case (language=None -> "text").
TEXT_FILE_EXTENSIONS = {
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".sql",
    ".sh",
    ".env.example",
}
TEXT_FILE_NAMES = {"Makefile", "LICENSE", "LICENSE.md", "Procfile", ".gitignore", ".dockerignore", ".editorconfig"}

# Auto-generated lockfiles: huge, machine-written, no value for a "chat with
# your codebase" answer — excluded even though their extension (.json/.yaml)
# would otherwise pass the text-file check above.
EXCLUDED_FILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
    "Cargo.lock",
}


@dataclasses.dataclass
class RepoFile:
    path: str
    content: str
    language: str


async def fetch_repo_files(installation_id: int, repo_full_name: str, ref: str) -> list[RepoFile]:
    token = await get_installation_token(installation_id)
    tarball_bytes = await _download_tarball(repo_full_name, ref, token)
    return _extract_code_files(tarball_bytes)


async def _download_tarball(repo_full_name: str, ref: str, token: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(
            f"{GITHUB_API_BASE}/repos/{repo_full_name}/tarball/{ref}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    response.raise_for_status()
    return response.content


def _extract_code_files(tarball_bytes: bytes) -> list[RepoFile]:
    files: list[RepoFile] = []
    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or member.size > MAX_FILE_SIZE_BYTES:
                continue

            relative_path = _strip_archive_root(member.name)
            if relative_path is None or not _is_code_path(relative_path):
                continue
            if _basename(relative_path) in EXCLUDED_FILE_NAMES:
                continue

            language = detect_language(relative_path)
            if language is None and not _is_text_file(relative_path):
                continue

            extracted = tar.extractfile(member)
            if extracted is None:
                continue

            try:
                content = extracted.read().decode("utf-8")
            except UnicodeDecodeError:
                continue  # binary file with a misleadingly code-like extension

            files.append(RepoFile(path=relative_path, content=content, language=language or "text"))
    return files


def _strip_archive_root(member_name: str) -> str | None:
    # GitHub tarballs wrap everything in a single "{owner}-{repo}-{sha}/" root dir.
    parts = member_name.split("/", 1)
    if len(parts) != 2 or not parts[1]:
        return None
    return parts[1]


def _is_code_path(relative_path: str) -> bool:
    directory_parts = relative_path.split("/")[:-1]
    return not any(part in EXCLUDED_DIR_NAMES for part in directory_parts)


def _basename(relative_path: str) -> str:
    return relative_path.rsplit("/", 1)[-1]


def _is_text_file(relative_path: str) -> bool:
    basename = _basename(relative_path)
    if basename in TEXT_FILE_NAMES or basename.startswith("Dockerfile"):
        return True
    return any(relative_path.endswith(ext) for ext in TEXT_FILE_EXTENSIONS)
