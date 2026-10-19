"""Shared page shell for the Service / Garansi mockups: sidebar + top bar as in frontend/index.html,
with the proposed 'Service / Garansi' menu and 'Master Service' sub-item highlighted as new."""

NAV = [
    ("Kasir", None), ("Sales Order", None), ("Purchase Order", None), ("Retur Supplier", None),
    ("Service / Garansi", "new"), ("Riwayat Invoice", None), ("Faktur Pembelian", None), ("Inventori", "group"),
    ("Barang Masuk", "sub"), ("Master Barang", "sub"), ("Master Brand", "sub"), ("Master Supplier", "sub"),
    ("Master Kategori", "sub"), ("Stock Opname", "sub"), ("Stok Karantina", "sub"), ("Master Customer", "sub"),
    ("Master Service", "subnew"), ("Dashboard", None), ("Pengguna", None), ("Log Aktivitas", None),
]


def page(title: str, subtitle: str, body: str, active: str = "Service / Garansi", user: str = "sinargalaksi88",
         role: str = "Admin", extra: str = "") -> str:
    items = []
    for name, kind in NAV:
        cls = []
        if kind in ("sub", "subnew"):
            cls.append("sub")
        if name == active:
            cls.append("on")
        badge = '<span class="new">BARU</span>' if kind in ("new", "subnew") else ""
        chev = ' <span style="margin-left:auto">▾</span>' if kind == "group" else ""
        items.append(f'<a class="{" ".join(cls)}"><i></i>{name}{badge}{chev}</a>')
    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8"><title>{title}</title>
<link rel="stylesheet" href="app.css"></head><body>
<div class="shell">
  <aside class="side">
    <div class="brand"><div class="logo">SG</div><div><b>Sinar Galaksi</b><span>Variasi Mobil · Kasir &amp; stok</span></div></div>
    <nav class="nav">{''.join(items)}</nav>
  </aside>
  <main class="main">
    <header class="top"><div class="ico">⚒</div><div><h1>{title}</h1><p>{subtitle}</p></div>
      <div class="user"><div class="avatar">SI</div><div><b>{user}</b><div class="tiny muted">{role}</div></div>
      <span class="btn sm">Keluar</span></div></header>
    <div class="content">{body}</div>
  </main>
</div>{extra}
</body></html>"""


def steps(active: int) -> str:
    names = ["Customer & Nota Asal", "Identitas Barang", "Penanganan", "Review & Simpan"]
    out = []
    for i, n in enumerate(names, start=1):
        cls = "done" if i < active else "on" if i == active else ""
        out.append(f'<div class="step {cls}"><b>{"✓" if i < active else i}</b>{n}</div>')
    return f'<div class="steps">{"".join(out)}</div>'


def doc_header(no: str = "SVC-20261016-001", stage: str = "Diterima", pill: str = "p-coral",
               right: str = '<span class="btn">Cetak ▾</span>') -> str:
    return f"""<div class="between"><div class="row" style="align-items:center;gap:8px 10px;flex-wrap:wrap">
      <span class="btn sm">← Daftar Service</span><h2 class="serif" style="margin:0;font-size:18px;white-space:nowrap">{no}</h2>
      <span class="pill {pill}">{stage}</span><span class="tag">Budi Santoso · 0812-3456-7890</span>
      <span class="tag">HEAD UNIT ANDROID 9" · SN HU12345678</span><span class="tag">Nota PJSG/26/08/0125</span></div>
      <div class="row" style="gap:8px;flex:none">{right}</div></div>"""


def field(label: str, value: str, cls: str = "", req: bool = False, hint: str = "", span: str = "") -> str:
    r = ' <span class="req">*</span>' if req else ""
    h = f'<div class="hint">{hint}</div>' if hint else ""
    sp = f' class="field {span}"' if span else ' class="field"'
    return f'<div{sp}><label>{label}{r}</label><div class="input {cls}">{value}</div>{h}</div>'


def chk(label: str, on: bool = False, radio: bool = False) -> str:
    return f'<span class="chk{" on" if on else ""}{" radio" if radio else ""}"><i></i>{label}</span>'


def choice(title: str, desc: str, on: bool = False, extra: str = "") -> str:
    return f'<div class="choice{" on" if on else ""}"><b>{title}</b><div class="small muted" style="margin-top:3px">{desc}</div>{extra}</div>'
