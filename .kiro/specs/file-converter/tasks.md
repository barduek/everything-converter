# Implementation Plan: file-converter

## Overview

El plan construye el Conversor desde cero en Python 3.12, siguiendo `design.md`. El orden es incremental: primero el esqueleto y el núcleo puro (configuración, nombres, mensajes, Registro_de_Conversiones, Detector_de_Tipo, SRT), después el Gestor_de_Archivos_Temporales, el Ejecutor aislado, el Gestor_de_Tareas, la API HTTP y el arranque. Con eso el Conversor ya arranca, recibe archivos y detecta su Tipo_Real. Luego cada familia de convertidores (imágenes, PDF, documentos de texto, DOCX, audio, video, transcripción) agrega sus módulos y registra sus Pares_de_Conversión en `catalog/pairs.toml`, sin tocar el núcleo. Al final se agregan la Interfaz_de_Usuario, la Imagen_de_Contenedor multiarquitectura, la Guía_de_Uso, el Inventario_de_Licencias, la integración continua y las pruebas en contenedor. Primero se ejecuta un MVP con una parte de ese alcance; la sección «Fuera del alcance del MVP» detalla lo que queda para después.

Convenciones del plan:

- Cada propiedad de correctitud se implementa en su propio archivo `tests/properties/test_pNN_<tema>.py`, con el comentario `# Feature: file-converter, Property N: <título>` y el perfil de Hypothesis del proyecto (al menos 100 ejemplos).
- Los generadores compartidos viven en `tests/strategies/<área>.py`. Solo la sub-tarea que lo indica crea ese módulo; las demás lo importan y definen en su propio archivo cualquier generador adicional.
- Las pruebas unitarias van en `tests/unit/test_<módulo>.py` y usan `@pytest.mark.req("X.Y")` para la trazabilidad.
- Las pruebas que necesitan FFmpeg, LibreOffice, whisper-cli o exiftool usan la marca `requires_tool`: se omiten fuera del contenedor y corren dentro de la imagen.

## Fuera del alcance del MVP

El MVP cubre el flujo completo: recibir un archivo, detectar su Tipo_Real, ofrecer los Formatos_de_Destino, convertir y descargar el resultado desde la Interfaz_de_Usuario, con una Imagen_de_Contenedor construida para la arquitectura local. Incluye 30 de los 36 Pares_de_Conversión del Mapa_de_Conversión_Inicial: 9 de imagen, 2 de PDF, 8 de documentos de texto, 1 de DOCX, 6 de audio y 4 de video. Lo que sigue queda para después: sus sub-tareas están marcadas con `*`, y las tareas que se simplificaron tienen una línea **MVP**.

### Transcripción

- Quedan fuera los 6 Pares_de_Transcripción: MP3, WAV y MP4 a TXT y a SRT.
- Tareas: 21.1 a 21.7 y 24.2. La tarjeta de Tarea (23.4) no muestra el selector de Idioma_de_Transcripción ni el Idioma_Detectado.
- Criterios sin cumplir: todo el Requisito 11, 12.2, 12.3, 12.9, 12.10 y 13.8. En parte: 4.9 (el registro tiene 30 pares en lugar de 36), 12.1 (el Formato SRT está registrado, pero sin pares) y 20.1 (la imagen no incluye el Modelo_de_Transcripción).
- Ya está hecho, con sus pruebas: el Serializador_SRT, el Intérprete_SRT, `normalize_segments` y `render_transcript_txt` (tarea 7).
- Para agregarla: compilar whisper.cpp y descargar el modelo en la imagen (24.2), generar `languages.es.json` (21.1), implementar y registrar el convertidor (21.2 y 21.3) y agregar el selector de idioma y el Idioma_Detectado a la tarjeta de Tarea.

### Simplificaciones en tareas del MVP

- **Imagen a PDF con Pillow (15.6).** No se usa el escritor de PDF propio. JPG a PDF re-codifica el JPEG y un PNG de 16 bits se reduce a 8 bits, así que esos pares pueden registrarse como Con_Pérdida en lugar de la clasificación de la tabla Imágenes (4.9, 4.11).
- **DOCX a PDF sin saneamiento (18.4, 18.5 y 18.11 fuera).** Con el perfil propio, LibreOffice no ejecuta macros ni actualiza vínculos, pero el Conversor no informa esas omisiones (7.5). El PDF muestra los comentarios y las marcas de revisión como los muestre LibreOffice, sin aviso (7.6). Sin U+2060, el texto extraído puede partir palabras junto a signos de puntuación (7.3).
- **HTML a MD con `markdownify` básico (17.4).** Sin los escapes, las cercas de código ni la sangría a medida, no se garantizan la Estructura_de_Documento ni la ida y vuelta MD→HTML→MD→HTML (6.5, 6.10).
- **Mezcla a 2 canales con `-ac 2` (19.6).** Con distribuciones de canales que FFmpeg no reconoce, algún canal puede no llegar a la mezcla (9.3, 10.9).
- **FFmpeg del paquete de Debian (24.1 fuera).** Está compilado con `--enable-gpl` y con protocolos de red, así que la imagen queda bajo GPL y hay que revisarlo antes de distribuirla. FFmpeg sigue sin acceder a la red porque corre con `-protocol_whitelist file` y el entorno del Ejecutor aislado (13.7).
- **Imagen para la arquitectura local.** Sin el manifiesto con las variantes amd64 y arm64 (20.1).

### Entregables del repositorio

- Inventario_de_Licencias y `THIRD_PARTY_LICENSES.md` (25.3 y 25.4): el criterio 20.12 queda sin cumplir.
- Integración continua (25.5) y comprobación de trazabilidad entre criterios y pruebas (25.6).
- La Guía_de_Uso (25.1) sí está en el MVP y lista estos pendientes.

### Pruebas

- Las tareas de implementación del MVP traen sus propias pruebas unitarias. Ya están hechas las propiedades 3, 8, 11, 30, 34, 37, 38, 39, 46 a 50, 54, 58, 59 y 64 a 66.
- Quedan fuera:
  - las otras 48 pruebas de propiedades: 1, 2, 4 a 7, 9, 10, 12 a 29, 31 a 33, 35, 36, 40 a 45, 51 a 53, 55 a 57, 60 a 63 y 67;
  - las sub-tareas de pruebas unitarias 5.4, 6.8, 9.3, 10.4, 11.18, 12.8, 13.3, 15.13, 16.8, 17.18, 18.12, 19.9, 20.7, 21.6 y 21.7;
  - las pruebas de extremo a extremo en navegadores (23.6 a 23.8);
  - las pruebas de la imagen y en contenedor (24.4 a 24.14) y la de la Guía_de_Uso (25.2).
- Por eso varios criterios quedan implementados pero sin verificar, por ejemplo el funcionamiento sin red (13.3), el determinismo (13.4), los tiempos de detección y de arranque (2.11, 20.2), el comportamiento en cada navegador (Requisito 18) y el aislamiento entre Tareas (19.3 a 19.6).

### Cómo retomar un pendiente

- Quitar el `*` de sus sub-tareas y ejecutarlas. El grafo de dependencias pone las sub-tareas pendientes en olas posteriores a las del MVP, en su orden original, así que no bloquean al MVP.
- Las propiedades del Gestor_de_Tareas (11.10 a 11.17) necesitan antes 11.9, que crea `tests/fakes.py` y `tests/strategies/tasks.py`. Las pruebas en contenedor (24.6 a 24.14) necesitan antes 24.5, que crea el arnés y el corpus.
- Una simplificación ya implementada no vuelve atrás sola, porque su tarea queda completada: hace falta una sub-tarea nueva que lleve esa parte a lo que pide el diseño.

## Tasks

- [x] 1. Crear el esqueleto del proyecto
  - [x] 1.1 Crear `pyproject.toml`, la estructura de paquetes y la configuración de pruebas
    - `pyproject.toml` con Python 3.12 y el paquete `converter` en `src/`. Dependencias de ejecución fijadas: fastapi, starlette, uvicorn, anyio, pillow, pypng, pypdfium2, weasyprint, markdown-it-py, beautifulsoup4, html5lib, markdownify, lxml, olefile, fonttools. Dependencias de desarrollo: pytest, pytest-asyncio, hypothesis, httpx, python-docx, playwright, babel, ruff, mypy. Generar `uv.lock` con `uv lock`.
    - Crear los paquetes vacíos de `src/converter/` que lista "Estructura del repositorio" (`api`, `registry`, `catalog`, `detection`, `tasks`, `storage`, `sandbox`, `converters`, `workers`, `srt`) y la carpeta `src/converter/web/`.
    - Crear `tests/` con `unit/`, `properties/`, `integration/`, `e2e/`, `benchmarks/`, `fixtures/` y el paquete vacío `tests/strategies/`.
    - `tests/conftest.py`: perfiles de Hypothesis `ci` (`max_examples=100`, `deadline=None`) y `nightly` (1000 ejemplos), elegidos con `HYPOTHESIS_PROFILE`; marcas `req("X.Y")`, `integration` y `requires_tool("nombre")`, que omite la prueba si la herramienta no está en el `PATH`.
    - Prueba de humo `tests/unit/test_smoke.py` que importa `converter`.
    - _Requirements: 20.1, 20.3_

- [x] 2. Implementar la configuración
  - [x] 2.1 Implementar `src/converter/config.py` con `Config`, `ConfigError` y `load_config(env)`
    - `Config` inmutable con las 20 Variables_de_Configuración de la tabla "Configuración" del diseño: nombre, unidad, rango admitido y valor predeterminado.
    - Enteros en base 10 sin signo ni espacios. Un valor vacío, no numérico o fuera de rango es inválido.
    - Rangos dependientes: cada Límite_de_Tamaño por Categoría admite como máximo `CONVERTER_MAX_UPLOAD_MB`, y `CONVERTER_OGG_MAX_SAMPLE_RATE_HZ` admite como mínimo `CONVERTER_OGG_MIN_SAMPLE_RATE_HZ`.
    - Acumular todos los errores antes de devolverlos, uno por variable con su motivo. Exponer los valores derivados en bytes, segundos y Hz.
    - _Requirements: 20.5, 20.6, 1.3, 1.5, 2.9, 5.9, 11.5, 14.4, 14.7, 15.8_

  - [x] 2.2 Escribir la prueba de propiedad de `load_config`
    - **Property 64: La configuración acepta los valores válidos y nombra los inválidos**
    - **Validates: Requirements 20.5, 20.6**
    - Generador local `env_mappings()` con enteros dentro y fuera de rango, vacíos, texto, signos y espacios.

  - [x] 2.3 Escribir pruebas unitarias de la configuración
    - Bordes de cada rango, rangos dependientes, variables no definidas que toman el valor predeterminado y varios errores informados juntos.
    - _Requirements: 20.5, 20.6_

- [x] 3. Implementar la normalización de nombres
  - [x] 3.1 Implementar `src/converter/naming.py`
    - `FORBIDDEN`, `split_name`, `truncate_utf8`, `normalize_base`, `result_name(original, target, kind)` y `page_entry_name(base, n, total)`, como en "Normalización de nombres y entrega".
    - Aplicar el orden de Req 15.12: reemplazo de Caracteres_No_Permitidos, truncado a 200 bytes en UTF-8 por caracteres completos y sustitución de la base vacía por "resultado".
    - _Requirements: 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.10, 15.12, 8.8_

  - [x] 3.2 Escribir la prueba de propiedad del nombre del Archivo_Resultado
    - **Property 58: El nombre del Archivo_Resultado cumple las reglas de normalización**
    - **Validates: Requirements 15.2, 15.3, 15.4, 15.5, 15.6, 15.10, 15.12**
    - Crear `tests/strategies/names.py` con `original_names()` (Unicode, separadores de ruta, `..`, caracteres de control, de 0 a 300 caracteres), que también usan las propiedades 57 y 59.

  - [x] 3.3 Escribir la prueba de propiedad de idempotencia de la Normalización_de_Nombre
    - **Property 59: La Normalización_de_Nombre es idempotente**
    - **Validates: Requirements 15.7**

  - [x] 3.4 Escribir la prueba de propiedad de los nombres de página del ZIP
    - **Property 34: Los nombres de página del ZIP se ordenan como las páginas**
    - **Validates: Requirements 8.8**

  - [x] 3.5 Escribir pruebas unitarias de nombres
    - "Vacaciones 2024.PNG" a JPG da "Vacaciones 2024.jpg"; ".png" da "resultado.jpg"; "notas." no tiene Extensión_del_Nombre; "informe.final.docx" da "informe.final"; "informe.pdf" de 3 páginas a PNG da "informe.zip"; truncado de caracteres multibyte en el byte 200.
    - _Requirements: 15.2, 15.4, 15.5, 15.10_

