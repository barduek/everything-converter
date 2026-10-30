"""Idiomas_Soportados del Modelo_de_Transcripción, con sus nombres en español (Req 11.3, 11.9,
11.10, 13.8).

`LanguageCatalog` lee `languages.es.json`, que se genera al construir la Imagen_de_Contenedor a
partir de la lista de idiomas que declara el propio Modelo_de_Transcripción. El archivo es una
lista JSON, en el orden en que se ofrecen los idiomas, de objetos con exactamente dos claves:

```json
[{"code": "es", "name": "español"}, {"code": "en", "name": "inglés"}]
```

- `code` es el código del idioma tal como lo recibe el Transcriptor: de 2 a 3 letras ASCII en
  minúsculas (por ejemplo "es", "pt", "haw"). Cada código aparece una sola vez y ninguno es
  "auto", que es la Detección_Automática_de_Idioma.
- `name` es el nombre en español que muestra la Interfaz_de_Usuario: un texto imprimible, no
  vacío y de hasta `NAME_TEXT_LIMIT` caracteres, para que los mensajes lo citen completo.
- Una lista no vacía incluye el español, el Idioma_de_Transcripción predeterminado (Req 11.4,
  11.10).

Si el archivo no existe, el catálogo queda vacío: no hay Modelo_de_Transcripción ni
Idiomas_Soportados. Es el caso del MVP, que no incluye la transcripción. Un archivo que existe
pero no cumple estas reglas lanza `LanguageCatalogError`, porque es un error del build.

`resolve` da el Idioma_de_Transcripción que corresponde al valor que envió el usuario al
confirmar un Par_de_Transcripción: el español si no eligió (Req 11.10), "auto" para la
Detección_Automática_de_Idioma, el código si es un Idioma_Soportado y None en cualquier otro
caso, que el Gestor_de_Tareas rechaza con `idioma_no_soportado` (Req 11.9, 13.8). La
comparación es exacta: "ES" o " es" no son Idiomas_Soportados. El catálogo no descarga nada ni
consulta otros servicios; solo lee ese archivo local (Req 13.8).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from converter.messages_es import NAME_TEXT_LIMIT

# Ubicación de languages.es.json en la Imagen_de_Contenedor
DEFAULT_LANGUAGES_PATH = Path("/opt/converter/languages.es.json")

AUTO_LANGUAGE = "auto"  # Detección_Automática_de_Idioma
DEFAULT_LANGUAGE = "es"  # español, si el usuario no elige un idioma (Req 11.10)

_CODE = re.compile(r"[a-z]{2,3}")  # se usa con `fullmatch`
_KEYS = frozenset({"code", "name"})


class LanguageCatalogError(ValueError):
    """`languages.es.json` no se puede leer o no cumple el formato del catálogo."""


@dataclass(frozen=True, slots=True)
class Language:
    """Idioma_Soportado: código del Transcriptor y nombre en español."""

    code: str  # p. ej. "pt"
    name: str  # p. ej. "portugués"


def _check(language: object, position: int) -> Language:
    where = f"el idioma {position}"
    if not isinstance(language, Language):
        raise LanguageCatalogError(f"{where} no es un Language")
    if not isinstance(language.code, str) or _CODE.fullmatch(language.code) is None:
        raise LanguageCatalogError(f"{where}: el código debe tener de 2 a 3 letras minúsculas")
    if language.code == AUTO_LANGUAGE:
        raise LanguageCatalogError(f"{where}: el código «auto» es la detección automática")
    name = language.name
    if not isinstance(name, str) or not name.strip() or not name.isprintable():
        raise LanguageCatalogError(f"{where}: el nombre debe ser un texto imprimible no vacío")
    if len(name) > NAME_TEXT_LIMIT:
        raise LanguageCatalogError(f"{where}: el nombre supera los {NAME_TEXT_LIMIT} caracteres")
    return language


def _from_json(document: object) -> list[Language]:
    if not isinstance(document, list):
        raise LanguageCatalogError("el catálogo de idiomas debe ser una lista JSON")
    languages: list[Language] = []
    for position, item in enumerate(document, start=1):
        if not isinstance(item, dict) or set(item) != _KEYS:
            raise LanguageCatalogError(
                f"el idioma {position} debe ser un objeto con las claves «code» y «name»"
            )
        languages.append(Language(code=item["code"], name=item["name"]))
    return languages


class LanguageCatalog:
    """Idiomas_Soportados en el orden en que se ofrecen; inmutable."""

    __slots__ = ("_by_code", "_languages")

    def __init__(self, languages: Iterable[Language] = ()) -> None:
        """Lanza `LanguageCatalogError` si un idioma no cumple el formato, un código se repite
        o una lista no vacía no incluye el español."""
        checked = tuple(_check(language, n) for n, language in enumerate(languages, start=1))
        by_code: dict[str, Language] = {}
        for language in checked:
            if language.code in by_code:
                raise LanguageCatalogError(f"el código «{language.code}» está repetido")
            by_code[language.code] = language
        if checked and DEFAULT_LANGUAGE not in by_code:
            raise LanguageCatalogError("el catálogo de idiomas debe incluir el español («es»)")
        self._languages = checked
        self._by_code: Mapping[str, Language] = MappingProxyType(by_code)

    @classmethod
    def load(cls, path: Path = DEFAULT_LANGUAGES_PATH) -> LanguageCatalog:
        """Lee el catálogo de `path`; vacío si el archivo no existe.

        Lanza `LanguageCatalogError` si el archivo existe y no se puede leer, no es JSON en
        UTF-8 o no cumple el formato del catálogo.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except (OSError, UnicodeDecodeError) as error:
            raise LanguageCatalogError(f"no se pudo leer {path}") from error
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            raise LanguageCatalogError(f"{path} no es JSON válido") from error
        return cls(_from_json(document))

    @property
    def languages(self) -> tuple[Language, ...]:
        """Idiomas_Soportados, en orden."""
        return self._languages

    def codes(self) -> tuple[str, ...]:
        """Códigos de los Idiomas_Soportados, en orden."""
        return tuple(language.code for language in self._languages)

    def names(self) -> tuple[str, ...]:
        """Nombres en español de los Idiomas_Soportados, en orden."""
        return tuple(language.name for language in self._languages)

    def name(self, code: str) -> str | None:
        """Nombre en español del idioma `code`, o None si no es un Idioma_Soportado."""
        language = self._by_code.get(code) if isinstance(code, str) else None
        return None if language is None else language.name

    def resolve(self, value: str | None) -> str | None:
        """Idioma_de_Transcripción para `value`, o None si no es una opción válida.

        None da el español (Req 11.10) y "auto" la Detección_Automática_de_Idioma; un código
        de Idioma_Soportado se devuelve tal cual (Req 11.9).
        """
        if value is None:
            return DEFAULT_LANGUAGE
        if value == AUTO_LANGUAGE:
            return AUTO_LANGUAGE
        return value if value in self else None

    def __contains__(self, code: object) -> bool:
        return isinstance(code, str) and code in self._by_code

    def __len__(self) -> int:
        return len(self._languages)

    def __repr__(self) -> str:
        return f"LanguageCatalog({list(self.codes())!r})"
