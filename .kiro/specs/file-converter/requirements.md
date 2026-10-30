# Requirements Document

## Introduction

Este documento define los requisitos de un conversor de archivos. El usuario sube un archivo, el sistema detecta su tipo real, muestra a qué formatos puede convertirse, ejecuta la conversión elegida y entrega el resultado para descargar. También incluye la transcripción de audio y video a texto. El alcance inicial se limita a pocos formatos simples, y el sistema debe permitir sumar nuevos pares origen→destino más adelante sin rehacer el núcleo.

El Conversor es una aplicación web en español que se usa desde el navegador, se ejecuta como contenedor Docker en cualquier equipo con Docker y procesador amd64 o arm64, y funciona sin conexión a Internet. No requiere cuentas de usuario y admite que varias personas lo usen al mismo tiempo.

### Qué implica convertir archivos

- **Sin pérdida y con pérdida.** Una conversión es sin pérdida cuando el resultado no descarta información del origen. Ejemplos: PNG a WEBP en modo sin pérdida, o MP3 a WAV (no recupera la calidad que el MP3 ya perdió, pero tampoco pierde más). Una conversión es con pérdida cuando descarta información: calidad (WAV a MP3, PNG a JPG), transparencia (PNG a JPG), estructura (HTML a TXT) o una pista completa (MP4 a MP3 descarta la imagen). Convertir repetidamente entre formatos con pérdida (MP3 a OGG y de vuelta) acumula degradación.
- **Conversiones unidireccionales.** Casi cualquier documento o imagen puede llevarse a PDF, porque un PDF describe cómo se ve cada página. El camino inverso es limitado: un PDF guarda texto y gráficos ubicados en coordenadas, sin párrafos, tablas ni estilos como estructura editable. Por eso PDF a TXT solo extrae el texto (y solo si el PDF tiene capa de texto; un PDF escaneado es una imagen), y PDF a DOCX con fidelidad queda fuera del alcance.
- **Tipo real y extensión.** La extensión del nombre puede estar mal. El sistema identifica el formato por su contenido (firmas de bytes o estructura interna). Los formatos de texto plano (TXT, Markdown) no tienen firma y se distinguen por la extensión. Se aceptan textos en UTF-8 y en Windows-1252 (ISO-8859-1), codificación frecuente en archivos antiguos en español; los resultados de texto se escriben siempre en UTF-8.
- **Costo de procesamiento.** Imágenes y documentos se convierten en segundos. Audio, video y transcripción pueden tardar minutos, por lo que se procesan como tareas con estado y progreso.

### Conversión sin IA

Las conversiones de imágenes, documentos, audio y video pueden resolverse por completo con herramientas deterministas que implementan las especificaciones de cada formato, sin IA. Ejemplos de herramientas existentes: FFmpeg (audio y video), LibreOffice en modo headless (DOCX a PDF), Pandoc (Markdown, HTML y texto), ImageMagick o Pillow (imágenes). Se mencionan solo para mostrar que es posible; la elección tecnológica corresponde al diseño.

### Transcripción y la restricción sobre IA

Transcribir voz a texto con calidad útil no puede resolverse con un algoritmo fijo. El reconocimiento de voz moderno se basa en modelos estadísticos o de aprendizaje automático entrenados con grandes volúmenes de audio, y una transcripción sin ningún modelo no es viable. Lo que sí es posible, y refleja la preferencia del usuario:

- **Procesamiento local.** El modelo se ejecuta en el propio equipo, sin conexión y sin enviar audio a servicios de IA en la nube. Ejemplos: Vosk o whisper.cpp (modelos de código abierto que corren en CPU), o CMU Sphinx (enfoque clásico más antiguo y de menor precisión).
- **Resultados reproducibles.** Con el mismo modelo y la misma configuración, el mismo audio produce el mismo texto.
- **Resultado aproximado.** La precisión depende de la calidad del audio, el ruido, el idioma y el acento. El sistema presenta la transcripción como aproximada.

Para un video, transcribir requiere primero extraer su pista de audio. El reconocimiento de texto en imágenes o PDFs escaneados (OCR) tiene la misma naturaleza estadística y queda fuera del alcance inicial. El Conversor incluye un único modelo multilingüe con el perfil descrito en S10; el usuario elige el idioma (español de forma predeterminada) o la detección automática del idioma. El motor de transcripción concreto se decide en el diseño.

### Mapa inicial de conversión

Clasificaciones usadas (definidas en el Glosario): **Sin_Pérdida**, **Con_Pérdida**, **Limitada** y **Aproximada**.

**Imágenes**

| Origen | Destino | Clasificación | Nota |
|---|---|---|---|
| PNG | JPG | Con_Pérdida | Compresión con pérdida; la transparencia se reemplaza por fondo blanco. |
| PNG | WEBP | Sin_Pérdida | Modo sin pérdida de WEBP. Las imágenes de 16 bits por canal se reducen a 8 bits. |
| PNG | PDF | Sin_Pérdida | Una página con la imagen. |
| JPG | PNG | Sin_Pérdida | No recupera la calidad perdida en el JPG; el archivo suele ser más grande. |
| JPG | WEBP | Con_Pérdida | Recompresión con pérdida. |
| JPG | PDF | Sin_Pérdida | Una página con la imagen. |
| WEBP | PNG | Sin_Pérdida | Solo WEBP estático. |
| WEBP | JPG | Con_Pérdida | Recompresión; la transparencia se reemplaza por fondo blanco. |
| WEBP | PDF | Sin_Pérdida | Una página con la imagen. |

**Documentos**

| Origen | Destino | Clasificación | Nota |
|---|---|---|---|
| TXT | PDF | Sin_Pérdida | Fuente y márgenes predeterminados; requiere que la fuente incluya los caracteres del texto. |
| TXT | HTML | Sin_Pérdida | Texto escapado dentro de un documento HTML. |
| MD | HTML | Sin_Pérdida | Interpretación según CommonMark. |
| MD | PDF | Sin_Pérdida | Estructura CommonMark con estilo predeterminado. |
| MD | TXT | Con_Pérdida | Se elimina el marcado. |
| HTML | PDF | Con_Pérdida | Se omiten scripts y recursos remotos; se pierde la interactividad. |
| HTML | MD | Con_Pérdida | Se descartan estilos y elementos sin equivalente en Markdown. |
| HTML | TXT | Con_Pérdida | Solo queda el texto. |
| DOCX | PDF | Con_Pérdida | La maquetación puede variar si faltan fuentes; comentarios, control de cambios y macros no se conservan. |

**PDF como origen (limitado)**

| Origen | Destino | Clasificación | Nota |
|---|---|---|---|
| PDF | TXT | Limitada | Solo extrae la capa de texto; se pierde la maquetación. Un PDF escaneado no tiene texto extraíble. |
| PDF | PNG | Con_Pérdida | Una imagen por página; el texto deja de ser seleccionable. Varias páginas se entregan en un archivo ZIP. |

**Audio**

| Origen | Destino | Clasificación | Nota |
|---|---|---|---|
| WAV | MP3 | Con_Pérdida | Compresión con pérdida. |
| WAV | OGG | Con_Pérdida | Compresión con pérdida (Vorbis). |
| MP3 | WAV | Sin_Pérdida | No recupera calidad; el archivo resulta unas 10 veces más grande. |
| MP3 | OGG | Con_Pérdida | Transcodificación entre formatos con pérdida; acumula degradación. |
| OGG | WAV | Sin_Pérdida | No recupera calidad; el archivo resulta más grande. |
| OGG | MP3 | Con_Pérdida | Transcodificación entre formatos con pérdida; acumula degradación. |

**Video**

| Origen | Destino | Clasificación | Nota |
|---|---|---|---|
| MP4 | MP3 | Con_Pérdida | Se descarta la imagen; el audio se recodifica con pérdida. |
| MP4 | WAV | Con_Pérdida | Se descarta la imagen; el audio no pierde calidad adicional. |
| MP4 | WEBM | Con_Pérdida | Recodificación de video; procesamiento lento. |
| WEBM | MP4 | Con_Pérdida | Recodificación de video; procesamiento lento. Un ancho o alto impar se completa con 1 píxel negro (D16). |

**Transcripción**

Un archivo SRT contiene subtítulos: bloques numerados, cada uno con un tiempo de inicio, un tiempo de fin y un texto, que los reproductores de video muestran sincronizados con la imagen. En el Conversor, SRT es solo Formato_de_Destino: un archivo ".srt" subido se detecta como SRT y se rechaza porque no es Formato_de_Origen de ningún Par_de_Conversión (Requisito 2, criterio 7).

| Origen | Destino | Clasificación | Nota |
|---|---|---|---|
| MP3 | TXT | Aproximada | Precisión variable según la calidad del audio. |
| WAV | TXT | Aproximada | Precisión variable según la calidad del audio. |
| MP4 | TXT | Aproximada | Requiere extraer la pista de audio primero. |
| MP3 | SRT | Aproximada | Texto con marcas de tiempo por segmento; precisión variable según la calidad del audio. |
| WAV | SRT | Aproximada | Texto con marcas de tiempo por segmento; precisión variable según la calidad del audio. |
| MP4 | SRT | Aproximada | Requiere extraer la pista de audio primero; texto con marcas de tiempo por segmento. |

**Fuera del alcance inicial**

| Conversión | Motivo |
|---|---|
| PDF → DOCX, MD o HTML | No viable con fidelidad: el PDF no conserva estructura editable y su reconstrucción es heurística y propensa a errores. |
| Imagen o PDF escaneado → texto (OCR) | Requiere modelos de reconocimiento; candidato para una fase posterior con motor local. |
| DOCX → TXT, MD o HTML; TXT, MD o HTML → DOCX | Viables; candidatos para la próxima fase. |
| Hojas de cálculo y presentaciones (XLSX, PPTX, ODS, ODP) | Fuera del alcance inicial. |
| Otros formatos de imagen (GIF, TIFF, BMP, HEIC, SVG) e imágenes animadas | Candidatos para fases posteriores. |
| Otros formatos de audio y video (FLAC, AAC/M4A, MOV, MKV, AVI) | Candidatos para fases posteriores. |
| Transcripción de OGG y WEBM | Viable con el mismo flujo; candidato para la próxima fase. |
| SRT como origen (SRT → TXT u otros formatos) | SRT es solo Formato_de_Destino en el alcance inicial; un archivo ".srt" subido se rechaza. |
| Texto a voz, traducción, identificación de hablantes | Requieren modelos adicionales. |
| Conversión por lotes, unión de varios archivos en uno, edición (recortar, redimensionar, comprimir) | Fuera del alcance inicial. |
| Preservación de metadatos (EXIF, ID3) | Fuera del alcance inicial. La Orientación_EXIF se aplica a los píxeles y no se conserva (D15). |
| Conversiones entre categorías sin relación (por ejemplo, imagen → audio) | No tienen sentido práctico. |

### Supuestos

- **S1.** La Interfaz_de_Usuario es una aplicación web en español que el usuario usa desde un navegador (D1).
- **S2.** El Conversor se ejecuta como un Contenedor de Docker en un único Equipo_Anfitrión (personal o servidor) bajo control del usuario, en cualquier equipo con Docker y procesador amd64 o arm64 (D2).
- **S3.** No hay cuentas ni autenticación. Cualquier persona con acceso a la Interfaz_de_Usuario puede usarla, y varias personas pueden usarla al mismo tiempo (D3).
- **S4.** Cada Tarea procesa un solo archivo.
- **S5.** La transcripción corre en CPU; no se asume GPU.
- **S6.** Los parámetros de calidad de salida (tasa de bits de audio, calidad JPG, fuentes de PDF) usan valores predeterminados definidos en el diseño y el usuario no los configura en el alcance inicial.
- **S7.** Valores configurables y sus valores predeterminados. Cada valor puede cambiarse con una Variable_de_Configuración al iniciar el Contenedor. El nombre, la unidad y el rango admitido de cada Variable_de_Configuración se documentan en la Guía_de_Uso (Requisito 20, criterios 5 y 6; D9):

| Valor | Propuesta inicial |
|---|---|
| Límite_de_Tamaño por categoría | Imagen: 25 MB; documento: 25 MB; audio: 200 MB; video: 500 MB |
| Límite_de_Tamaño_Global | 500 MB |
| Límite_de_Píxeles | 100 millones de píxeles (ancho × alto) |
| Duración_Máxima_de_Transcripción | 60 minutos de audio |
| Tiempo_Máximo_de_Procesamiento | Imagen y documento: 2 min; audio: 10 min; video: 30 min; transcripción: 120 min |
| Período_de_Retención | 60 minutos |
| Límite_de_Concurrencia | 2 Tareas en_proceso |
| Resolución_de_Rasterizado | 150 ppp |
| Espacio_Libre_Mínimo | 1 GB |
| Intervalo_de_Progreso | 5 segundos |
| Frecuencias_de_Muestreo_Admitidas de OGG | Cualquier valor entero entre 8000 y 192000 Hz |
| Puerto_de_la_Interfaz | 8080 |

- **S8.** Las Tareas no se conservan entre reinicios del Contenedor (D14). Al iniciar, el Conversor no tiene Tareas y el criterio 5 del Requisito 17 elimina todos los Espacios_de_Trabajo del Almacenamiento_Temporal, también cuando el Almacenamiento_Temporal está montado en un volumen.
- **S9.** El control de acceso a la Interfaz_de_Usuario (quién puede abrirla) queda a cargo de la red o del entorno donde se ejecuta el Contenedor y está fuera del alcance.
- **S10.** Perfil de transcripción equilibrado (D5): un único Modelo_de_Transcripción multilingüe de tamaño intermedio que, como objetivo, usa como máximo 1,5 GB de RAM durante la transcripción y procesa audio claro en CPU en un tiempo menor o igual a la duración del audio en el Equipo_de_Referencia. El objetivo se verifica en el diseño con un conjunto de audios de referencia y no es una garantía para todo audio ni para todo equipo. La elección concreta del motor y del modelo corresponde al diseño.
- **S11.** Todavía no está decidido si el Conversor se distribuirá o comercializará (D8). Por eso se prefieren componentes de terceros con licencias permisivas (por ejemplo MIT, BSD o Apache 2.0) cuando existe una alternativa equivalente, y cada componente incluido se registra en el Inventario_de_Licencias (Requisito 20, criterio 12).
- **S12.** Quien ejecuta el Contenedor elige el volumen que monta en la ruta del Almacenamiento_Temporal, y el Conversor no detecta ni rechaza sistemas de archivos de red (D17). Si se monta un volumen de red en esa ruta, los Archivos_de_Origen, Archivos_Intermedios y Archivos_Resultado se guardan en ese volumen bajo responsabilidad de quien lo monta. El acceso a ese volumen lo realiza el Equipo_Anfitrión y no cuenta como conexión de red iniciada por el Conversor (Requisito 13, criterio 7).
- **S13.** Los Límites_de_Descompresión de DOCX son valores fijos definidos por el diseño y no se cambian con Variables_de_Configuración (D18). En PDF a PNG, el tamaño de cada página rasterizada se acota con el Límite_de_Píxeles de S7.
- **S14.** MP4 usa los códecs H.264 (video) y AAC (audio), cubiertos por patentes que pueden requerir licencias si el Conversor se comercializa. Esto no afecta el uso propio y se evalúa si se decide distribuir el Conversor (S11, D19).

### Decisiones

