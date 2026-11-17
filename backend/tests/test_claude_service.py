import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from models.timesheet import TimesheetRow
from services.claude_service import (
    ClaudeExtractionError,
    extract_with_claude,
    extract_with_claude_adaptive,
    normalize_text_with_claude,
)


def _usage(**overrides):
    return SimpleNamespace(**{
        "input_tokens": 900, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 100, **overrides,
    })


def _message(payload, stop_reason="end_turn", usage=None) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=usage or _usage(),
    )


def _sdk_client(*, result=None, side_effect=None) -> MagicMock:
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=result, side_effect=side_effect)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _api_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request, json={"error": {"message": "boom"}})
    return anthropic.APIStatusError("boom", response=response, body=None)


@pytest.fixture(autouse=True)
def configure_claude(monkeypatch):
    from services import claude_service as service

    monkeypatch.setattr(service.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(service.settings, "ANTHROPIC_MODEL", "claude-haiku-5-5")


def _extract(client, pdf=b"fake pdf"):
    with patch("services.claude_service._new_client", return_value=client):
        return asyncio.run(extract_with_claude(pdf))


def test_extract_success():
    client = _sdk_client(result=_message([
        {"data": "01/03/2024", "marcacoes": ["08:00", "17:00"], "ocorrencia_raw": None},
    ]))

    rows = _extract(client)

    assert len(rows) == 1
    assert rows[0].data == "01/03/2024"
    assert rows[0].marcacoes[0] == "08:00"


def test_extract_sends_the_pdf_as_a_document_block_to_the_configured_model():
    client = _sdk_client(result=_message([]))

    _extract(client, b"%PDF-1.4")

    kwargs = client.messages.create.await_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    document, prompt = kwargs["messages"][0]["content"]
    assert document["type"] == "document"
    assert document["source"]["media_type"] == "application/pdf"
    assert prompt["type"] == "text"
    assert "temperature" not in kwargs


def test_extract_raises_on_api_error():
    client = _sdk_client(side_effect=_api_error(400))

    with pytest.raises(ClaudeExtractionError, match="400"):
        _extract(client)


def test_extract_wraps_timeout_and_records_the_call_as_unmetered():
    from services import ai_usage

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = _sdk_client(side_effect=anthropic.APITimeoutError(request=request))

    with ai_usage.recording() as calls:
        with pytest.raises(ClaudeExtractionError):
            _extract(client)

    assert [call["metered"] for call in calls] == [False]


def test_extract_does_not_record_an_unreached_connection_failure():
    from services import ai_usage

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = _sdk_client(side_effect=anthropic.APIConnectionError(request=request))

    with ai_usage.recording() as calls:
        with pytest.raises(ClaudeExtractionError):
            _extract(client)

    assert calls == []


def test_extract_raises_on_invalid_json():
    with pytest.raises(ClaudeExtractionError):
        _extract(_sdk_client(result=_message("not valid json {{{")))


def test_extract_raises_on_refusal_but_still_meters_the_call():
    from services import ai_usage

    client = _sdk_client(result=_message("", stop_reason="refusal"))

    with ai_usage.recording() as calls:
        with pytest.raises(ClaudeExtractionError, match="declined"):
            _extract(client)

    assert len(calls) == 1 and calls[0]["metered"]


def test_extract_meters_cached_and_output_tokens():
    from services import ai_usage

    usage = _usage(input_tokens=100, cache_read_input_tokens=800, cache_creation_input_tokens=50, output_tokens=70)
    client = _sdk_client(result=_message([], usage=usage))

    with ai_usage.recording() as calls:
        _extract(client)

    assert calls[0]["provider"] == "claude"
    assert calls[0]["prompt_tokens"] == 950
    assert calls[0]["cached_tokens"] == 800
    assert calls[0]["output_tokens"] == 70


def test_extract_without_api_key_returns_nothing(monkeypatch):
    from services import claude_service as service

    monkeypatch.setattr(service.settings, "ANTHROPIC_API_KEY", "")

    assert asyncio.run(extract_with_claude(b"fake")) == []


def test_normalize_text_success():
    client = _sdk_client(result=_message([
        {"data": "01/03/2024", "marcacoes": ["08:00", "12:00"], "ocorrencia_raw": None},
    ]))

    with patch("services.claude_service._new_client", return_value=client):
        rows = asyncio.run(normalize_text_with_claude("some ocr text"))

    assert len(rows) == 1


def test_extract_with_occurrence():
    client = _sdk_client(result=_message([
        {"data": "05/03/2024", "marcacoes": [], "ocorrencia_raw": "FERIAS"},
    ]))

    rows = _extract(client)

    assert rows[0].ocorrencia_tipo == "ferias"
    assert rows[0].ocorrencia_raw == "FERIAS"


def test_extract_parses_records_payload():
    client = _sdk_client(result=_message({
        "records": [{"data": "08/08/2022", "entrada": "13:30", "saida": "23:02", "confidence": "high"}]
    }))

    rows = _extract(client)

    assert len(rows) == 1
    assert rows[0].data == "08/08/2022"
    assert rows[0].marcacoes == ["13:30", "23:02"]
    assert rows[0].ocr_confidence == "high"


def test_adaptive_extract_chunks_large_pdf_before_claude():
    rows = [TimesheetRow(data="01/03/2024", marcacoes=["08:00"])]
    reader = MagicMock()
    reader.pages = [object()] * 6

    with (
        patch("services.claude_service.pypdf.PdfReader", return_value=reader),
        patch(
            "services.claude_service._split_pdf_into_chunks",
            return_value=[b"chunk-1", b"chunk-2"],
        ) as split_mock,
        patch(
            "services.claude_service.extract_with_claude",
            AsyncMock(side_effect=[rows, []]),
        ) as claude_mock,
    ):
        result = asyncio.run(extract_with_claude_adaptive(b"full-pdf", chunk_size=5))

    split_mock.assert_called_once_with(b"full-pdf", 5, reader=reader)
    assert claude_mock.await_args_list[0].args == (b"chunk-1",)
    assert claude_mock.await_args_list[1].args == (b"chunk-2",)
    assert result == rows


def test_adaptive_extract_retries_failed_chunk_as_single_pages():
    rows = [TimesheetRow(data="01/03/2024", marcacoes=["08:00"])]
    reader = MagicMock()
    reader.pages = [object()] * 3

    with (
        patch("services.claude_service.pypdf.PdfReader", return_value=reader),
        patch(
            "services.claude_service._split_pdf_into_chunks",
            side_effect=[[b"chunk"], [b"page-1", b"page-2"]],
        ) as split_mock,
        patch(
            "services.claude_service.extract_with_claude",
            AsyncMock(side_effect=[
                ClaudeExtractionError("timeout"),
                rows,
                [],
            ]),
        ) as claude_mock,
    ):
        result = asyncio.run(extract_with_claude_adaptive(b"full-pdf", chunk_size=2))

    assert split_mock.call_count == 2
    assert claude_mock.await_args_list[0].args == (b"chunk",)
    assert claude_mock.await_args_list[1].args == (b"page-1",)
    assert claude_mock.await_args_list[2].args == (b"page-2",)
    assert result == rows

@pytest.mark.asyncio
async def test_adaptive_chunks_overlap_with_bounded_concurrency_and_ordered_rows(monkeypatch):
    from services import ai_usage, claude_service as service

    monkeypatch.setattr(service.settings, "ANTHROPIC_MAX_CONCURRENT_CHUNKS", 3)

    chunks = [bytes([index]) for index in range(7)]
    reader = MagicMock()
    reader.pages = [object()] * 14
    active = 0
    peak_active = 0
    completed = []

    async def extract(chunk, *, client=None):
        nonlocal active, peak_active
        index = chunk[0]
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.002 * (7 - index))
        ai_usage.record("claude", "test-model", "extract", {"total_tokens": index + 1})
        completed.append(index)
        active -= 1
        return [TimesheetRow(data=f"{index + 1:02d}/03/2024", marcacoes=["08:00"])]

    monkeypatch.setattr(service.pypdf, "PdfReader", lambda _: reader)
    monkeypatch.setattr(service, "_split_pdf_into_chunks", lambda *args, **kwargs: chunks)
    monkeypatch.setattr(service, "extract_with_claude", extract)

    with ai_usage.recording() as calls:
        rows = await service.extract_with_claude_adaptive(b"pdf")

    assert peak_active == 3
    assert completed != list(range(7))
    assert [row.data for row in rows] == [f"{index + 1:02d}/03/2024" for index in range(7)]
    assert len(calls) == 7
    assert sum(call["total_tokens"] for call in calls) == 28


