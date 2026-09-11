import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.local_vision_ocr_service import (
    _looks_like_service_form_evidence,
    _page_confidence,
    run_guia_local,
    _lmstudio_base_url,
    _call_lmstudio,
    _resolved_provider,
    _best_template_name,
    _guia_records_to_rows,
    _records_from_payload,
    _rows_from_payload,
    _response_to_json,
    LocalVisionOCRError,
)


def test_records_from_payload_keeps_first_record_per_date():
    payload = {
        "records": [
            {"data": "20/12/2023", "entrada": "07:50", "saida": "16:49"},
            {"data": "20/12/2023", "entrada": "08:30", "saida": "17:00"},
            {"data": "21/12/2023", "entrada": "07:55", "saida": "16:50"},
        ]
    }

    assert _records_from_payload(payload) == [
        {"data": "20/12/2023", "entrada": "07:50", "saida": "16:49"},
        {"data": "21/12/2023", "entrada": "07:55", "saida": "16:50"},
    ]


def test_records_from_payload_requires_service_evidence_when_requested():
    payload = {
        "records": [
            {
                "data": "08/08/2022",
                "entrada": "13:30",
                "saida": "23:02",
                "data_source": "DATA field",
                "entrada_source": "INICIO/TRABALHO field",
                "saida_source": "TERMINO/TRABALHO field",
                "confidence": "medium",
            },
            {
                "data": "08/07/2019",
                "entrada": "06:30",
                "saida": "23:08",
                "data_source": "signature date",
                "entrada_source": "earliest time",
                "saida_source": "latest time",
                "confidence": "high",
            },
        ]
    }

    assert _records_from_payload(payload, require_service_evidence=True) == [
        {"data": "08/08/2022", "entrada": "13:30", "saida": "23:02"},
    ]


def test_records_from_payload_keeps_low_confidence_service_record():
    payload = {
        "records": [
            {
                "data": "08/08/2022",
                "entrada": "13:30",
                "saida": "23:02",
                "data_source": "DATA field",
                "entrada_source": "INICIO/TRABALHO field",
                "saida_source": "TERMINO/TRABALHO field",
                "confidence": "low",
            },
        ]
    }

    assert _records_from_payload(payload, require_service_evidence=True, include_meta=True) == [
        {
            "data": "08/08/2022",
            "entrada": "13:30",
            "saida": "23:02",
            "_confidence_score": 0,
            "_confidence": "low",
            "_ocr_warning": "Baixa confianca OCR: conferir esta linha no PDF original.",
        }
    ]


def test_records_from_payload_can_include_confidence_metadata():
    payload = {
        "records": [
            {
                "data": "08/08/2022",
                "entrada": "13:30",
                "saida": "23:02",
                "data_source": "DATA field",
                "entrada_source": "INICIO/TRABALHO field",
                "saida_source": "TERMINO/TRABALHO field",
                "confidence": "high",
            },
        ]
    }

    assert _records_from_payload(
        payload,
        require_service_evidence=True,
        include_meta=True,
    ) == [
        {
            "data": "08/08/2022",
            "entrada": "13:30",
            "saida": "23:02",
            "_confidence_score": 3,
            "_confidence": "high",
        },
    ]


def test_best_template_prefers_more_pages_then_confidence():
    scores = {
        "top_wide": {"pages": 2, "confidence": 4},
        "top_left": {"pages": 1, "confidence": 3},
        "body_wide": {"pages": 2, "confidence": 5},
    }

    assert _best_template_name(scores) == "body_wide"


def test_best_template_returns_none_on_exact_tie():
    scores = {
        "top_wide": {"pages": 2, "confidence": 4},
        "body_wide": {"pages": 2, "confidence": 4},
    }

    assert _best_template_name(scores) is None


def test_guia_records_to_rows_maps_entrada_saida_to_cartao_marks():
    rows = _guia_records_to_rows([
        {"data": "20/12/2023", "entrada": "07:50", "saida": "16:49"},
    ])

    assert len(rows) == 1
    assert rows[0].data == "20/12/2023"
    assert rows[0].marcacoes == ["07:50", "16:49"]

def test_resolved_provider_detects_lmstudio_port(monkeypatch):
    from services import local_vision_ocr_service as service

    monkeypatch.setattr(service.settings, "LOCAL_VISION_OCR_PROVIDER", "auto")
    monkeypatch.setattr(service.settings, "LOCAL_VISION_OCR_BASE_URL", "http://127.0.0.1:1234")

    assert _resolved_provider() == "lmstudio"
    assert _lmstudio_base_url() == "http://127.0.0.1:1234/v1"


