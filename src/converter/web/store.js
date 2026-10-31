/** Identificadores de Tarea en localStorage, en orden de creación (Req 18.12, 19.7). */

const KEY = "converter.tasks";

export function loadIds() {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) || "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((id) => typeof id === "string");
  } catch {
    return [];
  }
}

function saveIds(ids) {
  localStorage.setItem(KEY, JSON.stringify(ids));
}

export function addId(id) {
  const ids = loadIds();
  if (!ids.includes(id)) {
    ids.push(id);
    saveIds(ids);
  }
  return ids;
}

export function removeId(id) {
  saveIds(loadIds().filter((item) => item !== id));
}

/** Otra pestaña del mismo perfil cambió la lista. */
export function onStorage(callback) {
  window.addEventListener("storage", (event) => {
    if (event.key === KEY) callback(loadIds());
  });
}
