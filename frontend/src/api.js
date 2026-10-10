const BASE = import.meta.env.VITE_API_URL || "";
const TOKEN_KEY = "pd_token";

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    return;
  }
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function parseError(res) {
  let message = `Request failed (${res.status})`;
  try {
    const data = await res.json();
    if (typeof data.detail === "string") message = data.detail;
    else if (Array.isArray(data.detail)) message = data.detail.map((d) => d.msg).join(", ");
  } catch {
    return new ApiError(message, res.status);
  }
  return new ApiError(message, res.status);
}

export async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(`${BASE}${path}`, { method, headers, body: payload });
  if (res.status === 401 && token) {
    setToken(null);
    window.dispatchEvent(new Event("pd:logout"));
  }
  if (!res.ok) throw await parseError(res);
  if (res.status === 204) return null;
  return res.json();
}

export async function streamChat({ question, conversationId, onEvent, signal }) {
  const res = await fetch(`${BASE}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({ question, conversation_id: conversationId || null }),
    signal,
  });
  if (!res.ok) throw await parseError(res);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const line = raw.split("\n").find((l) => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)));
    }
  }
}
