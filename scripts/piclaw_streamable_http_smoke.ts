// Manual compatibility smoke test for Piclaw's bundled MCP SDK.
// Start examples/calculator_server.py with Streamable HTTP, then set MCP_URL.

const sdkRoot = process.env.MCP_SDK_ROOT;
const serverUrl = process.env.MCP_URL;
if (!sdkRoot || !serverUrl) {
  throw new Error("Set MCP_SDK_ROOT and MCP_URL; see readme.md");
}

const { Client } = await import(`${sdkRoot}/dist/esm/client/index.js`);
const { StreamableHTTPClientTransport } = await import(
  `${sdkRoot}/dist/esm/client/streamableHttp.js`
);

const client = new Client({ name: "umcp-piclaw-smoke", version: "1.0.0" });
const transport = new StreamableHTTPClientTransport(new URL(serverUrl));

try {
  await client.connect(transport);
  const listed = await client.listTools();
  if (!listed.tools.some((tool: { name: string }) => tool.name === "add")) {
    throw new Error("calculator tool 'add' was not discovered");
  }
  const called = await client.callTool({ name: "add", arguments: { a: 2, b: 3 } });
  const text = called.content?.[0]?.type === "text" ? called.content[0].text : "";
  if (!text.includes('"result": 5')) {
    throw new Error(`unexpected tool result: ${text}`);
  }
  console.log("Streamable HTTP smoke passed: discovery and tool call succeeded");
} finally {
  await client.close();
}