- [x] 4. Implementar modelos de dominio, mensajes en español y Log_de_Eventos
  - [x] 4.1 Implementar `src/converter/models.py`
    - Enumeraciones `Category`, `LossClass`, `TaskState`, `Stage` y la constante `TERMINAL_STATES`, con los valores en español del diseño.
    - Dataclasses inmutables `Notice`, `Failure` y `DestinationRejection`.
    - _Requirements: 4.1, 4.2, 14.3, 16.1, 16.12_

  - [x] 4.2 Implementar `src/converter/messages_es.py`
    - `FailureReason` y un catálogo único con cada código de las tablas de "Error Handling": errores de recepción, Rechazos_de_Origen, Rechazos_de_Destino, fallos durante el procesamiento, errores de solicitudes sobre Tareas y motivos de cancelación. Cada código tiene su Etapa_de_Procesamiento, una plantilla en español y exactamente una acción sugerida no vacía.
    - Plantillas de avisos (`extension_no_coincide`, `bits_reducidos`, `recursos_externos_omitidos`, `fuentes_reemplazadas`, `comentarios_omitidos`, `cambios_aplicados`, `macros_omitidas`, `paginas_sin_texto`, `frecuencia_cambiada`, `canales_mezclados`, `pistas_no_usadas`, `dimensiones_rellenadas`, `sin_voz`) y nombres en español de los Estados_de_Tarea.
    - `render_failure`, `render_error` y `render_notice` aceptan solo parámetros validados (números, nombres de Formato o Códec, Números_de_Página, caracteres citados) y nunca incluyen rutas ni trazas.
    - _Requirements: 16.4, 16.5, 16.9, 16.13, 18.1, 1.3, 1.4, 1.5, 1.9, 1.10, 2.5, 2.7, 2.9, 3.4, 5.5, 5.6, 5.9, 6.11, 7.2, 7.7, 8.2, 8.7, 8.11, 9.6, 10.3, 10.8, 11.5, 11.6, 11.9, 14.7, 14.8, 14.11, 15.8, 15.11, 16.2, 16.10, 19.5_

  - [x] 4.3 Implementar `src/converter/eventlog.py`
    - Log_de_Eventos como objetos JSON, uno por línea, en la salida estándar, con `ts` en UTC, `level` y `event`. No registra Nombres_Originales ni contenido.
    - `sanitize_tool_output(tool, stderr_lines)` con el conjunto cerrado de patrones de FFmpeg, ffprobe, Pillow, pypdfium2, LibreOffice y whisper.cpp. Devuelve solo el identificador del patrón, su texto fijo, el código de salida, la señal y la cantidad de líneas no reconocidas.
    - Funciones para los eventos `task_failed`, `registry_rejected` y `cleanup_failed`.
    - _Requirements: 16.7, 16.8, 4.5, 4.6, 4.7, 4.12, 4.13, 17.9_

  - [x] 4.4 Implementar `src/converter/converters/errors.py`
    - Tabla de "Clasificación de errores de herramientas": señales de Pillow, pypdfium2, el lector ZIP de DOCX, FFmpeg, LibreOffice y whisper-cli, `ENOSPC` o espacio libre menor a 1 MB, timeout y señales no clasificadas, cada una con su `FailureReason`.
    - _Requirements: 16.2, 16.9, 16.10, 14.7, 8.7, 9.6, 10.8, 11.6_

  - [x] 4.5 Escribir la prueba de propiedad de los mensajes al usuario
    - **Property 66: Los mensajes al usuario tienen etapa, motivo y acción, sin detalle técnico**
    - **Validates: Requirements 16.4, 16.5, 16.13**

  - [x] 4.6 Escribir la prueba de propiedad del saneamiento del Log_de_Eventos
    - **Property 65: El Log_de_Eventos no contiene contenido de los archivos**
    - **Validates: Requirements 16.8**

  - [x] 4.7 Escribir pruebas unitarias de mensajes, log y clasificación
    - Ningún código del catálogo tiene acción vacía; formato del evento `task_failed`; un ejemplo por fila de la tabla de clasificación.
    - _Requirements: 16.4, 16.7, 16.9_

- [ ] 5. Implementar el Registro_de_Conversiones y el catálogo declarativo
  - [x] 5.1 Implementar `src/converter/registry/model.py` y `src/converter/registry/registry.py`
    - `MagicSignature`, `ProbeSignature`, `FormatDef` y `ConversionPair` (con `is_transcription`, `is_long_running` y `requires_confirmation`).
    - `ConversionRegistry(converter_ids)` con `register_format`, `register_pair`, `formats`, `conversion_map`, `targets_for`, `source_formats` y `text_format_for_extension`.
    - Reglas de la tabla "Validación del registro" con sus códigos de error (`formato_invalido`, `formato_en_conflicto`, `firma_duplicada`, `formato_faltante`, `par_duplicado`, `clasificacion_invalida`, `descripcion_faltante`, `descripcion_excedida`, `convertidor_desconocido`). Cada validación ocurre antes de modificar el estado, cada rechazo se registra en el Log_de_Eventos y el registro guarda copias inmutables.
    - _Requirements: 4.1, 4.2, 4.5, 4.6, 4.7, 4.8, 4.10, 4.12, 4.13_

  - [x] 5.2 Implementar `src/converter/registry/loader.py` y crear el catálogo de Formatos
    - Leer `catalog/formats.toml`, `catalog/pairs.toml` y `catalog/pairs.d/*.toml` con `tomllib`. Un error en el catálogo inicial lanza `CatalogError`; una entrada inválida de un catálogo adicional se rechaza, se registra y no cambia el resto del mapa.
    - `catalog/formats.toml` con los 14 Formatos (PNG, JPG, WEBP, PDF, TXT, MD, HTML, DOCX, MP3, WAV, OGG, MP4, WEBM, SRT): extensiones de Req 2.6, Extensión_Principal, tipo MIME, firma de la tabla "Firmas", inspector y la sonda de variante `docx_encrypted`. SRT es de Categoría documento, sin firma y con ".srt".
    - `catalog/pairs.toml` empieza vacío, con un comentario que explica que cada familia de convertidores agrega sus pares.
    - _Requirements: 4.3, 4.4, 4.9, 12.1, 2.6_

  - [x] 5.3 Escribir la prueba de propiedad del registro contra su modelo
    - **Property 8: El Registro_de_Conversiones se comporta como su modelo**
    - **Validates: Requirements 4.1, 4.2, 4.5, 4.6, 4.7, 4.8, 4.10, 4.12, 4.13**
    - `RuleBasedStateMachine` de Hypothesis. Crear `tests/strategies/registry.py` con `registry_specs()`, que también usa la propiedad 1.

  - [~] 5.4 Escribir pruebas unitarias del cargador
    - `formats.toml` carga los 14 Formatos con sus extensiones; atributos de SRT; una entrada inválida en `pairs.d` se rechaza y se registra; un catálogo inicial inválido lanza `CatalogError`.
    - _Requirements: 4.5, 4.6, 4.9, 12.1_

- [ ] 6. Implementar el Detector_de_Tipo
  - [x] 6.1 Implementar `src/converter/detection/text.py`
    - `TextKind`, `classify_text(chunks)` en bloques de 1 MiB con un decodificador UTF-8 incremental estricto y la regla de menos de 1 % de controles para Windows-1252.
    - `decode_text(data, kind)`: quita la BOM_UTF8 y, en Windows-1252, convierte 0x81, 0x8D, 0x8F, 0x90 y 0x9D en el carácter Unicode de igual valor con un manejador de errores propio.
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 2.12_

  - [x] 6.2 Escribir la prueba de propiedad de la clasificación de texto
    - **Property 3: La clasificación de texto sigue las definiciones del glosario**
    - **Validates: Requirements 2.5**
    - Crear `tests/strategies/text_bytes.py` con `valid_utf8_text()`, `windows1252_text()`, `arbitrary_bytes()` y el predicado de referencia del glosario, que también usan las propiedades 2 y 18.

  - [x] 6.3 Implementar las firmas y las sondas
    - `src/converter/detection/signatures.py`: coincidencia de `MagicSignature` sobre los primeros 64 KiB y elección por especificidad (sondas estructurales por encima de firmas de bytes; a igual especificidad, orden de registro).
    - Paquete `src/converter/detection/probes/` con descubrimiento automático y el decorador `@register_probe`: `pdf_header`, `ooxml_wordprocessing`, `mpeg_audio` (ID3v2 opcional y dos cabeceras Layer III consecutivas), `isobmff_mp4` (marcas admitidas y excluidas), `ebml_webm` y `docx_encrypted` (extensión ".docx" y archivo OLE con `EncryptionInfo` y `EncryptedPackage`, leído con olefile).
    - _Requirements: 2.1, 2.8, 2.10, 4.4, 7.2_

  - [x] 6.4 Implementar `src/converter/detection/source_info.py` y `src/converter/detection/detector.py`
    - `source_info.py`: clase base `SourceInfo` con registro de tipos para (de)serializar en JSON la salida de los inspectores, `TextInfo` y `DetectionResult`.
    - `detector.py`: el flujo del diagrama del Detector_de_Tipo. Firma, variante cifrada, Texto_Válido, Formato_de_Texto por extensión o TXT, comprobación de Formato_de_Origen (`tipo_sin_destinos` con la lista de orígenes), Límite_de_Tamaño de la Categoría y, al final, el inspector del Formato, recibido como función inyectada. El inspector `text` corre en el proceso principal.
    - Orden de evaluación de los rechazos del diseño y aviso `extension_no_coincide` cuando la Extensión_del_Nombre no está entre las del Tipo_Real.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.12, 3.6, 4.4, 16.1_

  - [~] 6.5 Escribir la prueba de propiedad de la detección por firma
    - **Property 1: La detección por firma no depende del nombre**
    - **Validates: Requirements 2.1, 2.6, 2.8, 4.4**

  - [~] 6.6 Escribir la prueba de propiedad de los Formatos_de_Texto
    - **Property 2: Los Formatos_de_Texto se determinan por la extensión**
    - **Validates: Requirements 2.2, 2.3, 2.4, 4.4**

  - [~] 6.7 Escribir la prueba de propiedad del Límite_de_Tamaño por Categoría
    - **Property 4: El Límite_de_Tamaño se aplica según la Categoría del Tipo_Real**
    - **Validates: Requirements 2.9**

  - [~] 6.8 Escribir pruebas unitarias de firmas y detección
    - Un ejemplo por firma; MP3 con doble cabecera frente a texto Windows-1252 que empieza con "ÿû"; marcas de MP4 excluidas (`qt  `, `M4A `, `heic`); DOCX cifrado con y sin ".docx"; un ".srt" se rechaza con la lista de Formatos_de_Origen.
    - _Requirements: 2.1, 2.5, 2.7, 2.10, 7.2_

