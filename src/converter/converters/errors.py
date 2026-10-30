"""Clasificación de los errores de las herramientas (Req 16.2, 16.9, 16.10, 14.7, 8.7, 9.6, 10.8,
11.6).

Implementa la tabla "Clasificación de errores de herramientas" del diseño. `classify_failure` es
una función pura: recibe la evidencia de un fallo y devuelve su Motivo_de_Fallo, sin E/S ni
reloj. La evidencia está saneada, así que no trae los parámetros del mensaje (el Códec y la pista,
las Dimensiones_Mostradas, el Límite_de_Píxeles): los agrega quien arma el `Failure`, a partir del
`SourceInfo` y de la configuración.

Evidencia que recibe `classify_failure`:

- `detail`: salida saneada de un subproceso (`eventlog.sanitize_tool_output`), con el patrón
  reconocido de mayor prioridad, el código de salida y la señal.
- `exception`: excepción capturada en el proceso que clasifica. Siempre se busca en ella y en las
  excepciones encadenadas (causa, contexto, miembros de un grupo) un `OSError` con errno `ENOSPC`.
  Con `raised_by`, además, la clase y el mensaje de cada una se comparan con los patrones de esa
  herramienta, como si fueran su salida. Así un inspector que usa Pillow o pypdfium2 en su propio
  proceso clasifica igual que el proceso principal cuando lee el traceback de un worker. Los
  mensajes no se guardan (Req 16.8).
- `timed_out`: el Gestor_de_Tareas detuvo el procesamiento al vencer el
  Tiempo_Máximo_de_Procesamiento.
- `free_bytes`: espacio libre del Almacenamiento_Temporal medido después del fallo.
- `no_streams`: FFmpeg o ffprobe no encontraron flujos en el origen.
- `package_damaged`: el paquete DOCX que convertía LibreOffice no pasó la comprobación estructural.

Orden de evaluación; decide la primera regla que se cumple:

1. Timeout: `tiempo_excedido` (Req 14.7). El Gestor_de_Tareas terminó el subproceso, así que su
   salida y su señal son consecuencia del timeout.
2. Falta de espacio: `sin_espacio_proceso` (Req 16.10). Cuenta el patrón `no_space` o `enospc`,
   un `ENOSPC` en la excepción o menos de 1 MB libre (`MIN_FREE_BYTES`). Va antes que las señales
   de cada herramienta por la misma razón que en `eventlog.PATTERNS`: los errores que informa una
   herramienta después de una escritura fallida son consecuencia de ella.
3. Patrón con motivo propio en `PATTERN_REASONS`: primero el de `detail`, después el de
   `exception`.
4. Código de salida 125, 126 o 127 sin patrón reconocido: `error_interno`. El lanzador no inició
   el programa aunque su mensaje no llegó. Va antes que la regla 5 para que un ffprobe que no se
   ejecutó no parezca un origen sin flujos.
5. Condiciones de una herramienta, que dan `archivo_danado` (Req 16.2): LibreOffice con
   `package_damaged` (con cualquier código de salida, o con el PDF ausente), y FFmpeg o ffprobe con
   un código de salida distinto de 0 y `no_streams`.
6. Todo lo demás: `error_interno` (Req 16.9). Incluye la terminación por una señal (SIGSEGV,
   SIGKILL, SIGXFSZ), los patrones sin motivo propio, la salida no reconocida, el resultado
   ausente y las excepciones no clasificadas.

Patrones con motivo propio (`PATTERN_REASONS`):

- `sin_espacio_proceso`: `no_space` y `enospc`, en todas las herramientas.
- `error_interno`: los mensajes del lanzador (`launch_*`), en todas las herramientas.
- `excede_limite_pixeles`: `decompression_bomb` de Pillow (`DecompressionBombError`).
- `pdf_protegido`: `pdf_password_error` de pypdfium2 (`FPDF_ERR_PASSWORD`).
- `codec_no_soportado`: `no_decoder` y `decoder_not_found` de FFmpeg, un flujo usado sin
  decodificador en la build.
- `modelo_no_disponible`: `model_load_failed`, `context_init_failed` e `invalid_model_data` de
  whisper-cli.
- `archivo_danado`:
  - Pillow: `unidentified_image` (`UnidentifiedImageError`), la imagen truncada
    (`truncated_image`, `truncated_png`, `truncated_read`) y los estados de error del
    decodificador (`buffer_overrun`, `decoder_error`, `unrecognized_data`,
    `codec_configuration_error`, `decoder_failed`);
  - pypdfium2: `pdf_format_error`, `pdf_file_error` y `pdf_page_error` (`FPDF_ERR_FORMAT`,
    `FPDF_ERR_FILE`, `FPDF_ERR_PAGE`), y `pdf_page_load_failed`, una página ilegible en un PDF
    que abrió;
  - FFmpeg y ffprobe: `invalid_data`, `moov_atom_not_found`, `ebml_header_failed`,
    `partial_file`, `codec_parameters_not_found` y `header_missing`;
  - FFmpeg: `error_while_decoding` y `no_output_streams`, una salida sin flujos.

Los demás patrones no tienen motivo propio y siguen con las reglas 4 a 6:

- falta de memoria y `RLIMIT_FSIZE` (`out_of_memory`, `file_too_large`, `memory_error`,
  `codec_out_of_memory`, `bad_alloc`, `allocation_failed`);
- errores del codificador o de la build (`encoder_failed`, `unknown_encoder`);
- líneas genéricas o sin causa (`conversion_failed`, `pdf_load_failed`, `pdf_unknown_error`,
  `pdf_text_page_load_failed`) y `pdf_security_error` (`FPDF_ERR_SECURITY`, que la tabla del
  diseño no clasifica; un PDF_Protegido es el que requiere contraseña);
- todos los de LibreOffice: su motivo depende de la comprobación estructural del paquete;
- los de whisper-cli que no son del modelo: el Audio_a_Transcribir es un Archivo_Intermedio que
  produjo FFmpeg y el idioma ya se validó, así que esos errores no indican un origen dañado.

`classify_zip_verdict` traduce el veredicto del lector ZIP de DOCX (`detection.zipcheck`): superar
un Límite_de_Descompresión da `docx_excede_limites_descompresion`, con prioridad sobre el daño;
un directorio central ilegible, un error de deflate o de CRC o un método no admitido dan
`archivo_danado`.
"""