- **D1. Interfaz:** aplicación web en español, usada desde el navegador (Requisito 18).
- **D2. Despliegue:** Imagen_de_Contenedor de Docker, con una variante para amd64 y otra para arm64, que se ejecuta en cualquier equipo con Docker de esas arquitecturas y funciona sin conexión a Internet (Requisito 20).
- **D3. Uso sin cuentas:** sin autenticación; varias personas pueden usar el Conversor a la vez y cada una ve solo sus Tareas (Requisito 19).
- **D4. Idiomas de transcripción:** un único Modelo_de_Transcripción multilingüe; español como opción predeterminada, otro Idioma_Soportado o Detección_Automática_de_Idioma (Requisito 11).
- **D5. Transcripción:** perfil equilibrado entre precisión y velocidad (S10).
- **D6. SRT:** incluido en el alcance inicial como Formato_de_Destino de transcripción (Requisito 12).
- **D7. Codificaciones de texto:** se aceptan UTF-8, con o sin BOM_UTF8, y Windows-1252 (ISO-8859-1); los resultados de texto se escriben en UTF-8 (Requisitos 2 y 6).
- **D8. Licencias:** preferencia por licencias permisivas e Inventario_de_Licencias en el repositorio (S11; Requisito 20).
- **D9. Límites:** los valores de S7 son los predeterminados y todos se cambian con Variables_de_Configuración, cuyos nombres, unidades y rangos admitidos se documentan en la Guía_de_Uso (Requisito 20).
- **D10. DOCX con control de cambios:** el PDF muestra la versión final, con los cambios aplicados (Requisito 7, criterio 6).
- **D11. PDF con permisos restringidos:** los PDF que se abren sin contraseña y restringen permisos se procesan; solo se rechazan los que piden contraseña de apertura (Requisito 8).
- **D12. Nombre_Base:** límite de 200 bytes en UTF-8, para no superar el límite de 255 bytes por nombre de los sistemas de archivos habituales (Requisito 15).
- **D13. Reintentos de limpieza:** hasta 3 intentos, con 60 segundos entre intentos (Requisito 17, criterio 9).
- **D14. Tareas no persistentes:** las Tareas no se conservan entre reinicios del Contenedor (S8; Requisito 14, criterio 13; Requisito 17, criterio 5).
- **D15. Orientación EXIF:** el Motor_de_Conversión aplica la Orientación_EXIF a los píxeles y el Archivo_Resultado no conserva la etiqueta, para que se vea igual que el origen en cualquier visor (Requisito 5, criterios 1, 4, 5, 7, 9 y 10).
- **D16. Dimensiones impares en MP4:** el códec de video de MP4 (H.264) requiere ancho y alto pares; en WEBM a MP4, cada dimensión impar se completa con 1 píxel negro a la derecha o abajo y se informa al usuario (Requisito 10, criterios 5 y 11).
- **D17. Almacenamiento_Temporal en volúmenes:** quien ejecuta el Contenedor elige el volumen montado; el Conversor no detecta ni rechaza sistemas de archivos de red (S12; Requisito 13, criterio 2; Requisito 20, criterio 8).
- **D18. Límites internos de seguridad:** un DOCX que supera los Límites_de_Descompresión se rechaza, y en PDF a PNG una página que rasterizada supera el Límite_de_Píxeles hace fallar la Tarea (S13; Requisito 7, criterio 7; Requisito 8, criterio 11; Requisito 16, criterio 1).
- **D19. Patentes de códecs:** las licencias de patentes de H.264 y AAC se evalúan solo si se decide distribuir o comercializar el Conversor (S14).

## Glossary

**Sistema y componentes**

- **Conversor**: El sistema completo descrito en este documento.
- **Interfaz_de_Usuario**: Aplicación web del Conversor, en español, que el usuario usa desde un navegador (D1).
- **Navegador_del_Usuario**: Perfil de un navegador web desde el que un usuario usa la Interfaz_de_Usuario. Si el usuario borra los datos del sitio de la Interfaz_de_Usuario en ese perfil, el perfil cuenta como un Navegador_del_Usuario nuevo.
- **Área_de_Carga**: Zona delimitada de la página de la Interfaz_de_Usuario sobre la que el usuario suelta un archivo arrastrado para enviarlo al Conversor, y junto a la cual la Interfaz_de_Usuario muestra los mensajes de recepción.
- **Selector_de_Archivos**: Control de la página de la Interfaz_de_Usuario que, al activarse, abre el cuadro de diálogo del navegador para elegir un archivo. Ese cuadro de diálogo pertenece al navegador o al sistema operativo y no forma parte del Selector_de_Archivos.
- **Detector_de_Tipo**: Componente del Conversor que determina el Tipo_Real de un archivo.
- **Registro_de_Conversiones**: Componente del Conversor que almacena las definiciones de Formatos y Pares_de_Conversión y expone el Mapa_de_Conversión.
- **Motor_de_Conversión**: Componente del Conversor que ejecuta los Pares_de_Conversión de imagen, documento, audio y video.
- **Extractor_de_Audio**: Componente del Conversor que obtiene la Pista_de_Audio_Predeterminada de un archivo de video.
- **Transcriptor**: Componente del Conversor que convierte voz en texto mediante el Modelo_de_Transcripción.
- **Serializador_SRT**: Componente del Conversor que escribe una lista de Segmentos_de_Transcripción en formato SRT.
- **Intérprete_SRT**: Componente del Conversor que lee un archivo SRT y obtiene la lista de Segmentos_de_Transcripción; se usa para verificar la salida SRT.
- **Gestor_de_Tareas**: Componente del Conversor que crea, encola, ejecuta y cancela Tareas, y reporta su estado.
- **Gestor_de_Archivos_Temporales**: Componente del Conversor que crea y elimina Espacios_de_Trabajo.

**Formatos y detección**

- **Formato**: Tipo de archivo (por ejemplo PNG o MP3) definido por un nombre, una Categoría, una o más extensiones, una Extensión_Principal y, si existe, su Firma_de_Formato.
- **Extensión_Principal**: Extensión, en minúsculas y con punto inicial, que el Registro_de_Conversiones asigna a cada Formato para nombrar los Archivos_Resultado; figura entre las extensiones del Formato. En el Mapa_de_Conversión_Inicial: PNG ".png", JPG ".jpg", WEBP ".webp", PDF ".pdf", TXT ".txt", MD ".md", HTML ".html", DOCX ".docx", MP3 ".mp3", WAV ".wav", OGG ".ogg", MP4 ".mp4", WEBM ".webm" y SRT ".srt".
- **Categoría**: Agrupación de Formatos: imagen (PNG, JPG, WEBP), documento (TXT, MD, HTML, DOCX, PDF y SRT), audio (MP3, WAV, OGG) o video (MP4, WEBM). La Categoría de un Par_de_Conversión es la Categoría de su Formato_de_Origen.
- **Firma_de_Formato**: Secuencia de bytes o estructura interna que identifica un Formato por su contenido (por ejemplo, los primeros 8 bytes de un PNG, o la estructura interna del paquete ZIP de un DOCX).
- **Formato_de_Texto**: Formato sin Firma_de_Formato cuyo contenido es Texto_Válido. En el Mapa_de_Conversión_Inicial: TXT, MD (Markdown), HTML y SRT.
- **Texto_UTF8_Válido**: Contenido que se decodifica completo como UTF-8 sin secuencias de bytes inválidas, con o sin BOM_UTF8 inicial, y que no contiene el carácter nulo U+0000.
- **Texto_Windows_1252**: Contenido que no es Texto_UTF8_Válido, no contiene el byte 0x00 y en el que los bytes 0x01 a 0x1F y 0x7F, excluidos tabulación (0x09), salto de línea (0x0A), avance de página (0x0C) y retorno de carro (0x0D), representan menos del 1 % del total de bytes. Se interpreta con la codificación Windows-1252, que contiene todos los caracteres imprimibles de ISO-8859-1; los bytes 0x81, 0x8D, 0x8F, 0x90 y 0x9D, sin carácter asignado en Windows-1252, se interpretan como el carácter Unicode de igual valor.
- **Texto_Válido**: Contenido que es Texto_UTF8_Válido o Texto_Windows_1252.
- **Texto_Decodificado**: Secuencia de caracteres que se obtiene de un Texto_Válido: decodificado como UTF-8 y sin la BOM_UTF8 inicial si es Texto_UTF8_Válido, o decodificado como Windows-1252 si es Texto_Windows_1252.
- **BOM_UTF8**: Secuencia de bytes EF BB BF al inicio de un contenido de texto.
- **Tipo_Real**: Formato que el Detector_de_Tipo determina para un archivo.

**Archivos y nombres**

- **Archivo_de_Origen**: Archivo subido por el usuario para convertir.
- **Archivo_Resultado**: Archivo producido por una conversión o transcripción.
- **Archivo_Intermedio**: Archivo generado durante el procesamiento que no se entrega al usuario (por ejemplo, el audio extraído de un video para transcribir).
- **Nombre_Original**: Nombre del archivo, incluida su extensión, tal como lo envía el usuario a través de la Interfaz_de_Usuario.
- **Extensión_del_Nombre**: El último punto del Nombre_Original junto con los caracteres que lo siguen, siempre que lo siga al menos un carácter (por ejemplo, ".txt" en "notas.txt" y ".png" en ".png"). Se compara sin distinguir mayúsculas de minúsculas. Un Nombre_Original sin punto o que termina en punto no tiene Extensión_del_Nombre.
- **Nombre_Base**: Parte del Nombre_Original anterior a su último punto; si el Nombre_Original no contiene punto, es el Nombre_Original completo. Nombre_Base y Extensión_del_Nombre se obtienen dividiendo el Nombre_Original en el mismo punto. Ejemplos: "informe.final.docx" da "informe.final"; "notas" da "notas"; ".png" da un Nombre_Base vacío.
- **Carácter_No_Permitido**: Cualquiera de `/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|` y los caracteres de control U+0000 a U+001F y U+007F.
- **Normalización_de_Nombre**: Transformación del Nombre_Base definida en los criterios 3, 4, 5 y 12 del Requisito 15: reemplazo de Caracteres_No_Permitidos, truncado a 200 bytes en UTF-8 y sustitución del Nombre_Base vacío por "resultado", en ese orden.

**Conversiones**

- **Formato_de_Origen**: Formato del Archivo_de_Origen en un Par_de_Conversión.
- **Formato_de_Destino**: Formato del Archivo_Resultado en un Par_de_Conversión.
- **Par_de_Conversión**: Combinación registrada de un Formato_de_Origen y un Formato_de_Destino, con su Categoría, Clasificación_de_Pérdida y Descripción_de_Pérdida.
- **Mapa_de_Conversión**: Conjunto de todos los Pares_de_Conversión registrados en el Registro_de_Conversiones.
- **Mapa_de_Conversión_Inicial**: Pares_de_Conversión listados en la sección "Mapa inicial de conversión" de este documento.
- **Clasificación_de_Pérdida**: Uno de cuatro valores asignados a cada Par_de_Conversión:
  - **Sin_Pérdida**: el Contenido_Decodificado del Archivo_Resultado coincide con el del Archivo_de_Origen, dentro de la capacidad de representación del Formato_de_Destino.
  - **Con_Pérdida**: la conversión descarta información (calidad, transparencia, estructura o pistas).
  - **Limitada**: la conversión solo funciona para una parte de los archivos de origen (por ejemplo, PDF con capa de texto).
  - **Aproximada**: el resultado es una interpretación estadística del origen (transcripción).
- **Contenido_Decodificado**: Valores de píxel en imágenes, después de aplicar la Orientación_EXIF; muestras de audio en audio; Secuencia_de_Palabras en documentos; en video, los valores de píxel de cada cuadro y las muestras de audio; en un archivo ZIP de imágenes, el Contenido_Decodificado de cada imagen y el orden de las imágenes.
- **Descripción_de_Pérdida**: Texto en español que explica qué información se pierde o limita en un Par_de_Conversión.
- **Rechazo_de_Origen**: Rechazo del Archivo_de_Origen de una Tarea en estado recibida, antes de que la Interfaz_de_Usuario muestre los Formatos_de_Destino, que lleva la Tarea de recibida a fallida. Las causas se enumeran en el criterio 1 del Requisito 16.
- **Rechazo_de_Destino**: Rechazo de la solicitud de un Formato_de_Destino para una Tarea en estado recibida, que mantiene la Tarea en estado recibida para que el usuario elija otro Formato_de_Destino. Las causas se enumeran en el criterio 12 del Requisito 16.

**Imágenes**

- **Imagen_Animada**: Archivo con Tipo_Real PNG o WEBP que contiene más de un cuadro de animación (PNG animado o WEBP animado).
- **Orientación_EXIF**: Valor, de 1 a 8, de la etiqueta de orientación (Orientation, 0x0112) de los metadatos EXIF de una imagen PNG, JPG o WEBP, que indica la rotación o el reflejo que un visor aplica a los píxeles almacenados para mostrarlos. El valor 1 es la orientación normal; una imagen sin esa etiqueta o con un valor fuera de 1 a 8 tiene orientación normal. Aplicar la Orientación_EXIF es rotar o reflejar los píxeles almacenados como indica ese valor (D15).
- **Dimensiones_Mostradas**: Ancho y alto en píxeles de una imagen después de aplicar su Orientación_EXIF: iguales al ancho y al alto almacenados con los valores 1 a 4, e intercambiados con los valores 5 a 8.

**Documentos**

- **Secuencia_de_Palabras**: Lista ordenada de las palabras de un texto. Una palabra es una secuencia máxima de caracteres distintos de espacio, tabulación, retorno de carro y salto de línea. Dos textos tienen la misma Secuencia_de_Palabras aunque difieran en la cantidad o el tipo de esos separadores.
- **Texto_Visible**: Texto que un documento presenta al lector como contenido. En TXT: el Texto_Decodificado completo. En HTML: el texto del cuerpo del documento, tomado de su Texto_Decodificado, sin etiquetas, comentarios, Scripts ni contenido de hojas de estilo, con las referencias de caracteres reemplazadas por el carácter que representan; el límite entre dos títulos, párrafos, elementos de lista, celdas de tabla o bloques de código, y cada salto de línea forzado, cuentan como salto de línea. En MD: el Texto_Visible del HTML que CommonMark define como resultado para el documento.
- **Carácter_Latino**: Letra, dígito, espacio o signo de puntuación de los bloques Unicode Latín básico (U+0020 a U+007E) y Suplemento Latín-1 (U+00A1 a U+00FF, excluido el guion suave U+00AD); incluye vocales acentuadas, ü y ñ.
- **Texto_Latino**: Texto compuesto solo por Caracteres_Latinos, tabulaciones y saltos de línea, en el que cada palabra tiene entre 1 y 20 caracteres.
- **Script**: Código ejecutable de un documento HTML, incluido el HTML embebido en un documento MD: elementos script, atributos de manejo de eventos como onclick y enlaces con esquema javascript:.
- **Recurso_Externo**: Contenido que un documento HTML, MD o DOCX referencia para incorporarlo al presentarse (imagen, hoja de estilo, fuente, marco, plantilla, objeto, audio o video) y que no está incluido dentro del Archivo_de_Origen, por ejemplo por URL remota o por ruta de archivo relativa, absoluta o de red. Los enlaces de navegación no son Recursos_Externos.
- **Estructura_de_Documento**: Lista ordenada, con su anidamiento, de los títulos con su nivel de 1 a 6, párrafos, listas ordenadas y no ordenadas, elementos de lista, énfasis simples y fuertes, enlaces cuyo destino no es un Script con su URL de destino, y bloques de código de un documento HTML, junto con el texto de cada elemento. Dos Estructuras_de_Documento son iguales cuando tienen los mismos elementos en el mismo orden y anidamiento, la misma Secuencia_de_Palabras en cada elemento y el mismo texto, carácter por carácter, en cada bloque de código.
- **Fuente_Predeterminada**: Fuente o conjunto de fuentes tipográficas que el Motor_de_Conversión usa para escribir texto en los PDF generados desde TXT, MD y HTML, definida en el diseño (ver S6).
- **Cuerpo_del_Documento**: Contenido principal de un DOCX (párrafos, tablas e imágenes), excluidos encabezados, pies de página, notas al pie, notas al final y comentarios.
- **DOCX_Cifrado**: Archivo_de_Origen con Extensión_del_Nombre ".docx" cuyo contenido es un documento de Office cifrado con contraseña de apertura, que no puede leerse sin esa contraseña. Un DOCX con restricción de edición y sin contraseña de apertura no es un DOCX_Cifrado.
- **Entrada_ZIP**: Cada archivo contenido en el paquete ZIP de un DOCX.
- **Tamaño_Descomprimido**: El mayor entre el tamaño descomprimido que el paquete ZIP declara para una Entrada_ZIP y la cantidad de bytes que la Entrada_ZIP produce al descomprimirse.
- **Razón_de_Compresión**: Cociente entre el Tamaño_Descomprimido y el tamaño comprimido de una Entrada_ZIP.
- **Límites_de_Descompresión**: Valores fijos, no configurables, que acotan un DOCX (D18, S13): una suma de Tamaños_Descomprimidos de sus Entradas_ZIP de hasta 512 MB, hasta 10 000 Entradas_ZIP y una Razón_de_Compresión de hasta 200 en cada Entrada_ZIP.

**PDF**

- **Capa_de_Texto**: Texto de una página PDF almacenado como caracteres extraíbles, a diferencia del texto que solo forma parte de una imagen, por ejemplo en una página escaneada.
- **Página_sin_Texto**: Página PDF de la que el Motor_de_Conversión no extrae ningún carácter distinto de espacios, tabulaciones o saltos de línea.
- **Número_de_Página**: Posición de una página dentro del PDF, contada desde 1 en el orden del documento, sin considerar las etiquetas de numeración que defina el PDF.
- **PDF_Protegido**: PDF que requiere contraseña para abrirse.
- **PDF_con_Permisos_Restringidos**: PDF que se abre sin contraseña y usa el cifrado del formato PDF para restringir permisos, como copiar o imprimir. Un PDF_con_Permisos_Restringidos no es un PDF_Protegido (D11).