@pytest.mark.asyncio
async def test_adaptive_waits_for_started_calls_before_propagating_unexpected_error(monkeypatch):
    from services import ai_usage, claude_service as service

    reader = MagicMock()
    reader.pages = [object()] * 4
    completed = asyncio.Event()

    async def extract(chunk, *, client=None):
        if chunk == b"bad":
            raise ValueError("invalid response")
        await asyncio.sleep(0.01)
        ai_usage.record("claude", "test-model", "extract", {"total_tokens": 9})
        completed.set()
        return []

    monkeypatch.setattr(service.pypdf, "PdfReader", lambda _: reader)
    monkeypatch.setattr(service, "_split_pdf_into_chunks", lambda *args, **kwargs: [b"bad", b"good"])
    monkeypatch.setattr(service, "extract_with_claude", extract)

    with ai_usage.recording() as calls:
        with pytest.raises(ValueError, match="invalid response"):
            await service.extract_with_claude_adaptive(b"pdf")
        assert completed.is_set()
        assert len(calls) == 1


def test_adaptive_reads_source_pdf_once():
    from io import BytesIO
    import pypdf
    from services import claude_service as service

    writer = pypdf.PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)

    with (
        patch.object(service.pypdf, "PdfReader", wraps=pypdf.PdfReader) as reader,
        patch.object(service, "extract_with_claude", AsyncMock(return_value=[])) as extract,
    ):
        assert asyncio.run(service.extract_with_claude_adaptive(output.getvalue())) == []

    assert reader.call_count == 1
    assert extract.await_count == 3


