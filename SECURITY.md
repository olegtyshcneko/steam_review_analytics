# Security policy

Please report vulnerabilities through GitHub's private security-advisory feature
for this repository. Do not include live credentials, private Steam data, or raw
review corpora in public issues.

The bundled MCP server is local and uses stdio by default. Its Streamable HTTP
transport is intended for development until authentication, authorization,
rate-limiting, and deployment-specific origin controls are configured. Do not
expose the development HTTP server directly to the public internet.
