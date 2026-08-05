from services.contracheque_service import _extract_page_pdfplumber


class _CDRJPage:
    width = 595.2756

    def extract_tables(self):
        return []

    def extract_text(self):
        return (
            "COMPANHIA DOCAS DO RIO DE JANEIRO(cid:13)(cid:10) "
            "Folha Normal de 01/04/24 a 30/04/24(cid:13)(cid:10)"
        )

    def extract_words(self):
        return [
            {"text": "1(cid:13)(cid:10)", "x0": 47.25, "x1": 51.70, "top": 121.41},
            {"text": "Salário", "x0": 60.00, "x1": 84.90, "top": 121.41},
            {"text": "-", "x0": 87.12, "x1": 89.78, "top": 121.41},
            {"text": "PCES(cid:13)(cid:10)", "x0": 92.01, "x1": 113.79, "top": 121.41},
            {"text": "180,00(cid:13)(cid:10)", "x0": 276.75, "x1": 301.21, "top": 121.41},
            {"text": "4.959,28(cid:13)(cid:10)", "x0": 367.50, "x1": 398.64, "top": 121.41},
            {"text": "1500(cid:13)(cid:10)", "x0": 33.25, "x1": 51.04, "top": 132.75},
            {"text": "INSS", "x0": 60.00, "x1": 78.67, "top": 132.75},
            {"text": "Salário(cid:13)(cid:10)", "x0": 80.90, "x1": 105.79, "top": 132.75},
            {"text": "7.786,02(cid:13)(cid:10)", "x0": 270.75, "x1": 301.89, "top": 132.75},
            {"text": "908,85(cid:13)(cid:10)", "x0": 468.75, "x1": 493.21, "top": 132.75},
        ]


def test_extracts_cdrj_coordinate_layout_and_ignores_discounts():
    assert _extract_page_pdfplumber(_CDRJPage()) == {
        "competencia": "04/2024",
        "itens": [{"descricao": "Salário - PCES", "valor": 4959.28}],
    }


def test_recognizes_cdrj_discount_only_continuation_page():
    page = _CDRJPage()
    page.extract_words = lambda: [
        {"text": "1500(cid:13)(cid:10)", "x0": 33.25, "x1": 51.04, "top": 121.41},
        {"text": "INSS", "x0": 60.00, "x1": 78.67, "top": 121.41},
        {"text": "Salário(cid:13)(cid:10)", "x0": 80.90, "x1": 105.79, "top": 121.41},
        {"text": "7.786,02(cid:13)(cid:10)", "x0": 270.75, "x1": 301.89, "top": 121.41},
        {"text": "908,85(cid:13)(cid:10)", "x0": 468.75, "x1": 493.21, "top": 121.41},
    ]

    assert _extract_page_pdfplumber(page) == {"competencia": "04/2024", "itens": []}
