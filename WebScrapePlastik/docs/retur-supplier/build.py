"""Builds the Retur Supplier design package from editable sources.

Sources (edit these, then re-run):
  mockups/src/screens_*.py    the 14 mockup screens (HTML built from shell.py + app.css)
  flowcharts/FLOW_*.mmd       one Mermaid flowchart per file; the %% lines at the top are the
                              documentation block printed under the diagram (Judul, Tujuan, ...)
  RETUR_SUPPLIER_DESAIN.md    the design document. Each "### Gambar NN — ..." block under the
                              "Penjelasan per Gambar" heading is printed next to mockup NN in the PDF.

Output:
  mockups/src/NN_*.html, mockups/NN_*.png      one desktop screen per file (1440 wide)
  flowcharts/FLOW_*.png                         one image per flowchart
  RETUR_SUPPLIER_DESAIN.pdf                     A3 landscape: document, mockups with explanation, flowcharts
  out/package.html                              the page the PDF is printed from

Run:  python3.11 docs/retur-supplier/build.py [--mockups]   (needs: playwright, markdown; playwright install chromium)
--mockups renders only the mockup PNGs. Mermaid is loaded from cdn.jsdelivr.net, so a full run needs internet.
"""

import base64
import html
import importlib.util
import re
import sys
from pathlib import Path

import markdown
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "mockups" / "src"
sys.path.insert(0, str(SRC))

import screens_a  # noqa: E402
import screens_b  # noqa: E402
import screens_c  # noqa: E402

# Same palette and Mermaid version as the main blueprint (docs/flowchart/build.py).
_spec = importlib.util.spec_from_file_location("blueprint_build", ROOT.parent / "flowchart" / "build.py")
_blueprint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_blueprint)
CLASS_DEFS, MERMAID_URL = _blueprint.CLASS_DEFS, _blueprint.MERMAID_URL

DOC = ROOT / "RETUR_SUPPLIER_DESAIN.md"
PDF = ROOT / "RETUR_SUPPLIER_DESAIN.pdf"
OUT = ROOT / "out"
FLOW_FIELDS = ["Tujuan", "Dimulai dari", "Aturan stok", "Aturan keuangan", "Warning", "Catatan Programmer"]


def write_mockups() -> list[Path]:
    paths = []
    for fn in screens_a.SCREENS + screens_b.SCREENS + screens_c.SCREENS:
        name, doc = fn()
        p = SRC / f"{name}.html"
        p.write_text(doc, encoding="utf-8")
        paths.append(p)
    if len(paths) != 14:
        sys.exit(f"Harus 14 mockup, ada {len(paths)}")
    return paths


def render_mockups(browser, paths: list[Path]) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1.5)
    for p in paths:
        page.goto(p.as_uri())
        page.wait_for_load_state("load")
        over = page.evaluate("document.documentElement.scrollWidth")
        if over > 1440:
            print(f"  ! {p.name}: lebar {over}px (melebihi 1440)")
        page.screenshot(path=str(ROOT / "mockups" / f"{p.stem}.png"), full_page=True)
        print(f"  {p.stem}.png  tinggi {page.evaluate('document.documentElement.scrollHeight')}px")
    page.close()


def parse_flow(path: Path) -> dict:
    meta, body = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^%%\s*([A-Za-z ]+):\s*(.*)$", line)
        if m and not body:
            meta[m.group(1).strip()] = m.group(2).strip()
        else:
            body.append(line)
    src = "\n".join(body).strip()
    first, _, rest = src.partition("\n")
    src = f"{first}\n{CLASS_DEFS}\n{rest}"
    return {"id": path.stem, "meta": meta, "src": src}


def split_doc(text: str):
    """Returns (main markdown, {NN: explanation markdown}). The explanations are cut out of the
    'Penjelasan per Gambar' section so the PDF can print each one next to its mockup."""
    expl = {}
    parts = re.split(r"(?m)^(### Gambar (\d\d) .*)$", text)
    head = parts[0]
    tail = ""
    for i in range(1, len(parts), 3):
        title, nn, block = parts[i], parts[i + 1], parts[i + 2]
        nxt = re.search(r"(?m)^## ", block)
        if nxt:
            tail += block[nxt.start():]
            block = block[:nxt.start()]
        expl[nn] = (title[4:], block)
    return head, expl, tail


