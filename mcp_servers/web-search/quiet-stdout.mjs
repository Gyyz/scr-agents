// Preload for Node MCP servers that write log lines with console.log.
// stdout is the MCP transport; anything that is not JSON-RPC on it is noise for the client.
// Usage (mcp.yaml args): ["--import", "{dir}/quiet-stdout.mjs", "{dir}/server/dist/index.js"]
console.log = (...args) => console.error(...args);
console.info = (...args) => console.error(...args);
