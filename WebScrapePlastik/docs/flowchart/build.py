"""Builds the flowchart blueprint from editable sources.

Sources (edit these, then re-run):
  diagrams/NN-nama.mmd   one Mermaid flowchart per file. The %% lines at the top are the
                         documentation block printed under the diagram (Gambar, Judul, Modul, ...).
  sections/NN-nama.md    text pages (pendahuluan, ringkasan integrasi, keputusan, validasi).

Output (out/):
  FLOWCHART_LENGKAP_SINAR_GALAKSI_VARIASI_MOBIL.pdf   landscape A3, one diagram per page
  png/GAMBAR-NN.png                                    one image per diagram
  blueprint.html                                       the page the PDF is printed from

Run:  python3.11 docs/flowchart/build.py      (needs: pip install playwright markdown; playwright install chromium)
Mermaid is loaded from cdn.jsdelivr.net, so the first run needs internet.
"""

import html
import re
import sys
from pathlib import Path

import markdown
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
PDF_NAME = "FLOWCHART_LENGKAP_SINAR_GALAKSI_VARIASI_MOBIL.pdf"
MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"

DOC_FIELDS = ["Modul", "Tujuan", "Dimulai dari", "Proses Utama", "Hasil", "Warning",
              "Modul Terkait", "Catatan Programmer", "Status Implementasi"]

# One palette for every diagram, so a colour means the same thing everywhere. The meaning is also
# carried by the shape and the text, so the PDF still reads in black and white.
CLASS_DEFS = """
classDef aksi fill:#FBE3D9,stroke:#C4633F,color:#141413,stroke-width:1.5px
classDef so fill:#DCE9F7,stroke:#3E6FA8,color:#141413
classDef ok fill:#DDF0DF,stroke:#3D8A4A,color:#141413
classDef tunggu fill:#FBF0C9,stroke:#B08A16,color:#141413
classDef err fill:#F8D9D6,stroke:#A83B3B,color:#141413
classDef off fill:#ECEBE6,stroke:#8A8983,color:#55554F,stroke-dasharray:4 3
classDef sub fill:#FFFFFF,stroke:#141413,color:#141413,stroke-width:1.5px
classDef data fill:#F0EEE6,stroke:#6B6B66,color:#141413
classDef cek fill:#FFFFFF,stroke:#141413,color:#141413
"""


def parse_diagram(path: Path) -> dict:
    meta, body = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^%%\s*([A-Za-z ]+):\s*(.*)$", line)
        if m and not body:
            meta[m.group(1).strip()] = m.group(2).strip()
        else:
            body.append(line)
    src = "\n".join(body).strip()
    first, _, rest = src.partition("\n")
    if first.strip().startswith(("flowchart", "graph")):
        src = f"{first}\n{CLASS_DEFS}\n{rest}"
    for key in ("Gambar", "Judul"):
        if key not in meta:
            sys.exit(f"{path.name}: baris '%% {key}: ...' wajib ada.")
    return {"file": path.name, "meta": meta, "src": src}


def page_for(d: dict) -> str:
    m = d["meta"]
    rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(m[k])}</td></tr>" for k in DOC_FIELDS if m.get(k)
    )
    return f"""
<section class="page diagram" id="g{html.escape(m['Gambar'])}">
  <header><span class="no">GAMBAR {html.escape(m['Gambar'])}</span><h2>{html.escape(m['Judul'])}</h2>
    <span class="src">sumber: diagrams/{html.escape(d['file'])}</span></header>
  <div class="chart"><pre class="mermaid">{html.escape(d['src'])}</pre></div>
  <table class="doc">{rows}</table>
</section>"""


def build_html(diagrams: list[dict], sections: list[Path]) -> str:
    md = markdown.Markdown(extensions=["tables", "sane_lists"])
    text_pages = []
    for s in sections:
        text_pages.append((s.name, f'<section class="page text">{md.convert(s.read_text(encoding="utf-8"))}</section>'))
        md.reset()
    before = [p for n, p in text_pages if n < "50"]
    after = [p for n, p in text_pages if n >= "50"]
    toc = "".join(
        f"<li><b>GAMBAR {html.escape(d['meta']['Gambar'])}</b> — {html.escape(d['meta']['Judul'])}</li>" for d in diagrams
    )
    cover = f"""
<section class="page cover">
  <div class="kicker">BLUEPRINT WORKFLOW</div>
  <h1>Flowchart Lengkap Sistem POS &amp; Inventory</h1>
  <div class="sub">SINAR GALAKSI VARIASI MOBIL</div>
  <p>Sales Order, Purchase Order, Kasir, Inventory, seluruh Master, dan integrasi antarmodul.</p>
  <ol class="toc">{toc}</ol>
</section>"""
    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<title>Flowchart Lengkap Sinar Galaksi</title>
