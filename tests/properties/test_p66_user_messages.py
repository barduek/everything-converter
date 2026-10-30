"""Propiedad 66: los mensajes al usuario tienen etapa, motivo y acción, sin detalle técnico
(Req 16.4, 16.5, 16.13).

La prueba corre con cada código del catálogo de `messages_es` (Motivos_de_Fallo, errores de
recepción, Rechazos_de_Destino, errores de solicitudes sobre Tareas y motivos de cancelación) y
con cada aviso, y cada ejemplo arma su mensaje con parámetros al azar de cada `ParamKind`. El caso
lleva además su propio detalle técnico, el que va al Log_de_Eventos (Req 16.7): la ruta de un
Espacio_de_Trabajo del Almacenamiento_Temporal (`/var/lib/converter/storage/ws-<32 hex>`), esa
ruta vista desde un anfitrión Windows, una traza de Python con rutas del código, la excepción con
su clase y su texto, y líneas de stderr de las herramientas con direcciones de memoria.

El ejemplo intenta, en la mitad de los casos, colar ese detalle en el mensaje de una de estas
formas, y entonces el mensaje se rechaza con `InvalidMessageError`:

- en un parámetro de tipo cerrado (número, duración, nombre de Formato o de Códec, Categoría,
  pista, Números_de_Página, caracteres citados o Estado_de_Tarea), en lugar de su valor o dentro
  de su lista;
- en un parámetro que el código no declara, como `detalle` o `traceback`;
- en un Motivo_de_Fallo, con una etapa que no es una de las suyas.

Sin esos intentos el mensaje se arma, y la prueba verifica que:

- lleva la etapa del caso si es un Motivo_de_Fallo, la etapa recepción si es un error de
  recepción y ninguna en los demás grupos (Req 16.4, 16.13);
- el motivo y la acción no están vacíos y el texto es exactamente "Etapa: <etapa>. Motivo:
  <motivo> Acción sugerida: <acción>", sin "Etapa:" cuando no hay etapa, con una sola acción;
- el motivo y la acción son la plantilla y la acción del catálogo con cada campo reemplazado por
  su parámetro, así que no quedan llaves de la plantilla ni texto agregado;
- fuera de los textos libres no hay separadores de ruta, llaves, marcas de traza o de stderr ni
  fragmentos del detalle, y el texto completo no tiene saltos de línea ni otros caracteres no
  imprimibles, ni el identificador del Espacio_de_Trabajo o la dirección de memoria del caso.

Los textos libres (destino o idioma pedidos, Extensión_del_Nombre y nombres de idiomas y de
fuentes) vienen del usuario o de su archivo, y los requisitos piden mostrarlos (Req 2.6, 3.4,
7.4, 11.9). El módulo no puede distinguir un texto del usuario que parece una ruta o una traza de
una ruta o una traza del Conversor, así que la prueba no les pone el detalle del caso: les pone
texto hostil del usuario (rutas, trazas de varias líneas, caracteres de control, marcas de
dirección, llaves de plantilla y las etiquetas "Motivo:" y "Acción sugerida:") y verifica que se
muestran neutralizados, en su lugar de la plantilla: cada carácter no imprimible pasa a U+FFFD
(los espacios Unicode, a un espacio) y el texto se recorta a 40 caracteres, o a 64 en los nombres
de idiomas y de fuentes, seguidos de "…".

El oráculo usa su propia copia de esas reglas y de la forma de cada tipo de parámetro. De
`messages_es` solo toma lo que recorre: los códigos, sus grupos, sus etapas, sus plantillas y los
tipos de parámetro que declaran.
"""

from __future__ import annotations

import re
import string
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from converter.messages_es import (
    CATALOG,
    NOTICES,
    CodecTrack,
    FailureReason,
    InvalidMessageError,
    MessageGroup,
    ParamKind,
    UserMessage,
    render_error,
    render_failure,
    render_notice,
)
from converter.models import Category, Failure, Notice, Stage, TaskState
from tests.strategies.names import LOOKALIKE_CHARS

# --- Detalle técnico del caso ---