- [x] 7. Implementar el Serializador_SRT, el Intérprete_SRT y la normalización de segmentos
  - [x] 7.1 Implementar `src/converter/srt/model.py`, `src/converter/srt/serializer.py` y `src/converter/srt/parser.py`
    - `Segment`, `RawSegment`, `format_timestamp`, `serialize_srt` (omite segmentos sin líneas no blancas, índices consecutivos, tiempos acotados a 359 999 999 ms, UTF-8 sin BOM con `\n`) y `parse_srt` (máquina de estados estricta que lanza `SrtParseError(line_number)`).
    - _Requirements: 12.4, 12.5, 12.6, 12.7, 12.8_

  - [x] 7.2 Escribir la prueba de propiedad del Serializador_SRT
    - **Property 48: El Serializador_SRT escribe un bloque por segmento con texto**
    - **Validates: Requirements 12.4, 12.5, 12.8**
    - Crear `tests/strategies/segments.py` con `raw_segments()` y `valid_segment_lists()`, que también usan las propiedades 46, 47, 49 y 50.

  - [x] 7.3 Escribir la prueba de propiedad de ida y vuelta SRT
    - **Property 49: Ida y vuelta SRT**
    - **Validates: Requirements 12.7**

  - [x] 7.4 Escribir la prueba de propiedad de los errores del Intérprete_SRT
    - **Property 50: El Intérprete_SRT informa la primera línea inválida**
    - **Validates: Requirements 12.6**

  - [x] 7.5 Implementar `src/converter/srt/segments.py`
    - `normalize_segments(raw, duration_ms)`: ordena, convierte centésimas a milisegundos, acota a la duración, recorta superposiciones, descarta segmentos sin texto o sin duración positiva y elimina Líneas_en_Blanco.
    - `render_transcript_txt(segments)`: una línea por segmento, sin marcas de tiempo, 0 bytes sin palabras.
    - _Requirements: 11.1, 11.7, 12.2, 12.3, 12.10_

  - [x] 7.6 Escribir la prueba de propiedad de la normalización de segmentos
    - **Property 47: La normalización produce una Lista_de_Segmentos_Válida dentro de la duración**
    - **Validates: Requirements 12.2, 12.3**

  - [x] 7.7 Escribir la prueba de propiedad del TXT de transcripción
    - **Property 46: El TXT de transcripción respeta el orden y queda vacío sin palabras**
    - **Validates: Requirements 11.1, 11.7, 12.10**

- [~] 8. Checkpoint - Verificar el núcleo puro
  - Asegurar que todas las pruebas pasan; consultar al usuario si surgen dudas.

- [ ] 9. Implementar el Gestor_de_Archivos_Temporales
  - [x] 9.1 Implementar `src/converter/storage/workspace.py`
    - `Workspace` y `WorkspaceManager` con `free_bytes` (statvfs), `create` (`ws-<32 hex>` con modo 0700 y `source/`, `work/`, `tmp/`, `result/`), `remove`, `prune_for_completed`, `purge_all_workspaces` y `check_writable`.
    - Raíz fija `/var/lib/converter/storage`, recibida como parámetro con ese valor predeterminado para poder probar con directorios temporales.
    - `remove` y `purge_all_workspaces` resuelven la ruta real, verifican que el padre sea el Almacenamiento_Temporal y no siguen enlaces simbólicos. La purga borra solo directorios reales que coinciden con `^ws-[0-9a-f]{32}$`. No se inspecciona el tipo de sistema de archivos (D17).
    - _Requirements: 1.5, 1.6, 13.2, 17.1, 17.2, 17.5, 17.6, 17.7, 17.8, 20.7, 20.8, 20.13_

  - [x] 9.2 Implementar `src/converter/storage/sweeper.py`
    - Barrendero con reloj inyectable que corre cada 5 segundos y ejecuta los borrados encolados.
    - Ante un fallo, registra en el Log_de_Eventos el identificador de la Tarea y el motivo, y reintenta hasta 3 veces con 60 segundos entre intentos, sin cambiar el Estado_de_Tarea.
    - _Requirements: 1.3, 1.4, 1.8, 1.10, 17.2, 17.3, 17.4, 17.9, 17.10_

  - [~] 9.3 Escribir pruebas unitarias del almacenamiento
    - Enlaces simbólicos que apuntan afuera, archivos y carpetas ajenos que se conservan, rutas fuera del almacenamiento que se rechazan, reintentos con reloj simulado, almacenamiento de solo lectura en `check_writable`.
    - _Requirements: 17.8, 17.9, 20.8, 20.13_

- [ ] 10. Implementar el Ejecutor aislado y los inspectores en subproceso
  - [x] 10.1 Implementar `src/converter/sandbox/launch.py`
    - `python -m converter.sandbox.launch --as-mb N --cpu-s N --fsize-mb N -- <argv>`: fija `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE` y `RLIMIT_NOFILE` con `resource.setrlimit` y hace `os.execvp`.
    - _Requirements: 13.7, 14.6_

  - [x] 10.2 Implementar `src/converter/sandbox/runner.py`, `src/converter/sandbox/limits.py` y `src/converter/sandbox/cpus.py`
    - `SandboxRunner.run` con `asyncio.create_subprocess_exec` a través del lanzador, `start_new_session=True`, entorno mínimo (`PATH`, `LANG=C.UTF-8`, `HOME` y `TMPDIR` en `tmp/`, `http_proxy`, `https_proxy` y `no_proxy` neutralizados, `FONTCONFIG_FILE` opcional) y callbacks por línea de stdout y stderr.
    - Cancelación: SIGTERM al grupo de procesos, espera de 2 segundos y SIGKILL.
    - `Limits` y límites predeterminados por herramienta. `cpus.py` calcula las CPU efectivas desde `/sys/fs/cgroup/cpu.max` y la afinidad del proceso, una vez por ciclo de vida.
    - _Requirements: 11.8, 13.3, 13.4, 13.6, 13.7, 14.6, 14.7_

  - [x] 10.3 Implementar `src/converter/detection/inspect_runner.py`, `src/converter/detection/inspectors/__init__.py` y `src/converter/workers/inspect.py`
    - Registro de inspectores por nombre con descubrimiento automático del paquete `detection/inspectors/`.
    - `workers/inspect.py`: `python -m converter.workers.inspect <nombre> <ruta>` escribe en stdout un `SourceInfo` o un rechazo en JSON.
    - `inspect_runner.py`: la función de inspector que recibe el detector. Ejecuta el subproceso con `SandboxRunner`, 8 segundos de timeout y un semáforo de 4 inspecciones simultáneas, separado del Límite_de_Concurrencia. Un inspector desconocido o una salida inválida produce `error_interno`.
    - _Requirements: 2.11, 16.2, 16.9, 19.10_

  - [~] 10.4 Escribir pruebas unitarias del ejecutor y los inspectores
    - El hijo ve los límites fijados; la cancelación mata a los nietos; un hijo que ignora SIGTERM termina con SIGKILL; el entorno es mínimo; lectura de `cpu.max`; timeout del inspector.
    - _Requirements: 14.6, 2.11, 13.7_

- [ ] 11. Implementar el Gestor_de_Tareas
  - [x] 11.1 Implementar `src/converter/tasks/state_machine.py`
    - Eventos (`ConfirmAccepted`, `SourceRejected`, `InternalError`, `CancelRequested`, `RetentionExpired`, `Started`, `Completed`, `Failed`, `TimedOut`) y la función pura `transition(state, event)` con la tabla del diseño. Cualquier otro par devuelve `InvalidTransition`, y un Estado_Terminal nunca cambia.
    - _Requirements: 14.3, 14.9_

  - [x] 11.2 Implementar `src/converter/tasks/scheduler.py`
    - `SchedulerCore` puro con una cola FIFO global, el conjunto de Tareas en_proceso y `enqueue`, `remove` y `dispatch`, que respetan el Límite_de_Concurrencia.
    - _Requirements: 14.4, 14.10, 19.8_

  - [x] 11.3 Implementar `src/converter/tasks/progress.py`
    - `ProgressReporter`: acota a [0, 1], ignora NaN, no retrocede y publica `floor(100 × máximo)`. Admite franjas para Tareas de varios pasos.
    - _Requirements: 14.5, 14.12_

  - [x] 11.4 Escribir la prueba de propiedad del Porcentaje_de_Avance
    - **Property 54: El Porcentaje_de_Avance es acotado y no decrece**
    - **Validates: Requirements 14.12**

  - [x] 11.5 Implementar `src/converter/converters/base.py` y el descubrimiento de convertidores
    - Protocolo `Converter` con `preflight` y `run`; `ConversionContext`, `ConversionOutcome`, `ConversionFailure`, `ConversionRequest` y `NoticeCollector`; decorador `@register_converter("familia.nombre")`.
    - Registro de funciones de arranque (`@on_startup`) para que una familia prepare datos al iniciar, como la lista de decodificadores de FFmpeg o la verificación del modelo.
    - `src/converter/converters/__init__.py` importa con `pkgutil.walk_packages` todos los módulos del paquete, así una familia nueva se registra sin cambios en el núcleo.
    - _Requirements: 4.3, 4.4_

  - [x] 11.6 Implementar la creación, la detección y la vista de Tareas
    - `src/converter/tasks/model.py` con `Task` y `ResultInfo`; `src/converter/tasks/view.py` con `TaskView`, que se arma solo con la Tarea pedida.
    - En `src/converter/tasks/manager.py`, `TaskManager` con reloj, ejecutor, registro, detector y almacenamiento inyectados. `create` genera un `uuid.uuid4()` que no se repite gracias al conjunto de identificadores emitidos, lanza la detección y espera su resultado como máximo 10 segundos. Un Rechazo_de_Origen o un error interno lleva la Tarea de recibida a fallida con su Motivo_de_Fallo. `view` devuelve los Formatos_de_Destino en orden de registro, con Clasificación_de_Pérdida y Descripción_de_Pérdida.
    - El Gestor_de_Tareas comienza sin Tareas.
    - _Requirements: 1.1, 1.2, 1.7, 2.6, 2.11, 3.1, 3.2, 3.5, 3.6, 14.13, 16.1, 19.2, 19.6, 19.10_

  - [x] 11.7 Implementar confirmación, rechazo de la confirmación y cancelación
    - **MVP:** sin transcripción, `languages.es.json` no existe y el catálogo de idiomas queda vacío; el idioma solo se valida en Pares_de_Transcripción, que todavía no hay.
    - `src/converter/languages.py` con `LanguageCatalog`, que lee `languages.es.json` (vacío si el archivo no existe) y entrega códigos y nombres en español.
    - `confirm` valida en el orden del diseño: estado recibida (si no, `estado_no_permite` con el estado actual), detección terminada, par existente (`destino_no_disponible` con los destinos ofrecidos), `acknowledge_loss` cuando el par no es Sin_Pérdida (`confirmacion_requerida`), Idioma_de_Transcripción (`null` es español, `auto` es Detección_Automática_de_Idioma; otro valor que no es Idioma_Soportado es `idioma_no_soportado`) y `preflight` del convertidor. Si todo pasa, asocia par e idioma y aplica `ConfirmAccepted`.
    - `decline` deja la Tarea en recibida y devuelve los destinos. `cancel` detiene la detección o el subproceso y aplica `CancelRequested`.
    - Cada solicitud que modifica una Tarea toma su `asyncio.Lock`.
    - _Requirements: 3.3, 3.4, 3.7, 11.3, 11.9, 11.10, 13.8, 14.1, 14.6, 14.11, 16.12, 19.11_

  - [x] 11.8 Implementar la ejecución, la entrega y la retención
    - Planificación: `dispatch` después de cada cambio que libera o agrega trabajo; cada Tarea iniciada publica 0 % y corre `converter.run(ctx)` dentro de `asyncio.timeout` con el Tiempo_Máximo_de_Procesamiento que le corresponde.
    - Fallos: cancelación y timeout con SIGTERM y SIGKILL al grupo; clasificación con `converters/errors.py`, incluida la falta de espacio; un resultado ausente es fallo; las excepciones quedan aisladas en la Tarea; evento `task_failed`.
    - Entrega: `os.replace` de `work/` a `result/output`, nombre con `result_name` (".zip" si `result_kind` es "zip"), y recién entonces `Completed` y la poda de `source/`, `work/` y `tmp/`.
    - Retención (cada 5 segundos): cancela las Tareas recibidas vencidas con `sin_destino_en_retencion`, marca como expirados los resultados vencidos, encola el borrado de Espacios_de_Trabajo de Tareas terminadas y conserva el registro sin contenido durante 24 horas.
    - _Requirements: 14.2, 14.5, 14.6, 14.7, 14.8, 14.10, 15.2, 15.8, 15.10, 16.3, 16.6, 16.7, 16.9, 16.10, 16.11, 17.2, 17.3, 17.4_

  - [~] 11.9 Escribir la prueba de propiedad de la máquina de estados
    - **Property 52: La máquina de estados solo admite las transiciones definidas**
    - **Validates: Requirements 14.3, 14.9, 14.11, 16.1**
    - Crear `tests/fakes.py` (`FakeClock`, `FakeRunner` que termina, falla o se cuelga según el ejemplo, `FakeConverter` con `preflight` configurable y `build_manager()`) y `tests/strategies/tasks.py` con `event_sequences()`, que usan las pruebas siguientes del Gestor_de_Tareas y de la API.

  - [~] 11.10 Escribir la prueba de propiedad del planificador
    - **Property 53: El planificador es FIFO global con un único límite**
    - **Validates: Requirements 14.4, 14.10, 19.8, 16.11**

  - [~] 11.11 Escribir la prueba de propiedad de las solicitudes simultáneas
    - **Property 55: Las solicitudes simultáneas sobre una Tarea se serializan**
    - **Validates: Requirements 19.11**

  - [~] 11.12 Escribir la prueba de propiedad de los destinos ofrecidos
    - **Property 5: Los destinos ofrecidos coinciden con el registro**
    - **Validates: Requirements 3.1, 3.2, 3.5, 4.3**

  - [~] 11.13 Escribir la prueba de propiedad de los Rechazos_de_Destino
    - **Property 6: Los Rechazos_de_Destino no modifican la Tarea**
    - **Validates: Requirements 3.4, 16.12**

  - [~] 11.14 Escribir la prueba de propiedad de la confirmación de pérdida
    - **Property 7: Los pares con pérdida exigen confirmación explícita**
    - **Validates: Requirements 3.3, 3.7, 14.1**

  - [~] 11.15 Escribir la prueba de propiedad del Idioma_de_Transcripción
    - **Property 44: El Idioma_de_Transcripción se asocia o se rechaza según la lista**
    - **Validates: Requirements 11.9, 11.10, 13.8**

  - [~] 11.16 Escribir la prueba de propiedad de los Identificadores_de_Tarea
    - **Property 62: Los Identificadores_de_Tarea son UUID v4 únicos**
    - **Validates: Requirements 19.2**

  - [~] 11.17 Escribir la prueba de propiedad de la limpieza
    - **Property 61: La limpieza deja solo los Espacios_de_Trabajo vigentes y no toca nada más**
    - **Validates: Requirements 17.2, 17.3, 17.4, 17.5, 17.6, 17.8, 20.8**

  - [~] 11.18 Escribir pruebas unitarias de escenarios del Gestor_de_Tareas
    - Con reloj simulado: cancelación en cada estado con un subproceso que ignora SIGTERM, timeout, retención de recibida, reinicio sin Tareas, error interno, falta de espacio, herramienta que termina sin producir su archivo y reintentos de limpieza.
    - _Requirements: 14.6, 14.7, 14.8, 14.13, 16.3, 16.9, 16.10, 17.9_