def test_call_lmstudio_uses_iterable_message_content(monkeypatch):
    from services import local_vision_ocr_service as service

    monkeypatch.setattr(service.settings, "LOCAL_VISION_OCR_BASE_URL", "http://127.0.0.1:1234")
    monkeypatch.setattr(service.settings, "LOCAL_VISION_OCR_MODEL", "vision-model")
    monkeypatch.setattr(service.settings, "LOCAL_VISION_OCR_TIMEOUT_SECONDS", 5.0)

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": '{"rows": []}'}}]
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        assert asyncio.run(_call_lmstudio("OCR prompt", "image-bytes")) == {"rows": []}

    payload = mock_client.post.await_args.kwargs["json"]
    messages = payload["messages"]

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert isinstance(messages[0]["content"], list)
    assert messages[0]["content"][0]["type"] == "text"
    assert messages[0]["content"][0]["text"].startswith("Return strict JSON only")
    assert "OCR prompt" in messages[0]["content"][0]["text"]
    assert messages[0]["content"][1]["type"] == "image_url"
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "local_vision_ocr_response"
    schema = response_format["json_schema"]["schema"]
    assert set(schema["properties"]) == {"rows", "records"}


def test_response_to_json_includes_preview_for_non_json_text():
    try:
        _response_to_json("I can read the page, but no table is visible here.")
    except LocalVisionOCRError as exc:
        assert "local vision model did not return JSON: I can read the page" in str(exc)
    else:
        raise AssertionError("Expected LocalVisionOCRError")

def test_rows_from_payload_parses_raw_ocr_text():
    rows = _records_from_payload({"_raw_text": "01/03/2024 Segunda 08:00 17:00"})

    assert rows == []

    from services.local_vision_ocr_service import _rows_from_payload

    timesheet_rows = _rows_from_payload({"_raw_text": "01/03/2024 Segunda 08:00 17:00"})
    assert len(timesheet_rows) == 1
    assert timesheet_rows[0].data == "01/03/2024"
    assert timesheet_rows[0].marcacoes == ["08:00", "17:00"]


def test_call_lmstudio_returns_raw_text_payload_when_model_ignores_json(monkeypatch):
    from services import local_vision_ocr_service as service

    monkeypatch.setattr(service.settings, "LOCAL_VISION_OCR_BASE_URL", "http://127.0.0.1:1234")
    monkeypatch.setattr(service.settings, "LOCAL_VISION_OCR_MODEL", "vision-model")
    monkeypatch.setattr(service.settings, "LOCAL_VISION_OCR_TIMEOUT_SECONDS", 5.0)

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": "01/03/2024 Segunda 08:00 17:00"}}]
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        payload = asyncio.run(_call_lmstudio("OCR prompt", "image-bytes"))

    assert payload == {"_raw_text": "01/03/2024 Segunda 08:00 17:00"}

def test_rows_from_payload_converts_single_service_records():
    rows = _rows_from_payload({
        "rows": [],
        "records": [
            {"data": "08/08/2022", "entrada": "13:30", "saida": "23:02"},
        ],
    })

    assert len(rows) == 1
    assert rows[0].data == "08/08/2022"
    assert rows[0].marcacoes == ["13:30", "23:02"]

# ── guia probe gate ──────────────────────────────────────────────────────────

def _record(confidence_score, *, entrada="14:50", saida="22:07", data="01/07/2021"):
    return {
        "data": data, "entrada": entrada, "saida": saida,
        "_confidence_score": confidence_score,
    }


def test_page_confidence_grades_the_model_self_report():
    assert _page_confidence(_record(3)) == 1.0            # high
    assert _page_confidence(_record(2)) == pytest.approx(2 / 3)   # medium
    assert _page_confidence(_record(1)) == pytest.approx(1 / 3)   # unstated
    assert _page_confidence(_record(0)) == 0.0            # low


def test_page_confidence_rejects_an_unread_page():
    assert _page_confidence(None) == 0.0