_STORAGE = "/var/lib/converter/storage"  # Almacenamiento_Temporal (Req 20.7)
_HEX = "0123456789abcdef"
# Archivos dentro de un Espacio_de_Trabajo
_WORKSPACE_FILES = (
    "source/original",
    "work/sanitized.docx",
    "work/page-0003.png",
    "work/audio.wav",
    "result/informe.zip",
    "tmp/lo-profile/user/registrymodifications.xcu",
)
# El volumen del Almacenamiento_Temporal visto desde un anfitrión Windows
_WINDOWS_ROOTS = (
    "C:\\ProgramData\\Docker\\volumes\\converter-storage\\_data",
    "\\\\wsl.localhost\\docker-desktop-data\\data\\docker\\volumes\\converter-storage\\_data",
)
_CODE_FILES = (
    "/app/src/converter/tasks/runner.py",
    "/app/src/converter/sandbox/executor.py",
    "/app/src/converter/converters/image.py",
    "/usr/local/lib/python3.12/site-packages/PIL/Image.py",
    "/usr/local/lib/python3.12/site-packages/pypdfium2/_helpers/document.py",
    "/usr/local/lib/python3.12/zipfile/__init__.py",
)
_FUNCTIONS = ("run", "convert", "open", "_open_image", "__init__", "_RealGetContents")
_CODE_LINES = (
    "image = Image.open(path)",
    "raise UnidentifiedImageError(msg)",
    "pdf = pdfium.PdfDocument(source)",
    "result = executor.run(command, timeout=deadline)",
    'raise BadZipFile("File is not a zip file")',
)
# Línea final de la traza: clase de la excepción y su texto, con la ruta del caso en {path}
_EXCEPTIONS = (
    "PIL.UnidentifiedImageError: cannot identify image file '{path}'",
    "OSError: [Errno 28] No space left on device: '{path}'",
    "zipfile.BadZipFile: Bad CRC-32 for file 'word/document.xml' in '{path}'",
    "pypdfium2._helpers.misc.PdfiumError: Failed to load document (PDFium: Incorrect password "
    "error). {path}",
    "subprocess.CalledProcessError: Command '['ffmpeg', '-i', '{path}']' returned non-zero exit "
    "status 1.",
    "converter.sandbox.ExecutorError: killed by SIGSEGV while reading {path}",
    "MemoryError: RLIMIT_AS reached while decoding {path}",
    "KeyError: '{path}'",
)
# Líneas de stderr de FFmpeg, ffprobe, LibreOffice y whisper.cpp; {ptr} es una dirección
_STDERR = (
    "[mp3 @ 0x{ptr}] Invalid data found when processing input",
    "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x{ptr}] moov atom not found",
    "[matroska,webm @ 0x{ptr}] EBML header parsing failed",
    "{path}: Invalid data found when processing input",
    "whisper_init_from_file_with_params_no_state: failed to load model from '{path}'",
    "Error: source file could not be loaded: {path}",
    "Segmentation fault (core dumped) at 0x{ptr}",
)
# Marcas de traza y de stderr que el texto propio del Conversor nunca contiene
_TRACE_MARKERS = ("Traceback", 'File "', "line ", "Errno", " @ 0x", "core dumped")
# Claves que un código no declara
_TECHNICAL_KEYS = ("detalle", "detalle_tecnico", "traceback", "stderr", "excepcion", "ruta")


@dataclass(frozen=True)
class _Detail:
    """Detalle técnico de un caso, como lo registra el Log_de_Eventos."""

    workspace: str  # "ws-<32 hex>"
    pointer: str  # "0x<12 hex>"
    full: str  # traza y stderr, en varias líneas
    # Rutas, traza completa, cada línea de la traza y cada línea de stderr. Todas tienen un
    # carácter que no admite un nombre de Formato o de Códec: espacio, barra o dos puntos
    fragments: tuple[str, ...]


