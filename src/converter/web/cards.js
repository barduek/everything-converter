/** Tarjeta de Tarea. Todo el texto va con textContent (Req 18.6 a 18.10). Sin selector de idioma: el MVP no transcribe. */

const LOSS = {
  Sin_Pérdida: "Sin pérdida",
  Con_Pérdida: "Con pérdida",
  Limitada: "Limitada",
  Aproximada: "Aproximada",
};

const ACTIVE = new Set(["recibida", "en_cola", "en_proceso"]);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function lossName(value) {
  return LOSS[value] || value;
}

export function renderCard(task, actions) {
  const card = el("article", "card");
  card.dataset.id = task.id;
  card.dataset.version = String(task.version ?? "");
  card.append(el("h3", "", task.original_name || "Archivo"));

  if (task.real_type) card.append(el("p", "", `Formato detectado: ${task.real_type}`));
  card.append(el("p", "", `Estado: ${task.state_label || task.state || ""}`));

  for (const notice of task.notices || []) {
    card.append(el("p", "", notice.message));
  }

  if (task.state === "recibida" && task.detection === "completa" && (task.targets || []).length) {
    const targets = el("div", "targets");
    for (const target of task.targets) {
      const label = `${target.format} · ${lossName(target.loss_class)}`;
      const button = el("button", "", label);
      button.type = "button";
      button.addEventListener("click", () => choose(card, task, target, actions));
      targets.append(button);
    }
    card.append(targets);
  }

  if (typeof task.progress === "number") {
    const bar = document.createElement("progress");
    bar.max = 100;
    bar.value = task.progress;
    bar.append(el("span", "", `${task.progress} %`));
    card.append(bar, el("p", "", `Avance: ${task.progress} %`));
  }

  if (task.failure) {
    const box = el("div", "failure");
    box.append(
      el("p", "", task.failure.message),
      el("p", "", task.failure.action),
    );
    card.append(box);
  }

  const row = el("div", "actions");
  if (ACTIVE.has(task.state)) {
    const cancel = el("button", "quiet", "Cancelar");
    cancel.type = "button";
    cancel.addEventListener("click", () => actions.cancel(task.id));
    row.append(cancel);
  }
  if (task.download && !task.download.expired) {
    const link = document.createElement("a");
    link.href = task.download.url;
    link.download = "";
    link.textContent = `Descargar ${task.download.name}`;
    row.append(link);
  } else if (task.download && task.download.expired) {
    card.append(el("p", "", "El resultado ya no está disponible."));
  }
  if (row.childNodes.length) card.append(row);
  return card;
}

function choose(card, task, target, actions) {
  const previous = card.querySelector(".confirm");
  if (previous) previous.remove();
  if (!target.requires_confirmation) {
    actions.confirm(task.id, { target: target.format, language: null, acknowledge_loss: false });
    return;
  }
  const panel = el("div", "confirm");
  panel.append(
    el("p", "", target.loss_description),
    el("p", "", "Esta conversión tiene pérdida. Confirmá para seguir."),
  );
  const ok = el("button", "", "Confirmar");
  ok.type = "button";
  ok.addEventListener("click", () => {
    actions.confirm(task.id, { target: target.format, language: null, acknowledge_loss: true });
  });
  const no = el("button", "quiet", "Volver");
  no.type = "button";
  no.addEventListener("click", () => {
    actions.decline(task.id, { target: target.format });
    panel.remove();
  });
  const row = el("div", "actions");
  row.append(ok, no);
  panel.append(row);
  card.append(panel);
}

export function upsert(container, task, actions) {
  const next = renderCard(task, actions);
  const current = container.querySelector(`[data-id="${CSS.escape(task.id)}"]`);
  if (current && current.dataset.version === next.dataset.version) return;
  if (current) current.replaceWith(next);
  else container.append(next);
}

export function markGone(container, id) {
  const current = container.querySelector(`[data-id="${CSS.escape(id)}"]`);
  const card = el("article", "card");
  card.dataset.id = id;
  card.append(el("p", "gone", "Esta tarea ya no está disponible."));
  if (current) current.replaceWith(card);
  else container.append(card);
}