- [ ] 12. Implementar la API HTTP
  - [x] 12.1 Implementar `src/converter/api/app.py` y `src/converter/api/security.py`
    - `create_app(deps)` con FastAPI. Middleware de cabeceras: CSP de la Interfaz_de_Usuario, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy` y `Cross-Origin-Resource-Policy`. Sin cabeceras CORS. Las solicitudes que modifican estado exigen `X-Converter-Request: 1`, y los cuerpos JSON exigen `Content-Type: application/json`.
    - Manejador propio de 404 para el prefijo `/api/tasks/` con un cuerpo `tarea_no_disponible` idéntico en todos los casos.
    - `GET /` y `GET /static/{file}` desde `src/converter/web/`, con una página mínima `web/index.html` en español que la tarea 23 reemplaza. `GET /api/health` (sin datos de Tareas) y `GET /api/config` (Intervalo_de_Progreso, Límite_de_Tamaño_Global, Idiomas_Soportados con nombres en español y Formatos_de_Origen).
    - _Requirements: 11.3, 18.15, 19.1, 19.5, 19.6, 20.9, 20.10_

  - [x] 12.2 Implementar `src/converter/api/upload.py` y registrar la ruta en `app.py`
    - `POST /api/tasks`: rechazo por `Content-Length` antes de leer, comprobación del Espacio_Libre_Mínimo antes de crear el Espacio_de_Trabajo, decodificación de `X-File-Name` (`recepcion_solicitud_invalida` si falta o es inválido), nombre de más de 255 caracteres, escritura en streaming con `anyio.to_thread` contando bytes y cortando al superar el límite, archivo vacío, error de escritura y desconexión del cliente.
    - Toda recepción que termina sin Tarea encola el borrado de su Espacio_de_Trabajo. Las recepciones simultáneas no se esperan entre sí. La respuesta 201 trae la `TaskView` con el Identificador_de_Tarea.
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 1.9, 1.10, 16.13, 17.1, 17.10, 19.3, 19.9_

  - [x] 12.3 Implementar `src/converter/api/tasks.py` y `src/converter/api/download.py` y registrar las rutas en `app.py`
    - `GET /api/tasks/{id}`, `POST .../confirm`, `POST .../decline`, `POST .../cancel` y `GET .../result`, con validación del identificador por expresión regular y los códigos HTTP de la tabla de la API.
    - Descarga con `FileResponse`, `Content-Type` del Formato_de_Destino, `Content-Disposition: attachment` con respaldo ASCII y `filename*` (RFC 6266), `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox` y `Cache-Control: no-store`; 409 si la Tarea no está completada y 410 si el resultado expiró.
    - _Requirements: 3.3, 3.4, 3.7, 14.6, 14.11, 15.8, 15.9, 15.11, 16.6, 18.10, 19.4, 19.5, 19.11_

  - [~] 12.4 Escribir la prueba de propiedad de la recepción
    - **Property 56: La recepción es fiel y respeta los límites**
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.10, 17.10**

  - [~] 12.5 Escribir la prueba de propiedad del almacenamiento independiente del nombre
    - **Property 57: El almacenamiento no depende del Nombre_Original**
    - **Validates: Requirements 1.6, 1.7, 17.1, 17.7**

  - [~] 12.6 Escribir la prueba de propiedad de la descarga
    - **Property 60: Solo se descarga el resultado completo de una Tarea completada y vigente**
    - **Validates: Requirements 15.8, 15.9, 15.11, 16.6**

  - [~] 12.7 Escribir la prueba de propiedad del aislamiento entre Tareas
    - **Property 63: Las respuestas no filtran datos de otras Tareas**
    - **Validates: Requirements 19.3, 19.4, 19.5, 19.6**

  - [~] 12.8 Escribir pruebas unitarias de la API
    - Cabeceras de seguridad en páginas y descargas, preflight CORS no aprobado, falta de `X-Converter-Request`, respaldo ASCII de `Content-Disposition`, `/api/tasks//cancel` con el mismo 404, Indicador_de_Salud sin datos de Tareas y todas las rutas sin credenciales ni cookies.
    - _Requirements: 18.10, 19.1, 19.5, 19.6_

- [ ] 13. Implementar el arranque y el Indicador_de_Salud
  - [x] 13.1 Implementar `src/converter/__main__.py`
    - Secuencia del diagrama de arranque: validar la configuración (log de cada variable inválida y salida 78), comprobar que el almacenamiento es escribible (salida 73), purgar los Espacios_de_Trabajo, cargar el catálogo y los convertidores (salida 70 si el catálogo inicial es inválido), ejecutar las funciones `@on_startup`, cargar `LanguageCatalog`, abrir el Puerto_de_la_Interfaz con Uvicorn (un worker, log de accesos desactivado) y marcar `ready`.
    - Las validaciones que terminan el proceso van primero y no hacen trabajo pesado. El puerto se abre después de la purga.
    - _Requirements: 4.9, 14.13, 17.5, 20.2, 20.5, 20.6, 20.9, 20.13_

  - [x] 13.2 Implementar `src/converter/healthcheck.py`
    - `python -m converter.healthcheck` pide `GET /` y `GET /api/health` a `127.0.0.1:<puerto>` con 5 segundos de timeout cada uno y termina con 0 solo si ambas respuestas llegan a tiempo y la segunda indica listo.
    - _Requirements: 20.9, 20.10_

  - [~] 13.3 Escribir pruebas unitarias del arranque y del Indicador_de_Salud
    - En proceso y con rutas inyectadas: códigos 78, 73 y 70 en menos de 10 segundos con el motivo en el log; la purga ocurre antes de aceptar archivos. Indicador_de_Salud con un servidor de prueba listo, no listo y lento.
    - _Requirements: 17.5, 20.6, 20.9, 20.10, 20.13_

- [~] 14. Checkpoint - Verificar que el Conversor arranca, recibe archivos y detecta el Tipo_Real
  - Asegurar que todas las pruebas pasan; consultar al usuario si surgen dudas.

