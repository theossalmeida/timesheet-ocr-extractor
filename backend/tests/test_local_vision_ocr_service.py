import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from services.local_vision_ocr_service import (
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