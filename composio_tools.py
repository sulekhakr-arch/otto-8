"""
Composio Integration Tools for Parlant/Otto

This module provides Parlant-compatible tools for executing Composio actions.
Composio enables integration with 500+ external services like GitHub, Slack, Gmail, etc.
"""

import json
import os
from typing import Annotated

import parlant.sdk as p
from composio import Composio
from dotenv import load_dotenv

load_dotenv()

# Composio configuration
COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY")
_composio_client = None


def get_composio_client() -> Composio:
    """Get or create a singleton Composio client."""
    global _composio_client
    if _composio_client is None:
        if not COMPOSIO_API_KEY:
            raise ValueError("COMPOSIO_API_KEY environment variable is required")
        _composio_client = Composio(api_key=COMPOSIO_API_KEY)
    return _composio_client


# =============================================================================
# Authentication Tools
# =============================================================================

@p.tool
async def connect_composio_account(
    context: p.ToolContext,
    user_id: Annotated[
        str,
        p.ToolParameterOptions(
            description="Unique identifier for the user (e.g., email or UUID)",
            source="customer",
            examples=["user-123", "john@example.com"],
        ),
    ],
    toolkit: Annotated[
        str,
        p.ToolParameterOptions(
            description="The toolkit/service to connect (e.g., 'github', 'slack', 'gmail')",
            source="customer",
            examples=["github", "slack", "gmail", "notion", "googlecalendar"],
        ),
    ],
    auth_config_id: Annotated[
        str,
        p.ToolParameterOptions(
            description="The Auth Config ID from Composio dashboard for this toolkit",
            source="context",
        ),
    ],
) -> p.ToolResult:
    """
    Connect a user's account to an external service via Composio.
    Returns a redirect URL for the user to complete authentication.
    """
    try:
        client = get_composio_client()
        
        connection_request = client.connected_accounts.link(
            user_id=user_id,
            auth_config_id=auth_config_id,
        )
        
        return p.ToolResult({
            "status": "pending",
            "message": f"Please authenticate with {toolkit}",
            "redirect_url": connection_request.redirect_url,
            "connection_id": connection_request.id,
            "instructions": f"Open the redirect_url in a browser to connect your {toolkit} account.",
        })
    except Exception as exc:
        return p.ToolResult({
            "status": "error",
            "error": str(exc),
        })


@p.tool
async def check_composio_connection(
    context: p.ToolContext,
    user_id: Annotated[
        str,
        p.ToolParameterOptions(
            description="Unique identifier for the user",
            source="customer",
        ),
    ],
    toolkit: Annotated[
        str,
        p.ToolParameterOptions(
            description="The toolkit to check connection status for",
            source="customer",
            examples=["github", "slack", "gmail"],
        ),
    ],
) -> p.ToolResult:
    """Check if a user has an active connection to a specific toolkit."""
    try:
        client = get_composio_client()
        
        connections = client.connected_accounts.list(
            user_ids=[user_id],
        )
        
        active_connections = [
            conn for conn in connections.items
            if conn.status == "ACTIVE" and toolkit.lower() in str(conn).lower()
        ]
        
        if active_connections:
            return p.ToolResult({
                "status": "connected",
                "toolkit": toolkit,
                "message": f"User has an active {toolkit} connection.",
            })
        else:
            return p.ToolResult({
                "status": "not_connected",
                "toolkit": toolkit,
                "message": f"User is not connected to {toolkit}. Use connect_composio_account to authenticate.",
            })
    except Exception as exc:
        return p.ToolResult({
            "status": "error",
            "error": str(exc),
        })


# =============================================================================
# Generic Tool Execution
# =============================================================================

