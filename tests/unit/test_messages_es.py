"""Pruebas de src/converter/messages_es.py: catálogo de códigos, plantillas de avisos, nombres de
los Estados_de_Tarea y validación de parámetros (Req 16.4, 16.5, 16.9, 16.13, 18.1).

Los códigos, etapas, estados HTTP y acciones sugeridas esperados se copian de las tablas de
"Error Handling" de design.md, para que un código mal escrito, una etapa faltante o una acción
cambiada en el módulo hagan fallar las pruebas.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from converter.messages_es import (
    CATALOG,
    NOTICES,
    STATE_LABELS,
    CancelReason,
    CodecTrack,
    FailureReason,
    InvalidMessageError,
    MessageGroup,
    NoticeCode,
    ParamKind,
    ReceptionError,
    RejectionCode,
    RequestError,
    UnknownCodeError,
    render_error,
    render_failure,
    render_notice,
    state_label,
)
from converter.models import Category, Failure, Notice, Stage, TaskState

_DET, _CONV, _TRANS, _DEL = Stage.DETECTION, Stage.CONVERSION, Stage.TRANSCRIPTION, Stage.DELIVERY
_RUNNING = {_CONV, _TRANS, _DEL}

# Código: (grupo, etapas, estado HTTP)
_EXPECTED: dict[str, tuple[MessageGroup, set[Stage], int | None]] = {
    # Errores de recepción
    "recepcion_excede_limite": (MessageGroup.RECEPTION, {Stage.RECEPTION}, 413),
    "recepcion_vacia": (MessageGroup.RECEPTION, {Stage.RECEPTION}, 400),
    "recepcion_sin_espacio": (MessageGroup.RECEPTION, {Stage.RECEPTION}, 507),
    "recepcion_error_escritura": (MessageGroup.RECEPTION, {Stage.RECEPTION}, 500),
    "recepcion_nombre_largo": (MessageGroup.RECEPTION, {Stage.RECEPTION}, 400),
    "recepcion_solicitud_invalida": (MessageGroup.RECEPTION, {Stage.RECEPTION}, 400),
    "recepcion_interrumpida": (MessageGroup.RECEPTION, {Stage.RECEPTION}, None),
    # Rechazos_de_Origen
    "formato_no_reconocido": (MessageGroup.FAILURE, {_DET}, None),
    "tipo_sin_destinos": (MessageGroup.FAILURE, {_DET}, None),
    "excede_limite_categoria": (MessageGroup.FAILURE, {_DET}, None),
    "imagen_animada": (MessageGroup.FAILURE, {_DET}, None),
    "excede_limite_pixeles": (MessageGroup.FAILURE, {_DET}, None),
    "docx_cifrado": (MessageGroup.FAILURE, {_DET}, None),
    "pdf_protegido": (MessageGroup.FAILURE, {_DET}, None),
    "docx_excede_limites_descompresion": (MessageGroup.FAILURE, {_DET}, None),
    # En las dos tablas de Motivos_de_Fallo
    "archivo_danado": (MessageGroup.FAILURE, {_DET, _CONV, _TRANS}, None),
    "error_interno": (MessageGroup.FAILURE, {_DET} | _RUNNING, None),
    # Fallos durante el procesamiento
    "codec_no_soportado": (MessageGroup.FAILURE, {_CONV, _TRANS}, None),
    "caracteres_no_representables": (MessageGroup.FAILURE, {_CONV}, None),
    "pdf_sin_texto": (MessageGroup.FAILURE, {_CONV}, None),
    "pagina_excede_pixeles": (MessageGroup.FAILURE, {_CONV}, None),
    "modelo_no_disponible": (MessageGroup.FAILURE, {_TRANS}, None),
    "tiempo_excedido": (MessageGroup.FAILURE, _RUNNING, None),
    "sin_espacio_proceso": (MessageGroup.FAILURE, _RUNNING, None),
    # Rechazos_de_Destino
    "destino_no_disponible": (MessageGroup.DESTINATION_REJECTION, set(), 422),
    "dimensiones_excedidas": (MessageGroup.DESTINATION_REJECTION, set(), 422),
    "video_sin_audio": (MessageGroup.DESTINATION_REJECTION, set(), 422),
    "transcripcion_excede_duracion": (MessageGroup.DESTINATION_REJECTION, set(), 422),
    "idioma_no_soportado": (MessageGroup.DESTINATION_REJECTION, set(), 422),
    # Errores de solicitudes sobre Tareas
    "tarea_no_disponible": (MessageGroup.TASK_REQUEST, set(), 404),
    "estado_no_permite": (MessageGroup.TASK_REQUEST, set(), 409),
    "confirmacion_requerida": (MessageGroup.TASK_REQUEST, set(), 409),
    "descarga_no_disponible": (MessageGroup.TASK_REQUEST, set(), 409),
    "resultado_expirado": (MessageGroup.TASK_REQUEST, set(), 410),
    "solicitud_invalida": (MessageGroup.TASK_REQUEST, set(), 400),
    # Motivos de cancelación
    "cancelada_por_usuario": (MessageGroup.CANCELLATION, set(), None),
    "sin_destino_en_retencion": (MessageGroup.CANCELLATION, set(), None),
}

# Acción sugerida de cada código en las tablas de "Error Handling" que tienen esa columna. Las
# tablas de solicitudes sobre Tareas y de cancelaciones no la tienen.
_RETRY_UPLOAD = "Envíe el archivo de nuevo."
_REMOVE_PASSWORD = "Quite la contraseña en el programa de origen y envíelo de nuevo."
_DESIGN_ACTIONS = {
    # Errores de recepción
    "recepcion_excede_limite": "Envíe un archivo más pequeño.",
    "recepcion_vacia": "Elija un archivo con contenido.",
    "recepcion_sin_espacio": "Intente más tarde, cuando terminen otras conversiones.",
    "recepcion_error_escritura": _RETRY_UPLOAD,
    "recepcion_nombre_largo": "Acorte el nombre y envíe el archivo de nuevo.",
    "recepcion_solicitud_invalida": "Recargue la página y envíe el archivo de nuevo.",
    "recepcion_interrumpida": _RETRY_UPLOAD,
    # Rechazos_de_Origen
    "formato_no_reconocido": "Verifique el archivo o guárdelo como texto UTF-8.",
    "tipo_sin_destinos": "Envíe un archivo de alguno de esos formatos.",
    "excede_limite_categoria": "Envíe un archivo más pequeño.",
    "imagen_animada": "Envíe una imagen estática.",
    "excede_limite_pixeles": "Reduzca la imagen y envíela de nuevo.",
    "docx_cifrado": _REMOVE_PASSWORD,
    "pdf_protegido": _REMOVE_PASSWORD,
    "docx_excede_limites_descompresion": (
        "Simplifique el documento o divídalo en varios y envíelos de nuevo."
    ),
    "archivo_danado": "Verifique que el archivo abra en otro programa y envíelo de nuevo.",
    "error_interno": "Vuelva a subir el archivo e intente la conversión de nuevo.",
    # Rechazos_de_Destino; el diseño escribe {lista} donde el catálogo usa {destinos}
    "destino_no_disponible": "Elija uno de los formatos de destino disponibles: {destinos}.",
    "dimensiones_excedidas": "Elija otro formato de destino.",
    "video_sin_audio": "Elija un formato de destino de video.",
    "transcripcion_excede_duracion": "Recorte el audio o elija otro destino.",
    "idioma_no_soportado": "Elija uno de los idiomas disponibles.",
    # Fallos durante el procesamiento
    "codec_no_soportado": (
        "Convierta el archivo a un códec común con otro programa y envíelo de nuevo."
    ),
    "caracteres_no_representables": "Elija HTML como destino o quite esos caracteres.",
    "pdf_sin_texto": "Elija PNG como destino para obtener las páginas como imágenes.",
    "pagina_excede_pixeles": "Elija TXT como destino o use un PDF con páginas más pequeñas.",
    "modelo_no_disponible": (
        "Avise a quien administra el Conversor; las demás conversiones siguen disponibles."
    ),
    "tiempo_excedido": "Intente con un archivo más corto o más pequeño.",
    "sin_espacio_proceso": "Intente más tarde.",
}

_EXPECTED_NOTICES = {
    "extension_no_coincide",
    "bits_reducidos",
    "recursos_externos_omitidos",
    "fuentes_reemplazadas",
    "comentarios_omitidos",
    "cambios_aplicados",
    "macros_omitidas",
    "paginas_sin_texto",
    "frecuencia_cambiada",
    "canales_mezclados",
    "pistas_no_usadas",
    "dimensiones_rellenadas",
    "sin_voz",
}

# Un valor válido de cada tipo de parámetro
_SAMPLES: dict[ParamKind, object] = {
    ParamKind.NUMBER: 7,
    ParamKind.DURATION_MS: 61_000,
    ParamKind.FORMAT: "PNG",
    ParamKind.FORMATS: ["JPG", "PDF"],
    ParamKind.CATEGORY: Category.IMAGE,
    ParamKind.CODEC: "h264",
    ParamKind.TRACK: CodecTrack.VIDEO,
    ParamKind.PAGES: [2, 5],
    ParamKind.CHARACTERS: ["字"],
    ParamKind.STATE: TaskState.FAILED,
    ParamKind.USER_VALUE: "xx",
    ParamKind.EXTENSION: ".txt",
    ParamKind.LANGUAGES: ["español", "inglés"],
    ParamKind.FONT_REPLACEMENTS: [("Calibri", "Carlito")],
}


def _sample_params(spec: Mapping[str, ParamKind]) -> dict[str, object]:
    return {name: _SAMPLES[kind] for name, kind in spec.items()}


# --- Catálogo ---


@pytest.mark.req("16.4", "16.9", "16.13")
def test_catalog_has_each_code_of_the_error_handling_tables() -> None:
    assert set(CATALOG) == set(_EXPECTED)
    for code, (group, stages, http_status) in _EXPECTED.items():
        definition = CATALOG[code]
        assert (definition.group, set(definition.stages), definition.http_status) == (
            group,
            stages,
            http_status,
        ), code


@pytest.mark.req("16.4", "16.9", "16.13")
def test_no_catalog_code_has_an_empty_action() -> None:
    for code, definition in CATALOG.items():
        action = definition.action
        assert action.strip(), code
        assert action == action.strip(), code
        # Una sola acción: una oración que termina en punto
        assert action.endswith(".") and ". " not in action, code


@pytest.mark.req("16.4", "16.9", "16.13")
def test_each_code_has_the_action_of_the_design_tables() -> None:
    without_column = {MessageGroup.TASK_REQUEST, MessageGroup.CANCELLATION}
    assert set(_DESIGN_ACTIONS) == {
        code for code, definition in CATALOG.items() if definition.group not in without_column
    }
    assert {code: CATALOG[code].action for code in _DESIGN_ACTIONS} == _DESIGN_ACTIONS


def test_code_enums_match_the_catalog_groups() -> None:
    enums = {
        MessageGroup.RECEPTION: ReceptionError,
        MessageGroup.FAILURE: FailureReason,
        MessageGroup.DESTINATION_REJECTION: RejectionCode,
        MessageGroup.TASK_REQUEST: RequestError,
        MessageGroup.CANCELLATION: CancelReason,
    }
    for group, codes in enums.items():
        assert {c.value for c in codes} == {
            code for code, (expected_group, _, _) in _EXPECTED.items() if expected_group is group
        }


@pytest.mark.req("16.4", "16.13")
@pytest.mark.parametrize("code", sorted(_EXPECTED))
def test_every_code_renders_with_its_stage_reason_and_one_action(code: str) -> None:
    definition = CATALOG[code]
    params = _sample_params(definition.params)
    if definition.group is MessageGroup.FAILURE:
        # Los parámetros de los Motivos_de_Fallo son números, textos y listas de textos
        failure_params = cast(Mapping[str, str | int | list[str]], params)
        messages = [
            render_failure(Failure(reason=FailureReason(code), stage=stage, params=failure_params))
            for stage in definition.stages
        ]
    else:
        messages = [render_error(code, params)]
    for message in messages:
        assert message.code == code
        assert message.message.strip() and message.action.strip()
        assert "{" not in message.message + message.action
        assert "}" not in message.message + message.action
        if definition.stages:
            assert message.stage in definition.stages
            assert message.text == (
                f"Etapa: {message.stage}. Motivo: {message.message} "
                f"Acción sugerida: {message.action}"
            )
        else:
            assert message.stage is None
            assert message.text == f"Motivo: {message.message} Acción sugerida: {message.action}"


@pytest.mark.parametrize(
    ("reason", "stage", "requirement_text"),
    [
        (FailureReason.DAMAGED, _CONV, "archivo dañado o ilegible"),  # Req 16.2
        (FailureReason.TIMED_OUT, _TRANS, "tiempo de procesamiento excedido"),  # Req 14.7
        (FailureReason.INTERNAL_ERROR, _DET, "error interno del Conversor"),  # Req 16.9
        (
            FailureReason.DECOMPRESSION_LIMITS,
            _DET,
            "documento demasiado complejo o potencialmente malicioso",  # Req 7.7
        ),
    ],
)
def test_failure_reasons_use_the_wording_of_the_requirements(
    reason: FailureReason, stage: Stage, requirement_text: str
) -> None:
    message = render_failure(Failure(reason=reason, stage=stage, params={}))
    assert message.message.casefold() == requirement_text.casefold() + "."


@pytest.mark.req("16.9")
def test_internal_error_action_is_to_upload_again_and_retry() -> None:
    message = render_failure(Failure(reason=FailureReason.INTERNAL_ERROR, stage=_CONV, params={}))
    assert message.action == "Vuelva a subir el archivo e intente la conversión de nuevo."


# --- Ejemplos de mensajes ---


@pytest.mark.req("1.3", "16.13")
def test_reception_error_carries_the_reception_stage_and_the_limit_in_mb() -> None:
    message = render_error(ReceptionError.TOO_LARGE, {"limite_mb": 500})
    assert message.stage is Stage.RECEPTION
    assert message.message == "El archivo excede el límite de 500 MB."
    assert message.action == "Envíe un archivo más pequeño."


@pytest.mark.req("3.4")
def test_target_unavailable_matches_the_design_example() -> None:
    message = render_error(
        "destino_no_disponible",
        {"destino": "MP3", "tipo_real": "PNG", "destinos": ["JPG", "WEBP", "PDF"]},
    )
    assert message.stage is None
    assert message.message == "El destino «MP3» no está disponible para archivos PNG."
    assert message.action == "Elija uno de los formatos de destino disponibles: JPG, WEBP, PDF."


@pytest.mark.req("2.7")
def test_type_without_targets_lists_the_source_formats() -> None:
    message = render_failure(
        Failure(
            reason=FailureReason.NO_TARGETS,
            stage=_DET,
            params={"tipo_real": "SRT", "formatos_origen": ["PNG", "MP3"]},
        )
    )
    assert "SRT" in message.message
    assert message.message.endswith("Formatos de origen admitidos: PNG, MP3.")


@pytest.mark.req("9.6", "10.8")
@pytest.mark.parametrize(
    ("track", "text"),
    [
        (CodecTrack.VIDEO, "El códec «vp6f» de la pista de video no está soportado."),
        (
            CodecTrack.DEFAULT_AUDIO,
            "El códec «vp6f» de la pista de audio predeterminada no está soportado.",
        ),
        (CodecTrack.AUDIO, "El códec «vp6f» del audio no está soportado."),
    ],
)
def test_unsupported_codec_names_the_codec_and_the_track(track: CodecTrack, text: str) -> None:
    failure = Failure(
        reason=FailureReason.UNSUPPORTED_CODEC,
        stage=_CONV,
        params={"codec": "vp6f", "pista": track},
    )
    assert render_failure(failure).message == text


@pytest.mark.req("6.11")
def test_unrepresentable_characters_are_cited_with_their_code_point() -> None:
    failure = Failure(
        reason=FailureReason.UNREPRESENTABLE_CHARACTERS,
        stage=_CONV,
        params={"caracteres": ["字", "\u0301", "\x01"]},
    )
    # La marca combinable y el carácter de control se citan solo por su punto de código
    assert render_failure(failure).message.endswith(
        "no puede representar: «字» (U+5B57), U+0301, U+0001."
    )


@pytest.mark.req("6.11")
@pytest.mark.parametrize(
    "characters",
    [
        [],  # vacío
        [chr(0x4E00 + i) for i in range(11)],  # más de 10
        ["a", "a"],  # repetidos
        ["ab"],  # no es un único carácter
        [" "],  # espacios, tabulaciones y saltos de línea no se citan
        ["\n"],
    ],
)
def test_unrepresentable_characters_reject_invalid_lists(characters: list[str]) -> None:
    failure = Failure(
        reason=FailureReason.UNREPRESENTABLE_CHARACTERS,
        stage=_CONV,
        params={"caracteres": characters},
    )
    with pytest.raises(InvalidMessageError):
        render_failure(failure)


@pytest.mark.req("8.11")
def test_page_pixel_limit_names_the_page_and_the_sizes() -> None:
    params: dict[str, str | int | list[str]] = {
        "pagina": 3,
        "ppp": 150,
        "ancho": 30_000,
        "alto": 40_000,
        "limite_pixeles": 100_000_000,
    }
    message = render_failure(
        Failure(reason=FailureReason.PAGE_PIXEL_LIMIT, stage=_CONV, params=params)
    )
    assert message.message == (
        "La página 3 es demasiado grande: rasterizada a 150 ppp mediría 30000 × 40000 píxeles "
        "y supera el límite de 100000000 píxeles (ancho × alto)."
    )


@pytest.mark.req("11.5")
@pytest.mark.parametrize(
    ("duration_ms", "shown"),
    [
        (3_600_001, "60 min 1 s"),  # hacia arriba: si supera el máximo, nunca se ve igual
        (3_601_000, "60 min 1 s"),
        (3_660_000, "61 min"),
        (3_600_000, "60 min"),
        (45_000, "45 s"),
        (0, "0 s"),
    ],
)
def test_transcription_duration_is_shown_in_minutes_and_seconds(
    duration_ms: int, shown: str
) -> None:
    message = render_error(
        RejectionCode.TRANSCRIPTION_TOO_LONG, {"duracion": duration_ms, "maximo_min": 60}
    )
    assert message.message == f"El audio dura {shown}; el máximo para transcribir es de 60 min."


@pytest.mark.req("14.11", "15.11", "18.1")
def test_state_errors_show_the_spanish_state_name() -> None:
    message = render_error(RequestError.STATE_FORBIDS, {"estado": TaskState.COMPLETED})
    assert "«Completada»" in message.message
    message = render_error(RequestError.DOWNLOAD_UNAVAILABLE, {"estado": "en_proceso"})
    assert message.message.startswith("La tarea está en estado «En proceso»")


@pytest.mark.req("11.9")
def test_user_values_are_shown_without_control_characters_and_truncated() -> None:
    message = render_error(
        RejectionCode.UNSUPPORTED_LANGUAGE,
        {"valor": "x\u202e\x07" + "y" * 100, "idiomas": ["español", "inglés"]},
    )
    expected_value = "x\ufffd\ufffd" + "y" * 37 + "…"
    assert message.message == (
        f"El idioma «{expected_value}» no está disponible. Idiomas disponibles: español, "
        "inglés, o detección automática."
    )


@pytest.mark.req("15.8")
def test_expired_result_says_to_send_the_file_again() -> None:
    message = render_error(RequestError.RESULT_EXPIRED)
    assert message.message == "El resultado expiró."
    assert message.action == "Envíe el archivo de nuevo para convertirlo."


@pytest.mark.req("14.8")
def test_retention_cancellation_names_the_retention_period() -> None:
    message = render_error(CancelReason.RETENTION_EXPIRED, {"retencion_min": 60})
    assert message.message == "No se eligió un formato de destino dentro de 60 minutos."


# --- Validación ---


@pytest.mark.req("16.4")
def test_failure_in_a_stage_of_another_table_is_rejected() -> None:
    for stage in (Stage.RECEPTION, Stage.DELIVERY):
        with pytest.raises(InvalidMessageError):
            render_failure(Failure(reason=FailureReason.DAMAGED, stage=stage, params={}))
    # Un Rechazo_de_Origen solo ocurre en la detección
    with pytest.raises(InvalidMessageError):
        render_failure(Failure(reason=FailureReason.ANIMATED_IMAGE, stage=_CONV, params={}))


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    "params",
    [
        {},  # falta
        {"limite_mb": 25, "detalle": "Traceback (most recent call last)"},  # sobra
        {"limite_mb": True},  # bool no es un número
        {"limite_mb": 0},  # no positivo
        {"limite_mb": "25"},  # texto
    ],
)
def test_parameters_must_match_the_declared_kinds(params: dict[str, object]) -> None:
    with pytest.raises(InvalidMessageError):
        render_error(ReceptionError.TOO_LARGE, params)


@pytest.mark.req("16.5")
@pytest.mark.parametrize(
    "codec",
    ["/var/lib/converter/storage/ws-0/source/original", "C:\\temp", "h 264", "", "_x", "x" * 65],
)
def test_codec_names_cannot_carry_paths_or_free_text(codec: str) -> None:
    failure = Failure(
        reason=FailureReason.UNSUPPORTED_CODEC,
        stage=_CONV,
        params={"codec": codec, "pista": CodecTrack.AUDIO},
    )
    with pytest.raises(InvalidMessageError) as raised:
        render_failure(failure)
    # El error nombra el parámetro, no el valor
    assert "'codec'" in str(raised.value)
    if codec:
        assert codec not in str(raised.value)


@pytest.mark.req("16.5")
@pytest.mark.parametrize("name", ["PNG/../x", "Open Doc", "é", ""])
def test_format_names_follow_a_closed_pattern(name: str) -> None:
    with pytest.raises(InvalidMessageError):
        render_error(
            RejectionCode.DIMENSIONS_EXCEEDED,
            {"ancho": 20_000, "alto": 10, "destino": name, "maximo": 16_383},
        )


def test_failure_codes_are_rendered_only_by_render_failure() -> None:
    with pytest.raises(InvalidMessageError):
        render_error(FailureReason.DAMAGED)


def test_unknown_codes_are_rejected() -> None:
    with pytest.raises(UnknownCodeError):
        render_error("no_existe")
    with pytest.raises(UnknownCodeError):
        render_notice(Notice(code="no_existe", params={}))


# --- Avisos ---


def test_notices_have_each_template_of_the_plan() -> None:
    assert set(NOTICES) == _EXPECTED_NOTICES == {code.value for code in NoticeCode}


@pytest.mark.parametrize("code", sorted(_EXPECTED_NOTICES))
def test_every_notice_renders(code: str) -> None:
    text = render_notice(Notice(code=code, params=_sample_params(NOTICES[code].params)))
    assert text.strip()
    assert "{" not in text and "}" not in text


@pytest.mark.req("2.6")
def test_extension_mismatch_names_the_extension_and_the_real_type() -> None:
    notice = Notice(
        code=NoticeCode.EXTENSION_MISMATCH, params={"extension": ".png", "tipo_real": "JPG"}
    )
    assert render_notice(notice) == (
        "La extensión «.png» del nombre no corresponde al formato detectado, JPG; el archivo se "
        "procesa como JPG."
    )


@pytest.mark.req("5.8")
def test_bit_reduction_notice() -> None:
    notice = Notice(code="bits_reducidos", params={})
    assert render_notice(notice) == "La profundidad de color se redujo de 16 a 8 bits por canal."


@pytest.mark.req("7.4")
def test_font_replacements_list_each_font_and_its_replacement() -> None:
    notice = Notice(
        code=NoticeCode.FONTS_REPLACED,
        params={"reemplazos": [("Calibri", "Carlito"), ("Wingdings", None)]},
    )
    assert render_notice(notice).endswith(
        "instaladas: Calibri → Carlito, Wingdings → otra fuente instalada."
    )


@pytest.mark.req("8.3")
def test_pages_without_text_list_every_page_number() -> None:
    notice = Notice(code=NoticeCode.PAGES_WITHOUT_TEXT, params={"paginas": [2, 5, 12]})
    assert render_notice(notice).endswith("Páginas sin texto: 2, 5, 12.")
    for pages in ([], [5, 2], [2, 2], [0]):
        with pytest.raises(InvalidMessageError):
            render_notice(Notice(code=NoticeCode.PAGES_WITHOUT_TEXT, params={"paginas": pages}))


@pytest.mark.req("9.3", "9.4", "10.4", "10.11")
@pytest.mark.parametrize(
    ("code", "params", "text"),
    [
        (
            NoticeCode.SAMPLE_RATE_CHANGED,
            {"origen_hz": 96_000, "resultado_hz": 48_000},
            "La frecuencia de muestreo cambió de 96000 Hz a 48000 Hz",
        ),
        (
            NoticeCode.CHANNELS_MIXED,
            {"canales_origen": 6, "canales_resultado": 2},
            "El audio tenía 6 canales y el resultado tiene 2 canales",
        ),
        (NoticeCode.UNUSED_TRACKS, {"cantidad": 2}, "Pistas de audio no usadas: 2."),
        (
            NoticeCode.DIMENSIONS_PADDED,
            {
                "ancho_origen": 1279,
                "alto_origen": 721,
                "ancho_resultado": 1280,
                "alto_resultado": 722,
            },
            "El video mide 1279 × 721 píxeles y el resultado mide 1280 × 722 píxeles",
        ),
    ],
)
def test_media_notices_report_the_values_of_the_case(
    code: NoticeCode, params: dict[str, object], text: str
) -> None:
    assert text in render_notice(Notice(code=code, params=params))


# --- Estados_de_Tarea ---


@pytest.mark.req("18.1")
def test_state_labels_in_spanish() -> None:
    assert {state.value: label for state, label in STATE_LABELS.items()} == {
        "recibida": "Recibida",
        "en_cola": "En cola",
        "en_proceso": "En proceso",
        "completada": "Completada",
        "fallida": "Fallida",
        "cancelada": "Cancelada",
    }
    assert state_label("en_cola") == "En cola"
    with pytest.raises(InvalidMessageError):
        state_label("pausada")