**Audio y video**

- **Par_de_Conversión_de_Audio**: Cualquiera de los Pares_de_Conversión WAV a MP3, WAV a OGG, MP3 a WAV, MP3 a OGG, OGG a WAV y OGG a MP3 del Mapa_de_Conversión_Inicial.
- **Duración_de_Audio**: Cantidad de muestras por canal que se obtienen al decodificar un audio, dividida por su frecuencia de muestreo y expresada en milisegundos.
- **Frecuencias_de_Muestreo_Admitidas**: Frecuencias de muestreo que acepta cada Formato de audio. MP3: 8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100 y 48000 Hz. OGG: el rango indicado en S7. WAV: cualquier valor.
- **Canal_LFE**: Canal de efectos de baja frecuencia de un audio multicanal, por ejemplo el ".1" de un audio 5.1.
- **Pista_de_Audio**: Flujo de audio contenido en un archivo de video; un archivo de video puede contener cero, una o varias.
- **Pista_de_Video**: Flujo de imágenes en movimiento contenido en un archivo de video.
- **Pista_de_Audio_Predeterminada**: Primera Pista_de_Audio marcada como predeterminada según el orden en que las pistas aparecen en el archivo de video; si ninguna está marcada, la primera Pista_de_Audio en ese orden.
- **Códec**: Método de codificación de una Pista_de_Audio, de una Pista_de_Video o de un archivo de audio, identificado por el nombre que declara el archivo.

**Transcripción**

- **Modelo_de_Transcripción**: Modelo estadístico de reconocimiento de voz multilingüe incluido en la Imagen_de_Contenedor. El Conversor incluye uno solo, que reconoce todos los Idiomas_Soportados (D4, S10).
- **Modelo_de_Aprendizaje_Automático**: Modelo cuyo comportamiento se obtiene entrenando con datos (por ejemplo, redes neuronales o modelos estadísticos de voz).
- **Idioma_Soportado**: Idioma cuyo habla reconoce el Modelo_de_Transcripción, según la lista de idiomas que declara el propio Modelo_de_Transcripción.
- **Idioma_de_Transcripción**: Opción de idioma de una transcripción, elegida por el usuario: un Idioma_Soportado o la Detección_Automática_de_Idioma. Si el usuario no elige una opción, el Idioma_de_Transcripción es el español.
- **Detección_Automática_de_Idioma**: Opción de Idioma_de_Transcripción con la que el Transcriptor determina el idioma del habla a partir del Audio_a_Transcribir.
- **Idioma_Detectado**: Idioma_Soportado que el Transcriptor determina para un Audio_a_Transcribir con la Detección_Automática_de_Idioma.
- **Par_de_Transcripción**: Par_de_Conversión con Clasificación_de_Pérdida Aproximada: MP3 a TXT, WAV a TXT, MP4 a TXT, MP3 a SRT, WAV a SRT y MP4 a SRT.
- **Audio_a_Transcribir**: Audio que procesa el Transcriptor en una Tarea: el Archivo_de_Origen cuando es MP3 o WAV, o la Pista_de_Audio_Predeterminada del Archivo_de_Origen cuando es MP4.
- **Segmento_de_Transcripción**: Fragmento de transcripción con tiempo de inicio y tiempo de fin en milisegundos y el texto reconocido en ese intervalo.
- **Línea_en_Blanco**: Línea vacía o compuesta solo por espacios o tabulaciones.
- **Lista_de_Segmentos_Válida**: Lista de Segmentos_de_Transcripción ordenada por tiempo de inicio, sin superposición entre segmentos, con tiempo de inicio mayor o igual a 0 y menor al tiempo de fin en cada segmento, y con un texto en cada segmento formado por una o más líneas, ninguna de las cuales es Línea_en_Blanco.
- **Línea_de_Tiempos_SRT**: Línea con formato `HH:MM:SS,mmm --> HH:MM:SS,mmm`, con HH de 00 a 99, MM de 00 a 59, SS de 00 a 59 y mmm de 000 a 999, cada campo completado con ceros a la izquierda; el primer valor es el tiempo de inicio y el segundo el tiempo de fin.
- **Bloque_SRT**: Grupo de líneas consecutivas de un archivo SRT que representa un Segmento_de_Transcripción, en este orden: una línea con el índice del bloque (entero positivo decimal sin ceros a la izquierda), una Línea_de_Tiempos_SRT, una o más líneas de texto que no son Líneas_en_Blanco y una línea vacía.

**Tareas**

- **Tarea**: Unidad de trabajo que asocia un Identificador_de_Tarea, un Archivo_de_Origen, su Nombre_Original, su Tipo_Real, el Par_de_Conversión elegido, el Idioma_de_Transcripción si el Par_de_Conversión es un Par_de_Transcripción, su Estado_de_Tarea y, si falla, su Motivo_de_Fallo.
- **Identificador_de_Tarea**: Identificador de una Tarea generado al azar por el Conversor (por ejemplo, un UUID versión 4), que una solicitud debe incluir para operar sobre esa Tarea (Requisito 19).
- **Estado_de_Tarea**: Uno de: recibida, en_cola, en_proceso, completada, fallida, cancelada.
- **Estado_Terminal**: Los Estados_de_Tarea completada, fallida y cancelada.
- **Tarea_de_Larga_Duración**: Tarea cuyo Par_de_Conversión tiene Categoría audio o video, o Clasificación_de_Pérdida Aproximada.
- **Porcentaje_de_Avance**: Número entero entre 0 y 100 que indica la proporción estimada del procesamiento completado de una Tarea en_proceso.
- **Etapa_de_Procesamiento**: Fase en la que ocurre un fallo o un rechazo: recepción (recepción y almacenamiento del archivo enviado, antes de crear la Tarea), detección (Detector_de_Tipo), conversión (Motor_de_Conversión o Extractor_de_Audio), transcripción (Transcriptor) y entrega (preparación del Archivo_Resultado para su descarga, incluido el empaquetado ZIP).
- **Motivo_de_Fallo**: Causa, expresada en español, por la que una Tarea termina en estado fallida. Puede incluir valores del caso que otros requisitos piden informar, como el valor de un límite, el nombre de un Códec o de un Formato, o Números_de_Página. Cada Motivo_de_Fallo tiene asociada exactamente una acción sugerida en español, no vacía, que indica un paso que el usuario puede dar.
- **Datos_de_Tarea**: Información asociada a una Tarea que el Conversor puede entregar o mostrar: su Identificador_de_Tarea, Nombre_Original, Tipo_Real, los Formatos_de_Destino ofrecidos, el Par_de_Conversión, el Idioma_de_Transcripción, el Idioma_Detectado, el Estado_de_Tarea, el Porcentaje_de_Avance, el Motivo_de_Fallo con su acción sugerida, el nombre de descarga del Archivo_Resultado y el contenido del Archivo_de_Origen y del Archivo_Resultado.

**Ejecución y almacenamiento**

- **Imagen_de_Contenedor**: Imagen de Docker que contiene el Conversor con todos sus componentes, el Modelo_de_Transcripción y las dependencias que estos usan (D2).
- **Contenedor**: Instancia en ejecución de la Imagen_de_Contenedor.
- **Equipo_Anfitrión**: Computadora con Docker que ejecuta el Contenedor.
- **Equipo_de_Ejecución**: El Contenedor en ejecución junto con el Equipo_Anfitrión que lo ejecuta.
- **Equipo_de_Referencia**: Equipo_Anfitrión de 4 núcleos, con procesador x86-64 (amd64) o ARM de 64 bits (arm64), que el diseño usa para verificar los objetivos de rendimiento de S10 y el tiempo de inicio del criterio 2 del Requisito 20.
- **Variable_de_Configuración**: Variable de entorno del Contenedor que fija, al iniciar el Contenedor, uno de los valores configurables de S7.
- **Indicador_de_Salud**: Verificación de estado del Contenedor (healthcheck de Docker) que informa uno de dos resultados: listo o no listo.
- **Inventario_de_Licencias**: Archivo del repositorio de código del Conversor que lista cada componente de terceros incluido en la Imagen_de_Contenedor con su nombre, versión y licencia.
- **Guía_de_Uso**: Documento del repositorio de código del Conversor que indica cómo ejecutar la Imagen_de_Contenedor: el comando del criterio 2 del Requisito 20, la ruta del Almacenamiento_Temporal dentro del Contenedor y, para cada valor configurable de S7, el nombre de su Variable_de_Configuración, su tipo, su unidad, su rango admitido y su valor predeterminado.
- **Almacenamiento_Temporal**: Ubicación de almacenamiento reservada al Conversor, en la ruta fija del Contenedor indicada en la Guía_de_Uso, donde el Gestor_de_Archivos_Temporales crea los Espacios_de_Trabajo. Corresponde al sistema de archivos del Contenedor o al volumen que se monta en esa ruta (S12).
- **Espacio_de_Trabajo**: Ubicación dentro del Almacenamiento_Temporal exclusiva de una Tarea. El Gestor_de_Archivos_Temporales la crea al iniciar la recepción del archivo, antes de que exista la Tarea, y la asigna a la Tarea cuando el Conversor la crea al completar la recepción.
- **Log_de_Eventos**: Registro técnico de eventos del Conversor destinado a diagnóstico.

**Valores configurables**

- **Límite_de_Tamaño**: Tamaño máximo de Archivo_de_Origen para cada Categoría (valor configurable; ver S7).
- **Límite_de_Tamaño_Global**: Tamaño máximo de cualquier archivo recibido (valor configurable; ver S7).
- **Límite_de_Píxeles**: Cantidad máxima de píxeles (ancho × alto) de un Archivo_de_Origen de imagen y de cada página rasterizada en PDF a PNG (valor configurable; ver S7).
- **Duración_Máxima_de_Transcripción**: Duración máxima del Audio_a_Transcribir (valor configurable; ver S7).
- **Tiempo_Máximo_de_Procesamiento**: Tiempo máximo en estado en_proceso para una Tarea según la Categoría de su Par_de_Conversión o, en un Par_de_Transcripción, el valor de transcripción (valor configurable; ver S7).
- **Período_de_Retención**: Tiempo durante el cual el Conversor conserva una Tarea en estado recibida, contado desde la creación de la Tarea, o el Archivo_Resultado de una Tarea completada, contado desde el cambio a completada; las descargas no reinician el conteo (valor configurable; ver S7).
- **Límite_de_Concurrencia**: Cantidad máxima de Tareas en_proceso simultáneas (valor configurable; ver S7).
- **Resolución_de_Rasterizado**: Resolución en puntos por pulgada para convertir páginas PDF en imágenes (valor configurable; ver S7).
- **Espacio_Libre_Mínimo**: Espacio libre del Almacenamiento_Temporal requerido para aceptar un nuevo archivo (valor configurable; ver S7).
- **Intervalo_de_Progreso**: Intervalo máximo entre actualizaciones de progreso de una Tarea (valor configurable; ver S7).
- **Puerto_de_la_Interfaz**: Puerto TCP del Contenedor en el que el Conversor atiende la Interfaz_de_Usuario (valor configurable; ver S7).

## Requirements

### Requisito 1: Recepción del archivo

**Historia de usuario:** Como usuario, quiero subir un archivo al Conversor, para poder convertirlo a otro formato.

#### Criterios de aceptación

1. WHEN el Conversor completa la recepción de un archivo enviado a través de la Interfaz_de_Usuario cuyo tamaño está entre 1 byte y el Límite_de_Tamaño_Global inclusive, THE Conversor SHALL crear una Tarea en estado recibida, asignarle el Espacio_de_Trabajo en que se recibió el archivo, conservar el archivo en ese Espacio_de_Trabajo como Archivo_de_Origen con contenido idéntico byte a byte al archivo enviado, e informar al usuario que el archivo fue recibido.
2. THE Conversor SHALL asociar exactamente un Archivo_de_Origen a cada Tarea.
3. IF la cantidad de bytes recibidos de un archivo supera el Límite_de_Tamaño_Global, THEN THE Conversor SHALL interrumpir la recepción en ese momento, eliminar los datos recibidos de ese archivo dentro de los 60 segundos posteriores sin crear una Tarea, e informar al usuario que el archivo excede el Límite_de_Tamaño_Global, indicando su valor en megabytes.
4. IF el archivo recibido tiene un tamaño de 0 bytes, THEN THE Conversor SHALL rechazar el archivo sin crear una Tarea, eliminar los datos recibidos de ese archivo dentro de los 60 segundos posteriores, e informar al usuario que el archivo está vacío.
5. IF, al iniciar la recepción de un archivo, el espacio libre del Almacenamiento_Temporal es menor al Espacio_Libre_Mínimo, THEN THE Conversor SHALL rechazar el archivo antes de almacenar cualquier dato y sin crear una Tarea, e informar al usuario que no hay espacio de almacenamiento suficiente.
6. THE Conversor SHALL almacenar cada Archivo_de_Origen dentro del Espacio_de_Trabajo de su Tarea, bajo un identificador generado por el Conversor y distinto para cada Tarea, aunque el Nombre_Original contenga separadores de ruta o la secuencia «..», o coincida con el Nombre_Original de otra Tarea.
7. THE Conversor SHALL conservar como dato de la Tarea el Nombre_Original del archivo, sin modificaciones e incluida su extensión.
8. IF la recepción de un archivo se interrumpe antes de completarse por pérdida de conexión con la Interfaz_de_Usuario, por cancelación del envío por parte del usuario o por un error de escritura en el Almacenamiento_Temporal, THEN THE Conversor SHALL eliminar los datos recibidos de ese archivo dentro de los 60 segundos posteriores a la interrupción, sin crear una Tarea.
9. IF la recepción de un archivo se interrumpe por un error de escritura en el Almacenamiento_Temporal, THEN THE Conversor SHALL informar al usuario que el archivo no pudo almacenarse.
10. IF el Nombre_Original del archivo tiene más de 255 caracteres, THEN THE Conversor SHALL rechazar el archivo sin crear una Tarea, eliminar los datos recibidos de ese archivo dentro de los 60 segundos posteriores, e informar al usuario que el nombre supera los 255 caracteres.

### Requisito 2: Detección del tipo real

**Historia de usuario:** Como usuario, quiero que el Conversor identifique el formato real de mi archivo, para que la conversión funcione aunque la extensión esté mal.

#### Criterios de aceptación