- [ ] 15. Implementar las conversiones de imagen
  - [x] 15.1 Implementar `src/converter/converters/orientation.py`
    - `read_orientation`, `displayed_size`, `apply_orientation` (tabla de las ocho orientaciones con `Image.transpose`) y `orient_samples` para arreglos planos de 8 o 16 bits, con cortes de paso igual al largo de fila en las orientaciones 5 a 8.
    - _Requirements: 5.1, 5.4, 5.5, 5.9, 5.10_

  - [x] 15.2 Escribir la prueba de propiedad de la Orientación_EXIF
    - **Property 11: Aplicar la Orientación_EXIF produce las Dimensiones_Mostradas**
    - **Validates: Requirements 5.1, 5.10**
    - Crear `tests/strategies/images.py` con `pixel_arrays()` e `images()` (modos `1`, `L`, `LA`, `P`, `RGB`, `RGBA`, PNG de 16 bits con pypng, orientación de 1 a 8, fuera de rango o ausente, contenido asimétrico), que usan las propiedades 10 y 12 a 16.

  - [x] 15.3 Implementar el inspector `image` en `src/converter/detection/inspectors/image.py`
    - `ImageInfo` registrado en `SourceInfo`, con `displayed_size` calculado por `orientation.displayed_size`.
    - Lectura de cabecera con Pillow sin decodificar los píxeles, `Image.MAX_IMAGE_PIXELS` igual al Límite_de_Píxeles, bits por canal desde `IHDR` en PNG, transparencia, cantidad de cuadros y Orientación_EXIF.
    - Rechazos `imagen_animada`, `excede_limite_pixeles` (sobre las Dimensiones_Mostradas) y `archivo_danado`.
    - _Requirements: 5.6, 5.9, 16.1, 16.2_

  - [x] 15.4 Implementar el convertidor `image.raster` en `src/converter/converters/images.py` y `src/converter/workers/image.py`
    - Normalización de modo, `apply_orientation` antes de cualquier otro paso, `flatten_on_white` con la fórmula del diseño para JPG, opciones de guardado por destino (JPEG calidad 90 y 4:2:0; WEBP sin pérdida con `exact=True` desde PNG y calidad 90 desde JPG; PNG nivel 6) y resultado sin EXIF ni XMP.
    - Aviso `bits_reducidos` para PNG de 16 bits a JPG o WEBP. `preflight` con `dimensiones_excedidas` (WEBP 16383, JPG 65535) sobre las Dimensiones_Mostradas.
    - _Requirements: 5.1, 5.2, 5.3, 5.5, 5.7, 5.8, 5.10, 13.4_

  - [~] 15.5 Escribir la prueba de propiedad de la composición sobre blanco
    - **Property 12: La composición sobre blanco respeta la opacidad**
    - **Validates: Requirements 5.2**

  - [x] 15.6 Implementar el convertidor `image.pdf` en `src/converter/converters/pdf_image_writer.py`, `src/converter/converters/image_pdf.py` y `src/converter/workers/image_pdf.py`
    - **MVP:** en lugar del escritor de PDF propio, guardar la imagen ya orientada con Pillow (`save(format="PDF")`), reduciendo a 8 bits si hace falta. Si un par no preserva el contenido (JPG re-codificado, PNG de 16 bits), registrarlo en 15.7 con la Clasificación_de_Pérdida que corresponda. El escritor propio queda para después.
    - Escritor de PDF 1.7 de una página con un único objeto de imagen de ancho y alto iguales a las Dimensiones_Mostradas.
    - JPG con orientación normal: flujo original en `/DCTDecode` sin segmentos APP1, con `/Decode` invertido para CMYK Adobe. JPG con otra orientación, PNG y WEBP: píxeles orientados en `/FlateDecode` con `/SMask` si hay transparencia. PNG de 16 bits con pypng y `orient_samples` en `/BitsPerComponent 16`.
    - Página de Dimensiones_Mostradas × 0,75 pt con escala uniforme si el lado mayor supera 14 400 pt; `/ID` derivado de un hash del contenido y sin fechas.
    - _Requirements: 5.4, 5.10, 4.11, 13.4_

  - [~] 15.7 Registrar los 9 pares de imagen en `catalog/pairs.toml`
    - PNG, JPG y WEBP entre sí con `image.raster`, y cada uno a PDF con `image.pdf`, con la Clasificación_de_Pérdida y la Descripción_de_Pérdida de la tabla Imágenes.
    - _Requirements: 3.1, 3.2, 4.1, 4.9_

  - [~] 15.8 Escribir la prueba de propiedad de las conversiones entre PNG, JPG y WEBP
    - **Property 10: Las conversiones entre PNG, JPG y WEBP aplican la Orientación_EXIF y conservan las Dimensiones_Mostradas**
    - **Validates: Requirements 5.1, 5.10**

  - [~] 15.9 Escribir la prueba de propiedad de la reducción de 16 bits
    - **Property 13: Los PNG de 16 bits se reducen a 8 bits con aviso**
    - **Validates: Requirements 5.3, 5.8**

  - [~] 15.10 Escribir la prueba de propiedad de ida y vuelta PNG→WEBP→PNG
    - **Property 14: Ida y vuelta PNG→WEBP→PNG**
    - **Validates: Requirements 5.7**

  - [~] 15.11 Escribir la prueba de propiedad de imagen a PDF
    - **Property 15: Una imagen se convierte en un PDF de una página que la contiene completa y orientada**
    - **Validates: Requirements 5.4, 5.10**

  - [~] 15.12 Escribir la prueba de propiedad de los límites de dimensiones y píxeles
    - **Property 16: Los límites de dimensiones y píxeles se aplican exactamente sobre las Dimensiones_Mostradas**
    - **Validates: Requirements 5.5, 5.9**

  - [~] 15.13 Escribir pruebas unitarias de imágenes
    - Un JPG, un PNG y un WEBP por cada orientación de 1 a 8, con valor 0 y sin etiqueta, a cada destino, verificados con `exiftool`; JPG de 3000 × 17000 con orientación 6 rechazado para WEBP informando 17000 × 3000; APNG y WEBP animados rechazados y APNG de un cuadro aceptado.
    - _Requirements: 5.1, 5.4, 5.5, 5.6, 5.10_

- [ ] 16. Implementar las conversiones desde PDF
  - [x] 16.1 Implementar el inspector `pdf` en `src/converter/detection/inspectors/pdf.py`
    - `PdfInfo` y `PdfPage` (tamaño mostrado con CropBox y `/Rotate`) con pypdfium2. `FPDF_ERR_PASSWORD` produce `pdf_protegido`; `FPDF_ERR_FORMAT` y `FPDF_ERR_FILE` producen `archivo_danado`. Registra si el PDF tiene permisos restringidos.
    - _Requirements: 8.7, 8.10, 16.1, 16.2_

  - [x] 16.2 Implementar el convertidor `pdf.text` en `src/converter/converters/pdf_text.py` y `src/converter/workers/pdf_text.py`
    - Texto por página con `get_text_range()` en orden de Número_de_Página, `\r\n` normalizado a `\n`, eliminación de U+2060 y U+FEFF, guion de fin de línea conservado y `\n\n` entre páginas.
    - Todas las páginas sin texto: fallo `pdf_sin_texto`. Algunas: aviso `paginas_sin_texto` con sus números. Progreso por página.
    - _Requirements: 8.1, 8.2, 8.3, 8.10_

  - [x] 16.3 Implementar el convertidor `pdf.raster` en `src/converter/converters/pdf_raster.py` y `src/converter/workers/pdf_raster.py`
    - `raster_size(page, dpi)` compartida por la verificación y el renderizado. Antes de rasterizar se recorren todas las páginas; la primera que supera el Límite_de_Píxeles produce `pagina_excede_pixeles` con su número.
    - Renderizado con `page.render(scale=dpi/72, may_draw_forms=True)`. Una página: PNG. Varias: ZIP con `ZIP_DEFLATED`, sin carpetas, fecha fija 1980-01-01 y nombres de `page_entry_name`; `result_kind` "zip".
    - _Requirements: 8.4, 8.5, 8.6, 8.8, 8.9, 8.11, 15.10_

  - [~] 16.4 Registrar PDF a TXT y PDF a PNG en `catalog/pairs.toml`
    - _Requirements: 3.1, 3.2, 4.9_

  - [~] 16.5 Escribir la prueba de propiedad de PDF a TXT
    - **Property 32: PDF a TXT respeta el orden de páginas y las Páginas_sin_Texto**
    - **Validates: Requirements 8.1, 8.2, 8.3**
    - Crear `tests/strategies/pdfs.py` con `pdfs()` (generados con `pdf_image_writer` y con texto, con tamaños y `/Rotate` aleatorios), que usan las propiedades 33 y 35.

  - [~] 16.6 Escribir la prueba de propiedad de PDF a PNG
    - **Property 33: PDF a PNG produce una imagen por página con el tamaño correcto o falla en la primera página demasiado grande**
    - **Validates: Requirements 8.4, 8.5, 8.6, 8.9, 8.11**

  - [~] 16.7 Escribir la prueba de propiedad de los PDF con permisos restringidos
    - **Property 35: Un PDF con permisos restringidos se procesa como el mismo PDF sin cifrar**
    - **Validates: Requirements 8.10**

  - [~] 16.8 Escribir pruebas unitarias de PDF
    - PDF de 5 páginas cuyas páginas 3 y 5 superan el Límite_de_Píxeles: la Tarea falla informando la página 3 y no hay PNG en `work/`. PDF con contraseña de apertura rechazado en la detección.
    - _Requirements: 8.7, 8.11_

- [ ] 17. Implementar las conversiones de documentos de texto
  - [x] 17.1 Implementar `src/converter/converters/text_docs/dom.py`
    - `parse_html` (BeautifulSoup con html5lib), `sanitize_dom` con `SanitizeReport` (elimina `script`, atributos `on*` y enlaces `javascript:`; cuenta y neutraliza los Recursos_Externos de la lista del diseño, sin contar enlaces de navegación ni URL `data:`), `visible_text`, `markdown_to_html` (`MarkdownIt("commonmark")`) e `insert_word_joiners` (U+2060 después de la puntuación dentro de una palabra).
    - _Requirements: 2.12, 6.4, 6.6, 6.7, 6.13_

  - [x] 17.2 Implementar el convertidor `text.html` en `src/converter/converters/text_docs/to_html.py` y `src/converter/workers/text_html.py`
    - TXT a HTML con `<pre>` escapado con `html.escape(quote=True)`, `lang="es"` y `<meta charset="utf-8">`. MD a HTML con el cuerpo igual a la salida CommonMark. Ambos parten del Texto_Decodificado y escriben UTF-8.
    - _Requirements: 6.2, 6.3, 6.14, 2.12_

  - [x] 17.3 Implementar el convertidor `text.plain` en `src/converter/converters/text_docs/to_txt.py` y `src/converter/workers/text_plain.py`
    - MD a TXT y HTML a TXT con `visible_text` sobre el DOM saneado, marcadores de lista `- ` y `N. ` y UTF-8 sin BOM. Aviso de Recursos_Externos omitidos cuando el origen es HTML.
    - _Requirements: 6.4, 6.6, 6.7, 6.13, 6.14_

  - [x] 17.4 Implementar el convertidor `text.markdown` en `src/converter/converters/text_docs/to_md.py` y `src/converter/workers/text_markdown.py`
    - **MVP:** usar `markdownify` con títulos ATX y viñetas `-`; los escapes, las cercas de código y la sangría a medida quedan para después.
    - Subclase de `markdownify.MarkdownConverter`: títulos ATX, viñetas `-`, sangría según el ancho del marcador, cercas de código más largas que la secuencia de acentos graves del contenido, escapes de puntuación ASCII y de marcadores de bloque, `br` como barra invertida y salto, enlaces a Scripts como texto. Aviso de Recursos_Externos omitidos.
    - _Requirements: 6.5, 6.6, 6.7, 6.13, 6.14_

  - [x] 17.5 Implementar `src/converter/converters/text_docs/layout.py` y `docker/fontconfig/default-only.conf`
    - `wrap_lines` con tabulaciones en múltiplos de 8, corte en el último espacio que cabe medido con los avances de fontTools y corte dentro de la palabra solo si no cabe sola; paginación A4 con márgenes de 2 cm y DejaVu Sans Mono 10 pt.
    - Cobertura de glifos desde los `cmap` de la Fuente_Predeterminada y `find_unrepresentable` (sin espacios, tabulaciones ni saltos, orden de aparición, máximo 10). El directorio de fuentes es un parámetro con valor predeterminado `/opt/converter/fonts/default`.
    - `default-only.conf` hace que fontconfig solo vea ese directorio.
    - _Requirements: 6.1, 6.11_

  - [-] 17.6 Implementar el convertidor `text.pdf` en `src/converter/converters/text_docs/to_pdf.py` y `src/converter/workers/text_pdf.py`
    - WeasyPrint con un fetcher que solo responde URL `data:` y rechaza todo otro esquema, incluido `file:`, y `FONTCONFIG_FILE` apuntando a `default-only.conf`.
    - TXT a PDF con la maquetación de `layout.py` y `white-space: pre`. MD y HTML a PDF con el DOM saneado, U+2060 insertado y la hoja base (A4, DejaVu, `hyphens: manual`, `font-variant-ligatures: none`); en HTML, las hojas del documento tienen prioridad.
    - TXT y MD con caracteres no representables fallan con `caracteres_no_representables`. Aviso de Recursos_Externos omitidos en HTML y MD.
    - _Requirements: 6.1, 6.6, 6.7, 6.8, 6.11, 6.12, 6.13, 13.7_

  - [~] 17.7 Registrar los 8 pares de documentos de texto en `catalog/pairs.toml`
    - TXT a PDF y HTML; MD a HTML, PDF y TXT; HTML a PDF, MD y TXT, con la Clasificación_de_Pérdida y la Descripción_de_Pérdida de la tabla Documentos.
    - _Requirements: 3.1, 3.2, 4.9_

  - [~] 17.8 Escribir la prueba de propiedad de TXT a HTML
    - **Property 17: TXT a HTML escapa y conserva líneas y palabras**
    - **Validates: Requirements 6.2, 6.14**
    - Crear `tests/strategies/documents.py` con `latin_text()`, `html_documents()` y `markdown_documents()` (generados desde una Estructura_de_Documento conocida), que usan las propiedades 18 a 27 y 23.

  - [~] 17.9 Escribir la prueba de propiedad de ida y vuelta TXT→HTML→TXT
    - **Property 18: Ida y vuelta TXT→HTML→TXT**
    - **Validates: Requirements 6.9, 6.15, 6.14**

  - [~] 17.10 Escribir la prueba de propiedad de la maquetación de TXT a PDF
    - **Property 19: La maquetación de TXT a PDF no pierde ni trunca texto**
    - **Validates: Requirements 6.1**

  - [~] 17.11 Escribir la prueba de propiedad de HTML y MD a TXT
    - **Property 20: HTML y MD a TXT conservan palabras y líneas**
    - **Validates: Requirements 6.4, 6.14, 2.12**

  - [~] 17.12 Escribir la prueba de propiedad de HTML a MD
    - **Property 21: HTML a MD conserva la Estructura_de_Documento**
    - **Validates: Requirements 6.5**

  - [~] 17.13 Escribir la prueba de propiedad de ida y vuelta MD→HTML→MD→HTML
    - **Property 22: Ida y vuelta MD→HTML→MD→HTML**
    - **Validates: Requirements 6.10**

  - [~] 17.14 Escribir la prueba de propiedad de los Scripts
    - **Property 24: Los Scripts no se ejecutan ni aparecen en el resultado**
    - **Validates: Requirements 6.7, 6.13**
    - Usa una URL canario servida por un servidor HTTP local de la prueba.

  - [~] 17.15 Escribir la prueba de propiedad de ida y vuelta TXT→PDF→TXT
    - **Property 25: Ida y vuelta TXT→PDF→TXT con Texto_Latino**
    - **Validates: Requirements 6.8**

  - [~] 17.16 Escribir la prueba de propiedad de ida y vuelta MD o HTML→PDF→TXT
    - **Property 26: Ida y vuelta MD o HTML→PDF→TXT con Texto_Latino**
    - **Validates: Requirements 6.12**

  - [~] 17.17 Escribir la prueba de propiedad de los caracteres no representables
    - **Property 27: Los caracteres no representables se detectan exactamente**
    - **Validates: Requirements 6.11**

  - [~] 17.18 Escribir pruebas unitarias de documentos de texto
    - Suite de ejemplos `spec.json` de CommonMark contra `markdown_to_html`; `meta charset` contradictorio ignorado; conteo de Recursos_Externos en el aviso; Windows-1252 escrito como UTF-8.
    - _Requirements: 6.3, 6.6, 2.12, 6.14_

