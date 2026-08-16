async function request(path, options) {
  const res = await fetch(path, options);
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