1. WHEN la recepción de un Archivo_de_Origen finaliza y la Tarea queda en estado recibida, THE Detector_de_Tipo SHALL determinar el Tipo_Real a partir de la Firma_de_Formato de un Formato registrado en el Registro_de_Conversiones que esté presente en el contenido del Archivo_de_Origen.
2. WHEN el Detector_de_Tipo analiza un Archivo_de_Origen cuyo contenido no presenta la Firma_de_Formato de ningún Formato registrado y es Texto_Válido, THE Detector_de_Tipo SHALL determinar como Tipo_Real el Formato_de_Texto registrado cuyas extensiones incluyen la Extensión_del_Nombre del Nombre_Original: TXT para ".txt", MD para ".md", HTML para ".html" o ".htm" y SRT para ".srt".
3. IF el contenido del Archivo_de_Origen no presenta la Firma_de_Formato de ningún Formato registrado, es Texto_Válido y el Nombre_Original no tiene Extensión_del_Nombre o tiene una Extensión_del_Nombre que no pertenece a ningún Formato_de_Texto registrado, THEN THE Detector_de_Tipo SHALL asignar TXT como Tipo_Real.
4. THE Detector_de_Tipo SHALL determinar para un Texto_UTF8_Válido que comienza con la BOM_UTF8 el mismo Tipo_Real que para el mismo contenido sin la BOM_UTF8.
5. IF el contenido del Archivo_de_Origen no presenta la Firma_de_Formato de ningún Formato registrado, no es Texto_Válido y el Archivo_de_Origen no es un DOCX_Cifrado, THEN THE Conversor SHALL rechazar el Archivo_de_Origen sin ofrecer Formatos_de_Destino e informar al usuario, en un mismo mensaje, que el formato no es reconocido y que los archivos de texto se aceptan solo con codificación UTF-8 o Windows-1252 (ISO-8859-1).
6. IF el Nombre_Original tiene una Extensión_del_Nombre que no figura entre las extensiones registradas del Tipo_Real (".jpg" y ".jpeg" para JPG; ".html" y ".htm" para HTML; para cada uno de los demás Formatos del Mapa_de_Conversión_Inicial, su nombre en minúsculas precedido de un punto, por ejemplo ".png" o ".docx"), THEN THE Conversor SHALL usar el Tipo_Real como Formato_de_Origen de la Tarea e informar al usuario la Extensión_del_Nombre y el Tipo_Real detectado antes de que el usuario elija el Formato_de_Destino.
7. IF el Tipo_Real no es Formato_de_Origen de ningún Par_de_Conversión del Mapa_de_Conversión (por ejemplo, SRT, que es solo Formato_de_Destino), THEN THE Conversor SHALL rechazar el Archivo_de_Origen e informar al usuario el Tipo_Real detectado y la lista completa de Formatos_de_Origen del Mapa_de_Conversión.
8. FOR ALL Archivos_de_Origen cuyo contenido presenta la Firma_de_Formato de un Formato registrado, THE Detector_de_Tipo SHALL determinar ese Formato como Tipo_Real tanto con cualquier Extensión_del_Nombre como sin Extensión_del_Nombre.
9. IF el tamaño del Archivo_de_Origen supera el Límite_de_Tamaño de la Categoría de su Tipo_Real, THEN THE Conversor SHALL rechazar el Archivo_de_Origen e informar al usuario la Categoría detectada y el valor del Límite_de_Tamaño de esa Categoría.
10. THE Detector_de_Tipo SHALL identificar como DOCX_Cifrado todo Archivo_de_Origen que cumple la definición de DOCX_Cifrado, para que el Conversor aplique el rechazo definido en el criterio 2 del Requisito 7.
11. WHEN la recepción de un Archivo_de_Origen finaliza y la Tarea queda en estado recibida, THE Conversor SHALL registrar el Tipo_Real como dato de la Tarea o rechazar el Archivo_de_Origen dentro de los 10 segundos siguientes.
12. WHEN el Detector_de_Tipo determina un Formato_de_Texto como Tipo_Real, THE Conversor SHALL procesar el Archivo_de_Origen a partir de su Texto_Decodificado, con independencia de la codificación que declare el propio documento (por ejemplo, la etiqueta meta charset de un HTML).

### Requisito 3: Consulta de destinos disponibles

**Historia de usuario:** Como usuario, quiero ver a qué formatos puedo convertir mi archivo y qué se pierde en cada caso, para elegir el destino con información.

#### Criterios de aceptación

1. WHEN el Detector_de_Tipo determina el Tipo_Real de un Archivo_de_Origen y el Conversor no realiza un Rechazo_de_Origen de ese Archivo_de_Origen, THE Interfaz_de_Usuario SHALL mostrar, en un plazo máximo de 1 segundo, el nombre de cada Formato_de_Destino de los Pares_de_Conversión registrados en el Registro_de_Conversiones con ese Tipo_Real como Formato_de_Origen, aunque la Extensión_del_Nombre del Nombre_Original corresponda a otro Formato.
2. WHEN la Interfaz_de_Usuario muestra los Formatos_de_Destino de un Archivo_de_Origen, THE Interfaz_de_Usuario SHALL mostrar, en la misma entrada de cada Formato_de_Destino, el valor de Clasificación_de_Pérdida (Sin_Pérdida, Con_Pérdida, Limitada o Aproximada) registrado para el Par_de_Conversión correspondiente.
3. WHEN el usuario selecciona un Formato_de_Destino cuyo Par_de_Conversión con el Tipo_Real tiene Clasificación_de_Pérdida Con_Pérdida, Limitada o Aproximada, THE Conversor SHALL mostrar en la Interfaz_de_Usuario la Descripción_de_Pérdida completa de ese Par_de_Conversión, solicitar la confirmación del usuario y mantener la Tarea en estado recibida hasta recibir esa confirmación.
4. IF el usuario solicita un Formato_de_Destino que no forma un Par_de_Conversión registrado con el Tipo_Real del Archivo_de_Origen (incluidos un Formato_de_Destino igual al Tipo_Real y un nombre que no corresponde a ningún Formato del Registro_de_Conversiones), THEN THE Conversor SHALL rechazar la solicitud sin generar un Archivo_Resultado, mantener la Tarea en estado recibida con su Archivo_de_Origen e informar al usuario un mensaje de error que indique que el destino no está disponible, junto con los Formatos_de_Destino definidos en el criterio 1.
5. THE Interfaz_de_Usuario SHALL mostrar, para todo Formato_de_Origen del Mapa_de_Conversión, un conjunto de Formatos_de_Destino igual al conjunto de Formatos_de_Destino de los Pares_de_Conversión registrados con ese Formato_de_Origen, sin Formatos_de_Destino omitidos, adicionales ni repetidos, incluidos los Pares_de_Conversión agregados al Registro_de_Conversiones además del Mapa_de_Conversión_Inicial.
6. IF el Tipo_Real de un Archivo_de_Origen no es Formato_de_Origen de ningún Par_de_Conversión registrado, THEN THE Interfaz_de_Usuario SHALL informar al usuario, en un plazo máximo de 1 segundo desde que el Detector_de_Tipo determina el Tipo_Real, que no hay Formatos_de_Destino disponibles para ese Tipo_Real, junto con el rechazo definido en el criterio 7 del Requisito 2.
7. IF el usuario rechaza la confirmación solicitada según el criterio 3, THEN THE Conversor SHALL mantener la Tarea en estado recibida sin generar un Archivo_Resultado y volver a mostrar en la Interfaz_de_Usuario los Formatos_de_Destino definidos en el criterio 1.

### Requisito 4: Registro extensible de conversiones

**Historia de usuario:** Como desarrollador, quiero agregar nuevos formatos y pares de conversión mediante un registro, para ampliar el mapa a medida que surjan necesidades sin rediseñar el núcleo.

#### Criterios de aceptación

1. THE Registro_de_Conversiones SHALL almacenar cada Par_de_Conversión con su Formato_de_Origen, Formato_de_Destino, Categoría, Clasificación_de_Pérdida y Descripción_de_Pérdida.
2. THE Registro_de_Conversiones SHALL asociar a cada Par_de_Conversión del Mapa_de_Conversión exactamente una Clasificación_de_Pérdida, con uno de estos valores: Sin_Pérdida, Con_Pérdida, Limitada o Aproximada.
3. WHEN el Registro_de_Conversiones acepta el registro de un Par_de_Conversión entre dos Formatos registrados, THE Conversor SHALL ofrecer en la Interfaz_de_Usuario el Formato_de_Destino de ese Par_de_Conversión para todo Archivo_de_Origen cuyo Tipo_Real es su Formato_de_Origen, a más tardar desde el siguiente inicio del Conversor y sin modificar el Detector_de_Tipo, el Gestor_de_Tareas, el Gestor_de_Archivos_Temporales ni la Interfaz_de_Usuario.
4. WHEN el Registro_de_Conversiones acepta el registro de un Formato nuevo con nombre, Categoría, una o más extensiones, su Extensión_Principal y, si existe, su Firma_de_Formato, THE Detector_de_Tipo SHALL determinar ese Formato como Tipo_Real de todo archivo cuyo contenido presenta su Firma_de_Formato o, si el Formato no tiene Firma_de_Formato, de todo archivo cuyo contenido es Texto_Válido sin la Firma_de_Formato de ningún Formato registrado y cuya Extensión_del_Nombre es una de las extensiones del Formato, a más tardar desde el siguiente inicio del Conversor y sin modificar el Detector_de_Tipo.
5. IF se registra un Par_de_Conversión con el mismo Formato_de_Origen y el mismo Formato_de_Destino que un Par_de_Conversión ya registrado, THEN THE Registro_de_Conversiones SHALL rechazar el registro, devolver un error que identifica el Par_de_Conversión existente, registrar el rechazo en el Log_de_Eventos y conservar sin cambios el Mapa_de_Conversión, incluidos los atributos del Par_de_Conversión existente.
6. IF se registra un Par_de_Conversión cuyo Formato_de_Origen o Formato_de_Destino no es un Formato registrado, THEN THE Registro_de_Conversiones SHALL rechazar el registro, devolver un error que nombra cada Formato faltante, registrar el rechazo en el Log_de_Eventos y conservar sin cambios el Mapa_de_Conversión.
7. IF se registra un Par_de_Conversión con Clasificación_de_Pérdida Con_Pérdida, Limitada o Aproximada y una Descripción_de_Pérdida vacía o formada solo por espacios en blanco, o un Par_de_Conversión de cualquier Clasificación_de_Pérdida con una Descripción_de_Pérdida de más de 500 caracteres, THEN THE Registro_de_Conversiones SHALL rechazar el registro, devolver un error que indica si la Descripción_de_Pérdida falta o excede los 500 caracteres, registrar el rechazo en el Log_de_Eventos y conservar sin cambios el Mapa_de_Conversión.
8. WHEN se consulta el Mapa_de_Conversión, THE Registro_de_Conversiones SHALL devolver cada Par_de_Conversión registrado exactamente una vez, con su Formato_de_Origen, Formato_de_Destino, Categoría, Clasificación_de_Pérdida y Descripción_de_Pérdida.
9. WHEN el Conversor inicia, THE Registro_de_Conversiones SHALL contener los 36 Pares_de_Conversión del Mapa_de_Conversión_Inicial (9 de la tabla Imágenes, 9 de Documentos, 2 de PDF como origen, 6 de Audio, 4 de Video y 6 de Transcripción), cada uno con la Clasificación_de_Pérdida indicada en su tabla.
10. FOR ALL Pares_de_Conversión cuyo registro fue aceptado, THE Registro_de_Conversiones SHALL devolver en cada consulta del Mapa_de_Conversión ese Par_de_Conversión con atributos idénticos a los que se usaron al registrarlo.
11. FOR ALL Pares_de_Conversión con Clasificación_de_Pérdida Sin_Pérdida y todo Archivo_de_Origen cuyo contenido es representable en el Formato_de_Destino, THE Motor_de_Conversión SHALL producir un Archivo_Resultado cuyo Contenido_Decodificado coincide con el Contenido_Decodificado del Archivo_de_Origen.
12. IF se registra un Par_de_Conversión sin Clasificación_de_Pérdida, con más de una Clasificación_de_Pérdida o con un valor que no es Sin_Pérdida, Con_Pérdida, Limitada ni Aproximada, THEN THE Registro_de_Conversiones SHALL rechazar el registro, devolver un error que indica que la Clasificación_de_Pérdida es inválida, registrar el rechazo en el Log_de_Eventos y conservar sin cambios el Mapa_de_Conversión.
13. IF se registra un Formato con nombre vacío, sin extensiones, sin Extensión_Principal, con una Extensión_Principal que no figura entre sus extensiones, con una Categoría que no es imagen, documento, audio ni video, con un nombre o una extensión que ya pertenece a otro Formato registrado (comparados sin distinguir mayúsculas de minúsculas), o con una Firma_de_Formato idéntica a la de otro Formato registrado, THEN THE Registro_de_Conversiones SHALL rechazar el registro, devolver un error que identifica el atributo inválido o el Formato en conflicto, registrar el rechazo en el Log_de_Eventos y conservar sin cambios los Formatos y Pares_de_Conversión ya registrados.

### Requisito 5: Conversión de imágenes

**Historia de usuario:** Como usuario, quiero convertir imágenes entre PNG, JPG y WEBP y llevarlas a PDF, para usarlas donde se requiera cada formato.

#### Criterios de aceptación

1. WHEN el Motor_de_Conversión ejecuta un Par_de_Conversión entre PNG, JPG y WEBP, THE Motor_de_Conversión SHALL producir un Archivo_Resultado con las mismas Dimensiones_Mostradas que el Archivo_de_Origen.
2. WHEN el Motor_de_Conversión ejecuta PNG a JPG o WEBP a JPG sobre un Archivo_de_Origen con al menos un píxel de opacidad menor a la total, THE Motor_de_Conversión SHALL componer cada píxel sobre un fondo blanco en proporción a su opacidad, de modo que un píxel totalmente transparente resulte blanco, un píxel totalmente opaco conserve su color y un píxel parcialmente transparente resulte en la mezcla de su color con el blanco según su opacidad.
3. WHEN el Motor_de_Conversión ejecuta PNG a JPG o PNG a WEBP sobre un Archivo_de_Origen de 16 bits por canal, THE Motor_de_Conversión SHALL producir el Archivo_Resultado con una profundidad de color de 8 bits por canal.
4. WHEN el Motor_de_Conversión ejecuta PNG a PDF, JPG a PDF o WEBP a PDF, THE Motor_de_Conversión SHALL producir un PDF de exactamente una página que contiene la imagen del Archivo_de_Origen con un ancho y un alto en píxeles iguales a sus Dimensiones_Mostradas, con todos sus píxeles dentro de los límites de la página y con una relación entre el ancho y el alto con que la página presenta la imagen que difiere de la relación de las Dimensiones_Mostradas del Archivo_de_Origen en 0,5 % o menos.
5. IF el usuario confirma WEBP o JPG como Formato_de_Destino y el ancho o el alto de las Dimensiones_Mostradas del Archivo_de_Origen supera el máximo de ese Formato_de_Destino (16383 píxeles para WEBP; 65535 píxeles para JPG), THEN THE Conversor SHALL rechazar la conversión, mantener la Tarea en estado recibida e informar al usuario las Dimensiones_Mostradas del Archivo_de_Origen y las dimensiones máximas del Formato_de_Destino.
6. IF el Detector_de_Tipo determina que el Archivo_de_Origen es una Imagen_Animada, THEN THE Conversor SHALL rechazar el Archivo_de_Origen antes de mostrar los Formatos_de_Destino e informar al usuario que las imágenes animadas están fuera del alcance.
7. FOR ALL imágenes PNG válidas de hasta 8 bits por canal, THE Motor_de_Conversión SHALL producir, al convertir PNG a WEBP y el resultado nuevamente a PNG, una imagen con las mismas Dimensiones_Mostradas que el PNG original y con los mismos valores de rojo, verde, azul y opacidad en cada píxel que el PNG original después de aplicar su Orientación_EXIF, comparados en 8 bits por canal e incluidos los píxeles totalmente transparentes.
8. WHEN una Tarea de PNG a JPG o PNG a WEBP con un Archivo_de_Origen de 16 bits por canal cambia a estado completada, THE Interfaz_de_Usuario SHALL informar al usuario, junto con la oferta de descarga del Archivo_Resultado, que la profundidad de color se redujo de 16 a 8 bits por canal.
9. IF el Tipo_Real del Archivo_de_Origen es PNG, JPG o WEBP y el producto del ancho por el alto de sus Dimensiones_Mostradas supera el Límite_de_Píxeles, THEN THE Conversor SHALL rechazar el Archivo_de_Origen antes de mostrar los Formatos_de_Destino e informar al usuario las Dimensiones_Mostradas del Archivo_de_Origen y el valor del Límite_de_Píxeles.
10. WHEN el Motor_de_Conversión ejecuta un Par_de_Conversión cuyo Formato_de_Origen es PNG, JPG o WEBP sobre un Archivo_de_Origen con una Orientación_EXIF distinta de la normal, THE Motor_de_Conversión SHALL producir un Archivo_Resultado cuya imagen tiene los píxeles rotados o reflejados según esa Orientación_EXIF y que no contiene la etiqueta de Orientación_EXIF.

### Requisito 6: Conversión de documentos de texto (TXT, Markdown, HTML)

**Historia de usuario:** Como usuario, quiero convertir documentos de texto simples entre sí y a PDF, para compartirlos o publicarlos en el formato que necesito.

#### Criterios de aceptación

