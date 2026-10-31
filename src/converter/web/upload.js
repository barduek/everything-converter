/** Envío de un archivo con XMLHttpRequest (Req 18.3, 18.4, 18.5, 18.11). */

function messageOf(xhr) {
  try {
    const data = JSON.parse(xhr.responseText);
    if (data && data.error && data.error.message) return data.error.message;
  } catch {
    /* el cuerpo no es JSON */
  }
  return "No se pudo recibir el archivo. Probá de nuevo.";
}

function oneFile(dataTransfer) {
  const items = dataTransfer.items;
  if (!items || items.length !== 1 || items[0].kind !== "file") {
    return { error: items && items.length > 1 ? "Soltá un solo archivo." : "Soltá un archivo." };
  }
  const entry = items[0].webkitGetAsEntry ? items[0].webkitGetAsEntry() : null;
  if (entry && entry.isDirectory) return { error: "No se pueden soltar carpetas." };
  const file = items[0].getAsFile();
  if (!file) return { error: "Soltá un archivo." };
  return { file };
}

export function bindUpload({ area, input, button, status, onTask }) {
  let current = null;

  function say(text) {
    status.textContent = text;
  }

  function send(file) {
    if (current) current.abort();
    say(`Recibiendo «${file.name}»…`);
    const xhr = new XMLHttpRequest();
    current = xhr;
    xhr.open("POST", "/api/tasks");
    xhr.setRequestHeader("X-Converter-Request", "1");
    xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
    xhr.onload = () => {
      current = null;
      if (xhr.status === 201) {
        say("");
        onTask(JSON.parse(xhr.responseText));
        return;
      }
      say(messageOf(xhr));
    };
    xhr.onerror = () => {
      current = null;
      say("Se interrumpió el envío. Revisá la conexión y probá de nuevo.");
    };
    xhr.onabort = () => {
      current = null;
      say("Envío cancelado.");
    };
    xhr.send(file);
  }

  button.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    input.value = "";
    if (file) send(file);
  });

  document.addEventListener("dragover", (event) => {
    event.preventDefault();
  });
  document.addEventListener("drop", (event) => {
    if (!area.contains(event.target)) event.preventDefault();
  });
  area.addEventListener("dragover", (event) => {
    event.preventDefault();
    area.classList.add("drag");
  });
  area.addEventListener("dragleave", () => area.classList.remove("drag"));
  area.addEventListener("drop", (event) => {
    event.preventDefault();
    area.classList.remove("drag");
    const picked = oneFile(event.dataTransfer);
    if (picked.error) say(picked.error);
    else send(picked.file);
  });
}
