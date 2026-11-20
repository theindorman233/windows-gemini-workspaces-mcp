import argparse
import logging
import os
import secrets
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import StrictBool, StrictInt
from typing_extensions import NotRequired, TypedDict

import synthetic_edits
import workspace_commands
import workspace_files
import live_workspaces


PLACEHOLDER_VALUES = frozenset({
    "123456789",
    "example-github-oauth-client-id",
    "your_github_oauth_client_id",
    "https://your-node.ts.net",
})
DEFAULT_MAX_REGISTERED_CLIENTS = 32


def _read_required_setting(env_var: str, fallback: str = "") -> str:
    value = os.environ.get(env_var, fallback).strip().rstrip("/")
    if not value or value in PLACEHOLDER_VALUES:
        raise ValueError(f"{env_var} must be configured with a real non-placeholder value")
    return value


PUBLIC_BASE = os.environ.get("MCP_PUBLIC_BASE", "").strip().rstrip("/")
PUBLIC_HOST = urlsplit(PUBLIC_BASE).hostname if PUBLIC_BASE else None
CLIENT_ID = os.environ.get("MCP_GITHUB_CLIENT_ID", "").strip()
OWNER_ID = os.environ.get("MCP_GITHUB_OWNER_ID", "").strip()
GOOGLE_CALLBACKS = [
    f"https://oauth-redirect{environment}.googleusercontent.com/{route}/*"
    for environment in ("", "-sandbox", "-test")
    for route in ("r", "a")
]
logger = logging.getLogger(__name__)


