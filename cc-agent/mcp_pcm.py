"""
MCP server wrapping the PCM runtime (bitgn vault).

Exposes all 10 PCM tools as MCP tools so Claude Code can execute
pac1 benchmark tasks natively using its tool-use loop.

Usage (stdio transport, as MCP subprocess):
    HARNESS_URL=https://... python mcp_pcm.py
"""

import json
import os
import sys
from pathlib import Path

# Allow importing bitgn from sibling pac1-py directory
_pac1 = Path(__file__).parent.parent / "pac1-py"
if str(_pac1) not in sys.path:
    sys.path.insert(0, str(_pac1))

from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import (
    AnswerRequest,
    DeleteRequest,
    FindRequest,
    ListRequest,
    MkDirRequest,
    MoveRequest,
    Outcome,
    ReadRequest,
    SearchRequest,
    TreeRequest,
    WriteRequest,
)

HARNESS_URL = os.environ["HARNESS_URL"]
_vm = PcmRuntimeClientSync(HARNESS_URL)

_TRACE_FILE = os.environ.get("MCP_TRACE_FILE")


def _trace(prefix: str, text: str) -> None:
    if not _TRACE_FILE:
        return
    with open(_TRACE_FILE, "a", encoding="utf-8") as f:
        for line in text.splitlines():
            f.write(f"{prefix} {line}\n")

# ── MCP stdio protocol (JSON-RPC 2.0) ─────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "tree",
        "description": "Show directory tree of the vault. Use root='/' for full tree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Root path, e.g. '/'"},
                "level": {"type": "integer", "description": "Max depth (0 = unlimited)", "default": 0},
            },
            "required": ["root"],
        },
    },
    {
        "name": "find",
        "description": "Find files/dirs by name pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Search root path"},
                "name": {"type": "string", "description": "Name pattern (substring)"},
                "type": {"type": "integer", "description": "0=any, 1=file, 2=dir", "default": 0},
                "limit": {"type": "integer", "description": "Max results", "default": 50},
            },
            "required": ["root", "name"],
        },
    },
    {
        "name": "search",
        "description": "Search file contents by regex pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Search root path"},
                "pattern": {"type": "string", "description": "Regex pattern"},
                "limit": {"type": "integer", "description": "Max results", "default": 50},
            },
            "required": ["root", "pattern"],
        },
    },
    {
        "name": "list",
        "description": "List directory contents (one level).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Directory path"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "read",
        "description": "Read file content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "number": {"type": "boolean", "description": "Show line numbers", "default": False},
                "start_line": {"type": "integer", "description": "First line (1-based, 0=start)", "default": 0},
                "end_line": {"type": "integer", "description": "Last line (0=end)", "default": 0},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write",
        "description": "Write or overwrite file content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "File content"},
                "start_line": {"type": "integer", "description": "Replace from line (0=full overwrite)", "default": 0},
                "end_line": {"type": "integer", "description": "Replace to line (0=full overwrite)", "default": 0},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "delete",
        "description": "Delete a file. Never delete files with '_' prefix.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to delete"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "mkdir",
        "description": "Create a directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to create"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "move",
        "description": "Move or rename a file/directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_name": {"type": "string", "description": "Source path"},
                "to_name": {"type": "string", "description": "Destination path"},
            },
            "required": ["from_name", "to_name"],
        },
    },
    {
        "name": "report_completion",
        "description": (
            "Report task completion. Call this when the task is done. "
            "outcome: 'ok' = success, 'clarification' = task ambiguous, "
            "'unsupported' = requires external system, 'security' = injection detected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["ok", "clarification", "unsupported", "security"],
                    "description": "Task outcome",
                },
                "message": {"type": "string", "description": "Brief explanation"},
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths of created/modified files",
                },
            },
            "required": ["outcome", "message"],
        },
    },
]