1. WHEN el Motor_de_Conversión ejecuta TXT a PDF, THE Motor_de_Conversión SHALL producir un PDF que contiene el Texto_Visible completo del Archivo_de_Origen, en el que cada salto de línea del Archivo_de_Origen inicia una línea nueva, cada línea más ancha que el área de texto de la página continúa en la línea siguiente sin truncarse y el texto que no cabe en una página continúa en las páginas siguientes.
2. WHEN el Motor_de_Conversión ejecuta TXT a HTML, THE Motor_de_Conversión SHALL producir un documento HTML codificado en UTF-8, que declara la codificación UTF-8, cuyo Texto_Visible tiene la misma Secuencia_de_Palabras que el Archivo_de_Origen, que muestra cada línea del Archivo_de_Origen en una línea distinta y que escribe cada carácter reservado de HTML (`<`, `>`, `&`, `"`) del Archivo_de_Origen como referencia de carácter HTML (por ejemplo, `&` como `&amp;`).
3. WHEN el Motor_de_Conversión ejecuta MD a HTML, THE Motor_de_Conversión SHALL producir un documento HTML codificado en UTF-8, que declara la codificación UTF-8, cuyo cuerpo coincide, sin considerar espacios en blanco entre etiquetas, con el HTML que la especificación CommonMark define como resultado para el Archivo_de_Origen.
4. WHEN el Motor_de_Conversión ejecuta MD a TXT o HTML a TXT, THE Motor_de_Conversión SHALL producir un archivo TXT codificado en UTF-8 sin BOM_UTF8, con la misma Secuencia_de_Palabras que el Texto_Visible del Archivo_de_Origen sin considerar los marcadores de elementos de lista (viñetas y números), y con cada título, párrafo, elemento de lista, bloque de código y salto de línea forzado del Archivo_de_Origen iniciando una línea nueva.
5. WHEN el Motor_de_Conversión ejecuta HTML a MD, THE Motor_de_Conversión SHALL producir un archivo Markdown codificado en UTF-8 cuyo HTML, según la especificación CommonMark, contiene en el mismo orden todos los elementos de la Estructura_de_Documento del Archivo_de_Origen, con la misma Secuencia_de_Palabras en cada elemento y el mismo texto, carácter por carácter, en cada bloque de código.
6. IF un Archivo_de_Origen HTML referencia Recursos_Externos, o un Archivo_de_Origen MD referencia Recursos_Externos y el Formato_de_Destino es PDF, THEN THE Motor_de_Conversión SHALL completar la conversión sin leer ni descargar los Recursos_Externos e informar al usuario la cantidad de Recursos_Externos omitidos.
7. IF un Archivo_de_Origen HTML o MD contiene Scripts, THEN THE Motor_de_Conversión SHALL completar la conversión sin ejecutar los Scripts.
8. FOR ALL archivos TXT cuyo Texto_Decodificado es Texto_Latino, THE Motor_de_Conversión SHALL producir, al convertir TXT a PDF y el resultado PDF a TXT, la misma Secuencia_de_Palabras que el TXT original.
9. FOR ALL archivos TXT cuyo contenido es Texto_UTF8_Válido sin caracteres de control distintos de tabulación, retorno de carro y salto de línea, THE Motor_de_Conversión SHALL producir, al convertir TXT a HTML y el resultado HTML a TXT, la misma Secuencia_de_Palabras que el Texto_Visible del TXT original.
10. FOR ALL documentos Markdown sin HTML embebido compuestos solo por títulos, párrafos, listas, énfasis simples y fuertes, enlaces y bloques de código, THE Motor_de_Conversión SHALL producir, al convertir MD a HTML, el resultado HTML a MD y ese MD nuevamente a HTML, un HTML cuya Estructura_de_Documento es igual a la del HTML de la primera conversión.
11. IF el Motor_de_Conversión ejecuta TXT a PDF o MD a PDF y el Texto_Visible del Archivo_de_Origen contiene caracteres que la Fuente_Predeterminada no puede representar, THEN THE Conversor SHALL cambiar el Estado_de_Tarea de en_proceso a fallida sin entregar un Archivo_Resultado e informar al usuario los caracteres no representables, hasta un máximo de 10 caracteres distintos.
12. FOR ALL archivos MD sin bloques de código, y archivos HTML sin tablas, bloques de código, hojas de estilo ni atributos de estilo, cuyo Texto_Visible es Texto_Latino, THE Motor_de_Conversión SHALL producir, al convertir el archivo a PDF y el resultado PDF a TXT, la misma Secuencia_de_Palabras que el Texto_Visible del Archivo_de_Origen, sin considerar los marcadores de elementos de lista (viñetas y números).
13. IF un Archivo_de_Origen HTML o MD contiene Scripts y el Formato_de_Destino es PDF, TXT o MD, THEN THE Motor_de_Conversión SHALL excluir el código de los Scripts del Archivo_Resultado.
14. WHEN el Motor_de_Conversión ejecuta un Par_de_Conversión cuyo Formato_de_Origen y cuyo Formato_de_Destino son TXT, MD o HTML, THE Motor_de_Conversión SHALL codificar el Archivo_Resultado en UTF-8, también cuando el Archivo_de_Origen es Texto_Windows_1252.
15. FOR ALL archivos TXT cuyo contenido es Texto_Windows_1252 y cuyo Texto_Decodificado contiene al menos uno de los caracteres á, é, í, ó, ú, ü, ñ, Á, É, Í, Ó, Ú, Ü, Ñ, ¿ o ¡ y no contiene caracteres de control (U+0000 a U+001F y U+007F a U+009F) distintos de tabulación, retorno de carro y salto de línea, THE Motor_de_Conversión SHALL producir, al convertir TXT a HTML y el resultado HTML a TXT, un TXT en UTF-8 con la misma Secuencia_de_Palabras que el Texto_Decodificado del TXT original.

### Requisito 7: Conversión de DOCX a PDF

**Historia de usuario:** Como usuario, quiero convertir documentos DOCX a PDF, para compartirlos en un formato que se ve igual en cualquier equipo.

#### Criterios de aceptación

1. WHEN el Motor_de_Conversión ejecuta DOCX a PDF, THE Motor_de_Conversión SHALL producir un PDF con todas las fuentes usadas incrustadas, que contiene todas las palabras de los párrafos y de las celdas de tabla del Cuerpo_del_Documento como texto extraíble, cada tabla del Cuerpo_del_Documento con la misma cantidad de filas y columnas, y una imagen por cada imagen insertada en el Cuerpo_del_Documento con una relación de aspecto que difiere en 1 % o menos de la que muestra el DOCX.
2. IF el Archivo_de_Origen es un DOCX_Cifrado, THEN THE Conversor SHALL rechazar el Archivo_de_Origen antes de mostrar los Formatos_de_Destino, finalizar la Tarea como fallida sin producir un Archivo_Resultado e informar al usuario que el documento está protegido con contraseña de apertura.
3. FOR ALL documentos DOCX cuyo Cuerpo_del_Documento contiene solo párrafos cuyo texto es Texto_Latino, sin tablas, imágenes, campos, notas al pie, texto oculto ni cambios pendientes, sin encabezados ni pies de página y con el guionado automático desactivado, THE Motor_de_Conversión SHALL producir un PDF cuyo texto extraído tiene la misma Secuencia_de_Palabras que el Cuerpo_del_Documento.
4. IF el DOCX usa una fuente que no está instalada en el Equipo_de_Ejecución ni incrustada en el DOCX, THEN THE Motor_de_Conversión SHALL reemplazarla por una fuente instalada en el Equipo_de_Ejecución, completar la conversión e informar al usuario los nombres de las fuentes reemplazadas.
5. IF el DOCX contiene macros o Recursos_Externos (por ejemplo, imágenes vinculadas, plantillas u objetos por URL o ruta de red), THEN THE Motor_de_Conversión SHALL omitir las macros sin ejecutarlas y los Recursos_Externos sin acceder a ellos, completar la conversión e informar la omisión al usuario.
6. WHEN el Motor_de_Conversión ejecuta DOCX a PDF y el DOCX contiene comentarios o cambios pendientes del control de cambios, THE Motor_de_Conversión SHALL producir el PDF sin comentarios ni marcas de revisión, con el texto insertado incluido y el texto eliminado excluido, e informar al usuario que se omitieron los comentarios y se aplicaron los cambios pendientes.
7. IF el Tipo_Real del Archivo_de_Origen es DOCX y la suma de los Tamaños_Descomprimidos de sus Entradas_ZIP supera 512 MB, la cantidad de Entradas_ZIP supera 10 000 o alguna Entrada_ZIP tiene una Razón_de_Compresión mayor a 200 (Límites_de_Descompresión), THEN THE Conversor SHALL rechazar el Archivo_de_Origen antes de mostrar los Formatos_de_Destino, finalizar la Tarea como fallida sin producir un Archivo_Resultado, con el Motivo_de_Fallo "documento demasiado complejo o potencialmente malicioso".

### Requisito 8: Conversión desde PDF (limitada)

**Historia de usuario:** Como usuario, quiero extraer el texto de un PDF u obtener sus páginas como imágenes, sabiendo que estas conversiones son limitadas.

#### Criterios de aceptación

1. WHEN el Motor_de_Conversión ejecuta PDF a TXT, THE Motor_de_Conversión SHALL producir un Archivo_Resultado TXT en UTF-8 con el texto de la Capa_de_Texto de todas las páginas en orden de Número_de_Página, con al menos un salto de línea entre el texto de una página y el de la página siguiente.
2. IF durante la ejecución de PDF a TXT todas las páginas del Archivo_de_Origen son Páginas_sin_Texto, THEN THE Conversor SHALL cambiar el Estado_de_Tarea de en_proceso a fallida sin entregar Archivo_Resultado e informar al usuario que el PDF parece escaneado y que el reconocimiento de texto en imágenes (OCR) está fuera del alcance.
3. IF durante la ejecución de PDF a TXT al menos una página del Archivo_de_Origen es Página_sin_Texto y al menos una página contiene Capa_de_Texto, THEN THE Conversor SHALL finalizar la Tarea como completada, entregar el Archivo_Resultado TXT con el texto de las páginas que contienen Capa_de_Texto e informar al usuario el Número_de_Página de cada Página_sin_Texto.
4. WHEN el Motor_de_Conversión ejecuta PDF a PNG, THE Motor_de_Conversión SHALL generar una imagen PNG por cada página, incluidas las Páginas_sin_Texto, con un ancho y un alto en píxeles iguales al ancho y al alto de la página en pulgadas (72 puntos PDF por pulgada), en la orientación en que se muestra la página, multiplicados por la Resolución_de_Rasterizado, con una tolerancia de ±1 píxel.
5. WHEN el Motor_de_Conversión completa PDF a PNG de un Archivo_de_Origen con más de una página, THE Conversor SHALL entregar como Archivo_Resultado un único archivo ZIP que contiene solo las imágenes PNG de las páginas, sin carpetas, con el nombre definido en el criterio 10 del Requisito 15.
6. FOR ALL PDFs que no son PDF_Protegido y que el Motor_de_Conversión puede decodificar, THE Motor_de_Conversión SHALL producir en PDF a PNG una cantidad de imágenes igual a la cantidad de páginas del PDF, donde la imagen con Número_de_Página N es el rasterizado de la página N.
7. IF el Archivo_de_Origen es un PDF_Protegido, THEN THE Conversor SHALL rechazar el Archivo_de_Origen antes de mostrar los Formatos_de_Destino y sin solicitar contraseña, finalizar la Tarea como fallida sin producir Archivo_Resultado e informar al usuario que el documento está protegido y que los PDF protegidos no se admiten.
8. WHEN el Conversor genera el archivo ZIP de PDF a PNG, THE Conversor SHALL nombrar cada imagen con el Nombre_Base después de aplicar la Normalización_de_Nombre, un guion bajo, el Número_de_Página completado con ceros a la izquierda hasta la cantidad de dígitos del total de páginas y la extensión ".png" (por ejemplo, de "informe_01.png" a "informe_12.png" en un PDF de 12 páginas).
9. WHEN el Motor_de_Conversión completa PDF a PNG de un Archivo_de_Origen con una sola página, THE Conversor SHALL entregar como Archivo_Resultado esa imagen PNG, sin archivo ZIP, con el nombre definido en el criterio 2 del Requisito 15.
10. WHEN el Archivo_de_Origen es un PDF_con_Permisos_Restringidos, THE Conversor SHALL procesarlo sin solicitar contraseña, con los mismos Formatos_de_Destino y con un Archivo_Resultado del mismo Contenido_Decodificado que el mismo PDF sin cifrado.
11. IF durante la ejecución de PDF a PNG el producto del ancho por el alto en píxeles que tendría una página rasterizada a la Resolución_de_Rasterizado, calculados según el criterio 4, supera el Límite_de_Píxeles, THEN THE Conversor SHALL cambiar el Estado_de_Tarea de en_proceso a fallida sin rasterizar esa página ni entregar Archivo_Resultado, con un Motivo_de_Fallo que indica que la página es demasiado grande y el Número_de_Página de la primera página, en orden de Número_de_Página, que supera el Límite_de_Píxeles.

### Requisito 9: Conversión de audio

**Historia de usuario:** Como usuario, quiero convertir audio entre MP3, WAV y OGG, para reproducirlo o editarlo con la herramienta que uso.

#### Criterios de aceptación

1. WHEN el Motor_de_Conversión ejecuta un Par_de_Conversión_de_Audio, THE Motor_de_Conversión SHALL producir un Archivo_Resultado cuya Duración_de_Audio difiere de la Duración_de_Audio del Archivo_de_Origen en 100 ms o menos.
2. WHEN el Motor_de_Conversión ejecuta un Par_de_Conversión_de_Audio y el número de canales del Archivo_de_Origen es menor o igual al máximo del Formato_de_Destino (2 canales para MP3, 255 canales para OGG y 65535 canales para WAV), THE Motor_de_Conversión SHALL producir un Archivo_Resultado con el mismo número de canales que el Archivo_de_Origen.
3. IF el Formato_de_Destino de un Par_de_Conversión_de_Audio es MP3 y el Archivo_de_Origen tiene más de 2 canales, THEN THE Motor_de_Conversión SHALL producir un Archivo_Resultado de 2 canales en el que el audio de cada canal del Archivo_de_Origen, salvo el Canal_LFE, está presente en al menos uno de los 2 canales, e informar al usuario el número de canales del Archivo_de_Origen y del Archivo_Resultado a más tardar cuando la Interfaz_de_Usuario ofrece la descarga del Archivo_Resultado.
4. WHEN el Motor_de_Conversión ejecuta un Par_de_Conversión_de_Audio y la frecuencia de muestreo del Archivo_de_Origen no está entre las Frecuencias_de_Muestreo_Admitidas del Formato_de_Destino, THE Motor_de_Conversión SHALL remuestrear el audio a la frecuencia más cercana dentro de las Frecuencias_de_Muestreo_Admitidas (la mayor, si dos frecuencias están a igual distancia) e informar al usuario, en Hz, la frecuencia de muestreo del Archivo_de_Origen y la del Archivo_Resultado a más tardar cuando la Interfaz_de_Usuario ofrece la descarga del Archivo_Resultado.
5. WHEN el Motor_de_Conversión ejecuta un Par_de_Conversión_de_Audio y la frecuencia de muestreo del Archivo_de_Origen está entre las Frecuencias_de_Muestreo_Admitidas del Formato_de_Destino, THE Motor_de_Conversión SHALL producir un Archivo_Resultado con la misma frecuencia de muestreo que el Archivo_de_Origen.
6. IF el Archivo_de_Origen de un Par_de_Conversión_de_Audio está codificado con un Códec que el Motor_de_Conversión no puede decodificar, THEN THE Conversor SHALL cambiar el Estado_de_Tarea de en_proceso a fallida e informar al usuario el nombre del Códec no soportado.

### Requisito 10: Conversión de video y extracción de audio

**Historia de usuario:** Como usuario, quiero extraer el audio de un video y convertir video entre MP4 y WEBM, para usar el contenido en el formato que necesito.

#### Criterios de aceptación