@p.tool
async def execute_composio_tool(
    context: p.ToolContext,
    user_id: Annotated[
        str,
        p.ToolParameterOptions(
            description="Unique identifier for the user",
            source="customer",
        ),
    ],
    tool_name: Annotated[
        str,
        p.ToolParameterOptions(
            description="The Composio tool slug to execute (e.g., 'GITHUB_CREATE_ISSUE')",
            source="context",
            examples=[
                "GITHUB_CREATE_ISSUE",
                "GITHUB_CREATE_PULL_REQUEST",
                "SLACK_SEND_MESSAGE",
                "GMAIL_SEND_EMAIL",
                "NOTION_CREATE_PAGE",
            ],
        ),
    ],
    arguments_json: Annotated[
        str,
        p.ToolParameterOptions(
            description="JSON string of arguments to pass to the tool",
            source="context",
            examples=['{"owner": "myorg", "repo": "myrepo", "title": "Bug fix", "body": "Details here"}'],
        ),
    ],
) -> p.ToolResult:
    """
    Execute any Composio tool action on behalf of the user.
    Use list_composio_tools to discover available tools and their parameters.
    """
    try:
        client = get_composio_client()
        
        # Parse arguments
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError as exc:
            return p.ToolResult({
                "status": "error",
                "error": f"Invalid JSON arguments: {exc.msg}",
            })
        
        # Execute the tool
        result = client.tools.execute(
            tool_name,
            user_id=user_id,
            arguments=args,
        )
        
        return p.ToolResult({
            "status": "success",
            "tool": tool_name,
            "result": result,
        })
    except Exception as exc:
        return p.ToolResult({
            "status": "error",
            "tool": tool_name,
            "error": str(exc),
        })


@p.tool
async def list_composio_tools(
    context: p.ToolContext,
    toolkit: Annotated[
        str,
        p.ToolParameterOptions(
            description="The toolkit to list available tools for (e.g., 'GITHUB', 'SLACK')",
            source="customer",
            examples=["GITHUB", "SLACK", "GMAIL", "NOTION", "GOOGLECALENDAR"],
        ),
    ],
    limit: Annotated[
        int,
        p.ToolParameterOptions(
            description="Maximum number of tools to return (default: 10)",
            source="context",
        ),
    ] = 10,
) -> p.ToolResult:
    """
    List available tools for a Composio toolkit.
    Use this to discover what actions are available for a service.
    """
    try:
        client = get_composio_client()
        
        tools = client.tools.get_raw_composio_tools(
            toolkits=[toolkit.upper()],
            limit=limit,
        )
        
        tool_list = []
        for tool in tools:
            # Tools are Pydantic models, access attributes directly
            tool_info = {
                "name": getattr(tool, "name", ""),
                "slug": getattr(tool, "slug", ""),
                "description": (getattr(tool, "description", "") or "")[:200],
            }
            tool_list.append(tool_info)
        
        return p.ToolResult({
            "status": "success",
            "toolkit": toolkit.upper(),
            "tools": tool_list,
            "count": len(tool_list),
            "message": f"Found {len(tool_list)} tools for {toolkit}. Use execute_composio_tool with the 'slug' to run any of these.",
        })
    except Exception as exc:
        return p.ToolResult({
            "status": "error",
            "error": str(exc),
        })


# =============================================================================
# GitHub-Specific Tools
# =============================================================================

@p.tool
async def github_create_issue(
    context: p.ToolContext,
    user_id: Annotated[
        str,
        p.ToolParameterOptions(
            description="Unique identifier for the user",
            source="customer",
        ),
    ],
    owner: Annotated[
        str,
        p.ToolParameterOptions(
            description="GitHub repository owner (username or organization)",
            source="customer",
            examples=["microsoft", "facebook", "mycompany"],
        ),
    ],
    repo: Annotated[
        str,
        p.ToolParameterOptions(
            description="GitHub repository name",
            source="customer",
            examples=["vscode", "react", "my-project"],
        ),
    ],
    title: Annotated[
        str,
        p.ToolParameterOptions(
            description="Issue title",
            source="customer",
        ),
    ],
    body: Annotated[
        str,
        p.ToolParameterOptions(
            description="Issue body/description (supports markdown)",
            source="customer",
        ),
    ],
) -> p.ToolResult:
    """Create a new issue in a GitHub repository."""
    try:
        client = get_composio_client()
        
        result = client.tools.execute(
            "GITHUB_CREATE_ISSUE",
            user_id=user_id,
            arguments={
                "owner": owner,
                "repo": repo,
                "title": title,
                "body": body,
            },
        )
        
        return p.ToolResult({
            "status": "success",
            "message": f"Created issue '{title}' in {owner}/{repo}",
            "result": result,
        })
    except Exception as exc:
        return p.ToolResult({
            "status": "error",
            "error": str(exc),
        })


