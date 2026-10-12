# Shared assistant API

GET /api/copilot/info reports the app origin and stable data-space identity. GET /api/copilot/launcher.js provides the family launcher; GET /api/copilot/panel.js and /api/copilot/proxy relay the configured global Faustus service, including streamed requests. Tokens remain on the server.

The browser context includes the native selected service, site, incident or GPU, the current filters and edited form fields. New and missing objects have no active ID. Turn completion reloads the visible native data while retaining pending form changes. Site saves send only changed fields and merge into the existing site.

The existing 16 MCP tools remain available through mcp_server.py and the native /api/agent/tools and /api/agent/call contracts. Conversation history and model execution belong to Faustus; this app introduces no duplicate agent loop or chat store.