1. WHEN el Conversor ejecuta MP4 a MP3 o MP4 a WAV, THE Extractor_de_Audio SHALL producir un Archivo_Resultado en el Formato_de_Destino que contiene solo el audio de la Pista_de_Audio_Predeterminada del Archivo_de_Origen.
2. FOR ALL archivos MP4 con al menos una Pista_de_Audio, THE Extractor_de_Audio SHALL producir, al ejecutar MP4 a MP3 o MP4 a WAV, un Archivo_Resultado cuya duración difiere de la duración de la Pista_de_Audio_Predeterminada en 100 ms o menos.
3. IF el usuario confirma un Formato_de_Destino de Categoría audio o un Par_de_Transcripción para un Archivo_de_Origen cuyo Tipo_Real es de Categoría video y que no contiene Pistas_de_Audio, THEN THE Conversor SHALL rechazar la solicitud sin cambiar el Estado_de_Tarea a en_cola, mantener la Tarea en estado recibida e informar al usuario que el video no tiene audio.
4. WHEN el Conversor ejecuta un Par_de_Conversión sobre un Archivo_de_Origen cuyo Tipo_Real es de Categoría video y que contiene más de una Pista_de_Audio, THE Conversor SHALL usar solo la Pista_de_Audio_Predeterminada e informar al usuario la cantidad de Pistas_de_Audio no usadas.
5. WHEN el Motor_de_Conversión ejecuta MP4 a WEBM, o ejecuta WEBM a MP4 sobre un Archivo_de_Origen cuya Pista_de_Video tiene ancho y alto pares, THE Motor_de_Conversión SHALL producir un Archivo_Resultado cuya Pista_de_Video tiene el mismo ancho y alto en píxeles que la Pista_de_Video del Archivo_de_Origen.
6. WHEN el Motor_de_Conversión ejecuta MP4 a WEBM o WEBM a MP4, THE Motor_de_Conversión SHALL producir un Archivo_Resultado cuya duración difiere de la duración del Archivo_de_Origen en 500 ms o menos.
7. WHEN el Motor_de_Conversión ejecuta MP4 a WEBM o WEBM a MP4 y el Archivo_de_Origen contiene al menos una Pista_de_Audio, THE Motor_de_Conversión SHALL incluir la Pista_de_Audio_Predeterminada del Archivo_de_Origen como única Pista_de_Audio del Archivo_Resultado.
8. IF una pista que el Par_de_Conversión elegido usa (la Pista_de_Audio_Predeterminada en los Pares_de_Conversión con Formato_de_Destino de Categoría audio y en los Pares_de_Transcripción; la Pista_de_Video y la Pista_de_Audio_Predeterminada en MP4 a WEBM y WEBM a MP4) está codificada con un Códec que el Conversor identifica pero no puede decodificar, THEN THE Conversor SHALL cambiar el Estado_de_Tarea de en_proceso a fallida e informar al usuario el nombre del Códec y si corresponde a la Pista_de_Video o a la Pista_de_Audio_Predeterminada.
9. WHEN el Conversor ejecuta MP4 a MP3 o MP4 a WAV, THE Extractor_de_Audio SHALL aplicar a la Pista_de_Audio_Predeterminada las reglas de los criterios 2, 3, 4 y 5 del Requisito 9 (conservación del número de canales, mezcla en 2 canales para MP3 y frecuencia de muestreo dentro de las Frecuencias_de_Muestreo_Admitidas del Formato_de_Destino, con aviso al usuario), tratando la Pista_de_Audio_Predeterminada como Archivo_de_Origen de audio.
10. WHEN el Motor_de_Conversión ejecuta MP4 a WEBM o WEBM a MP4 y el Archivo_de_Origen no contiene Pistas_de_Audio, THE Motor_de_Conversión SHALL producir un Archivo_Resultado sin Pistas_de_Audio.
11. IF el Motor_de_Conversión ejecuta WEBM a MP4 y la Pista_de_Video del Archivo_de_Origen tiene ancho o alto impar, THEN THE Motor_de_Conversión SHALL producir una Pista_de_Video con 1 píxel más en cada dimensión impar, agregando una columna de píxeles negros a la derecha si el ancho es impar y una fila de píxeles negros abajo si el alto es impar, con el contenido completo de cada cuadro del Archivo_de_Origen en su tamaño original y alineado a la esquina superior izquierda, e informar al usuario el ancho y el alto de la Pista_de_Video del Archivo_de_Origen y del Archivo_Resultado junto con la oferta de descarga del Archivo_Resultado.

### Requisito 11: Transcripción de audio y video a texto

**Historia de usuario:** Como usuario, quiero obtener el texto hablado en un archivo de audio o video, procesado en mi propio equipo, para no enviar el contenido a servicios externos.

#### Criterios de aceptación

1. WHEN el Conversor ejecuta MP3 a TXT, WAV a TXT o MP4 a TXT, THE Transcriptor SHALL producir un Archivo_Resultado TXT en UTF-8, sin marcas de tiempo, con el texto que el Modelo_de_Transcripción reconoce en el Audio_a_Transcribir según el Idioma_de_Transcripción de la Tarea, en el orden en que se pronuncia en el audio.
2. WHEN el Conversor ejecuta MP4 a TXT, THE Extractor_de_Audio SHALL extraer la Pista_de_Audio_Predeterminada del Archivo_de_Origen como Archivo_Intermedio en el Espacio_de_Trabajo de la Tarea antes de que el Transcriptor inicie la transcripción.
3. WHEN el usuario selecciona un Par_de_Transcripción para una Tarea en estado recibida, THE Interfaz_de_Usuario SHALL mostrar como opciones de Idioma_de_Transcripción todos los Idiomas_Soportados, y solo esos, más la Detección_Automática_de_Idioma, con el español preseleccionado, y permitir elegir exactamente una opción antes de confirmar el Formato_de_Destino.
4. THE Conversor SHALL incluir en la Imagen_de_Contenedor exactamente un Modelo_de_Transcripción, cuyos Idiomas_Soportados incluyen el español y al menos otro idioma.
5. IF el usuario confirma un Par_de_Transcripción y la duración del Audio_a_Transcribir supera la Duración_Máxima_de_Transcripción, THEN THE Conversor SHALL rechazar la solicitud sin ejecutar el Transcriptor, mantener la Tarea en estado recibida e informar al usuario la duración del Audio_a_Transcribir y el valor de la Duración_Máxima_de_Transcripción.
6. IF el Transcriptor no puede cargar el Modelo_de_Transcripción cuando la Tarea pasa a en_proceso, THEN THE Conversor SHALL cambiar el Estado_de_Tarea de en_proceso a fallida sin transcribir el Audio_a_Transcribir e informar al usuario que el Modelo_de_Transcripción no está disponible.
7. IF el Formato_de_Destino es TXT y el Transcriptor reconoce cero palabras en el Audio_a_Transcribir, THEN THE Conversor SHALL finalizar la Tarea como completada con un Archivo_Resultado TXT de 0 bytes e informar al usuario que no se detectó voz.
8. FOR ALL Audios_a_Transcribir, THE Transcriptor SHALL producir un texto idéntico carácter a carácter al transcribir el mismo Audio_a_Transcribir dos veces en el mismo Equipo_de_Ejecución con el mismo Modelo_de_Transcripción, el mismo Idioma_de_Transcripción y la misma configuración.
9. IF el usuario confirma un Par_de_Transcripción con un idioma que no es un Idioma_Soportado, THEN THE Conversor SHALL rechazar la solicitud, mantener la Tarea en estado recibida e informar al usuario los Idiomas_Soportados y la opción de Detección_Automática_de_Idioma.
10. WHEN el usuario confirma un Par_de_Transcripción sin elegir una opción de Idioma_de_Transcripción, THE Gestor_de_Tareas SHALL asociar a la Tarea el español como Idioma_de_Transcripción.
11. WHEN el Transcriptor procesa una Tarea cuyo Idioma_de_Transcripción es la Detección_Automática_de_Idioma, THE Transcriptor SHALL determinar el Idioma_Detectado a partir del Audio_a_Transcribir y producir la transcripción en el Idioma_Detectado.
12. WHEN una Tarea cuyo Idioma_de_Transcripción es la Detección_Automática_de_Idioma cambia a estado completada y el Transcriptor reconoció al menos una palabra, THE Interfaz_de_Usuario SHALL mostrar el nombre del Idioma_Detectado junto con la oferta de descarga del Archivo_Resultado.

### Requisito 12: Subtítulos SRT

**Historia de usuario:** Como usuario, quiero obtener la transcripción con marcas de tiempo en formato SRT, para usarla como subtítulos.

#### Criterios de aceptación

1. WHEN el Conversor inicia, THE Registro_de_Conversiones SHALL contener el Formato SRT (Categoría documento, sin Firma_de_Formato, con extensión y Extensión_Principal ".srt") y registrar para los Pares_de_Conversión MP3 a SRT, WAV a SRT y MP4 a SRT, incluidos en el criterio 9 del Requisito 4, una Descripción_de_Pérdida que indica que el texto y las marcas de tiempo son aproximados.
2. WHEN el Conversor ejecuta MP3 a SRT, WAV a SRT o MP4 a SRT, THE Transcriptor SHALL producir una Lista_de_Segmentos_Válida según el Idioma_de_Transcripción de la Tarea a partir del Audio_a_Transcribir, que en MP4 a SRT es la Pista_de_Audio_Predeterminada que el Extractor_de_Audio extrae como Archivo_Intermedio.
3. WHEN el Transcriptor produce Segmentos_de_Transcripción para un Par_de_Conversión con Formato_de_Destino SRT, THE Transcriptor SHALL asignar a cada Segmento_de_Transcripción un tiempo de inicio mayor o igual a 0 ms y un tiempo de fin menor o igual a la duración en milisegundos del Audio_a_Transcribir.
4. THE Serializador_SRT SHALL escribir, a partir de una lista de Segmentos_de_Transcripción sea o no una Lista_de_Segmentos_Válida, un archivo SRT en UTF-8 formado por un Bloque_SRT por cada Segmento_de_Transcripción cuyo texto tiene al menos una línea que no es Línea_en_Blanco, en el orden de la lista recibida, con índices que comienzan en 1 y aumentan en 1 por bloque, y con el tiempo de inicio y el tiempo de fin del segmento en la Línea_de_Tiempos_SRT de cada bloque.
5. IF el texto de un Segmento_de_Transcripción contiene Líneas_en_Blanco y al menos una línea que no es Línea_en_Blanco, THEN THE Serializador_SRT SHALL eliminar las Líneas_en_Blanco y escribir las líneas restantes del texto en el Bloque_SRT en su orden original.
6. IF el contenido que recibe el Intérprete_SRT no está formado por cero o más Bloques_SRT con índices que comienzan en 1 y aumentan en 1 por bloque, THEN THE Intérprete_SRT SHALL devolver un error que indica el número de la primera línea que no cumple ese formato, contando las líneas desde 1, sin devolver Segmentos_de_Transcripción.
7. FOR ALL Listas_de_Segmentos_Válidas con tiempos de fin menores o iguales a 359999999 ms (99:59:59,999), incluida la lista vacía, THE Intérprete_SRT SHALL producir, al leer el archivo SRT que el Serializador_SRT escribe para esa lista, la misma cantidad de Segmentos_de_Transcripción, en el mismo orden, con los mismos tiempos de inicio y fin en milisegundos y los mismos textos que la lista original.
8. IF el texto de un Segmento_de_Transcripción está vacío o contiene solo Líneas_en_Blanco, THEN THE Serializador_SRT SHALL omitir ese segmento y mantener consecutivos los índices de los Bloques_SRT restantes.
9. WHEN el Transcriptor produce la Lista_de_Segmentos_Válida de un Par_de_Conversión con Formato_de_Destino SRT, THE Conversor SHALL entregar como Archivo_Resultado el archivo SRT que el Serializador_SRT escribe a partir de esa lista.
10. IF el Transcriptor reconoce cero palabras en el Audio_a_Transcribir de un Par_de_Conversión con Formato_de_Destino SRT, THEN THE Conversor SHALL finalizar la Tarea como completada con un Archivo_Resultado SRT de 0 bytes e informar al usuario que no se detectó voz.

### Requisito 13: Procesamiento local y sin IA en las conversiones

**Historia de usuario:** Como usuario, quiero que los archivos se procesen en mi equipo y que las conversiones no usen IA, para mantener el control y la privacidad de mi contenido.

#### Criterios de aceptación

1. THE Conversor SHALL ejecutar cada Par_de_Conversión del Mapa_de_Conversión, incluidas las transcripciones, solo con los componentes y el Modelo_de_Transcripción incluidos en la Imagen_de_Contenedor.
2. THE Conversor SHALL almacenar los Archivos_de_Origen, Archivos_Intermedios y Archivos_Resultado, desde que se recibe el Archivo_de_Origen hasta que se elimina el Espacio_de_Trabajo, solo en Espacios_de_Trabajo dentro del Almacenamiento_Temporal (S12).
3. WHILE el Equipo_de_Ejecución no tiene conexión a Internet, WHEN se procesa una Tarea con un Par_de_Conversión del Mapa_de_Conversión_Inicial registrado en el Registro_de_Conversiones, THE Conversor SHALL llevar la Tarea al mismo Estado_Terminal que con conexión a Internet y producir un Archivo_Resultado con el mismo Contenido_Decodificado que se obtiene al procesar el mismo Archivo_de_Origen con conexión.
4. THE Motor_de_Conversión SHALL producir un Archivo_Resultado con el mismo Contenido_Decodificado cada vez que ejecute el mismo Par_de_Conversión de imagen, documento, audio o video sobre el mismo Archivo_de_Origen, en el mismo Equipo_de_Ejecución, con los mismos componentes instalados y los mismos valores configurables.
5. THE Conversor SHALL limitar el uso de Modelos_de_Aprendizaje_Automático al Transcriptor, de modo que el Detector_de_Tipo, el Registro_de_Conversiones, el Motor_de_Conversión, el Extractor_de_Audio, el Serializador_SRT, el Intérprete_SRT, el Gestor_de_Tareas y el Gestor_de_Archivos_Temporales funcionen sin Modelos_de_Aprendizaje_Automático.
6. THE Conversor SHALL limitar el envío de contenido de Archivos_de_Origen, Archivos_Intermedios y Archivos_Resultado fuera del Equipo_de_Ejecución a la entrega, a través de la Interfaz_de_Usuario, del contenido del Archivo_Resultado que el usuario descarga y a la escritura en un volumen de red montado en la ruta del Almacenamiento_Temporal (S12).
7. WHILE el Detector_de_Tipo, el Motor_de_Conversión, el Extractor_de_Audio o el Transcriptor procesa un archivo, THE Conversor SHALL limitar las conexiones de red que inicia a destinos dentro del Equipo_de_Ejecución, sin solicitar los Recursos_Externos que el Archivo_de_Origen referencia por URL.
8. IF el usuario elige para una Tarea un idioma de transcripción que no es un Idioma_Soportado, THEN THE Conversor SHALL aplicar el rechazo definido en el criterio 9 del Requisito 11 sin descargar modelos de reconocimiento de voz ni enviar contenido del Archivo_de_Origen fuera del Equipo_de_Ejecución.

### Requisito 14: Tareas de larga duración

**Historia de usuario:** Como usuario, quiero ver el estado y el avance de las conversiones largas y poder cancelarlas, para saber cuánto falta y no esperar sin información.

#### Criterios de aceptación

1. WHEN el usuario confirma un Formato_de_Destino que forma un Par_de_Conversión registrado con el Tipo_Real de una Tarea en estado recibida y el Conversor no realiza un Rechazo_de_Destino, THE Gestor_de_Tareas SHALL asociar a la Tarea ese Par_de_Conversión y, en un Par_de_Transcripción, el Idioma_de_Transcripción que corresponde según los criterios 3 y 10 del Requisito 11, y cambiar el Estado_de_Tarea a en_cola.
2. WHEN el Estado_de_Tarea de una Tarea cambia, THE Gestor_de_Tareas SHALL informar el nuevo Estado_de_Tarea a la Interfaz_de_Usuario en un plazo menor o igual al Intervalo_de_Progreso desde el cambio.
3. THE Gestor_de_Tareas SHALL restringir los cambios de Estado_de_Tarea a las transiciones: recibida a en_cola, recibida a fallida, recibida a cancelada, en_cola a en_proceso, en_cola a cancelada, en_proceso a completada, en_proceso a fallida y en_proceso a cancelada.
4. WHILE el número de Tareas en_proceso es igual al Límite_de_Concurrencia, THE Gestor_de_Tareas SHALL mantener en estado en_cola todas las Tareas en_cola.
5. WHILE una Tarea_de_Larga_Duración está en_proceso, THE Gestor_de_Tareas SHALL informar a la Interfaz_de_Usuario el Porcentaje_de_Avance de la Tarea, con la primera actualización en un plazo menor o igual al Intervalo_de_Progreso desde el cambio a en_proceso y un tiempo entre actualizaciones consecutivas menor o igual al Intervalo_de_Progreso.
6. WHEN el usuario cancela una Tarea en estado recibida, en_cola o en_proceso, THE Gestor_de_Tareas SHALL detener el procesamiento de la Tarea y cambiar el Estado_de_Tarea a cancelada dentro de los 10 segundos siguientes a la solicitud.
7. IF el tiempo transcurrido desde que una Tarea cambió a en_proceso supera el Tiempo_Máximo_de_Procesamiento que le corresponde (el de transcripción si el Par_de_Conversión es un Par_de_Transcripción, o el de la Categoría del Par_de_Conversión en los demás casos), THEN THE Gestor_de_Tareas SHALL detener el procesamiento y cambiar el Estado_de_Tarea a fallida con el Motivo_de_Fallo "tiempo de procesamiento excedido" dentro de los 10 segundos siguientes al vencimiento.
8. WHEN transcurre el Período_de_Retención desde la creación de una Tarea que sigue en estado recibida, THE Gestor_de_Tareas SHALL cambiar el Estado_de_Tarea a cancelada y registrar como motivo que no se eligió un Formato_de_Destino dentro del Período_de_Retención.
9. FOR ALL secuencias de eventos aplicadas a una Tarea en un Estado_Terminal (confirmaciones de Formato_de_Destino, cancelaciones, finalizaciones de procesamiento y vencimientos del Tiempo_Máximo_de_Procesamiento o del Período_de_Retención), THE Gestor_de_Tareas SHALL mantener sin cambios el Estado_de_Tarea.
10. WHILE el número de Tareas en_proceso es menor al Límite_de_Concurrencia y existe al menos una Tarea en_cola, THE Gestor_de_Tareas SHALL cambiar a en_proceso la Tarea en_cola que ingresó primero a en_cola, dentro de los 5 segundos siguientes a que se cumpla esta condición.
11. IF el usuario solicita cancelar una Tarea en un Estado_Terminal, o confirmar el Formato_de_Destino de una Tarea en un estado distinto de recibida, THEN THE Gestor_de_Tareas SHALL rechazar la solicitud, mantener sin cambios el Estado_de_Tarea e informar al usuario el Estado_de_Tarea actual.
12. FOR ALL Tareas_de_Larga_Duración, THE Gestor_de_Tareas SHALL informar Porcentajes_de_Avance que no disminuyen entre actualizaciones consecutivas.
13. WHEN el Conversor inicia, THE Gestor_de_Tareas SHALL comenzar sin Tareas, sin recuperar las Tareas que existían antes del inicio.