@p.tool
async def github_list_repos(
    context: p.ToolContext,
    user_id: Annotated[
        str,
        p.ToolParameterOptions(
            description="Unique identifier for the user",
            source="customer",
        ),
    ],
) -> p.ToolResult:
    """List repositories accessible to the authenticated GitHub user."""
    try:
        client = get_composio_client()
        
        result = client.tools.execute(
            "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER",
            user_id=user_id,
            arguments={},
        )
        
        return p.ToolResult({
            "status": "success",
            "repositories": result,
        })
    except Exception as exc:
        return p.ToolResult({
            "status": "error",
            "error": str(exc),
        })


# =============================================================================
# Slack-Specific Tools
# =============================================================================

@p.tool
async def slack_send_message(
    context: p.ToolContext,
    user_id: Annotated[
        str,
        p.ToolParameterOptions(
            description="Unique identifier for the user",
            source="customer",
        ),
    ],
    channel: Annotated[
        str,
        p.ToolParameterOptions(
            description="Slack channel name (e.g., '#general') or channel ID",
            source="customer",
            examples=["#general", "#engineering", "C01234567"],
        ),
    ],
    message: Annotated[
        str,
        p.ToolParameterOptions(
            description="Message text to send (supports Slack markdown)",
            source="customer",
        ),
    ],
) -> p.ToolResult:
    """Send a message to a Slack channel."""
    try:
        client = get_composio_client()
        
        result = client.tools.execute(
            "SLACK_SEND_MESSAGE",
            user_id=user_id,
            arguments={
                "channel": channel,
                "text": message,
            },
        )
        
        return p.ToolResult({
            "status": "success",
            "message": f"Sent message to {channel}",
            "result": result,
        })
    except Exception as exc:
        return p.ToolResult({
            "status": "error",
            "error": str(exc),
        })


# =============================================================================
# Gmail-Specific Tools
# =============================================================================

@p.tool
async def gmail_send_email(
    context: p.ToolContext,
    user_id: Annotated[
        str,
        p.ToolParameterOptions(
            description="Unique identifier for the user",
            source="customer",
        ),
    ],
    to: Annotated[
        str,
        p.ToolParameterOptions(
            description="Recipient email address",
            source="customer",
            examples=["recipient@example.com"],
        ),
    ],
    subject: Annotated[
        str,
        p.ToolParameterOptions(
            description="Email subject line",
            source="customer",
        ),
    ],
    body: Annotated[
        str,
        p.ToolParameterOptions(
            description="Email body content",
            source="customer",
        ),
    ],
) -> p.ToolResult:
    """Send an email using Gmail."""
    try:
        client = get_composio_client()
        
        result = client.tools.execute(
            "GMAIL_SEND_EMAIL",
            user_id=user_id,
            arguments={
                "to": to,
                "subject": subject,
                "body": body,
            },
        )
        
        return p.ToolResult({
            "status": "success",
            "message": f"Sent email to {to}",
            "result": result,
        })
    except Exception as exc:
        return p.ToolResult({
            "status": "error",
            "error": str(exc),
        })


# =============================================================================
# Export all tools for easy import
# =============================================================================

# List of all Composio tools for use in guidelines
ALL_COMPOSIO_TOOLS = [
    connect_composio_account,
    check_composio_connection,
    execute_composio_tool,
    list_composio_tools,
    github_create_issue,
    github_list_repos,
    slack_send_message,
    gmail_send_email,
]

# Grouped tools for specific use cases
AUTH_TOOLS = [connect_composio_account, check_composio_connection]
GITHUB_TOOLS = [github_create_issue, github_list_repos]
SLACK_TOOLS = [slack_send_message]
GMAIL_TOOLS = [gmail_send_email]
GENERIC_TOOLS = [execute_composio_tool, list_composio_tools]
