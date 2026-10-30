"""Nombre_Base, Extensión_del_Nombre y Normalización_de_Nombre (Req 15.2 a 15.7, 15.10, 15.12, 8.8).

- `split_name` divide el Nombre_Original en su último punto: lo anterior es el Nombre_Base y el
  punto con lo que lo sigue es la Extensión_del_Nombre, siempre que lo siga al menos un carácter.
- `normalize_base` aplica la Normalización_de_Nombre en el orden de Req 15.12: reemplaza cada
  Carácter_No_Permitido por "_", trunca a 200 bytes en UTF-8 por caracteres completos y, si el
  resultado queda vacío, usa "resultado". La salida no tiene Caracteres_No_Permitidos, no está
  vacía y ocupa 200 bytes o menos, por lo que aplicarla otra vez no la cambia (Req 15.7).
- `result_name` arma el nombre de descarga del Archivo_Resultado y `page_entry_name` el de cada
  imagen dentro del ZIP de PDF a PNG.
"""

from __future__ import annotations

from typing import Literal, Protocol

# Carácter_No_Permitido: separadores y comodines de los sistemas de archivos habituales y los
# caracteres de control U+0000 a U+001F y U+007F
FORBIDDEN: frozenset[str] = frozenset('/\\:*?"<>|') | {chr(c) for c in range(0x20)} | {"\x7f"}

REPLACEMENT_CHAR = "_"
MAX_BASE_BYTES = 200  # D12: deja lugar a la extensión dentro de los 255 bytes por nombre
EMPTY_BASE = "resultado"
ZIP_EXTENSION = ".zip"
PAGE_EXTENSION = ".png"

ResultKind = Literal["single", "zip"]


class HasPrimaryExtension(Protocol):
    """Lo que `result_name` necesita del Formato_de_Destino; `FormatDef` lo cumple."""

    @property
    def primary_extension(self) -> str:  # Extensión_Principal, p. ej. ".jpg"
        ...


def split_name(original: str) -> tuple[str, str | None]:
    """Divide el Nombre_Original en Nombre_Base y Extensión_del_Nombre en su último punto.

    "informe.final.docx" da ("informe.final", ".docx"); "notas" da ("notas", None);
    ".png" da ("", ".png"); "notas." da ("notas", None).
    """
    base, dot, after = original.rpartition(".")
    if not dot:
        return original, None
    return base, (dot + after) if after else None


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Devuelve el prefijo más largo de caracteres completos que ocupa `max_bytes` o menos."""
    if max_bytes < 0:
        raise ValueError(f"max_bytes debe ser 0 o mayor: {max_bytes}")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # El corte puede caer dentro de un carácter: "ignore" descarta solo esa secuencia incompleta
    # final, porque el resto del prefijo es UTF-8 válido
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def normalize_base(base: str) -> str:
    """Aplica la Normalización_de_Nombre al Nombre_Base, en el orden de Req 15.12."""
    replaced = "".join(REPLACEMENT_CHAR if ch in FORBIDDEN else ch for ch in base)  # Req 15.3
    truncated = truncate_utf8(replaced, MAX_BASE_BYTES)  # Req 15.5
    return truncated or EMPTY_BASE  # Req 15.4


def result_name(original: str, target: HasPrimaryExtension, kind: ResultKind) -> str:
    """Nombre de descarga del Archivo_Resultado (Req 15.2, 15.10).

    Con `kind="single"` termina en la Extensión_Principal del Formato_de_Destino; con
    `kind="zip"` (PDF a PNG de más de una página) termina en ".zip".
    """
    base, _ = split_name(original)
    extension = ZIP_EXTENSION if kind == "zip" else target.primary_extension
    return normalize_base(base) + extension


def page_entry_name(normalized_base: str, page: int, total: int) -> str:
    """Nombre de la imagen de la página `page` de `total` dentro del ZIP (Req 8.8).

    El Número_de_Página se completa con ceros a la izquierda hasta la cantidad de dígitos de
    `total`: con 12 páginas, de "informe_01.png" a "informe_12.png".
    """
    if not 1 <= page <= total:
        raise ValueError(f"page debe estar entre 1 y total ({total}): {page}")
    return f"{normalized_base}_{page:0{len(str(total))}d}{PAGE_EXTENSION}"