### Requisito 15: Entrega del resultado

**Historia de usuario:** Como usuario, quiero descargar el archivo convertido con un nombre reconocible, para encontrarlo y usarlo con facilidad.

#### Criterios de aceptación

1. WHEN una Tarea cambia a estado completada, THE Interfaz_de_Usuario SHALL ofrecer la descarga del Archivo_Resultado e indicar el nombre con que se descargará.
2. WHEN el Conversor genera el Archivo_Resultado de una Tarea, THE Conversor SHALL nombrar el Archivo_Resultado con el Nombre_Base, después de aplicar la Normalización_de_Nombre, seguido de la Extensión_Principal del Formato_de_Destino, excepto en el caso del criterio 10 (por ejemplo, "Vacaciones 2024.PNG" convertido a JPG se descarga como "Vacaciones 2024.jpg").
3. IF el Nombre_Base contiene Caracteres_No_Permitidos, THEN THE Conversor SHALL reemplazar cada Carácter_No_Permitido por un guion bajo (`_`) y conservar sin cambios los demás caracteres, incluidos espacios, letras acentuadas y mayúsculas.
4. IF el Nombre_Base está vacío, THEN THE Conversor SHALL usar "resultado" como Nombre_Base (por ejemplo, el Nombre_Original ".png" convertido a JPG produce "resultado.jpg").
5. IF el Nombre_Base ocupa más de 200 bytes en UTF-8, THEN THE Conversor SHALL truncar el Nombre_Base al prefijo más largo de caracteres completos que ocupa 200 bytes o menos en UTF-8.
6. FOR ALL Nombres_Originales de Archivos_de_Origen, THE Conversor SHALL producir un nombre de Archivo_Resultado sin Caracteres_No_Permitidos, cuya parte anterior a la extensión no está vacía y ocupa 200 bytes o menos en UTF-8, y que termina en la Extensión_Principal del Formato_de_Destino o, en el caso del criterio 10, en ".zip".
7. FOR ALL Nombres_Base, THE Conversor SHALL producir el mismo resultado al aplicar la Normalización_de_Nombre dos veces seguidas que al aplicarla una vez.
8. IF el usuario solicita la descarga de un Archivo_Resultado cuando ya transcurrió el Período_de_Retención desde que la Tarea cambió a estado completada, THEN THE Conversor SHALL rechazar la descarga sin entregar contenido e informar al usuario que el resultado expiró y que debe enviar el archivo de nuevo para convertirlo.
9. WHILE no transcurrió el Período_de_Retención desde que la Tarea cambió a estado completada, THE Conversor SHALL entregar el Archivo_Resultado completo, con el mismo contenido byte a byte, en cada solicitud de descarga del usuario.
10. WHEN el Conversor entrega en un archivo ZIP las imágenes de un PDF de origen con más de una página, THE Conversor SHALL nombrar el archivo ZIP con el Nombre_Base, después de aplicar la Normalización_de_Nombre, seguido de la extensión ".zip" (por ejemplo, "informe.pdf" de 3 páginas convertido a PNG se descarga como "informe.zip", y de 1 página como "informe.png").
11. IF el usuario solicita la descarga del Archivo_Resultado de una Tarea que no está en estado completada, THEN THE Conversor SHALL rechazar la descarga sin entregar contenido e informar al usuario el Estado_de_Tarea actual.
12. THE Conversor SHALL aplicar la Normalización_de_Nombre en este orden: primero el reemplazo de Caracteres_No_Permitidos (criterio 3), después el truncado a 200 bytes (criterio 5) y por último la sustitución del Nombre_Base vacío por "resultado" (criterio 4).

### Requisito 16: Manejo de errores

**Historia de usuario:** Como usuario, quiero mensajes de error claros cuando una conversión falla, para entender qué pasó y qué puedo hacer.

#### Criterios de aceptación

1. IF el Conversor realiza un Rechazo_de_Origen de una Tarea en estado recibida por formato no reconocido o texto en una codificación no admitida (criterio 5 del Requisito 2), por un Tipo_Real que no es Formato_de_Origen de ningún Par_de_Conversión (criterio 7 del Requisito 2), por un tamaño mayor al Límite_de_Tamaño de la Categoría del Tipo_Real (criterio 9 del Requisito 2), por una Imagen_Animada (criterio 6 del Requisito 5), por una imagen que supera el Límite_de_Píxeles (criterio 9 del Requisito 5), por un DOCX_Cifrado (criterio 2 del Requisito 7), por un DOCX que supera los Límites_de_Descompresión (criterio 7 del Requisito 7), por un PDF_Protegido (criterio 7 del Requisito 8) o por un archivo dañado detectado por el Detector_de_Tipo (criterio 2 de este requisito), THEN THE Gestor_de_Tareas SHALL cambiar el Estado_de_Tarea de recibida a fallida y registrar el Motivo_de_Fallo como dato de la Tarea.
2. IF el Archivo_de_Origen contiene la Firma_de_Formato de su Tipo_Real y el Detector_de_Tipo, el Motor_de_Conversión, el Extractor_de_Audio o el Transcriptor no pueden decodificar su contenido porque su estructura interna está incompleta o es inválida para ese Formato (por ejemplo, un archivo truncado), sin que el archivo sea un DOCX_Cifrado o un PDF_Protegido, supere los Límites_de_Descompresión ni use un Códec no soportado, THEN THE Conversor SHALL cambiar el Estado_de_Tarea a fallida, desde recibida si el fallo ocurre en el Detector_de_Tipo o desde en_proceso si ocurre durante la ejecución del Par_de_Conversión, con el Motivo_de_Fallo "archivo dañado o ilegible".
3. IF el Motor_de_Conversión, el Extractor_de_Audio o el Transcriptor terminan con error, o finalizan sin producir su Archivo_Resultado o Archivo_Intermedio, mientras ejecutan el Par_de_Conversión de una Tarea en_proceso, THEN THE Gestor_de_Tareas SHALL cambiar el Estado_de_Tarea de en_proceso a fallida y registrar el Motivo_de_Fallo como dato de la Tarea.
4. WHEN una Tarea cambia a estado fallida, THE Interfaz_de_Usuario SHALL mostrar, a más tardar un Intervalo_de_Progreso después del cambio, un mensaje en español compuesto solo por la Etapa_de_Procesamiento en que ocurrió el fallo (detección, conversión, transcripción o entrega), el Motivo_de_Fallo y la acción sugerida asociada a ese Motivo_de_Fallo.
5. THE Conversor SHALL excluir de todo mensaje mostrado al usuario las rutas de almacenamiento del Equipo_de_Ejecución, las trazas de ejecución y el detalle técnico del error registrado en el Log_de_Eventos.
6. IF una Tarea cambia de en_proceso a fallida, THEN THE Conversor SHALL impedir la descarga de todo Archivo_Resultado de esa Tarea, completo o parcial (por ejemplo, las imágenes de páginas ya generadas en PDF a PNG).
7. WHEN una Tarea cambia a estado fallida, THE Conversor SHALL registrar en el Log_de_Eventos la fecha y hora del cambio, el identificador de la Tarea, el Tipo_Real si fue determinado, el Par_de_Conversión si el usuario eligió un Formato_de_Destino, la Etapa_de_Procesamiento, el Motivo_de_Fallo y el detalle técnico del error.
8. THE Conversor SHALL excluir del Log_de_Eventos todo fragmento del contenido de los Archivos_de_Origen, Archivos_Intermedios y Archivos_Resultado, incluidos el texto extraído de documentos y el texto transcripto, también cuando ese contenido aparezca en el detalle técnico del error.
9. IF un componente del Conversor termina con un error que no corresponde a ningún Motivo_de_Fallo definido mientras procesa una Tarea en estado recibida o en_proceso, THEN THE Gestor_de_Tareas SHALL cambiar el Estado_de_Tarea a fallida con el Motivo_de_Fallo "error interno del Conversor" y una acción sugerida que indica volver a subir el archivo e intentar la conversión de nuevo.
10. IF una escritura en el Almacenamiento_Temporal falla por falta de espacio mientras una Tarea está en_proceso, THEN THE Gestor_de_Tareas SHALL cambiar el Estado_de_Tarea de en_proceso a fallida con un Motivo_de_Fallo que indica falta de espacio de almacenamiento en el Equipo_de_Ejecución.
11. IF una Tarea cambia a estado fallida, THEN THE Gestor_de_Tareas SHALL continuar el procesamiento de las demás Tareas en estado en_cola o en_proceso.
12. IF el Conversor realiza un Rechazo_de_Destino por un Formato_de_Destino no registrado para el Tipo_Real (criterio 4 del Requisito 3), por dimensiones mayores al máximo del Formato_de_Destino (criterio 5 del Requisito 5), por un video sin Pistas_de_Audio (criterio 3 del Requisito 10), por una duración mayor a la Duración_Máxima_de_Transcripción (criterio 5 del Requisito 11) o por un idioma de transcripción que no es un Idioma_Soportado (criterio 9 del Requisito 11 y criterio 8 del Requisito 13), THEN THE Gestor_de_Tareas SHALL mantener la Tarea en estado recibida con su Archivo_de_Origen, sin registrar un Motivo_de_Fallo, para que el usuario elija otro Formato_de_Destino.
13. WHEN el Conversor rechaza o interrumpe la recepción de un archivo sin crear una Tarea (criterios 3, 4, 5, 9 y 10 del Requisito 1), THE Interfaz_de_Usuario SHALL mostrar un mensaje en español compuesto solo por la Etapa_de_Procesamiento recepción, el motivo del rechazo y una acción sugerida.

### Requisito 17: Archivos temporales y limpieza

**Historia de usuario:** Como usuario, quiero que el Conversor elimine los archivos que ya no hacen falta, para no acumular datos ni ocupar espacio de más.

#### Criterios de aceptación

1. WHEN el Conversor inicia la recepción de un archivo y el espacio libre del Almacenamiento_Temporal es mayor o igual al Espacio_Libre_Mínimo, THE Gestor_de_Archivos_Temporales SHALL crear, antes de almacenar cualquier dato del archivo, un Espacio_de_Trabajo dentro del Almacenamiento_Temporal distinto del Espacio_de_Trabajo de cualquier otra Tarea o recepción en curso.
2. WHEN una Tarea cambia a estado completada, THE Gestor_de_Archivos_Temporales SHALL eliminar el Archivo_de_Origen y los Archivos_Intermedios de la Tarea dentro de los 60 segundos siguientes, conservando el Archivo_Resultado en el Espacio_de_Trabajo.
3. WHEN transcurre el Período_de_Retención desde que una Tarea cambió a estado completada, THE Gestor_de_Archivos_Temporales SHALL eliminar el Espacio_de_Trabajo de la Tarea, incluido el Archivo_Resultado, dentro de los 60 segundos siguientes, con independencia de que el usuario haya descargado el Archivo_Resultado.
4. WHEN una Tarea cambia a estado fallida o cancelada, THE Gestor_de_Archivos_Temporales SHALL eliminar el Espacio_de_Trabajo de la Tarea, incluidos el Archivo_de_Origen, los Archivos_Intermedios y cualquier Archivo_Resultado parcial, dentro de los 60 segundos siguientes.
5. WHEN el Conversor inicia, THE Gestor_de_Archivos_Temporales SHALL eliminar todos los Espacios_de_Trabajo del Almacenamiento_Temporal antes de que el Conversor acepte un nuevo Archivo_de_Origen.
6. FOR ALL Tareas cuyo Espacio_de_Trabajo fue eliminado, THE Gestor_de_Archivos_Temporales SHALL dejar el Almacenamiento_Temporal, desde la eliminación en adelante, sin el Espacio_de_Trabajo de esa Tarea y sin ningún Archivo_de_Origen, Archivo_Intermedio ni Archivo_Resultado de esa Tarea.
7. THE Conversor SHALL almacenar el Archivo_de_Origen, los Archivos_Intermedios y el Archivo_Resultado de cada Tarea únicamente dentro del Espacio_de_Trabajo de esa Tarea.
8. THE Gestor_de_Archivos_Temporales SHALL eliminar únicamente Espacios_de_Trabajo y archivos ubicados dentro del Almacenamiento_Temporal.
9. IF la eliminación de un Espacio_de_Trabajo o de alguno de sus archivos falla, THEN THE Gestor_de_Archivos_Temporales SHALL registrar en el Log_de_Eventos el identificador de la Tarea y el motivo del fallo, reintentar la eliminación hasta 3 veces con 60 segundos entre intentos y mantener sin cambios el Estado_de_Tarea.
10. IF la recepción de un archivo termina sin crear una Tarea (criterios 3, 4, 8 y 10 del Requisito 1), THEN THE Gestor_de_Archivos_Temporales SHALL eliminar el Espacio_de_Trabajo creado para esa recepción, junto con los datos recibidos, dentro de los 60 segundos siguientes.

### Requisito 18: Interfaz web

**Historia de usuario:** Como usuario, quiero usar el Conversor desde el navegador y en español, para convertir archivos sin instalar programas adicionales en mi computadora.

#### Criterios de aceptación

