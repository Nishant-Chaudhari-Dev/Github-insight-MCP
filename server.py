"""github-insights: an MCP server with read-only tools for exploring GitHub.

The MCP host (Claude Desktop, Claude Code, MCP Inspector) launches this file and
speaks JSON-RPC over stdin/stdout, so nothing in this process may print to stdout.
All GitHub logic lives in github_client.py; this file only declares the tools.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

import github_client as gh

mcp = MCPServer(
    "github-insights",
    instructions=(
        "Read-only tools for exploring public GitHub repositories. Start with "
        "search_repositories to find candidates, then use get_repo_details, get_readme, "
        "get_contributors, get_commit_activity or compare_repos to dig in."
    ),
    version="1.0.0",
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

Owner = Annotated[str, Field(description="Repository owner (user or organization), e.g. 'pallets'.")]
Repo = Annotated[str, Field(description="Repository name, e.g. 'flask'.")]


@contextmanager
def _as_tool_error() -> Iterator[None]:
    """Re-raise GitHub failures as ToolError so the calling model can read them.

    Any other exception is a bug. The SDK reports those to the model as a bare
    "Error executing tool" and logs the traceback, instead of leaking internals.
    """
    try:
        yield
    except gh.GitHubError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
async def search_repositories(
    query: Annotated[str, Field(description="Search text. GitHub qualifiers such as 'topic:mcp' or 'stars:>100' also work.")],
    language: Annotated[str | None, Field(description="Only return repositories in this language, e.g. 'python'.")] = None,
    sort: gh.SortOption = "stars",
    limit: Annotated[int, Field(ge=1, le=gh.MAX_SEARCH_RESULTS, description="How many repositories to return.")] = 10,
) -> dict[str, Any]:
    """Search public GitHub repositories, most-starred first by default."""
    with _as_tool_error():
        return await gh.search_repositories(query, language=language, sort=sort, limit=limit)


@mcp.tool(annotations=READ_ONLY)
async def get_repo_details(owner: Owner, repo: Repo) -> dict[str, Any]:
    """Get one repository's stars, forks, open issues and PRs, license, topics and activity dates."""
    with _as_tool_error():
        return await gh.get_repo_details(owner, repo)


@mcp.tool(annotations=READ_ONLY)
async def get_readme(
    owner: Owner,
    repo: Repo,
    max_chars: Annotated[int, Field(ge=gh.README_MIN_CHARS, le=gh.README_MAX_CHARS, description="Truncate the README to this many characters.")] = 6000,
) -> str:
    """Fetch a repository's README as raw Markdown."""
    with _as_tool_error():
        return await gh.get_readme(owner, repo, max_chars=max_chars)


@mcp.tool(annotations=READ_ONLY)
async def get_contributors(
    owner: Owner,
    repo: Repo,
    limit: Annotated[int, Field(ge=1, le=gh.MAX_CONTRIBUTORS, description="How many contributors to return.")] = 10,
) -> dict[str, Any]:
    """List a repository's top contributors, ranked by number of commits."""
    with _as_tool_error():
        return await gh.get_contributors(owner, repo, limit=limit)


@mcp.tool(annotations=READ_ONLY)
async def get_commit_activity(
    owner: Owner,
    repo: Repo,
    weeks: Annotated[int, Field(ge=1, le=52, description="How many of the most recent weeks to list.")] = 12,
) -> dict[str, Any]:
    """Weekly commit counts on the default branch over the last year."""
    with _as_tool_error():
        return await gh.get_commit_activity(owner, repo, weeks=weeks)


@mcp.tool(annotations=READ_ONLY)
async def compare_repos(
    repos: Annotated[list[str], Field(min_length=1, max_length=gh.MAX_COMPARE, description="Repositories as 'owner/name' (github.com URLs also work), e.g. ['pallets/flask', 'fastapi/fastapi'].")],
) -> dict[str, Any]:
    """Compare up to 10 repositories side by side, sorted by stars. Fetched concurrently."""
    with _as_tool_error():
        return await gh.compare_repos(repos)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
