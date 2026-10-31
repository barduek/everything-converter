/**
 * Cliente de la API. Las solicitudes que cambian estado llevan X-Converter-Request
 * y, si hay cuerpo, Content-Type JSON (Req 19.1). El envío de archivos no pasa por acá.
 */

export function createApi(intervalSeconds) {
  const limit = Math.max(1, Number(intervalSeconds) || 5) * 3000;
  let lastOk = Date.now();
  const listeners = new Set();

  function offline() {
    return Date.now() - lastOk >= limit;
  }

  function notify() {
    const down = offline();
    for (const listener of listeners) listener(down);
  }

  function noteOk() {
    lastOk = Date.now();
    notify();
  }

  setInterval(notify, 1000);

  async function send(path, method, body) {
    const headers = { "X-Converter-Request": "1" };
    const init = { method, headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, init);
      noteOk();
      let data = null;
      try {
        data = await response.json();
      } catch {
        data = null;
      }
      return { ok: response.ok, status: response.status, data };
    } catch {
      notify();
      return { ok: false, status: 0, data: null };
    }
  }

  return {
    intervalSeconds: Math.max(1, Number(intervalSeconds) || 5),
    onConnection(listener) {
      listeners.add(listener);
      listener(offline());
    },
    getTask(id) {
      return send(`/api/tasks/${id}`, "GET");
    },
    confirm(id, body) {
      return send(`/api/tasks/${id}/confirm`, "POST", body);
    },
    decline(id, body) {
      return send(`/api/tasks/${id}/decline`, "POST", body);
    },
    cancel(id) {
      return send(`/api/tasks/${id}/cancel`, "POST");
    },
  };
}
