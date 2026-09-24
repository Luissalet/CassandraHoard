// Thin fetch wrapper: JSON in/out, `{ error }` bodies become exceptions.
async function request(method, path, { params, body } = {}) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== "" && value !== false) url.searchParams.set(key, value);
  }
  const response = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { error: text };
  }
  if (!response.ok) throw new Error((data && data.error) || `Error ${response.status}`);
  return data;
}

export const api = {
  status: () => request("GET", "/api/status"),
  services: () => request("GET", "/api/services"),
  service: (id) => request("GET", `/api/services/${encodeURIComponent(id)}`),
  lanes: (hours = 24) => request("GET", "/api/lanes", { params: { hours } }),
  incidents: (params) => request("GET", "/api/incidents", { params }),
  incident: (id) => request("GET", `/api/incidents/${id}`),
  logs: (params) => request("GET", "/api/logs", { params }),
  logSources: () => request("GET", "/api/logs/sources"),
  gpu: (params) => request("GET", "/api/gpu", { params }),
  settings: () => request("GET", "/api/settings"),
  watch: (body) => request("POST", "/api/services", { body }),
  unwatch: (id) => request("DELETE", `/api/services/${encodeURIComponent(id)}`),
  policy: (id, body) => request("PUT", `/api/services/${encodeURIComponent(id)}/policy`, { body }),
  restart: (id) => request("POST", `/api/services/${encodeURIComponent(id)}/restart`),
  poll: () => request("POST", "/api/poll"),
  audit: (params) => request("GET", "/api/audit", { params }),
  auditStats: (params) => request("GET", "/api/audit/stats", { params }),
  auditSync: () => request("POST", "/api/audit/sync"),
  secrets: () => request("GET", "/api/secrets"),
};