def img_uri(p: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


def build_html(mock_pngs: list[Path], flows: list[dict]) -> str:
    md = markdown.Markdown(extensions=["tables", "sane_lists", "fenced_code"])
    head, expl, tail = split_doc(DOC.read_text(encoding="utf-8"))

    def conv(t):
        md.reset()
        return md.convert(t)

    mock_pages = []
    for png in mock_pngs:
        nn = png.stem[:2]
        title, body = expl.get(nn, (png.stem, "*(penjelasan belum ditulis)*"))
        mock_pages.append(f"""<section class="page mock"><div class="img"><img src="{img_uri(png)}"></div>
<div class="ex"><div class="no">MOCKUP {nn}</div><h2>{html.escape(title.split('—', 1)[-1].strip())}</h2>
<div class="src">{png.name}</div>{conv(body)}</div></section>""")
    flow_pages = []
    for f in flows:
        rows = "".join(f"<tr><th>{k}</th><td>{html.escape(f['meta'][k])}</td></tr>" for k in FLOW_FIELDS if f["meta"].get(k))
        flow_pages.append(f"""<section class="page diagram" id="{f['id']}"><header><span class="no">{html.escape(f['meta'].get('Kode', ''))}</span>
<h2>{html.escape(f['meta'].get('Judul', f['id']))}</h2><span class="src">flowcharts/{f['id']}.mmd</span></header>
<div class="chart"><pre class="mermaid">{html.escape(f['src'])}</pre></div><table class="doc">{rows}</table></section>""")
    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8"><title>Retur Supplier — Desain</title>
<style>
  @page {{ size: A3 landscape; margin: 12mm; }}
  :root {{ --sub:#F0EEE6; --text:#141413; --muted:#6B6B66; --accent:#D97757; --border:#E5E3DC; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#fff; color:var(--text); font-family: Inter, system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 10.5pt; }}
  h1, h2, h3, h4 {{ font-family: Georgia, "Times New Roman", serif; font-weight: 600; }}
  .page {{ break-after: page; width: 396mm; position: relative; }}
  .text {{ line-height: 1.45; columns: 2; column-gap: 12mm; }}
  .text h1 {{ column-span: all; font-size: 24pt; margin: 0 0 2mm; }}
  .text h2 {{ column-span: all; font-size: 15pt; margin: 6mm 0 2mm; border-bottom: 1px solid var(--border); padding-bottom: 1.5mm; break-after: avoid; }}
  .text h3 {{ font-size: 12pt; margin: 4mm 0 1.5mm; break-after: avoid; }}
  .text table {{ border-collapse: collapse; width: 100%; font-size: 8.6pt; margin: 2mm 0 3mm; break-inside: auto; }}
  .text table, .text pre {{ column-span: all; }}
  th, td {{ border: 1px solid var(--border); padding: 1.1mm 1.6mm; vertical-align: top; text-align: left; }}
  .text th {{ background: var(--sub); }}
  tr {{ break-inside: avoid; }}
  code {{ font-family: ui-monospace, Menlo, monospace; font-size: 8.4pt; background: var(--sub); padding: 0 1mm; border-radius: 2px; }}
  pre {{ background: #fff; border: 1px solid var(--border); padding: 2mm 3mm; font-size: 7.6pt; line-height: 1.25; break-inside: avoid; overflow: hidden; }}
  pre code {{ background: none; padding: 0; font-family: "Courier New", monospace; font-size: 7.6pt; }}
  .mock {{ display: flex; gap: 7mm; height: 272mm; align-items: flex-start; }}
  .mock .img {{ width: 258mm; flex: none; }}
  .mock img {{ max-width: 258mm; max-height: 270mm; border: 1px solid var(--border); border-radius: 2mm; display: block; }}
  .mock .ex {{ flex: 1; font-size: 9.2pt; line-height: 1.4; }}
  .mock .ex h2 {{ margin: 1mm 0 1mm; font-size: 15pt; }}
  .mock .ex h4 {{ margin: 3mm 0 .8mm; font-family: Inter, sans-serif; font-size: 8.4pt; color: var(--accent); text-transform: uppercase; letter-spacing: .06em; }}
  .mock .ex p, .mock .ex ul {{ margin: 0 0 1.5mm; }} .mock .ex ul {{ padding-left: 4.5mm; }}
  .no {{ color: var(--accent); font-weight: 700; letter-spacing: .08em; font-size: 9pt; }}
  .src {{ color: var(--muted); font-size: 7.6pt; font-family: ui-monospace, Menlo, monospace; }}
  .diagram header {{ display:flex; align-items:baseline; gap: 5mm; border-bottom: 1px solid var(--border); padding-bottom: 2mm; }}
  .diagram h2 {{ margin: 0; font-size: 17pt; }} .diagram header .src {{ margin-left: auto; }}
  .chart {{ height: 200mm; display:flex; align-items:center; justify-content:center; padding: 2mm 0; overflow: hidden; }}
  pre.mermaid {{ border: 0; margin: 0; font-family: inherit; font-size: inherit; }}
  table.doc {{ width: 100%; border-collapse: collapse; font-size: 8.6pt; }}
  table.doc th {{ width: 34mm; color: var(--muted); border: 0; border-top: 1px solid var(--border); }}
  table.doc td {{ border: 0; border-top: 1px solid var(--border); }}
</style></head><body>
<section class="page text">{conv(head)}</section>
{''.join(mock_pages)}
<section class="page text">{conv(tail)}</section>
{''.join(flow_pages)}
<script src="{MERMAID_URL}"></script>
<script>
  window.__errors = [];
  mermaid.initialize({{ startOnLoad: false, securityLevel: "strict", theme: "base",
    themeVariables: {{ fontFamily: "Inter, system-ui, sans-serif", fontSize: "15px", primaryColor: "#FFFFFF",
      primaryBorderColor: "#141413", lineColor: "#3A3A36", clusterBkg: "#FAF9F5", clusterBorder: "#BDBBB3", edgeLabelBackground: "#FFFFFF" }},
    flowchart: {{ htmlLabels: true, curve: "basis", nodeSpacing: 34, rankSpacing: 42, padding: 8, useMaxWidth: false }} }});
  (async () => {{
    for (const el of document.querySelectorAll("pre.mermaid")) {{
      try {{ await mermaid.run({{ nodes: [el] }}); }}
      catch (e) {{ window.__errors.push((el.closest("section")?.id || "?") + ": " + (e.message || e)); }}
    }}
    for (const box of document.querySelectorAll(".chart")) {{
      const svg = box.querySelector("svg"); if (!svg) continue;
      const vb = svg.viewBox.baseVal;
      const w = vb && vb.width ? vb.width : svg.getBoundingClientRect().width;
      const h = vb && vb.height ? vb.height : svg.getBoundingClientRect().height;
      const k = Math.min(box.clientWidth / w, (box.clientHeight - 8) / h, 1.6);
      svg.style.maxWidth = "none"; svg.style.maxHeight = "none";
      svg.setAttribute("width", Math.floor(w * k)); svg.setAttribute("height", Math.floor(h * k));
      svg.style.width = Math.floor(w * k) + "px"; svg.style.height = Math.floor(h * k) + "px";
    }}
    window.__done = true;
  }})();
</script></body></html>"""


def main() -> None:
    only_mockups = "--mockups" in sys.argv
    paths = write_mockups()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        render_mockups(browser, paths)
        if only_mockups:
            browser.close()
            return
        flows = [parse_flow(f) for f in sorted((ROOT / "flowcharts").glob("FLOW_*.mmd"))]
        if len(flows) != 4:
            sys.exit(f"Harus 4 flowchart, ada {len(flows)}")
        OUT.mkdir(exist_ok=True)
        page_html = OUT / "package.html"
        page_html.write_text(build_html(sorted((ROOT / "mockups").glob("[0-9][0-9]_*.png")), flows), encoding="utf-8")
        page = browser.new_page(viewport={"width": 1500, "height": 1060}, device_scale_factor=2)
        page.goto(page_html.as_uri())
        page.wait_for_function("window.__done === true", timeout=180_000)
        errors = page.evaluate("window.__errors")
        if errors:
            browser.close()
            sys.exit("Mermaid gagal merender:\n  " + "\n  ".join(errors))
        page.emulate_media(media="print")
        for f in flows:
            page.locator(f"#{f['id']}").screenshot(path=str(ROOT / "flowcharts" / f"{f['id']}.png"))
        page.pdf(path=str(PDF), format="A3", landscape=True, print_background=True,
                 margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"})
        browser.close()
    print(f"OK: 14 mockup, {len(flows)} flowchart -> {PDF}")


if __name__ == "__main__":
    main()
