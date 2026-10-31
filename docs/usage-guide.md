# Guía de uso

El Conversor es una página web que recibe un archivo, detecta su tipo y lo convierte. Esta imagen es la del MVP: la arquitectura en la que se construyó, con el FFmpeg de Debian y sin transcripción.

## Arranque

```text
docker run --rm -p 8080:8080 converter:1.0.0
```

La página queda en `http://127.0.0.1:8080/`. El puerto de adentro es `CONVERTER_PORT` (8080 si no se cambia).

Los archivos de cada tarea viven en `/var/lib/converter/storage`. El proceso corre con el usuario `converter` (uid 10001).

## Volumen

Para conservar el almacenamiento entre arranques, montá un volumen en esa ruta:

```text
docker run --rm -p 8080:8080 \
  -v converter-data:/var/lib/converter/storage \
  converter:1.0.0
```

El volumen es responsabilidad de quien lo monta. El Conversor no detecta ni rechaza un sistema de archivos de red: si se monta uno, los archivos de origen, los intermedios y los resultados quedan en ese volumen. Al arrancar, la limpieza borra solo los espacios de trabajo (`ws-` y 32 hexadecimales) y deja el resto del volumen como está.

## Variables de configuración

Son enteros en base 10, sin signo ni espacios. Una variable vacía, no numérica o fuera de rango impide el arranque (salida 78) y el registro nombra cada variable inválida. Si no está definida, vale el predeterminado.

| Variable | Tipo | Unidad | Mínimo | Máximo | Predeterminado |
|---|---|---|---|---|---|
| CONVERTER_PORT | entero | puerto TCP | 1 | 65535 | 8080 |
| CONVERTER_MAX_UPLOAD_MB | entero | MB | 1 | 100000 | 500 |
| CONVERTER_MAX_IMAGE_MB | entero | MB | 1 | CONVERTER_MAX_UPLOAD_MB | 25 |
| CONVERTER_MAX_DOCUMENT_MB | entero | MB | 1 | CONVERTER_MAX_UPLOAD_MB | 25 |
| CONVERTER_MAX_AUDIO_MB | entero | MB | 1 | CONVERTER_MAX_UPLOAD_MB | 200 |
| CONVERTER_MAX_VIDEO_MB | entero | MB | 1 | CONVERTER_MAX_UPLOAD_MB | 500 |
| CONVERTER_MAX_IMAGE_PIXELS | entero | píxeles | 1 | 1000000000 | 100000000 |
| CONVERTER_MAX_TRANSCRIPTION_MINUTES | entero | minutos | 1 | 5999 | 60 |
| CONVERTER_TIMEOUT_IMAGE_MINUTES | entero | minutos | 1 | 1440 | 2 |
| CONVERTER_TIMEOUT_DOCUMENT_MINUTES | entero | minutos | 1 | 1440 | 2 |
| CONVERTER_TIMEOUT_AUDIO_MINUTES | entero | minutos | 1 | 1440 | 10 |
| CONVERTER_TIMEOUT_VIDEO_MINUTES | entero | minutos | 1 | 1440 | 30 |
| CONVERTER_TIMEOUT_TRANSCRIPTION_MINUTES | entero | minutos | 1 | 1440 | 120 |
| CONVERTER_RETENTION_MINUTES | entero | minutos | 1 | 10080 | 60 |
| CONVERTER_CONCURRENCY | entero | Tareas | 1 | 64 | 2 |
| CONVERTER_RASTER_DPI | entero | ppp | 36 | 600 | 150 |
| CONVERTER_MIN_FREE_SPACE_MB | entero | MB | 0 | 1000000 | 1000 |
| CONVERTER_PROGRESS_INTERVAL_SECONDS | entero | segundos | 1 | 60 | 5 |
| CONVERTER_OGG_MIN_SAMPLE_RATE_HZ | entero | Hz | 8000 | 192000 | 8000 |
| CONVERTER_OGG_MAX_SAMPLE_RATE_HZ | entero | Hz | CONVERTER_OGG_MIN_SAMPLE_RATE_HZ | 192000 | 192000 |

Ejemplo: `-e CONVERTER_PORT=9090 -e CONVERTER_CONCURRENCY=1`.

## Endurecimiento

Sobre el comando mínimo se pueden sumar:

```text
docker run --rm -p 8080:8080 \
  --read-only --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --memory 2g \
  converter:1.0.0
```

`--read-only` necesita `--tmpfs /tmp` porque algunas herramientas escriben ahí además del almacenamiento. `--memory` es un tope del contenedor; el valor lo elige quien lo opera.

## Qué quedó para después

- Transcripción (MP3, WAV y MP4 a TXT y a SRT), el modelo y el selector de idioma.
- Saneamiento de DOCX antes de LibreOffice: hoy el PDF muestra comentarios y marcas de revisión como los muestre LibreOffice, y no avisa que omitió macros.
- FFmpeg propio, compilado sin GPL. Esta imagen usa el paquete `ffmpeg` de Debian, que está compilado con `--enable-gpl`.
- Imagen multiarquitectura (amd64 y arm64 en el mismo manifiesto). Esta se construye para la arquitectura local.
- Pruebas de propiedades que todavía no están, pruebas de extremo a extremo en navegadores y pruebas de la imagen en un contenedor.
- La comparación del inventario de licencias contra los paquetes de Debian de la imagen. El inventario del repositorio cubre las dependencias Python y `licenses/manual.toml`.