@st.composite
def _details(draw: st.DrawFn) -> _Detail:
    workspace = "ws-" + draw(st.text(_HEX, min_size=32, max_size=32))
    pointer = "0x" + draw(st.text(_HEX, min_size=12, max_size=12))
    inner = draw(st.sampled_from(_WORKSPACE_FILES))
    path = f"{_STORAGE}/{workspace}/{inner}"
    windows = "\\".join((draw(st.sampled_from(_WINDOWS_ROOTS)), workspace, *inner.split("/")))
    trace = ["Traceback (most recent call last):"]
    frames = st.tuples(
        st.sampled_from(_CODE_FILES),
        st.integers(1, 5000),
        st.sampled_from(_FUNCTIONS),
        st.sampled_from(_CODE_LINES),
    )
    for code_file, line, function, code in draw(st.lists(frames, min_size=1, max_size=3)):
        trace += [f'  File "{code_file}", line {line}, in {function}', f"    {code}"]
    trace.append(draw(st.sampled_from(_EXCEPTIONS)).replace("{path}", path))
    stderr = [
        line.replace("{path}", path).replace("{ptr}", pointer[2:])
        for line in draw(st.lists(st.sampled_from(_STDERR), min_size=1, max_size=3, unique=True))
    ]
    full = "\n".join((*trace, *stderr))
    fragments = (path, windows, "\n".join(trace), *trace, *stderr)
    return _Detail(workspace, pointer, full, fragments)


# --- Parámetros válidos ---

# Nombres de Formato del Mapa_de_Conversión_Inicial y nombres con el patrón cerrado
_FORMAT_NAMES = (
    "PNG",
    "JPG",
    "WEBP",
    "PDF",
    "TXT",
    "MD",
    "HTML",
    "DOCX",
    "MP3",
    "WAV",
    "OGG",
    "MP4",
    "WEBM",
    "SRT",
)
_FORMAT_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}"
# Códecs como los informa ffprobe y nombres con el patrón cerrado
_CODEC_NAMES: tuple[str, ...] = ("h264", "hevc", "vp6f", "vp9", "av1", "msmpeg4v3", "prores")
_CODEC_NAMES += ("aac", "opus", "vorbis", "flac", "wmav2", "pcm_s16le", "adpcm_ima_wav", "amr_nb")
_CODEC_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}"
# Caracteres que vale la pena citar: llaves, barras, controles, marcas y sustitutos sueltos
_ODD_CHARACTERS = ("{", "}", "/", "\\", "«", "»", "\x00", "\x1b", "\u0301", "\u202e", "\ud800")

# Texto hostil del usuario o de su archivo para los parámetros de texto libre
_HOSTILE_PIECES = (
    "/etc/passwd",
    "../../",
    "C:\\Users\\",
    "\\\\servidor\\recurso",
    f"{_STORAGE}/",
    'Traceback (most recent call last):\n  File "x.py", line 1, in <module>\n',
    "ValueError: x",
    "[mp3 @ 0x0] Invalid data",
    "\x1b[31m",
    "\r\n",
    "\t",
    "\x00",
    "\x7f",
    "\ud800",
    "{",
    "}",
    "{destino}",
    "{tipo_real}",
    "{0}",
    "{0.__class__}",
    "{valor!r}",
    "{{",
    "}}",
    "%s",
    "«",
    "»",
    "Motivo: ",
    "Acción sugerida: ",
    "Etapa: entrega.",
)
# Tiradas en el borde de los recortes a 40 y a 64 caracteres
_NEAR_LIMITS = tuple("a" * length for length in (39, 40, 41, 63, 64, 65))
_PIECES = st.one_of(
    st.text(max_size=12),
    st.sampled_from(_HOSTILE_PIECES),
    st.sampled_from(LOOKALIKE_CHARS),
    st.sampled_from(_NEAR_LIMITS),
)
_NON_EMPTY_PIECES = st.one_of(
    st.text(min_size=1, max_size=12),
    st.sampled_from(_HOSTILE_PIECES),
    st.sampled_from(LOOKALIKE_CHARS),
    st.sampled_from(_NEAR_LIMITS),
)


def _joined(parts: tuple[list[str], str, list[str]]) -> str:
    before, middle, after = parts
    return "".join(before) + middle + "".join(after)


_user_texts = st.lists(_PIECES, max_size=6).map("".join)
_non_empty_user_texts = st.tuples(
    st.lists(_PIECES, max_size=3), _NON_EMPTY_PIECES, st.lists(_PIECES, max_size=3)
).map(_joined)
_LANGUAGE_NAMES = ("español", "inglés", "portugués", "javanés")
_FONTS = (("Calibri", "Carlito"), ("Cambria", "Caladea"), ("Wingdings", None))

