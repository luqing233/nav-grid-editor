const pending = new Map();
// Share only in-flight reads. Mutations never reuse an old completed response.
export function sharedJson(url) {
  if (!pending.has(url)) {
    const request = fetch(url).then(async response => {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).finally(() => pending.delete(url));
    pending.set(url, request);
  }
  return pending.get(url).then(data => ({ json: async () => structuredClone(data) }));
}
