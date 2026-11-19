"""A tiny .xlsx writer on the standard library (zipfile + XML), so Excel exports need no new dependency.
One sheet, text cells as inline strings, numbers as numbers (thousands separator), the first `bold_rows` rows bold."""
import io
import zipfile
from xml.sax.saxutils import escape


def _col(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def build_xlsx(rows: list[list], sheet_name: str = "Laporan", bold_rows: tuple = (0,), widths: list[int] | None = None) -> bytes:
    cells = []
    for ri, row in enumerate(rows):
        out = []
        for ci, v in enumerate(row):
            ref = f"{_col(ci)}{ri + 1}"
            bold = ri in bold_rows
            if v is None or v == "":
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(f'<c r="{ref}" s="{3 if bold else 2}"><v>{v}</v></c>')
            else:
                out.append(f'<c r="{ref}" t="inlineStr" s="{1 if bold else 0}"><is><t xml:space="preserve">{escape(str(v))}</t></is></c>')
        cells.append(f'<row r="{ri + 1}">{"".join(out)}</row>')
    cols = ""
    if widths:
        cols = "<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)) + "</cols>"
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'{cols}<sheetData>{"".join(cells)}</sheetData></worksheet>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
              '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '<xf numFmtId="3" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          f'<sheets><sheet name="{escape(sheet_name)[:31]}" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
               '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
              '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
              '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


# ---- Reading (Master Barang import, 19-11-2026) ----
# The first worksheet of an .xlsx as rows of strings, on the standard library too. Handles shared strings,
# inline strings, numbers and booleans, and skips gaps in a row by the cell reference (A1, C1 ...).
import re as _re
import xml.etree.ElementTree as _ET

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _col_index(ref: str) -> int:
    n = 0
    for ch in _re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter("{%s}t" % _NS["m"]))


def read_xlsx(data: bytes, max_rows: int = 20000) -> list[list[str]]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("File bukan Excel (.xlsx) yang valid.")
    names = set(z.namelist())
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        shared = [_text(si) for si in _ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS)]
    sheet_path = "xl/worksheets/sheet1.xml"
    try:  # the first sheet in the workbook's order, wherever its file is
        wb = _ET.fromstring(z.read("xl/workbook.xml"))
        first = wb.find("m:sheets/m:sheet", _NS)
        rels = _ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for r in rels:
            if r.get("Id") == first.get(_REL_NS):
                target = r.get("Target").lstrip("/")
                sheet_path = target if target.startswith("xl/") else "xl/" + target
    except (KeyError, AttributeError, _ET.ParseError):
        pass
    if sheet_path not in names:
        raise ValueError("Sheet pertama tidak ditemukan di file Excel.")
    rows: list[list[str]] = []
    for row in _ET.fromstring(z.read(sheet_path)).iter("{%s}row" % _NS["m"]):
        # Excel leaves empty rows out of the file; pad them back so a row number in a message is Excel's row number.
        if (row.get("r") or "").isdigit():
            while len(rows) < int(row.get("r")) - 1:
                rows.append([])
        out: list[str] = []
        for c in row.findall("m:c", _NS):
            ref, t = c.get("r"), c.get("t")
            idx = _col_index(ref) if ref else len(out)
            v = c.find("m:v", _NS)
            if t == "s" and v is not None:
                val = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
            elif t == "inlineStr":
                val = _text(c)
            elif t == "b" and v is not None:
                val = "TRUE" if v.text == "1" else "FALSE"
            else:
                val = (v.text or "") if v is not None else ""
                if val and _re.fullmatch(r"-?\d+(\.\d+)?(E[+-]?\d+)?", val, _re.I) and t != "str":
                    f = float(val)
                    val = str(int(f)) if f == int(f) else str(f)
            while len(out) < idx:
                out.append("")
            out.append(val.strip())
        rows.append(out)
        if len(rows) > max_rows:
            raise ValueError(f"File terlalu besar (lebih dari {max_rows} baris).")
    return rows