_names = st.one_of(_non_empty_user_texts, st.sampled_from(_LANGUAGE_NAMES))
_replacements = st.one_of(
    st.sampled_from(_FONTS),
    st.tuples(_non_empty_user_texts, st.one_of(st.none(), _non_empty_user_texts)),
)

_VALID: Mapping[ParamKind, st.SearchStrategy[object]] = {
    ParamKind.NUMBER: st.integers(1, 10**12),
    ParamKind.DURATION_MS: st.integers(0, 10**10),
    ParamKind.FORMAT: st.one_of(
        st.sampled_from(_FORMAT_NAMES), st.from_regex(_FORMAT_PATTERN, fullmatch=True)
    ),
    ParamKind.FORMATS: st.lists(
        st.one_of(st.sampled_from(_FORMAT_NAMES), st.from_regex(_FORMAT_PATTERN, fullmatch=True)),
        min_size=1,
        max_size=6,
    ),
    ParamKind.CATEGORY: st.sampled_from(list(Category)),
    ParamKind.CODEC: st.one_of(
        st.sampled_from(_CODEC_NAMES), st.from_regex(_CODEC_PATTERN, fullmatch=True)
    ),
    ParamKind.TRACK: st.sampled_from(list(CodecTrack)),
    ParamKind.PAGES: st.lists(st.integers(1, 10**6), min_size=1, max_size=20, unique=True).map(
        sorted
    ),
    ParamKind.CHARACTERS: st.lists(
        st.one_of(st.characters(exclude_characters=" \t\n\r"), st.sampled_from(_ODD_CHARACTERS)),
        min_size=1,
        max_size=10,
        unique=True,
    ),
    ParamKind.STATE: st.sampled_from(list(TaskState)),
    ParamKind.USER_VALUE: st.one_of(st.sampled_from(("MP3", "pdf", "", "es")), _user_texts),
    ParamKind.EXTENSION: st.one_of(
        st.sampled_from((".png", ".JPEG", ".tar.gz")), _non_empty_user_texts.map(".".__add__)
    ),
    ParamKind.LANGUAGES: st.lists(_names, min_size=1, max_size=4),
    ParamKind.FONT_REPLACEMENTS: st.lists(_replacements, min_size=1, max_size=4),
}

# Textos del usuario o de su archivo, que se muestran neutralizados
_FREE_TEXT = frozenset(
    {ParamKind.USER_VALUE, ParamKind.EXTENSION, ParamKind.LANGUAGES, ParamKind.FONT_REPLACEMENTS}
)
# Además de los textos libres, los caracteres citados pueden ser "/" o "{" y se dejan fuera del
# examen de separadores y llaves; su forma se verifica aparte
_SET_ASIDE = _FREE_TEXT | {ParamKind.CHARACTERS}
_LISTS = frozenset({ParamKind.FORMATS, ParamKind.PAGES, ParamKind.CHARACTERS})


# --- Casos ---

_Render = Literal["fallo", "error", "aviso"]
# "ninguno": parámetros válidos; "parámetro": detalle en un parámetro de tipo cerrado;
# "clave": detalle en un parámetro no declarado; "etapa": Motivo_de_Fallo en una etapa ajena
_Attempt = Literal["ninguno", "parámetro", "clave", "etapa"]


@dataclass(frozen=True)
class _Case:
    code: str
    render: _Render
    stage: Stage | None  # solo en los Motivos_de_Fallo
    params: dict[str, object]
    attempt: _Attempt
    detail: _Detail


@dataclass(frozen=True)
class _Code:
    render: _Render
    code: str
    stages: tuple[Stage, ...]
    params: Mapping[str, ParamKind]


def _render_kind(group: MessageGroup) -> _Render:
    return "fallo" if group is MessageGroup.FAILURE else "error"


# Todos los códigos del catálogo y todos los avisos. La prueba se parametriza con ellos: si el
# código se eligiera al azar, Hypothesis repetiría unos pocos códigos en los 100 ejemplos, porque
# genera los ejemplos siguientes modificando los datos de los anteriores
_CODES: tuple[_Code, ...] = tuple(
    _Code(_render_kind(d.group), code, d.stages, d.params) for code, d in sorted(CATALOG.items())
) + tuple(_Code("aviso", code, (), d.params) for code, d in sorted(NOTICES.items()))


