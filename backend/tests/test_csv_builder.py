from models.timesheet import ExtractionResult, TimesheetRow
from services.csv_builder import build_csv


def _result(*rows):
    return ExtractionResult(rows=list(rows), provider="pdfplumber", pdf_type="native", warnings=[], total_rows=len(rows))


def _row(data, e1=None, s1=None, e2=None, s2=None):
    return TimesheetRow(data=data, entrada_1=e1, saida_1=s1, entrada_2=e2, saida_2=s2, ocorrencia_raw=None, ocorrencia_tipo=None)


def test_header_single_pair():
    result = _result(_row("01/03/2024", "08:00", "17:00"))
    lines = build_csv(result).splitlines()
    assert lines[0] == "Data;Entrada 1;Saida 1"


def test_header_two_pairs():
    result = _result(_row("01/03/2024", "08:00", "12:00", "13:00", "17:00"))
    lines = build_csv(result).splitlines()
    assert lines[0] == "Data;Entrada 1;Saida 1;Entrada 2;Saida 2"


def test_fills_calendar_gaps():
    result = _result(
        _row("01/03/2024", "08:00", "17:00"),
        _row("04/03/2024", "08:00", "17:00"),
    )
    lines = build_csv(result).splitlines()
    dates = [l.split(";")[0] for l in lines[1:]]
    assert dates == ["01/03/2024", "02/03/2024", "03/03/2024", "04/03/2024"]


def test_gap_days_have_empty_times():
    result = _result(
        _row("01/03/2024", "08:00", "17:00"),
        _row("03/03/2024", "08:00", "17:00"),
    )
    lines = build_csv(result).splitlines()
    gap = lines[2].split(";")
    assert gap[0] == "02/03/2024"
    assert gap[1] == ""
    assert gap[2] == ""


def test_occurrence_day_empty_times():
    row = TimesheetRow(data="05/03/2024", entrada_1=None, saida_1=None, entrada_2=None, saida_2=None, ocorrencia_raw="FERIAS", ocorrencia_tipo="ferias")
    result = _result(_row("04/03/2024", "08:00", "17:00"), row)
    lines = build_csv(result).splitlines()
    ferias = lines[2].split(";")
    assert ferias[0] == "05/03/2024"
    assert ferias[1] == ""


def test_empty_result():
    result = _result()
    csv = build_csv(result)
    assert csv == "Data\n"


def test_semicolon_delimiter():
    result = _result(_row("01/03/2024", "08:00", "17:00"))
    lines = build_csv(result).splitlines()
    assert ";" in lines[0]
    assert "," not in lines[0]


def test_single_day_no_gaps():
    result = _result(_row("15/06/2023", "09:00", "18:00"))
    lines = build_csv(result).splitlines()
    assert len(lines) == 2
    assert lines[1].startswith("15/06/2023")