<style>
  @page {{ size: A3 landscape; margin: 12mm; }}
  :root {{ --bg:#FAF9F5; --sub:#F0EEE6; --text:#141413; --muted:#6B6B66; --accent:#D97757; --border:#E5E3DC; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#fff; color:var(--text); font-family: Inter, system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 11pt; }}
  .page {{ page-break-after: always; break-after: page; width: 396mm; min-height: 273mm; padding: 2mm 4mm; position: relative; }}
  .page:last-child {{ page-break-after: auto; }}
  h1, h2, h3 {{ font-family: Georgia, "Times New Roman", serif; font-weight: 600; }}
  .cover {{ display:flex; flex-direction:column; justify-content:flex-start; }}
  .cover .kicker {{ color: var(--accent); letter-spacing:.18em; font-size: 10pt; margin-top: 8mm; }}
  .cover h1 {{ font-size: 30pt; margin: 3mm 0 1mm; }}
  .cover .sub {{ font-size: 15pt; color: var(--muted); }}
  .cover .toc {{ columns: 2; column-gap: 14mm; font-size: 10.5pt; line-height: 1.55; margin-top: 6mm; }}
  .diagram header {{ display:flex; align-items:baseline; gap: 5mm; border-bottom: 1px solid var(--border); padding-bottom: 2mm; }}
  .diagram .no {{ color: var(--accent); font-weight: 700; letter-spacing: .06em; font-size: 11pt; white-space: nowrap; }}
  .diagram h2 {{ margin: 0; font-size: 17pt; }}
  .diagram .src {{ margin-left:auto; color: var(--muted); font-size: 8pt; font-family: ui-monospace, Menlo, monospace; }}
  .chart {{ height: 186mm; display:flex; align-items:center; justify-content:center; padding: 2mm 0; overflow: hidden; }}
  .chart svg {{ display: block; }}
  pre.mermaid {{ margin:0; font-family: inherit; }}
  table.doc {{ width: 100%; border-collapse: collapse; font-size: 8.6pt; line-height: 1.3; }}
  table.doc th {{ width: 34mm; text-align: left; vertical-align: top; color: var(--muted); font-weight: 600; padding: .6mm 2mm .6mm 0; }}
  table.doc td {{ padding: .6mm 0; vertical-align: top; }}
  table.doc tr + tr {{ border-top: 1px solid var(--border); }}
  .text {{ font-size: 10.5pt; line-height: 1.45; }}
  .text h1 {{ font-size: 20pt; margin: 2mm 0 4mm; border-bottom: 1px solid var(--border); padding-bottom: 2mm; }}
  .text h2 {{ font-size: 14pt; margin: 5mm 0 2mm; }}
  .text table {{ border-collapse: collapse; width: 100%; font-size: 9pt; margin: 2mm 0 4mm; }}
  .text th, .text td {{ border: 1px solid var(--border); padding: 1.4mm 2mm; vertical-align: top; text-align: left; }}
  .text th {{ background: var(--sub); }}
  .text td:first-child {{ white-space: nowrap; }}
  .text code {{ font-family: ui-monospace, Menlo, monospace; font-size: 8.8pt; background: var(--sub); padding: 0 1mm; border-radius: 2px; }}
  .err {{ color: #A83B3B; font-weight: 700; }}
</style></head><body>
{cover}
{''.join(before)}
{''.join(page_for(d) for d in diagrams)}
{''.join(after)}
<script src="{MERMAID_URL}"></script>
<script>
  window.__errors = [];
  mermaid.initialize({{
    startOnLoad: false, securityLevel: "strict", theme: "base",
    themeVariables: {{ fontFamily: "Inter, system-ui, sans-serif", fontSize: "15px", primaryColor: "#FFFFFF",
      primaryBorderColor: "#141413", lineColor: "#3A3A36", clusterBkg: "#FAF9F5", clusterBorder: "#BDBBB3", edgeLabelBackground: "#FFFFFF" }},
    flowchart: {{ htmlLabels: true, curve: "basis", nodeSpacing: 34, rankSpacing: 42, padding: 8, useMaxWidth: false }},
  }});
  (async () => {{
    for (const el of document.querySelectorAll("pre.mermaid")) {{
      try {{ await mermaid.run({{ nodes: [el] }}); }}
      catch (e) {{ window.__errors.push((el.closest("section")?.id || "?") + ": " + (e.message || e)); }}
    }}
    // Fit each diagram to its page area: shrink big ones, enlarge small ones (up to 1.6x) for legibility.
    for (const box of document.querySelectorAll(".chart")) {{
      const svg = box.querySelector("svg");
      if (!svg) continue;
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
    diagrams = [parse_diagram(p) for p in sorted((ROOT / "diagrams").glob("*.mmd"))]
    numbers = [d["meta"]["Gambar"] for d in diagrams]
    expected = [f"{i:02d}" for i in range(1, len(diagrams) + 1)]
    if numbers != expected:
        sys.exit(f"Nomor gambar tidak berurutan: {numbers}")
    sections = sorted((ROOT / "sections").glob("*.md"))
    OUT.mkdir(exist_ok=True)
    (OUT / "png").mkdir(exist_ok=True)
    page_html = OUT / "blueprint.html"
    page_html.write_text(build_html(diagrams, sections), encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1060}, device_scale_factor=2)
        page.goto(page_html.as_uri())
        page.wait_for_function("window.__done === true", timeout=180_000)
        errors = page.evaluate("window.__errors")
        if errors:
            browser.close()
            sys.exit("Mermaid gagal merender:\n  " + "\n  ".join(errors))
        page.emulate_media(media="print")
        for d in diagrams:
            page.locator(f"#g{d['meta']['Gambar']}").screenshot(path=str(OUT / "png" / f"GAMBAR-{d['meta']['Gambar']}.png"))
        page.pdf(path=str(OUT / PDF_NAME), format="A3", landscape=True, print_background=True,
                 margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"})
        browser.close()
    print(f"OK: {len(diagrams)} gambar -> {OUT / PDF_NAME}")


if __name__ == "__main__":
    main()