def _attempts(code: _Code) -> tuple[_Attempt, ...]:
    """Intentos posibles en `code`, con "ninguno" en la mitad de los casos."""
    attempts: list[_Attempt] = ["clave"]
    if any(kind not in _FREE_TEXT for kind in code.params.values()):
        attempts += ["parámetro", "parámetro"]
    if code.render == "fallo":
        attempts.append("etapa")
    none: list[_Attempt] = ["ninguno"] * len(attempts)
    return (*none, *attempts)


@st.composite
def _cases(draw: st.DrawFn, chosen: _Code) -> _Case:
    detail = draw(_details())
    attempt = draw(st.sampled_from(_attempts(chosen)))
    code, render, spec = chosen.code, chosen.render, chosen.params
    params = {name: draw(_VALID[kind]) for name, kind in spec.items()}

    if attempt == "parámetro":
        closed = [name for name, kind in spec.items() if kind not in _FREE_TEXT]
        name = draw(st.sampled_from(closed))
        fragment = draw(st.sampled_from(detail.fragments))
        valid = params[name]
        if spec[name] in _LISTS and draw(st.booleans()):
            items = list(cast(Sequence[object], valid))
            items.insert(draw(st.integers(0, len(items))), fragment)
            params[name] = items
        else:
            params[name] = fragment
    elif attempt == "clave":
        key = draw(st.sampled_from(_TECHNICAL_KEYS))
        params[key] = draw(st.sampled_from((detail.full, *detail.fragments)))

    stage: Stage | None = None
    if render == "fallo":
        others = [s for s in Stage if s not in chosen.stages]
        stage = draw(st.sampled_from(others if attempt == "etapa" else list(chosen.stages)))
    return _Case(code, render, stage, params, attempt, detail)


def _render(case: _Case) -> UserMessage | str:
    if case.render == "aviso":
        return render_notice(Notice(code=case.code, params=case.params))
    if case.render == "error":
        return render_error(case.code, case.params)
    assert case.stage is not None
    params = cast(Mapping[str, str | int | list[str]], case.params)
    return render_failure(Failure(reason=FailureReason(case.code), stage=case.stage, params=params))


# --- Oráculo ---

_USER_LIMIT = 40  # destino o idioma pedidos y Extensión_del_Nombre
_NAME_LIMIT = 64  # nombres de idiomas y de fuentes
# Palabras de letras separadas por un espacio: pistas, Estados_de_Tarea y "otra fuente instalada"
_WORDS = r"[^\W\d_]+(?: [^\W\d_]+)*"
_DURATION = r"[0-9]+ (?:min|s)(?: [0-9]+ s)?"


def _shown(text: str, limit: int) -> str:
    """Texto libre neutralizado: sin caracteres no imprimibles y recortado a `limit`."""
    shown = "".join(
        ch if ch.isprintable() else " " if unicodedata.category(ch) == "Zs" else "\ufffd"
        for ch in text
    )
    return shown if len(shown) <= limit else shown[:limit] + "…"


def _cited(ch: str) -> str:
    # Cada carácter se cita entre comillas con su punto de código, o solo con su punto de código
    code = re.escape(f"U+{ord(ch):04X}")
    return f"(?:«{re.escape(ch)}» \\({code}\\)|{code})"


def _pattern(kind: ParamKind, value: object) -> str:
    """Expresión que debe cumplir el parámetro `value` de tipo `kind` en el texto."""
    match kind:
        case ParamKind.NUMBER | ParamKind.FORMAT | ParamKind.CODEC | ParamKind.CATEGORY:
            return re.escape(str(value))
        case ParamKind.DURATION_MS:
            return _DURATION
        case ParamKind.TRACK | ParamKind.STATE:
            return _WORDS
        case ParamKind.FORMATS | ParamKind.PAGES:
            return re.escape(", ".join(str(item) for item in cast(Sequence[object], value)))
        case ParamKind.CHARACTERS:
            return ", ".join(_cited(ch) for ch in cast(Sequence[str], value))
        case ParamKind.USER_VALUE | ParamKind.EXTENSION:
            return re.escape(_shown(cast(str, value), _USER_LIMIT))
        case ParamKind.LANGUAGES:
            names = cast(Sequence[str], value)
            return re.escape(", ".join(_shown(name, _NAME_LIMIT) for name in names))
        case ParamKind.FONT_REPLACEMENTS:
            pairs = cast(Sequence[tuple[str, str | None]], value)
            return ", ".join(
                re.escape(_shown(font, _NAME_LIMIT))
                + " → "
                + (_WORDS if replacement is None else re.escape(_shown(replacement, _NAME_LIMIT)))
                for font, replacement in pairs
            )
    raise AssertionError(f"tipo de parámetro sin oráculo: {kind}")