from __future__ import annotations

import errno as errno_codes
from collections.abc import Iterator, Mapping
from types import MappingProxyType

from converter.config import BYTES_PER_MB
from converter.detection.zipcheck import LimitVerdict
from converter.eventlog import PATTERNS, Tool, ToolDetail, ToolPattern, sanitize_tool_output
from converter.messages_es import FailureReason
from converter.sandbox import launch

# Con menos espacio libre que esto al fallar, el motivo es la falta de espacio (1 MB)
MIN_FREE_BYTES = 1 * BYTES_PER_MB

# Códigos de salida propios del lanzador: argumentos o límites inválidos, programa que no puede
# ejecutarse y programa que no existe
LAUNCHER_EXIT_CODES = frozenset(
    {launch.EXIT_LAUNCH_ERROR, launch.EXIT_CANNOT_EXECUTE, launch.EXIT_NOT_FOUND}
)

_NO_SPACE = FailureReason.NO_SPACE
_DAMAGED = FailureReason.DAMAGED
_INTERNAL = FailureReason.INTERNAL_ERROR

# Identificador de patrón → motivo propio, o None si el patrón solo no decide el motivo
type _Reasons = dict[str, FailureReason | None]

# Patrones que `eventlog` agrega a toda herramienta
_COMMON: _Reasons = {
    "no_space": _NO_SPACE,
    "enospc": _NO_SPACE,
    "launch_invalid_args": _INTERNAL,
    "launch_limits_failed": _INTERNAL,
    "launch_not_found": _INTERNAL,
    "launch_cannot_execute": _INTERNAL,
    "out_of_memory": None,
    "file_too_large": None,
}

