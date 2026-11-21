"""CSV / Excel / PDF export for reports, and the GST invoice PDF."""
import csv
import io
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .money import D, fmt_dec, inr_plain, paise_to_rupees, state_label, words_inr
from .services.reports import fmt_cell


def _export_value(kind, v):
    """Raw value for CSV/XLSX: money in rupees (2 dp Decimal), qty as Decimal, others as-is."""
    if v is None or v == "":
        return None
    if kind == "money":
        return paise_to_rupees(v)
    if kind in ("qty", "pct"):
        return D(v)
    return v


def to_csv(rep) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([rep.title])
    w.writerow([c[0] + (" (Rs)" if c[1] == "money" else "") for c in rep.columns])
    for row in rep.rows:
        w.writerow(["" if (x := _export_value(k, v)) is None else (fmt_dec(x) if isinstance(x, Decimal) and k != "money" else x)
                    for (_, k), v in zip(rep.columns, row)])
    if rep.totals:
        w.writerow(["" if v is None else (v if i == 0 else (paise_to_rupees(v) if rep.columns[i][1] == "money" else fmt_dec(v)))
                    for i, v in enumerate(rep.totals)])
    return buf.getvalue()


def to_xlsx(rep) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = rep.key[:31]
    ws.append([rep.title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([c[0] + (" (Rs)" if c[1] == "money" else "") for c in rep.columns])
    for cell in ws[2]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E79")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in rep.rows:
        ws.append([_export_value(k, v) for (_, k), v in zip(rep.columns, row)])
    if rep.totals:
        ws.append([None if v is None else (v if i == 0 else (paise_to_rupees(v) if rep.columns[i][1] == "money" else v))
                   for i, v in enumerate(rep.totals)])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    for i, (label, kind) in enumerate(rep.columns, 1):
        col = get_column_letter(i)
        width = max([len(str(label))] + [len(str(fmt_cell(kind, r[i - 1]))) for r in rep.rows[:200]]) + 2
        ws.column_dimensions[col].width = min(max(width, 8), 40)
        if kind == "money":
            for c in ws[col][2:]:
                c.number_format = "#,##0.00"
    ws.freeze_panes = "A3"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def to_pdf(rep, company: str, period: str) -> bytes:
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=rep.title)
    st = getSampleStyleSheet()
    small = st["BodyText"].clone("small", fontSize=7, leading=8.5)
    cell = lambda t: Paragraph(str(t).replace("&", "&amp;").replace("<", "&lt;"), small)
    data = [[cell(c[0] + (" (Rs)" if c[1] == "money" else "")) for c in rep.columns]]
    for row in rep.rows:
        data.append([cell(fmt_cell(k, v)) for (_, k), v in zip(rep.columns, row)])
    if rep.totals:
        data.append([cell("" if v is None else (v if i == 0 else fmt_cell(rep.columns[i][1], v))) for i, v in enumerate(rep.totals)])
    t = Table(data, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6FA")])]
    if rep.totals:
        style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
    t.setStyle(TableStyle(style))
    for c in data[0]:
        c.style = small.clone("hdr", textColor=colors.white)
    story = [Paragraph(f"<b>{company}</b>", st["Heading3"]), Paragraph(f"{rep.title} | {period}", st["Normal"]),
             Spacer(1, 4 * mm), t]
    if rep.note:
        story += [Spacer(1, 3 * mm), Paragraph(rep.note, small)]
    doc.build(story)
    return out.getvalue()


def invoice_pdf(inv, lines, company: dict, kind="TAX INVOICE") -> bytes:
    """GST tax invoice. (Helvetica has no rupee glyph, so amounts print as 'Rs'.)"""
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=inv["invoice_no"])
    st = getSampleStyleSheet()
    n = st["BodyText"].clone("n", fontSize=8.5, leading=11)
    b = st["BodyText"].clone("b", fontSize=8.5, leading=11, fontName="Helvetica-Bold")
    P = lambda t, s=n: Paragraph(str(t).replace("&", "&amp;"), s)
    inter = inv["supply_type"] == "inter"
    story = [Paragraph(f"<b>{company['name']}</b>", st["Heading2"]),
             P(f"{company['address']}<br/>GSTIN: {company['gstin']} | State: {state_label(company['state_code'])}"),
             Spacer(1, 3 * mm), Paragraph(f"<b>{kind}</b>", st["Heading3"])]
    head = Table([[P(f"<b>Invoice no:</b> {inv['invoice_no']}<br/><b>Date:</b> {inv['inv_date']}<br/>"
                     f"<b>Order ref:</b> {inv['order_no'] or '-'}<br/><b>Place of supply:</b> {state_label(inv['place_of_supply'])}"),
                   P(f"<b>Bill to:</b><br/>{inv['customer_name']}<br/>{inv['customer_address'] or ''}<br/>"
                     f"GSTIN: {inv['customer_gstin'] or 'Unregistered'}")]], colWidths=[90 * mm, 92 * mm])
    head.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 3 * mm)]
    hdr = ["#", "Description", "HSN", "Qty", "Rate", "Taxable", "GST %"] + (["IGST"] if inter else ["CGST", "SGST"]) + ["Amount"]
    rows = [[P(h, b) for h in hdr]]
    for i, l in enumerate(lines, 1):
        tax = [inr_plain(l["igst_paise"])] if inter else [inr_plain(l["cgst_paise"]), inr_plain(l["sgst_paise"])]
        rows.append([P(i), P(l["description"]), P(l["hsn"] or ""), P(l["qty"]), P(inr_plain(l["rate_paise"])),
                     P(inr_plain(l["taxable_paise"])), P(fmt_dec(D(l["gst_rate_bp"]) / 100)), *[P(x) for x in tax],
                     P(inr_plain(l["total_paise"]))])
    widths = [8, 50, 20, 12, 16, 22, 12] + ([20] if inter else [18, 18]) + [24]
    t = Table(rows, repeatRows=1, colWidths=[w * mm for w in widths])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t, Spacer(1, 3 * mm)]
    tot = [["Taxable value", inr_plain(inv["taxable_paise"])]]
    if inter:
        tot.append(["IGST", inr_plain(inv["igst_paise"])])
    else:
        tot += [["CGST", inr_plain(inv["cgst_paise"])], ["SGST", inr_plain(inv["sgst_paise"])]]
    tot.append(["Invoice total (Rs)", inr_plain(inv["total_paise"])])
    tt = Table([[P(a, b if a.startswith("Invoice") else n), P(v, b if a.startswith("Invoice") else n)] for a, v in tot],
               colWidths=[40 * mm, 35 * mm], hAlign="RIGHT")
    tt.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black)]))
    story += [tt, Spacer(1, 3 * mm), P(f"<b>Amount in words:</b> {words_inr(inv['total_paise'])}")]
    if inv["status"] == "cancelled":
        story += [Spacer(1, 3 * mm), P("<b>*** CANCELLED - see credit note ***</b>")]
    story += [Spacer(1, 8 * mm), P("Declaration: goods once sold are not returnable except as agreed. "
                                   "This is a computer-generated invoice.")]
    doc.build(story)
    return out.getvalue()