def _outside_free_text(
    template: str, kinds: Mapping[str, ParamKind], params: Mapping[str, object], shown: str
) -> str:
    """Verifica que `shown` es `template` con sus campos reemplazados por los parámetros, y
    devuelve `shown` sin los textos libres ni los caracteres citados."""
    regex: list[str] = []
    set_aside: list[str] = []
    for index, (literal, field, _, _) in enumerate(string.Formatter().parse(template)):
        regex.append(re.escape(literal))
        if field is not None:
            group = f"campo{index}"
            regex.append(f"(?P<{group}>{_pattern(kinds[field], params[field])})")
            if kinds[field] in _SET_ASIDE:
                set_aside.append(group)
    found = re.fullmatch("".join(regex), shown, flags=re.DOTALL)
    assert found is not None, (template, shown)
    kept: list[str] = []
    position = 0
    for group in set_aside:
        start, end = found.span(group)
        kept.append(shown[position:start])
        position = end
    kept.append(shown[position:])
    return "".join(kept)


def _expected_stage(case: _Case) -> Stage | None:
    if case.render == "fallo":
        return case.stage
    if CATALOG[case.code].group is MessageGroup.RECEPTION:
        return Stage.RECEPTION  # Req 16.13
    return None


# Feature: file-converter, Property 66: Los mensajes al usuario tienen etapa, motivo y acción, sin detalle técnico  # noqa: E501
@pytest.mark.req("16.4", "16.5", "16.13")
@pytest.mark.parametrize("chosen", _CODES, ids=[chosen.code for chosen in _CODES])
@given(data=st.data())
def test_user_messages_have_stage_reason_and_action_without_technical_detail(
    chosen: _Code, data: st.DataObject
) -> None:
    case = data.draw(_cases(chosen), label="caso")
    event(f"intento: {case.attempt}")
    if case.attempt != "ninguno":
        with pytest.raises(InvalidMessageError):
            _render(case)
        return

    rendered = _render(case)
    if isinstance(rendered, str):
        notice = NOTICES[case.code]
        text = rendered
        kept = _outside_free_text(notice.template, notice.params, case.params, text)
    else:
        definition = CATALOG[case.code]
        stage = _expected_stage(case)
        assert rendered.code == case.code
        assert rendered.stage == stage
        assert stage is None or stage in definition.stages
        # Un motivo y una sola acción sugerida, no vacíos
        assert rendered.message.strip()
        assert rendered.action.strip()
        prefix = "" if stage is None else f"Etapa: {stage.value}. "
        text = rendered.text
        assert text == f"{prefix}Motivo: {rendered.message} Acción sugerida: {rendered.action}"
        message = _outside_free_text(
            definition.template, definition.params, case.params, rendered.message
        )
        action = _outside_free_text(
            definition.action, definition.params, case.params, rendered.action
        )
        kept = f"{prefix}Motivo: {message} Acción sugerida: {action}"
        assert kept.count("Etapa:") == (0 if stage is None else 1)
        assert kept.count("Motivo:") == 1
        assert kept.count("Acción sugerida:") == 1

    # Una sola línea, sin caracteres de control: una traza no se puede mostrar como tal
    assert text.isprintable(), text
    # El detalle del caso no aparece en ninguna parte, ni dentro de los textos libres
    assert case.detail.workspace not in text
    assert case.detail.pointer not in text
    # Lo que arma el Conversor no tiene rutas, llaves de plantilla, trazas ni stderr
    assert not set("/\\{}") & set(kept), kept
    for marker in _TRACE_MARKERS:
        assert marker not in kept, (marker, kept)
    for fragment in case.detail.fragments:
        assert fragment not in kept, fragment