def test_page_confidence_rejects_a_confident_but_incomplete_record():
    """A form read as "high" with no exit time is still unusable."""

    assert _page_confidence(_record(3, saida=None)) == 0.0
    assert _page_confidence(_record(3, entrada=None)) == 0.0
    assert _page_confidence(_record(3, data=None)) == 0.0


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _probe(monkeypatch, page_records, *, pages=2):
    """Drive run_guia_local with a fixed template and per-page records."""
    import services.local_vision_ocr_service as service

    monkeypatch.setattr(service, "is_local_vision_ocr_configured", lambda: True)
    monkeypatch.setattr(service, "_page_count", lambda pdf_bytes: pages)
    monkeypatch.setattr(
        service, "_render_crop_page_images",
        lambda pdf_bytes, page_indices=None: [object() for _ in (page_indices or range(pages))],
    )
    monkeypatch.setattr(
        service, "_select_service_crop_template",
        AsyncMock(return_value=(service._SERVICE_CROP_TEMPLATES[0], True)),
    )
    queue = list(page_records)
    monkeypatch.setattr(
        service, "_service_form_record_from_template",
        AsyncMock(side_effect=lambda image, template, **_: ([queue.pop(0)] if queue and queue[0] else [], True)),
    )


@pytest.mark.anyio
async def test_guia_probe_passes_when_every_probed_page_is_high(monkeypatch):
    _probe(monkeypatch, [_record(3), _record(3)])

    run = await run_guia_local(b"pdf")

    assert run.passed is True
    assert run.confidence == 1.0
    assert len(run.outcomes) == 2


@pytest.mark.anyio
async def test_guia_probe_fails_when_one_probed_page_is_only_medium(monkeypatch):
    _probe(monkeypatch, [_record(3), _record(2)])

    run = await run_guia_local(b"pdf")

    assert run.passed is False
    assert run.confidence == pytest.approx(5 / 6)
    assert run.outcomes == []
    assert "83%" in run.reason


@pytest.mark.anyio
async def test_guia_probe_fails_when_a_probed_page_is_unreadable(monkeypatch):
    _probe(monkeypatch, [_record(3), None])

    run = await run_guia_local(b"pdf")

    assert run.passed is False
    assert run.confidence == 0.5


@pytest.mark.anyio
async def test_guia_probe_covers_a_single_page_document(monkeypatch):
    """The example guia is one page, so the probe is the whole document."""

    _probe(monkeypatch, [_record(3)], pages=1)

    run = await run_guia_local(b"pdf")

    assert run.passed is True
    assert [outcome.page_number for outcome in run.outcomes] == [1]


@pytest.mark.anyio
async def test_guia_probe_is_skipped_when_local_vision_is_not_configured(monkeypatch):
    import services.local_vision_ocr_service as service

    monkeypatch.setattr(service, "is_local_vision_ocr_configured", lambda: False)

    run = await run_guia_local(b"pdf")

    assert run.passed is False
    assert run.reason == "local vision OCR is not configured"


# ── which crop wins, and which fields count as evidence ──────────────────────

def test_tied_crops_that_read_the_same_values_pick_one():
    """A one-page guia samples one page, so nearly every crop ties."""

    scores = {
        "top_wide": {"pages": 1, "confidence": 3},
        "body_wide": {"pages": 1, "confidence": 3},
    }
    keys = {"top_wide": [("01/07/2021", "14:50", "22:07")],
            "body_wide": [("01/07/2021", "14:50", "22:07")]}

    assert _best_template_name(scores, keys) == "top_wide"  # widest crop wins


def test_tied_crops_that_disagree_still_select_nothing():
    scores = {
        "top_wide": {"pages": 1, "confidence": 3},
        "body_wide": {"pages": 1, "confidence": 3},
    }
    keys = {"top_wide": [("01/07/2021", "14:50", "22:07")],
            "body_wide": [("28/08/2026", "13:25", "13:25")]}

    assert _best_template_name(scores, keys) is None


def test_service_evidence_accepts_the_papeleta_start_column():
    """This form labels its start time "HORA ESCALA / GARAGEM - PONTO"."""

    assert _looks_like_service_form_evidence({
        "data_source": "DATA field",
        "entrada_source": "HORA ESCALA / GARAGEM - PONTO",
        "saida_source": "TERMINO DO TRABALHO",
    })


def test_service_evidence_rejects_a_record_read_off_the_signature():
    """The bug: the signing date and time read as the work date and punches."""

    assert not _looks_like_service_form_evidence({
        "data_source": "signature footer",
        "entrada_source": "assinatura eletronica",
        "saida_source": "assinatura eletronica",
    })


