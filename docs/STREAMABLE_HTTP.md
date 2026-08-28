# Streamable HTTP contract

uMCP's Streamable HTTP transport supports ordinary HTTP persistence and MCP sessions as separate layers. A client may reuse one TCP connection for sequential POST requests, while a session-bound GET uses another connection for server-sent events.

## HTTP connections

* HTTP/1.1 connections are persistent unless either peer sends `Connection: close`, request framing is unsafe to continue, the 30-second idle read expires, or the configured request count is exhausted.
* HTTP/1.0 connections close after a response unless the client explicitly requests keep-alive.
* Requests on one connection are processed in order. Separate connections remain concurrent through the threaded synchronous server or asyncio tasks.
* Every response that terminates its connection includes `Connection: close`. A server must never advertise or imply persistence and then close an otherwise valid connection without warning.
* Request bodies keep the existing size limit. Malformed framing, ambiguous singleton headers, transfer encoding, oversized bodies and incomplete bodies close the connection after their error response because unread bytes cannot be parsed safely as another request.

## MCP sessions

A successful `initialize` response includes a new opaque `Mcp-Session-Id`. The session records the authenticated principal and negotiated protocol version; the identifier is routing state, not a credential.

Subsequent POST requests may include that identifier. Known sessions are checked against the authenticated principal and protocol version, then exposed through `MCPRequestContext.session_id`. Requests without a session remain available for backwards-compatible stateless use. Unknown sessions return 404, mismatched principals return 403 and mismatched protocol versions return 400.

Sessions are bounded to 1,024 live entries and expire after 30 minutes without activity. A client terminates its session with authenticated `DELETE /mcp`; deletion also closes an attached event stream and removes per-session resource subscriptions.

## Server event stream

An authenticated `GET /mcp` with `Accept: text/event-stream`, `MCP-Protocol-Version` and `Mcp-Session-Id` attaches one event stream to that session. A second simultaneous stream for the same session returns 409. The server sends SSE comments every 15 seconds so clients and intermediaries can detect a live connection.

Tool, prompt, resource, logging and progress notifications use the session's active stream. Resource subscriptions are scoped to the session. A disconnected stream may reconnect while the session is live; notifications emitted during the gap are not replayed.

GET without a usable session returns 400 or 404 rather than creating anonymous streaming state. Authentication and Origin checks run on POST, GET and DELETE, and a session cannot be moved between principals by presenting its identifier with different credentials.

## Browser access

Preflight allows `Content-Type`, `Accept`, `MCP-Protocol-Version`, `Mcp-Session-Id`, `Last-Event-ID` and `Authorization`. Allowed browser responses expose `Mcp-Session-Id`. Origin matching, duplicate-header rejection and auxiliary-route boundaries are unchanged.
