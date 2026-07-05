const BASE_URL = import.meta.env.VITE_API_BASE_URL;

async function request(path, options) {
  if (!BASE_URL) {
    throw new Error("VITE_API_BASE_URL is not set -- check webapp/.env");
  }
  const res = await fetch(`${BASE_URL}${path}`, options);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${text ? ` -- ${text}` : ""}`);
  }
  return res.json();
}

export function getReport() {
  return request("/report");
}

export function runPipeline() {
  return request("/run", { method: "POST" });
}