@pytest.mark.asyncio
async def test_service_form_success_skips_full_page_rendering(monkeypatch):
    from services import local_vision_ocr_service as service

    monkeypatch.setattr(service, "is_local_vision_ocr_configured", lambda: True)
    monkeypatch.setattr(service, "_render_crop_page_images", MagicMock(return_value=[object()]))
    resize = MagicMock()
    monkeypatch.setattr(service, "_resize_to_dpi", resize)
    monkeypatch.setattr(service, "_select_service_crop_template", AsyncMock(
        return_value=(service._SERVICE_CROP_TEMPLATES[0], True)
    ))
    monkeypatch.setattr(service, "_service_form_record_from_template", AsyncMock(
        return_value=([{"data": "01/03/2024", "entrada": "08:00", "saida": "17:00"}], True)
    ))

    rows = await service.extract_timesheet_rows_local_vision(b"pdf")

    assert [row.marcacoes for row in rows] == [["08:00", "17:00"]]
    resize.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("dpi", [300, 400])
async def test_full_page_fallback_downscales_crop_images_instead_of_rerendering(monkeypatch, dpi):
    """crop_images are rendered at max(DPI, 300). When DPI >= 300 that resize is
    a no-op, so the full-page fallback must reuse crop_images instead of
    re-rendering the whole PDF a second time."""
    from services import local_vision_ocr_service as service

    crop_image = object()
    resized_image = object()
    monkeypatch.setattr(service, "is_local_vision_ocr_configured", lambda: True)
    monkeypatch.setattr(service.settings, "LOCAL_VISION_OCR_DPI", dpi)
    monkeypatch.setattr(service, "_render_crop_page_images", MagicMock(return_value=[crop_image]))
    resize = MagicMock(return_value=resized_image)
    monkeypatch.setattr(service, "_resize_to_dpi", resize)
    render_full = MagicMock()
    monkeypatch.setattr(service, "_render_full_page_images", render_full)
    monkeypatch.setattr(service, "_select_service_crop_template", AsyncMock(return_value=(None, False)))
    encode = MagicMock(return_value="image")
    monkeypatch.setattr(service, "_image_to_base64", encode)
    monkeypatch.setattr(service, "_call_vision_model", AsyncMock(return_value={
        "rows": [{"data": "01/03/2024", "marcacoes": ["08:00", "17:00"]}]
    }))

    rows = await service.extract_timesheet_rows_local_vision(b"pdf")

    assert [row.marcacoes for row in rows] == [["08:00", "17:00"]]
    resize.assert_called_once_with(crop_image, max(dpi, 300), dpi)
    encode.assert_called_once_with(resized_image)
    render_full.assert_not_called()


@pytest.mark.asyncio
async def test_full_page_fallback_rerenders_natively_below_300_dpi(monkeypatch):
    """When LOCAL_VISION_OCR_DPI < 300, reusing the 300dpi-preprocessed
    crop_images (downscaled) would not match a native render+preprocess at the
    configured DPI, so the full-page fallback must render natively instead."""
    from services import local_vision_ocr_service as service

    crop_image = object()
    native_image = object()
    monkeypatch.setattr(service, "is_local_vision_ocr_configured", lambda: True)
    monkeypatch.setattr(service.settings, "LOCAL_VISION_OCR_DPI", 200)
    monkeypatch.setattr(service, "_render_crop_page_images", MagicMock(return_value=[crop_image]))
    resize = MagicMock()
    monkeypatch.setattr(service, "_resize_to_dpi", resize)
    render_full = MagicMock(return_value=[native_image])
    monkeypatch.setattr(service, "_render_full_page_images", render_full)
    monkeypatch.setattr(service, "_select_service_crop_template", AsyncMock(return_value=(None, False)))
    encode = MagicMock(return_value="image")
    monkeypatch.setattr(service, "_image_to_base64", encode)
    monkeypatch.setattr(service, "_call_vision_model", AsyncMock(return_value={
        "rows": [{"data": "01/03/2024", "marcacoes": ["08:00", "17:00"]}]
    }))

    rows = await service.extract_timesheet_rows_local_vision(b"pdf")

    assert [row.marcacoes for row in rows] == [["08:00", "17:00"]]
    render_full.assert_called_once_with(b"pdf")
    resize.assert_not_called()
    encode.assert_called_once_with(native_image)