def _is_dcr_allowed() -> bool:
    raw = os.environ.get("MCP_ALLOW_DCR", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _max_registered_clients() -> int:
    raw = os.environ.get("MCP_MAX_REGISTERED_CLIENTS", str(DEFAULT_MAX_REGISTERED_CLIENTS)).strip()
    if not raw:
        return DEFAULT_MAX_REGISTERED_CLIENTS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("Invalid MCP_MAX_REGISTERED_CLIENTS=%r; falling back to %d", raw, DEFAULT_MAX_REGISTERED_CLIENTS)
        return DEFAULT_MAX_REGISTERED_CLIENTS


class WorkspaceRange(TypedDict):
    path: str
    start_line: int
    end_line: int
    start_column: NotRequired[int]


class Replacement(TypedDict):
    old_text: str
    new_text: str


class OwnerGitHubProvider(GitHubProvider):
    async def _count_registered_clients(self) -> int:
        collection = self._client_store._default_collection
        storage = getattr(self._client_storage, "key_value", self._client_storage)
        if hasattr(storage, "keys"):
            return len(await storage.keys(collection=collection))
        if isinstance(storage, FileTreeStore):
            sanitized = storage._sanitize_collection(collection)
            col_dir = Path(storage._data_directory) / sanitized
            if not col_dir.exists():
                return 0
            return sum(1 for entry in col_dir.iterdir() if entry.is_file())
        return len(getattr(self, "_registered_client_ids", set()))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not _is_dcr_allowed():
            logger.warning("OAuth registration rejected: dynamic client registration is disabled (MCP_ALLOW_DCR)")
            raise RegistrationError("invalid_client_metadata", "Dynamic client registration is disabled.")
        existing = await self._client_store.get(key=client_info.client_id) if client_info.client_id else None
        if existing is None:
            max_clients = _max_registered_clients()
            current_count = await self._count_registered_clients()
            if current_count >= max_clients:
                logger.warning(
                    "OAuth registration rejected: registered client limit reached (%d/%d)",
                    current_count,
                    max_clients,
                )
                raise RegistrationError(
                    "invalid_client_metadata",
                    f"Maximum registered OAuth clients ({max_clients}) reached.",
                )
        try:
            await super().register_client(client_info)
        except RegistrationError as error:
            hosts = sorted({uri.host for uri in client_info.redirect_uris or [] if uri.host})
            logger.warning("OAuth registration rejected: %s; redirect hosts=%s", error.error, hosts[:10])
            raise
        if client_info.client_id:
            tracked = getattr(self, "_registered_client_ids", None)
            if tracked is None:
                tracked = set()
                self._registered_client_ids = tracked
            tracked.add(client_info.client_id)

    async def load_access_token(self, token: str) -> AccessToken | None:
        verified = await super().load_access_token(token)
        if verified is None:
            logger.warning("Rejected bearer access token: verification failed or expired")
            return None
        expected_owner = os.environ.get("MCP_GITHUB_OWNER_ID", OWNER_ID).strip()
        if not expected_owner or expected_owner in PLACEHOLDER_VALUES:
            logger.warning("Rejected bearer access token: MCP_GITHUB_OWNER_ID is missing or placeholder")
            return None
        if verified.subject != expected_owner:
            logger.warning("Rejected bearer access token for unexpected subject=%s", verified.subject)
            return None
        return verified


LIVE_ACCESS_TOKEN_EXPIRY_SECONDS = 30 * 86400
LIVE_FALLBACK_REFRESH_EXPIRY_SECONDS = 180 * 86400


def build_live_oauth_storage(workspace_store: Path, client_secret: str) -> tuple[AsyncKeyValue, bytes]:
    """Create an encrypted on-disk OAuth store and deterministic JWT key for live restarts."""
    oauth_dir = workspace_store / "oauth-proxy"
    oauth_dir.mkdir(parents=True, exist_ok=True)
    jwt_signing_key = derive_jwt_key(high_entropy_material=client_secret, salt="fastmcp-jwt-signing-key")
    storage_key = derive_jwt_key(high_entropy_material=client_secret, salt="fastmcp-storage-encryption-key")
    file_store = FileTreeStore(
        data_directory=oauth_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(oauth_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(oauth_dir),
    )
    encrypted_store = FernetEncryptionWrapper(
        key_value=file_store,
        fernet=Fernet(key=storage_key),
        raise_on_decryption_error=False,
    )
    return encrypted_store, jwt_signing_key


def create_probe(client_secret: str, *, synthetic_read: bool = False, edit_workspace: Path | None = None,
                 workspace_store: Path | None = None, live: bool = False) -> FastMCP:
    if not client_secret:
        raise ValueError("GitHub OAuth client secret is required")
    if live and workspace_store is not None:
        client_storage, jwt_signing_key = build_live_oauth_storage(workspace_store, client_secret)
        access_expiry = LIVE_ACCESS_TOKEN_EXPIRY_SECONDS
        refresh_expiry = LIVE_FALLBACK_REFRESH_EXPIRY_SECONDS
        expiry_threshold = 300
        cache_ttl: int | None = 300
    else:
        client_storage = MemoryStore(max_entries_per_collection=256)
        jwt_signing_key = secrets.token_urlsafe(48)
        access_expiry = 600
        refresh_expiry = 3600
        expiry_threshold = 0
        cache_ttl = None
    public_base = _read_required_setting("MCP_PUBLIC_BASE", PUBLIC_BASE)
    client_id = _read_required_setting("MCP_GITHUB_CLIENT_ID", CLIENT_ID)
    _read_required_setting("MCP_GITHUB_OWNER_ID", OWNER_ID)
    provider = OwnerGitHubProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=public_base,
        redirect_path="/auth/callback",
        allowed_client_redirect_uris=GOOGLE_CALLBACKS,
        required_scopes=[],
        require_authorization_consent=True,
        client_storage=client_storage,
        jwt_signing_key=jwt_signing_key,
        fastmcp_access_token_expiry_seconds=access_expiry,
        fallback_refresh_token_expiry_seconds=refresh_expiry,
        token_expiry_threshold_seconds=expiry_threshold,
        cache_ttl_seconds=cache_ttl,
        enable_cimd=False,
    )
    name = ("Windows Gemini Workspaces Live" if live else "Windows Gemini Workspaces") if workspace_store is not None else "Windows Gemini OAuth Probe"
    server = FastMCP(name, auth=provider)

    @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    def probe() -> str:
        """Return a fixed marker to verify that Gemini can call this MCP server."""
        return "WG-MCP-OAUTH-PROBE-001"

    if synthetic_read:
        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def read_synthetic_fixture() -> str:
            """Read the single synthetic text file used for this connectivity test."""
            fixture = Path(__file__).parent / "fixture" / "test.md"
            with fixture.open("rb") as source:
                content = source.read(128)
            normalized = content.replace(b"\r\n", b"\n")
            if normalized != b"Windows Gemini MCP synthetic fixture.\n\nMarker: WG-MCP-PROBE-001\n":
                raise ValueError("synthetic fixture does not match the approved sample")
            return normalized.decode("utf-8")

    if edit_workspace is not None:
        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def synthetic_file_version(path: str) -> dict:
            """Get the current version of sample.txt or new.txt in the synthetic workspace."""
            return synthetic_edits.fixture_version(edit_workspace, path)

        @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
        def propose_edit(path: str, version: str, replacement: str) -> dict:
            """Submit a synthetic file proposal for local review. This does not change the target file."""
            return synthetic_edits.propose_edit(edit_workspace, path, version, replacement)

    if workspace_store is not None:
        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def list_workspaces() -> dict[str, str]:
            """List the configured workspace names and their local roots."""
            return {name: str(path) for name, path in workspace_files.WORKSPACES.items()}

    if workspace_store is not None and live:
        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def list_workspace_dir(workspace: str, directory: str = "", depth: StrictInt = 2, limit: StrictInt = 100) -> dict:
            """List files and subdirectories inside one workspace directory up to depth (default 2, default limit 100). Use forward-slash relative directory paths; protected paths are skipped."""
            list_dir_fn = getattr(live_workspaces, "list_dir", None)
            if list_dir_fn is None:
                raise NotImplementedError("live_workspaces.list_dir is not available")
            return list_dir_fn(workspace, directory=directory, depth=depth, limit=limit)

        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def find_workspace_files(workspace: str, query: str, limit: StrictInt = 20) -> dict:
            """Find UTF-8 text file candidates in one workspace using a literal filename fragment and Windows Everything."""
            return live_workspaces.find_files(workspace_store, workspace, query, limit)

        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def search_workspace_content(workspace: str, query: str, directory: str = "", limit: StrictInt = 20) -> dict:
            """Search current UTF-8 text for a case-insensitive literal. Returns 'workspace/rel' forward-slash paths, one-based lines and snippets (files >256 KiB and protected paths are skipped). Use a forward-slash relative directory to narrow truncated scans."""
            return live_workspaces.search_content(workspace_store, workspace, query, directory, limit)

        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def read_workspace_ranges(ranges: list[WorkspaceRange]) -> list[dict]:
            """Read up to 8 inclusive one-based ranges from current UTF-8 files without sharing snapshots. Paths must use 'workspace/rel' forward-slash format; max file size is 256 KiB, and each range returns at most 120 lines and 4096 characters. Use returned versions for edits and next to continue; a missing file returns version 'missing'."""
            return workspace_files.read_workspace_ranges(workspace_store, ranges, reader=live_workspaces.read_live)

        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def query_workspace_git(workspace: str, operation: str = "status", directory: str = "",
                                path: str = "", limit: StrictInt = 20) -> dict:
            """Read-only git status, log or diff inside one workspace. diff needs a workspace file path and shows its unstaged changes. Use directory to select a nested repository."""
            return live_workspaces.git_query(workspace_store, workspace, operation, directory, path, limit)

        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def search_workspace_ast(workspace: str, pattern: str, language: str, directory: str = "",
                                 limit: StrictInt = 20) -> dict:
            """Search code structure with ast-grep, for example pattern 'def $NAME($$$): $$$' in language python. Returns 'workspace/rel' forward-slash paths, one-based lines and matched text (files >256 KiB and protected paths are skipped). Use a forward-slash relative directory to narrow large trees."""
            return live_workspaces.search_ast(workspace_store, workspace, pattern, language, directory, limit)


        @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
        def edit_workspace_file(path: str, edits: list[Replacement], version: str = "") -> dict:
            """Apply 1 to 12 unique exact replacements directly to an existing UTF-8 workspace file using a 'workspace/rel' forward-slash path (max file size 256 KiB; protected paths and the running MCP directory are blocked), saving an automatic backup before writing. Pass version from read_workspace_ranges to guard against concurrent edits."""
            return live_workspaces.edit_live_file(workspace_store, path, edits, version)

        @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
        def write_workspace_file(path: str, content: str, version: str = "") -> dict:
            """Create or overwrite a UTF-8 workspace file directly using a 'workspace/rel' forward-slash path (max file size 256 KiB; creates ordinary parent directories if needed; protected paths and the running MCP directory are blocked), saving an automatic backup before overwriting. Pass version='missing' to require creating a new file."""
            return live_workspaces.write_live_file(workspace_store, path, content, version)

        @server.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
        def run_workspace_command(workspace: str, command: str, directory: str = "",
                                  timeout_seconds: StrictInt = 20, background: StrictBool = False) -> dict:
            """Run a PowerShell command inside one workspace directory. Set background=True (or timeout_seconds>25) for long-running scripts, tests, or CLI tools, then poll with read_command_job."""
            return workspace_commands.run_workspace_command(
                workspace_store, workspace, command, directory, timeout_seconds, background,
            )

        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def read_command_job(job_id: str, wait_seconds: StrictInt = 0) -> dict:
            """Check status and captured stdout/stderr of a running or completed command job, optionally waiting up to 20 seconds."""
            return workspace_commands.read_command_job(job_id, wait_seconds)

        @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
        def stop_command_job(job_id: str) -> dict:
            """Stop a running background command job and return its captured output."""
            return workspace_commands.stop_command_job(job_id)

    if workspace_store is not None and not live:

        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def read_workspace_text(path: str) -> dict:
            """Read a locally shared workspace text snapshot and its version, such as thesis/notes.md."""
            return workspace_files.read_workspace_text(workspace_store, path)

        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def search_workspace(query: str, workspace: str = "", mode: str = "content", limit: int = 20,
                             after_path: str = "", after_line: int = 0) -> dict:
            """Search shared snapshots for a case-insensitive literal. Use content or path mode; pass next as pagination arguments."""
            return workspace_files.search_workspace(workspace_store, query, workspace, mode, limit, after_path, after_line)

        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def read_workspace_ranges(ranges: list[WorkspaceRange]) -> list[dict]:
            """Read up to eight inclusive, one-based ranges of shared snapshots. Use a result's next range to continue truncated text."""
            return workspace_files.read_workspace_ranges(workspace_store, ranges)

        @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
        def propose_workspace_patch(path: str, version: str, old_text: str, new_text: str) -> dict:
            """Propose replacing one exact, unique excerpt in a shared snapshot. Does not change the file until local approval."""
            return workspace_files.propose_workspace_patch(workspace_store, path, version, old_text, new_text)

        @server.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
        def propose_workspace_edit(path: str, version: str, replacement: str) -> dict:
            """Propose complete UTF-8 text for a locally shared path using its snapshot version. A local approval step applies the proposal."""
            return synthetic_edits.propose_edit(workspace_store, path, version, replacement,
                                                **workspace_files.PROPOSAL_SETTINGS)

    return server


def resolve_probe_path(options: argparse.Namespace) -> str:
    if options.live_workspaces_store:
        path = "/mcp-workspaces-live"
    elif options.workspace_store:
        path = "/mcp-workspaces-v2"
    elif options.edit_workspace:
        path = "/mcp-edit-fixture"
    else:
        path = "/mcp-fixture" if options.synthetic_read else "/mcp"
    raw = os.environ.get("MCP_PROBE_PATH")
    resolved = raw.strip() if raw is not None else ""
    if not resolved:
        return path
    if not resolved.startswith("/") or resolved.startswith("//") or any(ch in resolved for ch in "?# \t\r\n"):
        raise ValueError("MCP_PROBE_PATH must be a single path starting with '/'")
    return resolved


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--synthetic-read", action="store_true")
    modes.add_argument("--edit-workspace", type=Path)
    modes.add_argument("--workspace-store", type=Path)
    modes.add_argument("--live-workspaces-store", type=Path)
    options = parser.parse_args()
    if options.live_workspaces_store:
        with closing(synthetic_edits.connect(options.live_workspaces_store)):
            pass
    path = resolve_probe_path(options)
    public_base = os.environ.get("MCP_PUBLIC_BASE", PUBLIC_BASE).strip().rstrip("/") or PUBLIC_BASE
    public_host = urlsplit(public_base).hostname
    create_probe(
        sys.stdin.readline().strip(),
        synthetic_read=options.synthetic_read,
        edit_workspace=options.edit_workspace,
        workspace_store=options.live_workspaces_store or options.workspace_store,
        live=options.live_workspaces_store is not None,
    ).run(
        transport="streamable-http",
        host="127.0.0.1",
        port=int(os.environ.get("MCP_PROBE_PORT", "8765")),
        path=path,
        host_origin_protection=True,
        allowed_hosts=[public_host],
        allowed_origins=[public_base],
        uvicorn_config={"access_log": False, "proxy_headers": False},
    )


if __name__ == "__main__":
    main()
