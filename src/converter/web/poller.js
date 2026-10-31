/** Sondeo de cada Tarea según su estado (Req 3.1, 14.5, 18.12, 18.14). */

const TERMINAL = new Set(["completada", "fallida", "cancelada"]);

export function delayMs(task, intervalSeconds) {
  if (task.detection === "en_curso") return 200;
  if (!TERMINAL.has(task.state)) return Math.max(500, (intervalSeconds / 2) * 1000);
  return 15000;
}

export function createPoller({ api, onTask, onGone }) {
  const timers = new Map();

  function clear(id) {
    const timer = timers.get(id);
    if (timer) clearTimeout(timer);
    timers.delete(id);
  }

  async function tick(id) {
    const result = await api.getTask(id);
    if (result.status === 404) {
      clear(id);
      onGone(id);
      return;
    }
    if (!result.ok || !result.data) {
      const wait = delayMs({ state: "en_proceso", detection: "completa" }, api.intervalSeconds);
      timers.set(id, setTimeout(() => tick(id), wait));
      return;
    }
    onTask(result.data);
    timers.set(id, setTimeout(() => tick(id), delayMs(result.data, api.intervalSeconds)));
  }

  return {
    watch(id) {
      if (timers.has(id)) return;
      timers.set(id, setTimeout(() => tick(id), 0));
    },
    forget(id) {
      clear(id);
    },
  };
}