_LIBAV_INPUT: _Reasons = {
    "invalid_data": _DAMAGED,
    "moov_atom_not_found": _DAMAGED,
    "ebml_header_failed": _DAMAGED,
    "partial_file": _DAMAGED,
    "codec_parameters_not_found": _DAMAGED,
    "header_missing": _DAMAGED,
}

_FFMPEG_ONLY: _Reasons = {
    "no_decoder": FailureReason.UNSUPPORTED_CODEC,
    "decoder_not_found": FailureReason.UNSUPPORTED_CODEC,
    "unknown_encoder": None,
    "error_while_decoding": _DAMAGED,
    "no_output_streams": _DAMAGED,
    "conversion_failed": None,
}

_PILLOW: _Reasons = {
    "decompression_bomb": FailureReason.PIXEL_LIMIT,
    "unidentified_image": _DAMAGED,
    "truncated_image": _DAMAGED,
    "truncated_png": _DAMAGED,
    "truncated_read": _DAMAGED,
    "decoder_error": _DAMAGED,
    "unrecognized_data": _DAMAGED,
    "buffer_overrun": _DAMAGED,
    "codec_configuration_error": _DAMAGED,
    "codec_out_of_memory": None,
    "decoder_failed": _DAMAGED,
    "encoder_failed": None,
    "memory_error": None,
}

_PYPDFIUM2: _Reasons = {
    "pdf_password_error": FailureReason.PROTECTED_PDF,
    "pdf_security_error": None,
    "pdf_format_error": _DAMAGED,
    "pdf_file_error": _DAMAGED,
    "pdf_page_error": _DAMAGED,
    "pdf_unknown_error": None,
    "pdf_load_failed": None,
    "pdf_page_load_failed": _DAMAGED,
    "pdf_text_page_load_failed": None,
    "memory_error": None,
}

_LIBREOFFICE: _Reasons = {
    "source_not_loaded": None,
    "no_export_filter": None,
    "user_installation_failed": None,
    "fatal_exception": None,
    "bad_alloc": None,
}

_WHISPER_CPP: _Reasons = {
    "model_load_failed": FailureReason.MODEL_UNAVAILABLE,
    "context_init_failed": FailureReason.MODEL_UNAVAILABLE,
    "invalid_model_data": FailureReason.MODEL_UNAVAILABLE,
    "input_not_found": None,
    "audio_read_failed": None,
    "audio_process_failed": None,
    "unknown_language": None,
    "allocation_failed": None,
    "bad_alloc": None,
}


def _tool_reasons(tool: Tool, *specific: _Reasons) -> Mapping[str, FailureReason | None]:
    reasons: _Reasons = dict(_COMMON)
    for group in specific:
        reasons.update(group)
    # La tabla es cerrada: cada patrón de `eventlog.PATTERNS` tiene una decisión explícita
    expected = {pattern.id for pattern in PATTERNS[tool]}
    if reasons.keys() != expected:
        missing = sorted(expected - reasons.keys())
        extra = sorted(reasons.keys() - expected)
        raise ValueError(
            f"motivos de {tool} distintos de PATTERNS: faltan {missing}, sobran {extra}"
        )
    return MappingProxyType(reasons)


# Motivo propio de cada patrón de `eventlog.PATTERNS`, por herramienta
PATTERN_REASONS: Mapping[Tool, Mapping[str, FailureReason | None]] = MappingProxyType(
    {
        Tool.FFMPEG: _tool_reasons(Tool.FFMPEG, _LIBAV_INPUT, _FFMPEG_ONLY),
        Tool.FFPROBE: _tool_reasons(Tool.FFPROBE, _LIBAV_INPUT),
        Tool.PILLOW: _tool_reasons(Tool.PILLOW, _PILLOW),
        Tool.PYPDFIUM2: _tool_reasons(Tool.PYPDFIUM2, _PYPDFIUM2),
        Tool.LIBREOFFICE: _tool_reasons(Tool.LIBREOFFICE, _LIBREOFFICE),
        Tool.WHISPER_CPP: _tool_reasons(Tool.WHISPER_CPP, _WHISPER_CPP),
    }
)

_FFMPEG_TOOLS = frozenset({Tool.FFMPEG, Tool.FFPROBE})


