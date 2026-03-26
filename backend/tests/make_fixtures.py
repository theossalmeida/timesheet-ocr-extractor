"""Generate minimal PDF fixtures for testing. Run once."""
import os

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
os.makedirs(FIXTURES_DIR, exist_ok=True)


def make_native_pdf() -> bytes:
    """Minimal valid PDF with a text table."""
    content = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 320>>
stream
BT
/F1 10 Tf
50 750 Td (DATA       ENTRADA1  SAIDA1    ENTRADA2  SAIDA2    OCORRENCIA) Tj
0 -20 Td (01/03/2024 08:00     12:00     13:00     17:00               ) Tj
0 -20 Td (04/03/2024 08:00     12:00     13:00     17:00               ) Tj
0 -20 Td (05/03/2024                                         FERIAS     ) Tj
0 -20 Td (06/03/2024 08:30     12:00     13:00     17:30               ) Tj
ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Courier>>endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000266 00000 n
0000000638 00000 n
trailer<</Size 6/Root 1 0 R>>
startxref
717
%%EOF"""
    return content


def make_scanned_pdf() -> bytes:
    """Minimal valid PDF with no text layer (simulates scanned)."""
    content = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj
xref
0 4
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
trailer<</Size 4/Root 1 0 R>>
startxref
190
%%EOF"""
    return content


if __name__ == "__main__":
    native_path = os.path.join(FIXTURES_DIR, "native_table.pdf")
    scanned_path = os.path.join(FIXTURES_DIR, "scanned_stub.pdf")

    with open(native_path, "wb") as f:
        f.write(make_native_pdf())
    with open(scanned_path, "wb") as f:
        f.write(make_scanned_pdf())

    print(f"Created: {native_path}")
    print(f"Created: {scanned_path}")