@pytest.mark.asyncio
async def test_adaptive_concurrent_fallback_preserves_partial_rows_and_page_order(monkeypatch):
    from services import claude_service as service

    reader = MagicMock()
    reader.pages = [object()] * 6
    chunks = {
        b"pdf": [b"first", b"second", b"third"],
        b"first": [b"page-1", b"page-2"],
        b"third": [b"page-5", b"page-6"],
    }
    responses = {
        b"first": ClaudeExtractionError("first chunk failed"),
        b"page-1": [TimesheetRow(data="01/03/2024", marcacoes=["08:00"])],
        b"page-2": ClaudeExtractionError("unreadable page"),
        b"second": [TimesheetRow(data="03/03/2024", marcacoes=["08:00"])],
        b"third": ClaudeExtractionError("third chunk failed"),
        b"page-5": ClaudeExtractionError("unreadable page"),
        b"page-6": [TimesheetRow(data="06/03/2024", marcacoes=["08:00"])],
    }

    async def extract(chunk, *, client=None):
        await asyncio.sleep(0)
        result = responses[chunk]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(service.pypdf, "PdfReader", lambda _: reader)
    monkeypatch.setattr(service, "_split_pdf_into_chunks", lambda pdf, *args, **kwargs: chunks[pdf])
    monkeypatch.setattr(service, "extract_with_claude", extract)

    rows = await service.extract_with_claude_adaptive(b"pdf")

    assert [row.data for row in rows] == ["01/03/2024", "03/03/2024", "06/03/2024"]
