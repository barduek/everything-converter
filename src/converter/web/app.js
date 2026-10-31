/** Arranque de la página: configuración, tareas guardadas y módulos (Req 18.12, 18.13). */

import { createApi } from "./api.js";
import { markGone, upsert } from "./cards.js";
import { createPoller } from "./poller.js";
import { addId, loadIds, onStorage, removeId } from "./store.js";
import { bindUpload } from "./upload.js";

const cards = document.querySelector("#cards");
const connection = document.querySelector("#connection");

const configResponse = await fetch("/api/config");
const config = await configResponse.json();
const api = createApi(config.progress_interval_seconds);

api.onConnection((down) => {
  connection.hidden = !down;
});

function errorText(result) {
  const error = result.data && result.data.error;
  return error && error.message ? error.message : "No se pudo completar la solicitud.";
}

const actions = {
  async confirm(id, body) {
    const result = await api.confirm(id, body);
    if (result.ok) upsert(cards, result.data, actions);
    else if (result.data) say(id, errorText(result));
  },
  async decline(id, body) {
    const result = await api.decline(id, body);
    if (result.ok) upsert(cards, result.data, actions);
  },
  async cancel(id) {
    const result = await api.cancel(id);
    if (result.ok) upsert(cards, result.data, actions);
  },
};

function say(id, text) {
  const card = cards.querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (!card) return;
  const note = document.createElement("p");
  note.textContent = text;
  card.append(note);
}

const poller = createPoller({
  api,
  onTask(task) {
    upsert(cards, task, actions);
  },
  onGone(id) {
    markGone(cards, id);
    removeId(id);
  },
});

function watchSaved(ids) {
  for (const id of ids) poller.watch(id);
}

bindUpload({
  area: document.querySelector("#dropzone"),
  input: document.querySelector("#file"),
  button: document.querySelector("#choose"),
  status: document.querySelector("#upload-status"),
  onTask(task) {
    addId(task.id);
    upsert(cards, task, actions);
    poller.watch(task.id);
  },
});

watchSaved(loadIds());
onStorage((ids) => {
  const known = new Set(loadIds());
  for (const card of cards.querySelectorAll("[data-id]")) {
    if (!known.has(card.dataset.id) && !card.querySelector(".gone")) {
      markGone(cards, card.dataset.id);
      poller.forget(card.dataset.id);
    }
  }
  watchSaved(ids);
});
