# github-insights-mcp

[![tests](https://github.com/Nishant-Chaudhari-Dev/github-insights-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/Nishant-Chaudhari-Dev/github-insights-mcp/actions/workflows/tests.yml)

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP host) read-only tools for exploring public GitHub repositories: search, repository details, READMEs, contributors, commit activity, and side-by-side comparison.

Built on the MCP Python SDK v2 (`MCPServer`) with async `httpx2`.

<!-- Add a screenshot or GIF of Claude using these tools here. -->

## Tools

| Tool | What it returns |
|---|---|
| `search_repositories` | Repositories matching a query (GitHub qualifiers such as `topic:mcp` work), filterable by language, sorted by stars, forks, recent updates, or relevance |
| `get_repo_details` | Stars, forks, open issues and PRs, license, topics, default branch, activity dates |
| `get_readme` | The README as raw Markdown, truncated to a length you choose |
| `get_contributors` | Top contributors by commit count |
| `get_commit_activity` | Weekly commit counts over the past year |
| `compare_repos` | Up to 10 repositories side by side, fetched concurrently, sorted by stars |

## Design choices

- **Errors the model can act on.** GitHub failures (404s, rate limits, rejected tokens, timeouts) are raised as MCP `ToolError`s with specific messages, such as *"Repository nobody/nothing was not found (it may be private or misspelled)."* The model reads the message and can correct itself. Unexpected exceptions are reported generically, so internals never leak into the conversation.
- **Validated inputs.** Every parameter carries a description and bounds in the tool schema, so out-of-range values are rejected before any request is made. Owner and repository names are checked against GitHub's character set, which also blocks path tricks such as `owner=".."`.
- **GitHub quirks handled.** Statistics endpoints answer `202 Accepted` while GitHub computes them, so `get_commit_activity` retries briefly instead of failing. Renamed repositories are followed through redirects. GitHub's `open_issues_count` includes pull requests, so it is reported as `open_issues_and_prs`.
- **A testable core.** All GitHub logic lives in `github_client.py`, which has no MCP imports; `server.py` only declares the tools. The test suite runs offline against a mock transport, plus in-memory protocol tests through the SDK's `Client`.

## Setup

Requires Python 3.10 or newer.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

A GitHub token is optional but recommended. Without one, GitHub allows 60 requests per hour (10 searches per minute); with one, 5,000 requests per hour (30 searches per minute). A fine-grained personal access token with read-only access to public repositories is enough.

```bash
export GITHUB_TOKEN=github_pat_...
```

## Try it in the MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector .venv/bin/python server.py
```

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`. Running `python server.py` by itself just waits silently: it is a stdio server that expects an MCP host on the other end.

## Use it from Claude Desktop

Add this to `claude_desktop_config.json` (on macOS in `~/Library/Application Support/Claude/`, on Windows in `%APPDATA%\Claude\`), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "github-insights": {
      "command": "/absolute/path/to/github-insights-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/github-insights-mcp/server.py"],
      "env": { "GITHUB_TOKEN": "github_pat_..." }
    }
  }
}
```

Point `command` at the virtual environment's Python rather than a bare `python`. The host launches the server outside your shell, where `python` may be an interpreter without these dependencies. On Windows the path looks like `C:\\Users\\you\\github-insights-mcp\\.venv\\Scripts\\python.exe` (backslashes doubled, since this is JSON).

Then try a prompt like: *"Compare pallets/flask and fastapi/fastapi, then summarize the README of whichever had more commits in the last month."*

## Use it from Claude Code

```bash
claude mcp add-json github-insights '{"type":"stdio","command":"/absolute/path/to/github-insights-mcp/.venv/bin/python","args":["/absolute/path/to/github-insights-mcp/server.py"],"env":{"GITHUB_TOKEN":"github_pat_..."}}'
claude mcp list
```

`add-json` sidesteps a known argument-ordering pitfall in `claude mcp add --env`.

## Tests

```bash
python -m unittest discover -s tests -v
```

No network or token needed: `tests/fake_github.py` serves canned GitHub responses through `httpx2.MockTransport`. `pytest` runs the same suite if you have it installed.

## Project layout

```
github_client.py   GitHub REST calls, input validation, error messages (no MCP imports)
server.py          MCP tool declarations: schemas, annotations, error translation
tests/             offline unit tests and in-memory MCP protocol tests
```

## Ideas for next steps

- Cache responses for a few minutes to stretch the rate limit during long sessions
- Add a `list_issues` tool (open issues filtered by label) for triage workflows
- Serve over Streamable HTTP (`mcp.run(transport="streamable-http")`) for remote use

## License

MIT
