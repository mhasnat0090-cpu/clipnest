"""Generate a PDF snapshot of the supported extractors in the installed yt-dlp."""

from datetime import date
from pathlib import Path

import yt_dlp


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "static" / "supported-platforms.pdf"
PAGE_WIDTH = 612
PAGE_HEIGHT = 792
ROWS_PER_PAGE = 54


def pdf_escape(value: str) -> str:
    value = value.encode("ascii", "replace").decode("ascii")
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_page(lines: list[str], page_number: int, page_count: int, generated: str) -> bytes:
    commands = [
        "BT /F1 18 Tf 48 748 Td (Clipnest supported platforms) Tj",
        f"/F1 9 Tf 0 -18 Td (yt-dlp {pdf_escape(yt_dlp.version.__version__)}  |  Generated {pdf_escape(generated)}) Tj",
        "/F1 9 Tf 0 -28 Td 14 TL",
    ]
    commands.extend(f"({pdf_escape(line)}) Tj T*" for line in lines)
    commands.extend([
        "ET",
        "BT /F1 8 Tf 48 30 Td (Extractor list from the installed yt-dlp build. Site support and access may change.) Tj ET",
        f"BT /F1 8 Tf 540 30 Td ({page_number}/{page_count}) Tj ET",
    ])
    return "\n".join(commands).encode("ascii")


def write_pdf(platforms: list[str]) -> None:
    generated = date.today().isoformat()
    pages = [platforms[index:index + ROWS_PER_PAGE] for index in range(0, len(platforms), ROWS_PER_PAGE)] or [[]]
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    page_ids = [4 + index * 2 for index in range(len(pages))]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for index, lines in enumerate(pages):
        page_id = page_ids[index]
        stream_id = page_id + 1
        stream = make_page(lines, index + 1, len(pages), generated)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>".encode("ascii")
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream")

    pdf = bytearray(b"%PDF-1.4\n%Clipnest supported platform snapshot\n")
    offsets = [0]
    for object_id, content in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_id} 0 obj\n".encode("ascii"))
        pdf.extend(content)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    OUTPUT.write_bytes(pdf)


def main() -> None:
    labels = {
        str(getattr(extractor, "IE_NAME", "")).strip()
        for extractor in yt_dlp.list_extractors()
        if str(getattr(extractor, "IE_NAME", "")).strip()
    }
    write_pdf(sorted(labels, key=str.casefold))
    print(f"Wrote {OUTPUT} with {len(labels)} extractor entries for yt-dlp {yt_dlp.version.__version__}.")


if __name__ == "__main__":
    main()