def _pattern_reason(tool: Tool, pattern: ToolPattern | None) -> FailureReason | None:
    # Un patrón que no está en la tabla (un ToolDetail armado a mano) es salida no clasificada
    if pattern is None:
        return None
    return PATTERN_REASONS[tool].get(pattern.id)


def _linked_exceptions(exc: BaseException) -> Iterator[BaseException]:
    """`exc` y las excepciones encadenadas: causa, contexto y miembros de un grupo, sin repetir.

    El contexto cuenta aunque esté suprimido (`raise ... from None`): sigue siendo lo que pasó.
    """
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)


def _is_enospc(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno == errno_codes.ENOSPC


def _exception_line(exc: BaseException) -> str:
    """Última línea del traceback de `exc`: la clase con su módulo y el mensaje."""
    cls = type(exc)
    name = cls.__qualname__
    if cls.__module__ not in ("builtins", "__main__"):
        name = f"{cls.__module__}.{name}"
    try:
        message = str(exc)
    except Exception:  # un __str__ defectuoso no impide clasificar
        message = ""
    return f"{name}: {message}" if message else name


def classify_failure(
    *,
    detail: ToolDetail | None = None,
    exception: BaseException | None = None,
    raised_by: Tool | None = None,
    timed_out: bool = False,
    free_bytes: int | None = None,
    no_streams: bool = False,
    package_damaged: bool = False,
) -> FailureReason:
    """Motivo_de_Fallo de un fallo según su evidencia, en el orden del docstring del módulo.

    Sin evidencia (por ejemplo, una herramienta que terminó bien sin producir su archivo) el
    motivo es `error_interno`.

    Lanza `ValueError` si la evidencia es incoherente: `raised_by` sin `exception`, `no_streams`
    sin el `detail` de FFmpeg o ffprobe, `package_damaged` sin el `detail` de LibreOffice o
    `free_bytes` negativo.
    """
    if raised_by is not None and exception is None:
        raise ValueError("raised_by requiere exception")
    if no_streams and (detail is None or detail.tool not in _FFMPEG_TOOLS):
        raise ValueError("no_streams solo se aplica al detalle de FFmpeg o ffprobe")
    if package_damaged and (detail is None or detail.tool is not Tool.LIBREOFFICE):
        raise ValueError("package_damaged solo se aplica al detalle de LibreOffice")
    if free_bytes is not None and free_bytes < 0:
        raise ValueError(f"free_bytes no puede ser negativo: {free_bytes}")

    if timed_out:
        return FailureReason.TIMED_OUT

    linked = list(_linked_exceptions(exception)) if exception is not None else []
    reasons: list[FailureReason] = []
    if detail is not None:
        reason = _pattern_reason(detail.tool, detail.pattern)
        if reason is not None:
            reasons.append(reason)
    if raised_by is not None:
        matched = sanitize_tool_output(raised_by, (_exception_line(exc) for exc in linked))
        reason = _pattern_reason(matched.tool, matched.pattern)
        if reason is not None:
            reasons.append(reason)

    if (
        _NO_SPACE in reasons
        or any(_is_enospc(exc) for exc in linked)
        or (free_bytes is not None and free_bytes < MIN_FREE_BYTES)
    ):
        return _NO_SPACE
    if reasons:
        return reasons[0]
    if detail is not None:
        if detail.pattern is None and detail.exit_code in LAUNCHER_EXIT_CODES:
            return _INTERNAL
        if package_damaged:
            return _DAMAGED
        if no_streams and detail.exit_code not in (None, 0):
            return _DAMAGED
    return _INTERNAL


def classify_zip_verdict(verdict: LimitVerdict) -> FailureReason | None:
    """Motivo del Rechazo_de_Origen según el lector ZIP de DOCX, o None si el paquete pasa.

    Superar un Límite_de_Descompresión tiene prioridad sobre el daño, porque Req 16.2 excluye de
    `archivo_danado` a los DOCX que los superan (Req 7.7).
    """
    if verdict.exceeded is not None:
        return FailureReason.DECOMPRESSION_LIMITS
    if verdict.damaged:
        return _DAMAGED
    return None
