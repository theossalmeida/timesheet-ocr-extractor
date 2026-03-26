import pytest
from models.timesheet import TimesheetRow
from utils.validators import validate_row, validate_result


class TestValidateRow:
    def test_valid_row_no_warnings(self):
        row = TimesheetRow(
            data="05/03/2024",
            entrada_1="08:00",
            saida_1="12:00",
            entrada_2="13:00",
            saida_2="17:00",
        )
        warnings = validate_row(row)
        assert warnings == []

    def test_saida_before_entrada(self):
        row = TimesheetRow(
            data="05/03/2024",
            entrada_1="08:00",
            saida_1="07:00",
        )
        warnings = validate_row(row)
        assert len(warnings) == 1
        assert "saida_1" in warnings[0]
        assert "07:00" in warnings[0]

    def test_saida_equal_entrada(self):
        row = TimesheetRow(
            data="05/03/2024",
            entrada_1="08:00",
            saida_1="08:00",
        )
        warnings = validate_row(row)
        assert len(warnings) == 1

    def test_invalid_date_format(self):
        row = TimesheetRow(data="not-a-date")
        warnings = validate_row(row)
        assert any("inválida" in w or "inval" in w.lower() for w in warnings)

    def test_year_before_2000(self):
        row = TimesheetRow(data="01/01/1999")
        warnings = validate_row(row)
        assert any("1999" in w or "implaus" in w.lower() for w in warnings)

    def test_both_pairs_inconsistent(self):
        row = TimesheetRow(
            data="05/03/2024",
            entrada_1="10:00",
            saida_1="09:00",
            entrada_2="15:00",
            saida_2="14:00",
        )
        warnings = validate_row(row)
        assert len(warnings) == 2

    def test_row_without_times_no_warnings(self):
        row = TimesheetRow(
            data="05/03/2024",
            ocorrencia_raw="Férias",
            ocorrencia_tipo="ferias",
        )
        warnings = validate_row(row)
        assert warnings == []

    def test_second_pair_inconsistent(self):
        row = TimesheetRow(
            data="05/03/2024",
            entrada_1="08:00",
            saida_1="12:00",
            entrada_2="15:00",
            saida_2="14:00",
        )
        warnings = validate_row(row)
        assert len(warnings) == 1
        assert "saida_2" in warnings[0]


class TestValidateResult:
    def test_duplicate_dates(self):
        rows = [
            TimesheetRow(data="05/03/2024", entrada_1="08:00", saida_1="17:00"),
            TimesheetRow(data="05/03/2024", entrada_1="08:00", saida_1="17:00"),
            TimesheetRow(data="06/03/2024", entrada_1="08:00", saida_1="17:00"),
        ]
        warnings = validate_result(rows)
        assert any("duplicada" in w.lower() for w in warnings)
        assert any("05/03/2024" in w for w in warnings)

    def test_gap_greater_than_7_days(self):
        rows = [
            TimesheetRow(data="01/03/2024"),
            TimesheetRow(data="15/03/2024"),
        ]
        warnings = validate_result(rows)
        assert any("gap" in w.lower() or "Gap" in w for w in warnings)
        assert any("14" in w for w in warnings)

    def test_no_gap_within_7_days(self):
        rows = [
            TimesheetRow(data="01/03/2024"),
            TimesheetRow(data="07/03/2024"),
        ]
        warnings = validate_result(rows)
        assert not any("gap" in w.lower() for w in warnings)

    def test_no_warnings_for_clean_data(self):
        rows = [
            TimesheetRow(data="01/03/2024", entrada_1="08:00", saida_1="17:00"),
            TimesheetRow(data="04/03/2024", entrada_1="08:00", saida_1="17:00"),
            TimesheetRow(data="05/03/2024", entrada_1="08:00", saida_1="17:00"),
        ]
        warnings = validate_result(rows)
        assert warnings == []

    def test_empty_rows(self):
        warnings = validate_result([])
        assert warnings == []

    def test_rows_without_dates_no_warnings(self):
        rows = [TimesheetRow(), TimesheetRow()]
        warnings = validate_result(rows)
        assert warnings == []
