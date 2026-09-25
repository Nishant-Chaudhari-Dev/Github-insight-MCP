"""Async client for the parts of the GitHub REST API that the MCP server exposes.

This module has no MCP imports, so it can be tested and reused on its own.
Every failure is raised as GitHubError with a message written for the model
that called the tool: what went wrong and what to try next.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal, get_args

import httpx2

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "github-insights-mcp"
TIMEOUT_SECONDS = 15.0

SortOption = Literal["stars", "forks", "updated", "best-match"]
SORT_OPTIONS: tuple[str, ...] = get_args(SortOption)
MAX_SEARCH_RESULTS = 30
MAX_CONTRIBUTORS = 100
README_MIN_CHARS = 500
README_MAX_CHARS = 20_000
MAX_COMPARE = 10

# GitHub builds /stats/* data in the background and answers 202 until it is ready.
STATS_RETRIES = 3
STATS_RETRY_DELAY = 2.0  # seconds between attempts

# Tests swap in httpx2.MockTransport. None means "use the real network".
transport: httpx2.AsyncBaseTransport | None = None

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class GitHubError(Exception):
    """A GitHub failure, worded for the model that made the tool call."""


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #

def validate_name(value: str, kind: str) -> str:
    """Return a stripped owner or repository name, or raise if it cannot be one.

    Restricting names to GitHub's character set also stops path tricks such as
    owner="../.." from reaching other API endpoints.
    """
    name = value.strip()
    if not _NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise GitHubError(
            f"{value!r} is not a valid GitHub {kind} name "
            "(use letters, digits, '-', '_' or '.')."
        )
    return name


def parse_full_name(text: str) -> tuple[str, str]:
    """Split 'owner/repo', or a github.com URL for one, into validated parts."""
    rest = text.strip()
    for scheme in ("https://", "http://"):
        rest = rest.removeprefix(scheme)
    from_url = False
    for host in ("www.github.com/", "github.com/"):
        if rest.startswith(host):
            rest, from_url = rest[len(host):], True
            break
    parts = [part for part in rest.split("/") if part]
    # URLs may continue past the repo (".../tree/main"); bare names may not.
    if len(parts) < 2 or (len(parts) > 2 and not from_url):
        raise GitHubError(f"Expected a repository like 'owner/name', got {text!r}.")
    owner = validate_name(parts[0], "owner")
    repo = validate_name(parts[1].removesuffix(".git"), "repository")
    return owner, repo


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _headers(accept: str) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    accept: str = "application/vnd.github+json",
    not_found: str | None = None,
) -> httpx2.Response:
    """GET an API path and return the response, or raise GitHubError."""
    try:
        async with httpx2.AsyncClient(
            base_url=API_URL,
            headers=_headers(accept),
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,  # renamed repositories answer with a redirect
            transport=transport,
        ) as client:
            response = await client.get(path, params=params)
    except httpx2.TimeoutException as exc:
        raise GitHubError("GitHub did not respond in time. Try again in a moment.") from exc
    except httpx2.HTTPError as exc:
        raise GitHubError(f"Could not reach GitHub: {exc}") from exc
    _check_status(response, path, not_found)
    return response


def _check_status(response: httpx2.Response, path: str, not_found: str | None) -> None:
    if response.is_success:
        return
    status = response.status_code
    headers = response.headers
    rate_limited = status == 429 or (
        status == 403
        and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)
    )
    if rate_limited:
        raise GitHubError(_rate_limit_message(headers))
    if status == 401:
        raise GitHubError("GitHub rejected the token (401 Unauthorized). Fix or unset GITHUB_TOKEN.")
    if status == 404:
        raise GitHubError(not_found or f"Not found on GitHub: {path}")
    raise GitHubError(f"GitHub returned HTTP {status} for {path}: {_error_detail(response)}")


def _rate_limit_message(headers: Any) -> str:
    message = "GitHub's API rate limit was hit."
    retry_after = headers.get("retry-after") or ""
    reset = headers.get("x-ratelimit-reset") or ""
    if retry_after.isdigit():
        message += f" Retry in {retry_after} seconds."
    elif reset.isdigit():
        resets_at = datetime.fromtimestamp(int(reset), tz=timezone.utc)
        message += f" It resets at {resets_at:%H:%M} UTC."
    if not os.environ.get("GITHUB_TOKEN", "").strip():
        message += " Setting GITHUB_TOKEN raises the limit from 60 to 5,000 requests per hour."
    return message


def _error_detail(response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return response.text.strip()[:200] or response.reason_phrase


# --------------------------------------------------------------------------- #
# Response shaping
# --------------------------------------------------------------------------- #

def _summary(repo: dict[str, Any]) -> dict[str, Any]:
    return {
        "full_name": repo.get("full_name"),
        "description": repo.get("description"),
        "url": repo.get("html_url"),
        "stars": repo.get("stargazers_count"),
        "forks": repo.get("forks_count"),
        "language": repo.get("language"),
        "topics": repo.get("topics") or [],
        "archived": bool(repo.get("archived")),
        "last_push": repo.get("pushed_at"),
    }


def _iso_date(unix_seconds: int) -> str:
    return datetime.fromtimestamp(int(unix_seconds), tz=timezone.utc).date().isoformat()


# --------------------------------------------------------------------------- #
# Operations (one per MCP tool)
# --------------------------------------------------------------------------- #

async def search_repositories(
    query: str, language: str | None = None, sort: str = "stars", limit: int = 10
) -> dict[str, Any]:
    q = query.strip()
    if not q:
        raise GitHubError("The search query is empty.")
    if sort not in SORT_OPTIONS:
        raise GitHubError(f"sort must be one of: {', '.join(SORT_OPTIONS)}.")
    lang = (language or "").strip().replace('"', "")
    if lang:
        q += f' language:"{lang}"' if " " in lang else f" language:{lang}"
    params: dict[str, Any] = {"q": q, "per_page": _clamp(limit, 1, MAX_SEARCH_RESULTS)}
    if sort != "best-match":  # omitting sort gives GitHub's relevance ranking
        params.update(sort=sort, order="desc")

    data = (await _get("/search/repositories", params=params)).json()
    repositories = [_summary(item) for item in data.get("items", [])]
    result: dict[str, Any] = {
        "query": q,
        "total_count": data.get("total_count", len(repositories)),
        "returned": len(repositories),
        "repositories": repositories,
    }
    if data.get("incomplete_results"):
        result["note"] = "GitHub timed out part of this search, so results may be incomplete."
    return result


async def get_repo_details(owner: str, repo: str) -> dict[str, Any]:
    owner, repo = validate_name(owner, "owner"), validate_name(repo, "repository")
    response = await _get(
        f"/repos/{owner}/{repo}",
        not_found=f"Repository {owner}/{repo} was not found (it may be private or misspelled).",
    )
    data = response.json()
    return {
        **_summary(data),
        # GitHub's open_issues_count includes open pull requests.
        "open_issues_and_prs": data.get("open_issues_count"),
        "watchers": data.get("subscribers_count"),
        "license": (data.get("license") or {}).get("name"),
        "default_branch": data.get("default_branch"),
        "is_fork": bool(data.get("fork")),
        "created_at": data.get("created_at"),
        "homepage": data.get("homepage") or None,
    }


async def get_readme(owner: str, repo: str, max_chars: int = 6000) -> str:
    owner, repo = validate_name(owner, "owner"), validate_name(repo, "repository")
    response = await _get(
        f"/repos/{owner}/{repo}/readme",
        accept="application/vnd.github.raw+json",
        not_found=f"{owner}/{repo} has no README, or the repository does not exist.",
    )
    text = response.text
    limit = _clamp(max_chars, README_MIN_CHARS, README_MAX_CHARS)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[README truncated: showing {limit:,} of {len(text):,} characters]"


async def get_contributors(owner: str, repo: str, limit: int = 10) -> dict[str, Any]:
    owner, repo = validate_name(owner, "owner"), validate_name(repo, "repository")
    response = await _get(
        f"/repos/{owner}/{repo}/contributors",
        params={"per_page": _clamp(limit, 1, MAX_CONTRIBUTORS)},
        not_found=f"Repository {owner}/{repo} was not found (it may be private or misspelled).",
    )
    full_name = f"{owner}/{repo}"
    if response.status_code == 204 or not response.content:
        return {"repository": full_name, "contributors": [], "note": "The repository is empty."}
    contributors = [
        {"login": c.get("login"), "commits": c.get("contributions"), "profile": c.get("html_url")}
        for c in response.json()
    ]
    return {"repository": full_name, "contributors": contributors}


async def get_commit_activity(owner: str, repo: str, weeks: int = 12) -> dict[str, Any]:
    owner, repo = validate_name(owner, "owner"), validate_name(repo, "repository")
    path = f"/repos/{owner}/{repo}/stats/commit_activity"
    not_found = f"Repository {owner}/{repo} was not found (it may be private or misspelled)."
    full_name = f"{owner}/{repo}"

    response = await _get(path, not_found=not_found)
    retries = 0
    while response.status_code == 202 and retries < STATS_RETRIES:
        await asyncio.sleep(STATS_RETRY_DELAY)
        response = await _get(path, not_found=not_found)
        retries += 1
    if response.status_code == 202:
        return {
            "repository": full_name,
            "status": "computing",
            "message": "GitHub is still computing statistics for this repository. Try again in a minute.",
        }

    data = response.json() if response.status_code != 204 and response.content else None
    if not isinstance(data, list) or not data:
        return {
            "repository": full_name,
            "status": "no_data",
            "message": "GitHub returned no commit statistics (the repository may be empty).",
        }
    recent = data[-_clamp(weeks, 1, 52):]
    return {
        "repository": full_name,
        "status": "ok",
        "commits_last_52_weeks": sum(int(week.get("total", 0)) for week in data),
        "recent_weeks": [
            {"week_of": _iso_date(week["week"]), "commits": int(week.get("total", 0))}
            for week in recent
        ],
    }


async def compare_repos(repos: list[str]) -> dict[str, Any]:
    if not repos:
        raise GitHubError("Give at least one repository to compare.")
    unique: dict[tuple[str, str], tuple[str, str]] = {}
    for text in repos:
        owner, repo = parse_full_name(text)
        unique.setdefault((owner.lower(), repo.lower()), (owner, repo))  # names are case-insensitive
    if len(unique) > MAX_COMPARE:
        raise GitHubError(f"Compare at most {MAX_COMPARE} repositories at a time.")

    targets = list(unique.values())
    results = await asyncio.gather(
        *(get_repo_details(owner, repo) for owner, repo in targets), return_exceptions=True
    )
    found: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for (owner, repo), result in zip(targets, results):
        if isinstance(result, GitHubError):
            errors.append({"repository": f"{owner}/{repo}", "error": str(result)})
        elif isinstance(result, BaseException):
            raise result  # a bug, not a GitHub failure: let it surface
        else:
            found.append(result)
    found.sort(key=lambda r: r.get("stars") or 0, reverse=True)
    columns = ("full_name", "stars", "forks", "open_issues_and_prs", "language",
               "license", "last_push", "archived")
    return {"repositories": [{k: r.get(k) for k in columns} for r in found], "errors": errors}