- [ ] 18. Implementar la conversión de DOCX a PDF
  - [x] 18.1 Implementar `src/converter/detection/zipcheck.py`
    - `DecompressionLimits` (512 000 000 bytes, 10 000 Entradas_ZIP, razón 200), `ZipEntryStat` y `check_decompression_limits`.
    - Rechazo inmediato con los tamaños declarados en el directorio central. Lector propio que descomprime cada entrada desde su cabecera local con `zlib.decompressobj(-15)` descartando la salida, solo con los métodos sin compresión y deflate.
    - Corte temprano por razón y por suma; razón infinita con tamaño comprimido 0; errores de deflate o CRC marcan la entrada como dañada sin detener la verificación.
    - _Requirements: 7.7, 16.2, 2.11_

  - [~] 18.2 Escribir la prueba de propiedad de los Límites_de_Descompresión
    - **Property 31: Los Límites_de_Descompresión de DOCX se aplican exactamente**
    - **Validates: Requirements 7.7, 16.1, 16.2**
    - Crear `tests/strategies/zips.py` con `zip_packages()`, un escritor de prueba que declara tamaños distintos de los reales, alrededor de límites escalados.

  - [-] 18.3 Implementar el inspector `docx` en `src/converter/detection/inspectors/docx.py`
    - `DocxInfo` con presencia de macros, comentarios, control de cambios y relaciones externas, leídos con lxml (`resolve_entities=False`, `no_network=True`, `huge_tree=False`, sin DTD).
    - Rechazo `docx_excede_limites_descompresion` antes que `archivo_danado`, como exige el orden del diseño.
    - _Requirements: 7.7, 16.1, 16.2_

  - [~] 18.4 Implementar el saneamiento en `src/converter/converters/docx/sanitize.py` y `src/converter/workers/docx_sanitize.py`
    - Acepta los cambios del control de cambios, quita comentarios, macros, controles ActiveX y relaciones externas que no son hipervínculos, reemplaza `INCLUDEPICTURE`, `INCLUDETEXT`, `LINK` y `DDE` por su último resultado, desactiva el guionado automático e inserta U+2060 con `insert_word_joiners` respetando los límites entre runs.
    - Devuelve un informe con las cantidades de comentarios, cambios, macros y Recursos_Externos quitados.
    - _Requirements: 7.3, 7.5, 7.6, 13.7_

  - [~] 18.5 Escribir la prueba de propiedad del saneamiento de DOCX
    - **Property 29: El saneamiento de DOCX aplica cambios y quita lo no admitido**
    - **Validates: Requirements 7.5, 7.6**
    - Crear `tests/strategies/docx.py` con `docx_packages()` (python-docx más modificaciones de XML), que usan las propiedades 23 y 28.

  - [x] 18.6 Implementar `src/converter/converters/docx/fonts.py`
    - Reunir las fuentes de `fontTable.xml`, `w:rFonts` de estilos y runs y las del tema; restar las incrustadas y las instaladas sin distinguir mayúsculas; obtener el reemplazo de cada una con `fc-match`. La función de cálculo es pura y recibe los tres conjuntos.
    - _Requirements: 7.4_

  - [x] 18.7 Escribir la prueba de propiedad de las fuentes reemplazadas
    - **Property 30: Las fuentes informadas como reemplazadas son las que faltan**
    - **Validates: Requirements 7.4**

  - [~] 18.8 Implementar el convertidor `docx.libreoffice` en `src/converter/converters/docx/libreoffice.py` y el perfil `docker/libreoffice/profile/registrymodifications.xcu`
    - **MVP:** sin el saneamiento de 18.4: convertir el DOCX original con LibreOffice y el perfil con macros desactivadas y enlaces en "nunca". El aviso de fuentes reemplazadas usa `fonts.find_replacements`; los avisos de comentarios, cambios y macros quedan para después.
    - Comprobación de la parte principal del paquete, saneamiento en subproceso, análisis de fuentes y `soffice --headless` con perfil propio copiado de la plantilla (macros desactivadas, enlaces en "nunca") y la exportación PDF del diseño.
    - Verificación del PDF con pypdfium2 y avisos de fuentes reemplazadas, comentarios omitidos, cambios aplicados y macros o Recursos_Externos omitidos.
    - _Requirements: 7.1, 7.4, 7.5, 7.6, 16.2, 16.3_

  - [~] 18.9 Registrar DOCX a PDF en `catalog/pairs.toml`
    - _Requirements: 3.1, 3.2, 4.9_

  - [~] 18.10 Escribir la prueba de propiedad de DOCX simple a PDF
    - **Property 28: DOCX simple a PDF conserva la Secuencia_de_Palabras**
    - **Validates: Requirements 7.3**

  - [~] 18.11 Escribir la prueba de propiedad de los Recursos_Externos
    - **Property 23: Los Recursos_Externos se cuentan y nunca se acceden**
    - **Validates: Requirements 6.6, 7.5, 13.7**
    - Espías sobre el fetcher de WeasyPrint y el lector de archivos para HTML y MD, y DOCX con referencias externas.

  - [~] 18.12 Escribir pruebas unitarias de DOCX
    - Límites reales: razón 200 y 201, 10 000 y 10 001 entradas, suma de 512 000 000 y 512 000 001 bytes, y una entrada que produce más bytes de los que declara, cada detección en menos de 10 segundos. DOCX cifrado rechazado. DOCX con tablas, imágenes, Calibri, comentarios y control de cambios.
    - _Requirements: 7.1, 7.2, 7.4, 7.6, 7.7, 2.11_

- [ ] 19. Implementar las conversiones de audio
  - [x] 19.1 Implementar `src/converter/converters/media_rules.py`
    - `choose_sample_rate(origen, admitidas)` para conjuntos e intervalos, `build_downmix_matrix(distribución)`, `default_audio_track(streams)` y `padded_size(width, height)`, más las constantes de frecuencias de MP3 y de máximos de canales.
    - _Requirements: 9.2, 9.3, 9.4, 9.5, 10.4, 10.9, 10.11_

  - [x] 19.2 Escribir la prueba de propiedad de la frecuencia de muestreo
    - **Property 37: La frecuencia elegida es la admitida más cercana**
    - **Validates: Requirements 9.4, 9.5**

  - [x] 19.3 Escribir la prueba de propiedad de la mezcla a estéreo
    - **Property 38: La mezcla a estéreo no pierde canales salvo el LFE**
    - **Validates: Requirements 9.3, 10.9**

  - [x] 19.4 Escribir la prueba de propiedad de la Pista_de_Audio_Predeterminada
    - **Property 39: Se elige la Pista_de_Audio_Predeterminada**
    - **Validates: Requirements 10.4**

  - [ ] 19.5 Implementar el inspector `media` en `src/converter/detection/inspectors/media.py` y `src/converter/converters/ffmpeg_tools.py`
    - `MediaStream` y `MediaInfo` desde `ffprobe` con el demuxer forzado según el Tipo_Real; sin flujos decodificables produce `archivo_danado`.
    - `ffmpeg_tools.py`: función `@on_startup` que guarda la lista de `ffmpeg -decoders`; argumentos comunes (`-protocol_whitelist file`, demuxer forzado, `-map_metadata -1`, hilos fijos de `cpus.py`); parser de `-progress pipe:1`; comprobación de Códec que produce `codec_no_soportado` con el Códec y la pista.
    - _Requirements: 9.6, 10.8, 13.7, 14.5, 16.2_

  - [~] 19.6 Implementar el convertidor `audio.ffmpeg` en `src/converter/converters/audio.py`
    - **MVP:** para pasar a 2 canales usar `-ac 2` de FFmpeg en lugar del filtro `pan` con `build_downmix_matrix`.
    - MP3 con `libmp3lame -q:a 2`, OGG con `libvorbis -q:a 5` y WAV con `pcm_s16le`. Frecuencia con `choose_sample_rate` y aviso si cambia; canales conservados o mezclados con `pan` y aviso; progreso por `out_time_us`.
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 13.4, 14.5_

  - [~] 19.7 Registrar los 6 pares de audio en `catalog/pairs.toml`
    - _Requirements: 3.1, 3.2, 4.9_

  - [~] 19.8 Escribir la prueba de propiedad de la conversión de audio
    - **Property 36: La conversión de audio conserva duración, canales y frecuencia admitida**
    - **Validates: Requirements 9.1, 9.2, 9.5**
    - Crear `tests/strategies/media.py` con `synthetic_audio()` y `synthetic_video()` generados con FFmpeg (`sine`, `testsrc2`, distribuciones de canales y marcas de pista predeterminada aleatorias), que usan las propiedades 40 a 42.

  - [~] 19.9 Escribir pruebas unitarias de audio
    - WAV con un Códec sin decodificador; aviso de cambio de frecuencia en Hz; aviso de canales al mezclar 5.1 a MP3.
    - _Requirements: 9.3, 9.4, 9.6_