1. THE Interfaz_de_Usuario SHALL presentar en español, con independencia del idioma configurado en el navegador y en el sistema operativo, todos los textos, etiquetas, controles (incluido el Selector_de_Archivos), nombres de Estado_de_Tarea, nombres de idiomas y mensajes que muestra en sus páginas, con excepción de los Nombres_Originales, los nombres de archivo, los nombres de Formato y de Códec, las extensiones y los caracteres de archivos que los mensajes citan.
2. THE Interfaz_de_Usuario SHALL ofrecer todas las funciones de este requisito, sin que el usuario instale extensiones, complementos ni otros programas, en las dos versiones estables principales más recientes de Chrome, Firefox, Edge y Safari para escritorio disponibles a la fecha de publicación de cada versión del Conversor.
3. WHEN el usuario elige un archivo con el Selector_de_Archivos, que permite elegir un solo archivo y ninguna carpeta por elección, THE Interfaz_de_Usuario SHALL iniciar, dentro de 1 segundo desde la elección y sin requerir otra acción del usuario, el envío de ese archivo con su Nombre_Original al Conversor para su recepción según el Requisito 1.
4. WHEN el usuario arrastra un único archivo y lo suelta sobre el Área_de_Carga, THE Interfaz_de_Usuario SHALL iniciar, dentro de 1 segundo desde que lo suelta y sin requerir otra acción del usuario, el envío de ese archivo con su Nombre_Original al Conversor para su recepción según el Requisito 1.
5. IF el usuario suelta uno o más elementos en la página de la Interfaz_de_Usuario fuera del Área_de_Carga, o suelta sobre el Área_de_Carga más de un elemento, al menos una carpeta o contenido que no es un archivo (por ejemplo, texto o un enlace arrastrados desde otra página), THEN THE Interfaz_de_Usuario SHALL descartar todos los elementos soltados sin enviar archivos al Conversor, sin abandonar la página ni modificar las Tareas mostradas, e informar al usuario, junto al Área_de_Carga, que se acepta un solo archivo por vez soltado sobre el Área_de_Carga.
6. WHEN el Detector_de_Tipo determina el Tipo_Real de un Archivo_de_Origen y el Conversor no realiza un Rechazo_de_Origen, THE Interfaz_de_Usuario SHALL mostrar junto a la Tarea de ese Archivo_de_Origen, en una misma página y dentro del plazo de 1 segundo del criterio 1 del Requisito 3, el nombre del Tipo_Real detectado, los Formatos_de_Destino y la Clasificación_de_Pérdida de cada uno, definidos en los criterios 1 y 2 del Requisito 3.
7. WHILE el Formato_de_Destino seleccionado para una Tarea en estado recibida forma un Par_de_Transcripción con el Tipo_Real, THE Interfaz_de_Usuario SHALL mostrar junto a esa Tarea, en la misma página que el control de confirmación del Formato_de_Destino, las opciones de Idioma_de_Transcripción definidas en el criterio 3 del Requisito 11, disponibles para elegir antes de activar ese control.
8. WHILE la Interfaz_de_Usuario está abierta en un Navegador_del_Usuario, THE Interfaz_de_Usuario SHALL mostrar cada Tarea creada desde ese Navegador_del_Usuario identificada por su Nombre_Original y, una vez confirmado, por su Formato_de_Destino, con su Estado_de_Tarea actualizado a más tardar un Intervalo_de_Progreso después de cada cambio (criterio 2 del Requisito 14) y, en cada Tarea_de_Larga_Duración en_proceso, con su Porcentaje_de_Avance actualizado con la frecuencia del criterio 5 del Requisito 14, sin que el usuario recargue la página.
9. WHILE una Tarea creada desde el Navegador_del_Usuario está en estado recibida, en_cola o en_proceso, THE Interfaz_de_Usuario SHALL mostrar junto a la Tarea un control que solicita la cancelación definida en el criterio 6 del Requisito 14.
10. WHEN el usuario activa el control de descarga de una Tarea completada, THE Interfaz_de_Usuario SHALL hacer que el navegador guarde el Archivo_Resultado como archivo descargado, con contenido idéntico byte a byte al Archivo_Resultado y con el nombre definido en los criterios 2 y 10 del Requisito 15, sin mostrar ni ejecutar su contenido en una página o pestaña del navegador; solo se admite el cambio de nombre que el navegador aplique para no sobrescribir un archivo existente en la carpeta de descargas.
11. WHEN el Conversor rechaza o interrumpe la recepción de un archivo sin crear una Tarea, realiza un Rechazo_de_Destino, cambia una Tarea a estado fallida, o rechaza una solicitud de descarga (criterios 8 y 11 del Requisito 15), de cancelación o de confirmación (criterio 11 del Requisito 14), THE Interfaz_de_Usuario SHALL mostrar, sin que el usuario recargue la página y a más tardar un Intervalo_de_Progreso después del rechazo o del cambio, el mensaje que corresponde según los criterios 4, 12 y 13 del Requisito 16 o según el criterio que define el rechazo, junto al Área_de_Carga si no se creó una Tarea o junto a la Tarea afectada en los demás casos.
12. WHEN el usuario vuelve a abrir la Interfaz_de_Usuario en el mismo Navegador_del_Usuario (al recargar la página, al abrirla en otra pestaña o ventana, o después de cerrar y volver a abrir el navegador), THE Interfaz_de_Usuario SHALL mostrar, a más tardar un Intervalo_de_Progreso después de terminar de cargar la página, solo las Tareas creadas desde ese Navegador_del_Usuario que todavía existen en el Conversor, cada una con la información de los criterios 6 y 8, el control del criterio 9 y la oferta de descarga del criterio 1 del Requisito 15 que correspondan a su Estado_de_Tarea actual.
13. IF el Conversor no responde durante más de 3 veces el Intervalo_de_Progreso (15 segundos con el valor predeterminado) a una solicitud de la Interfaz_de_Usuario distinta del envío de un archivo, o el envío de un archivo se interrumpe por pérdida de conexión con el Conversor, THEN THE Interfaz_de_Usuario SHALL mostrar, hasta que el Conversor vuelva a responder, un aviso de que no hay conexión con el Conversor y de que los Estados_de_Tarea mostrados pueden no estar actualizados, conservar en pantalla las Tareas mostradas y, si se interrumpió un envío, informar junto al Área_de_Carga que ese archivo no se recibió y debe enviarse de nuevo.
14. IF el Conversor responde que una Tarea mostrada no existe o ya no está disponible (criterio 5 del Requisito 19), por ejemplo después de un reinicio del Contenedor, THEN THE Interfaz_de_Usuario SHALL indicar junto a esa Tarea, sin que el usuario recargue la página y a más tardar un Intervalo_de_Progreso después de esa respuesta, que la Tarea ya no está disponible, y retirar sus controles de cancelación y de descarga.
15. THE Interfaz_de_Usuario SHALL obtener del Conversor todos los recursos que usa (páginas, código, hojas de estilo, fuentes, íconos e imágenes), sin solicitar recursos a destinos fuera del Equipo_de_Ejecución, de modo que ofrece todas las funciones de este requisito aunque ni el Equipo_Anfitrión ni el equipo en el que se ejecuta el Navegador_del_Usuario tengan conexión a Internet.

### Requisito 19: Uso simultáneo sin cuentas

**Historia de usuario:** Como persona con acceso a la Interfaz_de_Usuario, quiero usar el Conversor sin crear una cuenta y sin que otras personas vean mis archivos, para convertir archivos aunque otras personas lo usen al mismo tiempo.

#### Criterios de aceptación

1. THE Conversor SHALL ofrecer todas sus funciones sin solicitar al usuario que cree una cuenta, inicie sesión o ingrese credenciales.
2. WHEN el Conversor crea una Tarea, THE Gestor_de_Tareas SHALL asignarle un Identificador_de_Tarea con al menos 122 bits obtenidos de un generador de números aleatorios criptográficamente seguro (por ejemplo, un UUID versión 4), distinto del Identificador_de_Tarea de toda otra Tarea creada desde el último inicio del Conversor, aunque esa otra Tarea ya no exista.
3. WHEN el Conversor crea una Tarea, THE Conversor SHALL entregar su Identificador_de_Tarea en la respuesta a la solicitud con la que el Navegador_del_Usuario envió el Archivo_de_Origen, sin incluirlo en ninguna otra respuesta a una solicitud que no contenga ese Identificador_de_Tarea.
4. THE Conversor SHALL exigir el Identificador_de_Tarea en toda solicitud que consulte Datos_de_Tarea de una Tarea, confirme su Formato_de_Destino o su Idioma_de_Transcripción, acepte o rechace la confirmación solicitada en el criterio 3 del Requisito 3, cancele la Tarea o descargue su Archivo_Resultado.
5. IF una solicitud de las indicadas en el criterio 4 no incluye un Identificador_de_Tarea, incluye un valor que no tiene el formato de un Identificador_de_Tarea o incluye un Identificador_de_Tarea que no corresponde a ninguna Tarea existente (por ejemplo, el de una Tarea creada antes del último inicio del Conversor), THEN THE Conversor SHALL rechazar la solicitud sin entregar Datos_de_Tarea de ninguna Tarea y sin modificar el Estado_de_Tarea de ninguna Tarea, e informar al usuario que la Tarea no existe o ya no está disponible, con el mismo mensaje en los tres casos y sin indicar cuál de ellos ocurrió.
6. THE Conversor SHALL incluir en cada respuesta solo Datos_de_Tarea de las Tareas cuyo Identificador_de_Tarea incluye la solicitud o, en la respuesta del criterio 3, de la Tarea creada, sin incluir Datos_de_Tarea en las respuestas a solicitudes que no se refieren a una Tarea (por ejemplo, el Indicador_de_Salud) y sin ofrecer funciones que listen, busquen o enumeren Tareas.
7. WHILE la Interfaz_de_Usuario está abierta en un Navegador_del_Usuario, THE Interfaz_de_Usuario SHALL mostrar solo las Tareas creadas desde ese Navegador_del_Usuario y ninguna Tarea creada desde otro Navegador_del_Usuario, aunque ese otro Navegador_del_Usuario se ejecute en el mismo equipo o en la misma red.
8. THE Gestor_de_Tareas SHALL aplicar los criterios 4 y 10 del Requisito 14 a todas las Tareas en_cola y en_proceso, sin importar el Navegador_del_Usuario desde el que se crearon, como una única cola ordenada según el orden en que el Gestor_de_Tareas registra el ingreso de cada Tarea a en_cola y con un único Límite_de_Concurrencia.
9. WHILE el Conversor recibe un archivo o existe al menos una Tarea en estado en_cola o en_proceso, THE Conversor SHALL iniciar la recepción de cada archivo enviado desde otro Navegador_del_Usuario sin esperar a que terminen las recepciones en curso ni a que esas Tareas cambien de Estado_de_Tarea, y rechazar o interrumpir esa recepción solo por las causas de los criterios 3, 4, 5, 8 y 10 del Requisito 1.
10. WHILE el número de Tareas en_proceso es igual al Límite_de_Concurrencia, WHEN el Conversor completa la recepción de un archivo, THE Conversor SHALL registrar el Tipo_Real o realizar el Rechazo_de_Origen dentro del plazo del criterio 11 del Requisito 2 y mostrar los Formatos_de_Destino dentro del plazo del criterio 1 del Requisito 3, sin esperar a que una Tarea en_proceso cambie de Estado_de_Tarea.
11. IF el Conversor recibe para una misma Tarea dos o más solicitudes de confirmación del Formato_de_Destino o de cancelación antes de terminar de atender la primera (por ejemplo, desde dos pestañas del mismo Navegador_del_Usuario), THEN THE Gestor_de_Tareas SHALL atenderlas de a una, aplicar a cada solicitud los criterios 1, 6 y 11 del Requisito 14 según el Estado_de_Tarea que dejó la solicitud atendida antes, y asociar a la Tarea un único Par_de_Conversión, el de la única confirmación aceptada.

### Requisito 20: Empaquetado y ejecución en contenedor

**Historia de usuario:** Como usuario, quiero ejecutar el Conversor con Docker en cualquier equipo, para ponerlo en marcha con un solo comando y usarlo sin conexión a Internet.

#### Criterios de aceptación

1. THE Conversor SHALL distribuirse como una única Imagen_de_Contenedor, identificada por un mismo nombre y etiqueta, con una variante para procesadores x86-64 (amd64) y otra para procesadores ARM de 64 bits (arm64) que Docker ejecuta sin emulación en un Equipo_Anfitrión de esa arquitectura, y en la que cada variante incluye todos los componentes del Conversor, el Modelo_de_Transcripción, la Fuente_Predeterminada y las demás dependencias que usa el procesamiento de los 36 Pares_de_Conversión del Mapa_de_Conversión_Inicial.
2. WHEN el usuario ejecuta en el Equipo_Anfitrión un único comando `docker run` con la Imagen_de_Contenedor y la publicación del Puerto_de_la_Interfaz en un puerto del Equipo_Anfitrión, sin Variables_de_Configuración ni volúmenes montados, THE Conversor SHALL atender la Interfaz_de_Usuario en ese puerto del Equipo_Anfitrión e informar listo en el Indicador_de_Salud dentro de los 60 segundos posteriores a que Docker inicia el Contenedor en el Equipo_de_Referencia, sin contar la descarga de la Imagen_de_Contenedor y sin otros pasos de instalación ni de configuración.
3. THE Conversor SHALL iniciar, procesar Tareas y entregar la Interfaz_de_Usuario usando solo los archivos incluidos en la Imagen_de_Contenedor, los valores de las Variables_de_Configuración y los archivos recibidos a través de la Interfaz_de_Usuario, sin descargar archivos desde fuera del Equipo_de_Ejecución y sin que la Interfaz_de_Usuario haga que el Navegador_del_Usuario solicite páginas, estilos, código, imágenes ni fuentes a destinos fuera del Equipo_de_Ejecución.
4. WHILE el Equipo_Anfitrión no tiene conexión a Internet, WHEN el usuario ejecuta el comando del criterio 2 con una Imagen_de_Contenedor ya presente en el Equipo_Anfitrión, THE Conversor SHALL informar listo en el Indicador_de_Salud dentro del mismo plazo de 60 segundos del criterio 2, ofrecer todas las funciones del Requisito 18 a un Navegador_del_Usuario que se ejecuta en un equipo sin conexión a Internet y procesar Tareas según el criterio 3 del Requisito 13.
5. WHEN el Contenedor inicia, THE Conversor SHALL tomar cada valor configurable de S7 de su Variable_de_Configuración, con el nombre y la unidad que indica la Guía_de_Uso, si está definida, o del valor predeterminado indicado en S7, si no está definida.
6. IF al iniciar el Contenedor una Variable_de_Configuración está definida con un valor vacío, con un valor que no corresponde al tipo de su valor configurable (por ejemplo, texto donde se espera un número) o con un valor fuera del rango admitido que indica la Guía_de_Uso, que como mínimo excluye un Puerto_de_la_Interfaz fuera de 1 a 65535, un Límite_de_Concurrencia menor a 1, un Espacio_Libre_Mínimo menor a 0 y valores menores o iguales a 0 en cualquier Límite_de_Tamaño, el Límite_de_Tamaño_Global, el Límite_de_Píxeles, la Duración_Máxima_de_Transcripción, cualquier Tiempo_Máximo_de_Procesamiento, el Período_de_Retención, la Resolución_de_Rasterizado o el Intervalo_de_Progreso, THEN THE Conversor SHALL registrar en el Log_de_Eventos y en la salida del Contenedor que muestra `docker logs` el nombre de cada Variable_de_Configuración inválida y el motivo, y finalizar el Contenedor con un código de salida distinto de 0 dentro de los 10 segundos posteriores a su inicio, sin atender la Interfaz_de_Usuario ni aceptar archivos.
7. THE Conversor SHALL ubicar el Almacenamiento_Temporal en una ruta fija dentro del Contenedor, igual en todos los inicios de la Imagen_de_Contenedor, independiente de las Variables_de_Configuración e indicada en la Guía_de_Uso.
8. WHERE el usuario monta un volumen, local del Equipo_Anfitrión o de red (S12), en la ruta del Almacenamiento_Temporal, THE Gestor_de_Archivos_Temporales SHALL crear los Espacios_de_Trabajo en ese volumen y conservar sin cambios los archivos y carpetas del volumen que no son Espacios_de_Trabajo, también durante la limpieza del criterio 5 del Requisito 17.
9. WHILE el Conversor completó el inicio, incluida la limpieza del criterio 5 del Requisito 17, y la Interfaz_de_Usuario responde en el Puerto_de_la_Interfaz, en 5 segundos o menos, a la solicitud con que el Navegador_del_Usuario abre la Interfaz_de_Usuario, THE Conversor SHALL informar listo en el Indicador_de_Salud.
10. WHILE el Conversor no completó el inicio, o la Interfaz_de_Usuario no responde en el Puerto_de_la_Interfaz en 5 segundos o menos a la solicitud con que el Navegador_del_Usuario abre la Interfaz_de_Usuario, THE Conversor SHALL informar no listo en el Indicador_de_Salud.
11. THE Conversor SHALL declarar el Indicador_de_Salud en la Imagen_de_Contenedor como verificación de salud de Docker, de modo que, sin opciones adicionales en el comando del criterio 2, Docker muestre el Contenedor como saludable (healthy) a más tardar 30 segundos después de que el Indicador_de_Salud comienza a informar listo, y como no saludable (unhealthy) a más tardar 60 segundos después de que el Indicador_de_Salud, tras haber informado listo, comienza a informar no listo sin interrupción.
12. THE Conversor SHALL incluir en su repositorio de código un Inventario_de_Licencias que lista cada componente de terceros incluido en cualquiera de las variantes de la Imagen_de_Contenedor, incluidos el Modelo_de_Transcripción, la Fuente_Predeterminada, las bibliotecas y los paquetes del sistema operativo, con su nombre, su licencia y la misma versión que contiene la Imagen_de_Contenedor construida a partir de ese estado del repositorio.
13. IF al iniciar el Contenedor el Conversor no puede crear archivos en el Almacenamiento_Temporal (por ejemplo, porque el volumen montado según el criterio 8 es de solo lectura o no da permiso de escritura al Contenedor), THEN THE Conversor SHALL registrar el motivo en el Log_de_Eventos y en la salida del Contenedor que muestra `docker logs`, y finalizar el Contenedor con un código de salida distinto de 0 dentro de los 10 segundos posteriores a su inicio, sin atender la Interfaz_de_Usuario ni aceptar archivos.
