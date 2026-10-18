"""Shared page shell for the Retur Supplier mockups: sidebar + top bar as in frontend/index.html,
branded SINAR GALAKSI VARIASI MOBIL, with the proposed 'Retur Supplier' menu item highlighted."""

NAV = [
    ("Kasir", None), ("Sales Order", None), ("Purchase Order", None), ("Retur Supplier", "new"),
    ("Riwayat Invoice", None), ("Faktur Pembelian", None), ("Inventori", "group"),
    ("Barang Masuk", "sub"), ("Master Barang", "sub"), ("Stok Karantina", "subnew"), ("Master Supplier", "sub"),
    ("Master Kategori", "sub"), ("Stock Opname", "sub"), ("Dashboard", None), ("Pengguna", None),
    ("Log Aktivitas", None),
]


def page(title: str, subtitle: str, body: str, active: str = "Retur Supplier", user: str = "sinargalaksi88",
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
    <header class="top"><div class="ico">↩</div><div><h1>{title}</h1><p>{subtitle}</p></div>
      <div class="user"><div class="avatar">SI</div><div><b>{user}</b><div class="tiny muted">{role}</div></div>
      <span class="btn sm">Keluar</span></div></header>
    <div class="content">{body}</div>
  </main>
</div>{extra}
</body></html>"""


def steps(active: int) -> str:
    names = ["Informasi Retur", "Barang & Alasan", "Packing Note", "Metode Pengembalian", "Review & Ajukan"]
    out = []
    for i, n in enumerate(names, start=1):
        cls = "done" if i < active else "on" if i == active else ""
        out.append(f'<div class="step {cls}"><b>{"✓" if i < active else i}</b>{n}</div>')
    return f'<div class="steps">{"".join(out)}</div>'


def doc_header(no: str = "RTS-20261018-001", status: str = "Draft", pill: str = "p-grey") -> str:
    return f"""<div class="between"><div class="row" style="align-items:center;gap:10px">
      <span class="btn sm">← Daftar Retur</span><h2 class="serif" style="margin:0;font-size:18px">{no}</h2>
      <span class="pill {pill}">{status}</span><span class="tag">ASTA JAYA</span><span class="tag">PO-SUP-20261010-001 · BM-20261012-002</span></div>
      <div class="row" style="gap:8px"><span class="btn">Simpan Draft</span></div></div>"""
