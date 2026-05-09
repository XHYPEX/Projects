import pdfplumber

def pdf_to_txt(pdf_path, txt_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n\n".join(page.extract_text() or "" for page in pdf.pages)
    
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)