- [ ] 20. Implementar las conversiones de video y la extracción de audio
  - [~] 20.1 Implementar el convertidor `video.ffmpeg` en `src/converter/converters/video.py`
    - **MVP:** con el FFmpeg de Debian, codificar H.264 con `libopenh264` si está disponible y si no con `libx264`.
    - MP4 a WEBM con `libvpx-vp9` y `libopus`; WEBM a MP4 con `libopenh264`, `aac` y `+faststart`.
    - Filtro `pad` solo en WEBM a MP4 cuando `padded_size` difiere del origen, con el aviso `dimensiones_rellenadas`. `-noautorotate`, `-fps_mode passthrough`, `-map 0:v:0` y la Pista_de_Audio_Predeterminada opcional, aviso de pistas no usadas y comprobación de Códec de las pistas usadas.
    - _Requirements: 10.4, 10.5, 10.6, 10.7, 10.8, 10.10, 10.11_

  - [~] 20.2 Implementar el convertidor `video.extract_audio` en `src/converter/converters/video_audio.py`
    - MP4 a MP3 y MP4 a WAV con la Pista_de_Audio_Predeterminada y las reglas de canales y frecuencia de `audio.py`. `preflight` con `video_sin_audio` y una función reutilizable para los Pares_de_Transcripción.
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.8, 10.9_

  - [~] 20.3 Registrar los 4 pares de video en `catalog/pairs.toml`
    - _Requirements: 3.1, 3.2, 4.9_

  - [~] 20.4 Escribir la prueba de propiedad de la extracción de audio
    - **Property 40: La extracción de audio de MP4 usa solo la pista predeterminada**
    - **Validates: Requirements 10.1, 10.2, 10.9**

  - [~] 20.5 Escribir la prueba de propiedad de la conversión de video
    - **Property 41: La conversión de video conserva dimensiones, duración y pista de audio**
    - **Validates: Requirements 10.5, 10.6, 10.7, 10.10, 10.11**

  - [~] 20.6 Escribir la prueba de propiedad del relleno a dimensiones pares
    - **Property 42: El relleno a dimensiones pares en MP4 conserva el contenido original en la región de origen**
    - **Validates: Requirements 10.11**

  - [~] 20.7 Escribir pruebas unitarias de video
    - WEBM de 321 × 240, 320 × 241 y 321 × 241 a MP4 con 322 × 240, 320 × 242 y 322 × 242 y aviso; 320 × 240 sin aviso; MP4 sin audio rechazado para destinos de audio; MP4 con una pista de audio de Códec sin decodificador.
    - _Requirements: 10.3, 10.5, 10.8, 10.11_

- [ ] 21. Implementar la transcripción
  - [~] 21.1 Implementar la generación de `languages.es.json` con `docker/whisper-langs.c` y `scripts/generate_languages.py`
    - Programa enlazado con `libwhisper` que imprime los códigos de `whisper_lang_str` hasta `whisper_lang_max_id`. Script que genera los nombres en español con Babel, corrige los códigos no ISO con una tabla de excepciones (`jw` → javanés) y escribe el JSON que lee `LanguageCatalog`.
    - _Requirements: 11.3, 11.4_

  - [~] 21.2 Implementar el convertidor `transcription.whisper` en `src/converter/converters/transcription.py`
    - `preflight`: `video_sin_audio` en MP4 sin audio y `transcripcion_excede_duracion` con la duración que informa el inspector `media`.
    - Extracción de `work/audio16k.wav` (PCM 16 bits, mono, 16 kHz) antes de transcribir. `whisper-cli` con `-bs 5 -bo 5 --no-fallback --suppress-nst --no-gpu -ojf`, hilos fijos y `-l <es|código|auto>`.
    - Progreso por segmentos (0–10 % extracción, 10–100 % transcripción en MP4); lectura del JSON a `RawSegment`; descarte de `[BLANK_AUDIO]`; `normalize_segments`; salida TXT con `render_transcript_txt` o SRT con `serialize_srt`; aviso `sin_voz` con 0 bytes; `detected_language` con `auto`; `modelo_no_disponible` si el modelo no carga.
    - Función `@on_startup` que verifica que el modelo exista con el tamaño esperado sin impedir el arranque.
    - _Requirements: 10.3, 11.1, 11.2, 11.5, 11.6, 11.7, 11.8, 11.10, 11.11, 11.12, 12.2, 12.3, 12.9, 12.10, 13.1, 13.5_

  - [~] 21.3 Registrar los 6 Pares_de_Transcripción en `catalog/pairs.toml`
    - MP3, WAV y MP4 a TXT y a SRT, con Clasificación_de_Pérdida Aproximada; las Descripciones_de_Pérdida a SRT indican que el texto y las marcas de tiempo son aproximados.
    - _Requirements: 3.1, 3.2, 4.9, 12.1_

  - [~] 21.4 Escribir la prueba de propiedad de la Duración_Máxima_de_Transcripción
    - **Property 43: La Duración_Máxima_de_Transcripción se aplica exactamente**
    - **Validates: Requirements 11.5**

  - [~] 21.5 Escribir la prueba de propiedad de la reproducibilidad de la transcripción
    - **Property 45: La transcripción es reproducible**
    - **Validates: Requirements 11.8**
    - Corre dentro de la imagen con clips de voz del corpus recortados y con ruido.

  - [~] 21.6 Escribir pruebas unitarias de la transcripción
    - Orden de pasos de MP4 a TXT con un ejecutor espía; modelo ausente o corrupto; entrega del SRT del serializador; descarte de `[BLANK_AUDIO]`; Idioma_Detectado con un Transcriptor simulado.
    - _Requirements: 11.2, 11.6, 11.11, 12.9_

  - [~] 21.7 Escribir pruebas unitarias del catálogo inicial completo
    - El catálogo carga los 36 Pares_de_Conversión (9, 9, 2, 6, 4 y 6) con su Clasificación_de_Pérdida, la Descripción_de_Pérdida de su tabla y un convertidor, sondas e inspectores registrados.
    - _Requirements: 4.9, 12.1_

- [~] 22. Checkpoint - Verificar los 36 Pares_de_Conversión
  - **MVP:** sin transcripción, son 30 pares: 9 de imagen, 2 de PDF, 8 de documentos de texto, 1 de DOCX, 6 de audio y 4 de video.
  - Asegurar que todas las pruebas pasan; consultar al usuario si surgen dudas.

- [ ] 23. Implementar la Interfaz_de_Usuario
  - [~] 23.1 Implementar `src/converter/web/index.html` y `src/converter/web/styles.css`
    - Página única en español con pila `system-ui`, íconos SVG en línea, botón "Elegir archivo" que activa un `<input type="file">` oculto sin `multiple`, Área_de_Carga, zona de avisos con `aria-live="polite"` y lista de tarjetas de Tarea. Sin recursos externos.
    - _Requirements: 18.1, 18.2, 18.3, 18.15_

  - [~] 23.2 Implementar `src/converter/web/api.js` y `src/converter/web/store.js`
    - Cliente de la API con `X-Converter-Request: 1` y JSON; detección de falta de conexión cuando ninguna solicitud distinta del envío responde durante 3 × Intervalo_de_Progreso.
    - `store.js`: lista de Identificadores_de_Tarea en `localStorage` (clave `converter.tasks`, en orden de creación) y sincronización entre pestañas con el evento `storage`.
    - _Requirements: 18.12, 18.13, 19.7_

  - [~] 23.3 Implementar `src/converter/web/upload.js`
    - Envío con `XMLHttpRequest` en el evento `change` o al soltar, con `X-File-Name` codificado con `encodeURIComponent`. Validación de `drop` (un solo elemento de tipo archivo y que no sea carpeta); cancelación de `dragover` y `drop` fuera del área. Mensajes de recepción e interrupción junto al Área_de_Carga.
    - _Requirements: 18.3, 18.4, 18.5, 18.11, 18.13, 16.13_

  - [~] 23.4 Implementar `src/converter/web/cards.js`
    - **MVP:** sin transcripción, no se muestran el selector de Idioma_de_Transcripción ni el Idioma_Detectado.
    - Tarjeta de Tarea renderizada con `textContent`: Nombre_Original, Tipo_Real, aviso de extensión, Formatos_de_Destino con su Clasificación_de_Pérdida, panel de confirmación con la Descripción_de_Pérdida, selector de Idioma_de_Transcripción con español preseleccionado, estado en español, barra de Porcentaje_de_Avance, avisos, mensaje de fallo con etapa, motivo y acción, botón de cancelación, enlace de descarga con el nombre de descarga, Idioma_Detectado y el estado "ya no está disponible".
    - _Requirements: 2.6, 3.1, 3.2, 3.3, 3.6, 3.7, 5.8, 9.3, 9.4, 10.4, 10.11, 11.3, 11.12, 15.1, 16.4, 18.6, 18.7, 18.8, 18.9, 18.10, 18.11, 18.14_

  - [~] 23.5 Implementar `src/converter/web/poller.js` y `src/converter/web/app.js`
    - Sondeo por Tarea: cada 200 ms durante la detección, cada `max(0,5; Intervalo_de_Progreso / 2)` segundos en estados no terminales y cada 15 segundos en Estados_Terminales. Un 404 marca la Tarea como no disponible, retira los controles y quita el identificador de `localStorage`.
    - `app.js` lee `/api/config`, restaura las Tareas guardadas al cargar la página y conecta los módulos.
    - _Requirements: 3.1, 14.2, 14.5, 18.8, 18.12, 18.13, 18.14, 19.7_

  - [~] 23.6 Escribir pruebas de extremo a extremo de envío
    - `tests/e2e/conftest.py` con el servidor de la aplicación y un Transcriptor simulado, en Chromium, Firefox, WebKit y el canal de Edge, con `locale` `en-US` y `de-DE`. Escenarios: elegir un archivo, soltarlo y los drops inválidos (dos archivos, carpeta, texto, fuera del área).
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5_

  - [~] 23.7 Escribir pruebas de extremo a extremo del flujo de una Tarea
    - Destinos y clasificación en menos de 1 segundo, confirmación y rechazo de pérdida, selector de idioma, Idioma_Detectado, progreso sin recargar, cancelación, descarga con nombre y contenido idénticos y mensajes junto al área o la Tarea.
    - _Requirements: 3.1, 3.3, 3.7, 11.3, 11.12, 14.2, 14.5, 15.1, 18.6, 18.7, 18.8, 18.9, 18.10, 18.11_

  - [~] 23.8 Escribir pruebas de extremo a extremo de sesiones y conexión
    - Recarga y nueva pestaña, servidor caído, servidor reiniciado, dos contextos de navegador que no ven sus Tareas y registro de solicitudes que solo van al mismo origen.
    - _Requirements: 18.12, 18.13, 18.14, 18.15, 19.7_