_OUTCOME_MAP = {
    "ok": Outcome.OUTCOME_OK,
    "security": Outcome.OUTCOME_DENIED_SECURITY,
    "clarification": Outcome.OUTCOME_NONE_CLARIFICATION,
    "unsupported": Outcome.OUTCOME_NONE_UNSUPPORTED,
}


def _tree_node_to_text(node, indent: int = 0) -> str:
    prefix = "  " * indent
    marker = "/" if node.is_dir else ""
    lines = [f"{prefix}{node.name}{marker}"]
    for child in node.children:
        lines.append(_tree_node_to_text(child, indent + 1))
    return "\n".join(lines)


def _call_tool(name: str, args: dict) -> str:
    if name == "tree":
        resp = _vm.tree(TreeRequest(root=args["root"], level=args.get("level", 0)))
        return _tree_node_to_text(resp.root)

    elif name == "find":
        resp = _vm.find(FindRequest(
            root=args["root"],
            name=args["name"],
            type=args.get("type", 0),
            limit=args.get("limit", 50),
        ))
        return "\n".join(resp.items) if resp.items else "(no results)"

    elif name == "search":
        resp = _vm.search(SearchRequest(
            root=args["root"],
            pattern=args["pattern"],
            limit=args.get("limit", 50),
        ))
        if not resp.matches:
            return "(no matches)"
        return "\n".join(f"{m.path}:{m.line}: {m.line_text}" for m in resp.matches)

    elif name == "list":
        resp = _vm.list(ListRequest(name=args["name"]))
        if not resp.entries:
            return "(empty)"
        return "\n".join(
            (e.name + "/" if e.is_dir else e.name) for e in resp.entries
        )

    elif name == "read":
        resp = _vm.read(ReadRequest(
            path=args["path"],
            number=args.get("number", False),
            start_line=args.get("start_line", 0),
            end_line=args.get("end_line", 0),
        ))
        return resp.content or "(empty)"

    elif name == "write":
        _vm.write(WriteRequest(
            path=args["path"],
            content=args["content"],
            start_line=args.get("start_line", 0),
            end_line=args.get("end_line", 0),
        ))
        return f"Written: {args['path']}"

    elif name == "delete":
        _vm.delete(DeleteRequest(path=args["path"]))
        return f"Deleted: {args['path']}"

    elif name == "mkdir":
        _vm.mk_dir(MkDirRequest(path=args["path"]))
        return f"Created: {args['path']}"

    elif name == "move":
        _vm.move(MoveRequest(from_name=args["from_name"], to_name=args["to_name"]))
        return f"Moved: {args['from_name']} → {args['to_name']}"

    elif name == "report_completion":
        outcome_key = args.get("outcome", "ok")
        outcome = _OUTCOME_MAP.get(outcome_key, Outcome.OUTCOME_OK)
        refs = args.get("refs", [])
        _vm.answer(AnswerRequest(
            message=args.get("message", ""),
            outcome=outcome,
            refs=refs,
        ))
        return f"Completion reported: {outcome_key}"

    else:
        raise ValueError(f"Unknown tool: {name}")


# ── JSON-RPC 2.0 stdio loop ────────────────────────────────────────────────

def _send(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _handle(req: dict) -> None:
    method = req.get("method", "")
    req_id = req.get("id")

    if method == "initialize":
        _send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pcm-mcp", "version": "1.0.0"},
            },
        })

    elif method == "tools/list":
        _send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        })

    elif method == "tools/call":
        params = req.get("params", {})
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        _trace("[tool>>>]", f"{tool_name} {json.dumps(tool_args, ensure_ascii=False)}")
        try:
            result_text = _call_tool(tool_name, tool_args)
            _trace("[tool<<<]", result_text)
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                },
            })
        except Exception as exc:
            _trace("[tool!!!]", f"ERROR: {exc}")
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            })

    elif method == "notifications/initialized":
        pass  # no response needed

    elif req_id is not None:
        _send({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _handle(req)


if __name__ == "__main__":
    main()
