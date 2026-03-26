import pytest
from utils.normalizers import normalize_date, normalize_time, normalize_ocorrencia


class TestNormalizeDate:
    def test_standard_format(self):
        assert normalize_date("01/03/2024") == "01/03/2024"

    def test_two_digit_year(self):
        assert normalize_date("01/01/24") == "01/01/2024"

    def test_single_digit_day_month(self):
        assert normalize_date("1/1/2024") == "01/01/2024"

    def test_dash_separator(self):
        assert normalize_date("15-06-2023") == "15/06/2023"

    def test_dot_separator(self):
        assert normalize_date("31.12.2023") == "31/12/2023"

    def test_empty_string(self):
        assert normalize_date("") is None

    def test_none_like_empty(self):
        assert normalize_date("  ") is None

    def test_invalid_format(self):
        assert normalize_date("not-a-date") is None

    def test_year_before_2000(self):
        assert normalize_date("01/01/1999") is None

    def test_invalid_month(self):
        assert normalize_date("01/13/2024") is None

    def test_invalid_day(self):
        assert normalize_date("32/01/2024") is None

    def test_two_digit_year_with_dash(self):
        assert normalize_date("05-03-23") == "05/03/2023"


class TestNormalizeTime:
    def test_standard_hhmm_colon(self):
        assert normalize_time("08:00") == "08:00"

    def test_four_digits(self):
        assert normalize_time("0800") == "08:00"

    def test_single_digit_hour(self):
        assert normalize_time("8:00") == "08:00"

    def test_dot_separator(self):
        assert normalize_time("08.00") == "08:00"

    def test_end_of_day(self):
        assert normalize_time("23:59") == "23:59"

    def test_empty_string(self):
        assert normalize_time("") is None

    def test_whitespace(self):
        assert normalize_time("  ") is None

    def test_invalid_hour(self):
        assert normalize_time("25:00") is None

    def test_invalid_minutes(self):
        assert normalize_time("08:60") is None

    def test_noon(self):
        assert normalize_time("12:00") == "12:00"

    def test_midnight(self):
        assert normalize_time("00:00") == "00:00"

    def test_four_digit_end(self):
        assert normalize_time("1730") == "17:30"


class TestNormalizeOcorrencia:
    def test_ferias(self):
        raw, tipo = normalize_ocorrencia("Férias")
        assert tipo == "ferias"

    def test_fer_abreviado(self):
        raw, tipo = normalize_ocorrencia("FER.")
        assert tipo == "ferias"

    def test_feriado(self):
        raw, tipo = normalize_ocorrencia("Feriado")
        assert tipo == "feriado"

    def test_licenca_medica(self):
        raw, tipo = normalize_ocorrencia("LIC.MED.")
        assert tipo == "licenca_medica"

    def test_atestado(self):
        raw, tipo = normalize_ocorrencia("Atestado")
        assert tipo == "licenca_medica"

    def test_afastamento(self):
        raw, tipo = normalize_ocorrencia("AFAST.")
        assert tipo == "afastamento"

    def test_folga_dsr(self):
        raw, tipo = normalize_ocorrencia("FOLGA/DSR")
        assert tipo == "folga"

    def test_dsr(self):
        raw, tipo = normalize_ocorrencia("DSR")
        assert tipo == "dsr"

    def test_falta(self):
        raw, tipo = normalize_ocorrencia("Falta")
        assert tipo == "falta_injustificada"

    def test_empty_string(self):
        raw, tipo = normalize_ocorrencia("")
        assert raw is None
        assert tipo is None

    def test_whitespace_only(self):
        raw, tipo = normalize_ocorrencia("   ")
        assert raw is None
        assert tipo is None

    def test_preserves_raw(self):
        raw, tipo = normalize_ocorrencia("Férias")
        assert raw == "Férias"

    def test_unknown_returns_outro(self):
        raw, tipo = normalize_ocorrencia("TEXTO_DESCONHECIDO_XYZ")
        assert tipo == "outro"
        assert raw == "TEXTO_DESCONHECIDO_XYZ"

    def test_case_insensitive(self):
        _, tipo1 = normalize_ocorrencia("FERIAS")
        _, tipo2 = normalize_ocorrencia("ferias")
        assert tipo1 == tipo2 == "ferias"