- [ ] 24. Construir la Imagen_de_Contenedor y verificarla
  - [~] 24.1 Implementar `docker/build-ffmpeg.sh`
    - Descarga de FFmpeg 7.1 con SHA-256 fijado y `configure` con `--disable-gpl --disable-nonfree --disable-network --disable-autodetect --enable-shared` más `--enable-libmp3lame --enable-libvorbis --enable-libopus --enable-libvpx --enable-libopenh264`. Guarda versión y hash de las fuentes para el Inventario_de_Licencias.
    - _Requirements: 13.3, 13.7, 20.1, 20.12_

  - [~] 24.2 Implementar `docker/build-whisper.sh`
    - whisper.cpp 1.8 con SHA-256 fijado, `GGML_NATIVE=OFF`, `GGML_BACKEND_DL=ON` y `GGML_CPU_ALL_VARIANTS=ON`; compila `whisper-cli` y `whisper-langs`; descarga `ggml-small-q8_0.bin` y lo verifica contra el SHA publicado y un SHA-256 propio.
    - _Requirements: 11.4, 11.8, 13.1, 20.1_

  - [~] 24.3 Implementar el `Dockerfile` y `.dockerignore`
    - **MVP:** FFmpeg desde el paquete `ffmpeg` de Debian (sin 24.1) y sin whisper.cpp, modelo ni `languages.es.json` (la transcripción queda para después). Se construye para la arquitectura local; el manifiesto multiarquitectura queda para después.
    - Etapas `ffmpeg-build`, `whisper-build`, `python-deps` (venv con `uv.lock`), `assets` (`languages.es.json`, perfil de LibreOffice, fontconfig) y final sobre `python:3.12-slim-trixie` con `libreoffice-writer-nogui`, fuentes DejaVu, Liberation, Carlito y Caladea, `/opt/converter/fonts/default`, bibliotecas `libmp3lame0`, `libvorbisenc2`, `libopus0`, `libvpx9` y `libopenh264-8`.
    - Usuario `converter` (uid 10001), `/var/lib/converter/storage`, `EXPOSE 8080`, `ENTRYPOINT ["/opt/converter/venv/bin/python", "-m", "converter"]` y el `HEALTHCHECK` del diseño. `.dockerignore` excluye `tests/`. Construcción con `docker buildx build --platform linux/amd64,linux/arm64`.
    - _Requirements: 11.4, 13.1, 13.5, 13.6, 20.1, 20.2, 20.3, 20.4, 20.7, 20.11_

  - [~] 24.4 Escribir pruebas de la imagen en `tests/integration/test_image.py`
    - El manifiesto tiene las dos variantes; `docker inspect` muestra el `HEALTHCHECK`; hay un único modelo con su hash; están las fuentes; `ffmpeg -buildconf` no tiene `--enable-gpl`, `--enable-nonfree` ni protocolos de red; el usuario es 10001.
    - _Requirements: 11.4, 13.5, 20.1, 20.7, 20.11_

  - [~] 24.5 Crear el arnés de pruebas en contenedor y el generador del corpus
    - `tests/integration/conftest.py` arranca el Contenedor y ofrece un cliente HTTP y funciones para comparar Contenido_Decodificado. `tests/fixtures/generate_corpus.py` genera al menos un archivo por Formato de origen y los DOCX, PDF y audios del diseño; los clips de voz CC0 se descargan con hash verificado solo en CI.
    - _Requirements: 4.9, 7.1, 11.11_

  - [~] 24.6 Escribir la prueba de propiedad de los pares Sin_Pérdida
    - **Property 9: Los pares Sin_Pérdida preservan el Contenido_Decodificado**
    - **Validates: Requirements 4.11**

  - [~] 24.7 Escribir la prueba de propiedad del determinismo
    - **Property 51: Las conversiones son deterministas**
    - **Validates: Requirements 13.4**

  - [~] 24.8 Escribir la prueba de propiedad de los archivos dañados
    - **Property 67: Un archivo dañado falla de forma controlada**
    - **Validates: Requirements 16.2**

  - [~] 24.9 Escribir pruebas de integración de los 36 pares con el corpus
    - Dimensiones_Mostradas, dimensiones de video, duraciones, páginas, fuentes incrustadas, filas y columnas de tablas, relación de aspecto de imágenes en DOCX a PDF y detección de idioma con clips en español e inglés.
    - _Requirements: 4.9, 5.1, 7.1, 8.4, 9.1, 10.6, 11.11_

  - [~] 24.10 Escribir la prueba sin red
    - `tests/integration/network/docker-compose.yml` con una red `internal` y un contenedor sumidero de DNS y HTTP. Con el corpus (incluidas URL canario) y la suite E2E se exigen cero consultas DNS, cero conexiones salientes y el mismo Contenido_Decodificado que con red.
    - _Requirements: 13.1, 13.3, 13.6, 13.7, 18.15, 20.3, 20.4_

  - [~] 24.11 Escribir la prueba de confinamiento de archivos
    - Tras el corpus no hay archivos nuevos de las Tareas fuera de `/var/lib/converter/storage`.
    - _Requirements: 13.2, 17.7_

  - [~] 24.12 Escribir la prueba de concurrencia
    - Varias subidas desde clientes distintos con el Límite_de_Concurrencia ocupado: la recepción empieza y la detección termina dentro de los plazos.
    - _Requirements: 19.9, 19.10, 2.11, 3.1_

  - [~] 24.13 Escribir pruebas del ciclo de vida del Contenedor
    - Variable inválida y volumen de solo lectura con código de salida distinto de 0 en menos de 10 segundos; volumen con archivos ajenos y un Espacio_de_Trabajo viejo donde solo se borra este último; paso a `unhealthy` en menos de 60 segundos al congelar el servidor (por ejemplo, con `docker kill --signal SIGSTOP`).
    - _Requirements: 17.5, 20.6, 20.8, 20.11, 20.13_

  - [~] 24.14 Escribir los benchmarks en `tests/benchmarks/`
    - Transcripción: pico de memoria residente de `whisper-cli` ≤ 1,5 GB y factor de tiempo real ≤ 1 sobre 10 audios; arranque hasta `healthy` ≤ 60 segundos; detección de archivos del tamaño límite, incluido un DOCX de 25 MB, ≤ 10 segundos. Se ejecutan con una marca propia, fuera de la suite por defecto.
    - _Requirements: 2.11, 20.2_

- [ ] 25. Documentar, generar el Inventario_de_Licencias y configurar la integración continua
  - [~] 25.1 Escribir `docs/usage-guide.md` en español
    - **MVP:** agregar una sección con lo que quedó para después: transcripción, saneamiento de DOCX, FFmpeg propio con licencia LGPL, imagen multiarquitectura, Inventario_de_Licencias, CI y las pruebas diferidas.
    - Comando `docker run --rm -p 8080:8080 converter:1.0.0`, ruta `/var/lib/converter/storage`, tabla de Variables_de_Configuración con nombre, tipo, unidad, rango y valor predeterminado, montaje de volúmenes (responsabilidad de quien monta, sin detección de sistemas de archivos de red, limpieza solo de `ws-…`) y opciones de endurecimiento (`--read-only` con `--tmpfs /tmp`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory`).
    - _Requirements: 20.2, 20.5, 20.7, 20.8_

  - [~] 25.2 Escribir la prueba que compara la Guía_de_Uso con `config.py`
    - _Requirements: 20.5_

  - [~] 25.3 Implementar `scripts/generate_license_inventory.py` y `licenses/manual.toml`
    - Combina `dpkg-query` y `/usr/share/doc/*/copyright`, los metadatos del venv y `licenses/manual.toml` (FFmpeg, whisper.cpp, el modelo y DejaVu). Genera `licenses/inventory.json` y `THIRD_PARTY_LICENSES.md` con nombre, versión, licencia, variante, obligaciones de copyleft débil y marca de códecs con patentes (OpenH264 y H.264 y AAC de FFmpeg). Generar ambos archivos ejecutando el script en cada variante de la imagen.
    - _Requirements: 20.12_

  - [~] 25.4 Escribir la prueba de regeneración del Inventario_de_Licencias
    - Regenera el inventario dentro de la imagen y falla si difiere de `licenses/inventory.json`.
    - _Requirements: 20.12_

  - [~] 25.5 Implementar `.github/workflows/ci.yml`
    - Lint y tipos, pruebas unitarias y de propiedades con el perfil `ci`, construcción multiarquitectura con buildx, pruebas dentro de la imagen en runners nativos amd64 y arm64, pruebas E2E con Playwright, prueba sin red y comparación del Inventario_de_Licencias.
    - _Requirements: 20.1, 20.12_

  - [~] 25.6 Implementar `tests/traceability.py`
    - Lee los criterios de `requirements.md`, las propiedades y sus "Validates" de `design.md`, los comentarios `Feature: file-converter, Property N` y las marcas `req`, y falla si algún criterio no tiene al menos una prueba asociada.
    - _Requirements: 20.12_

- [~] 26. Checkpoint final - Verificar la suite completa
  - Asegurar que todas las pruebas pasan; consultar al usuario si surgen dudas.

## Notes

- Las sub-tareas marcadas con `*` quedan fuera del MVP; la sección «Fuera del alcance del MVP» explica qué falta, qué criterios afecta y cómo retomarlo.
- Cada sub-tarea indica los criterios de `requirements.md` que cumple; las de propiedades indican el número de propiedad de `design.md`.
- Cada familia de convertidores registra sus pares en `catalog/pairs.toml` al final de su tarea, así cada familia queda conectada al Conversor apenas se implementa, sin cambios en el núcleo (Req 4.3).
- Los generadores compartidos se agrupan en el paquete `tests/strategies/`, la forma de paquete del `tests/strategies.py` del diseño, para que varias tareas puedan agregar generadores sin editar el mismo archivo.
- La verificación de Safari de escritorio es una lista de comprobación manual según el diseño y no forma parte de este plan.
- La evaluación legal de patentes (D19) y la publicación de la imagen están fuera del plan porque no son tareas de código.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "3.1", "4.1", "6.1", "7.1", "10.1", "11.2", "11.3", "15.1", "17.5", "18.1", "18.6", "19.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.2", "3.4", "3.5", "4.2", "4.3", "6.2", "7.2", "7.5", "11.1", "11.4", "15.2", "17.1", "18.7", "19.2", "19.3", "19.4"] },
    { "id": 3, "tasks": ["3.3", "4.4", "4.5", "4.6", "5.1", "7.3", "7.4", "7.6", "7.7", "9.1", "10.2"] },
    { "id": 4, "tasks": ["4.7", "5.2", "5.3", "6.3", "9.2", "11.5"] },
    { "id": 5, "tasks": ["6.4"] },
    { "id": 6, "tasks": ["10.3"] },
    { "id": 7, "tasks": ["11.6"] },
    { "id": 8, "tasks": ["11.7"] },
    { "id": 9, "tasks": ["11.8"] },
    { "id": 10, "tasks": ["12.1"] },
    { "id": 11, "tasks": ["12.2"] },
    { "id": 12, "tasks": ["12.3"] },
    { "id": 13, "tasks": ["13.1", "13.2"] },
    { "id": 14, "tasks": ["15.3", "15.4", "15.6", "16.1", "16.2", "16.3", "17.2", "17.3", "17.4", "17.6", "18.3", "19.5"] },
    { "id": 15, "tasks": ["15.7", "18.8", "19.6", "20.1"] },
    { "id": 16, "tasks": ["16.4", "20.2"] },
    { "id": 17, "tasks": ["17.7"] },
    { "id": 18, "tasks": ["18.9"] },
    { "id": 19, "tasks": ["19.7"] },
    { "id": 20, "tasks": ["20.3"] },
    { "id": 21, "tasks": ["23.1", "23.2"] },
    { "id": 22, "tasks": ["23.3", "23.4"] },
    { "id": 23, "tasks": ["23.5"] },
    { "id": 24, "tasks": ["24.3"] },
    { "id": 25, "tasks": ["25.1"] },
    { "id": 26, "tasks": ["5.4", "9.3"] },
    { "id": 27, "tasks": ["6.5", "6.6", "6.7", "6.8"] },
    { "id": 28, "tasks": ["10.4"] },
    { "id": 29, "tasks": ["11.9"] },
    { "id": 30, "tasks": ["11.10", "11.11", "11.12", "11.13", "11.14", "11.15", "11.16", "11.17", "11.18"] },
    { "id": 31, "tasks": ["12.4", "12.5", "12.6", "12.7", "12.8"] },
    { "id": 32, "tasks": ["13.3", "18.4"] },
    { "id": 33, "tasks": ["15.5", "18.5"] },
    { "id": 34, "tasks": ["15.8", "15.9", "15.10", "15.11", "15.12", "15.13"] },
    { "id": 35, "tasks": ["16.5", "16.8", "21.1"] },
    { "id": 36, "tasks": ["16.6", "16.7", "17.8", "17.18", "21.2"] },
    { "id": 37, "tasks": ["17.9", "17.10", "17.11", "17.12", "17.13", "17.14", "17.15", "17.16", "17.17", "18.2", "18.10", "18.11", "18.12"] },
    { "id": 38, "tasks": ["19.8", "19.9"] },
    { "id": 39, "tasks": ["20.4", "20.5", "20.6", "20.7", "21.3"] },
    { "id": 40, "tasks": ["21.4", "21.5", "21.6", "21.7", "24.1", "24.2"] },
    { "id": 41, "tasks": ["23.6"] },
    { "id": 42, "tasks": ["23.7", "23.8", "24.4", "24.5", "25.3"] },
    { "id": 43, "tasks": ["24.6", "24.7", "24.8", "24.9", "24.10", "24.11", "24.12", "24.13", "24.14", "25.2", "25.4", "25.5"] },
    { "id": 44, "tasks": ["25.6"] }
  ]
}
```
