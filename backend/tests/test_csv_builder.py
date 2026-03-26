from models.timesheet import ExtractionResult, TimesheetRow
from services.csv_builder import build_csv, _HEADER

EXPECTED_HEADER = "Data;Entrada1;Saída1;Entrada2;Saída2;Entrada3;Saída3;Entrada4;Saída4;Entrada5;Saída5;Entrada6;Saída6"


def _result(*rows):
    return ExtractionResult(rows=list(rows), provider="pdfplumber", pdf_type="native", warnings=[], total_rows=len(rows))


def _row(data, e1=None, s1=None, e2=None, s2=None):
    return TimesheetRow(data=data, entrada_1=e1, saida_1=s1, entrada_2=e2, saida_2=s2, ocorrencia_raw=None, ocorrencia_tipo=None)


def test_header_fixed_13_columns():
    result = _result(_row("01/03/2024", "08:00", "17:00"))
    lines = build_csv(result).splitlines()
    assert lines[0] == EXPECTED_HEADER
    assert lines[0].count(";") == 12


def test_header_always_13_columns_even_with_two_pairs():
    result = _result(_row("01/03/2024", "08:00", "12:00", "13:00", "17:00"))
    lines = build_csv(result).splitlines()
    assert lines[0] == EXPECTED_HEADER


def test_data_row_always_13_columns():
    result = _result(_row("01/03/2024", "08:00", "17:00"))
    lines = build_csv(result).splitlines()
    assert lines[1].count(";") == 12


def test_data_row_times_correct():
    result = _result(_row("01/03/2024", "08:00", "12:00", "13:00", "17:00"))
    lines = build_csv(result).splitlines()
    fields = lines[1].split(";")
    assert fields[0] == "01/03/2024"
    assert fields[1] == "08:00"
    assert fields[2] == "12:00"
    assert fields[3] == "13:00"
    assert fields[4] == "17:00"
    assert fields[5] == ""


def test_fills_calendar_gaps():
    result = _result(
        _row("01/03/2024", "08:00", "17:00"),
        _row("04/03/2024", "08:00", "17:00"),
    )
    lines = build_csv(result).splitlines()
    dates = [l.split(";")[0] for l in lines[1:]]
    assert dates == ["01/03/2024", "02/03/2024", "03/03/2024", "04/03/2024"]


def test_gap_days_have_12_empty_fields():
    result = _result(
        _row("01/03/2024", "08:00", "17:00"),
        _row("03/03/2024", "08:00", "17:00"),
    )
    lines = build_csv(result).splitlines()
    gap = lines[2]
    assert gap.startswith("02/03/2024")
    assert gap.count(";") == 12
    assert gap == "02/03/2024" + ";" * 12


def test_occurrence_day_12_empty_fields():
    row = TimesheetRow(data="05/03/2024", entrada_1=None, saida_1=None, entrada_2=None, saida_2=None, ocorrencia_raw="FERIAS", ocorrencia_tipo="ferias")
    result = _result(_row("04/03/2024", "08:00", "17:00"), row)
    lines = build_csv(result).splitlines()
    ferias = lines[2]
    assert ferias.startswith("05/03/2024")
    assert ferias.count(";") == 12


def test_empty_result():
    result = _result()
    csv = build_csv(result)
    assert csv.startswith(EXPECTED_HEADER)


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
