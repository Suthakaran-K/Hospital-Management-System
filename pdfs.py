"""PDF generation (ReportLab): patient report and payment bill, as separate documents."""
from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.graphics.barcode import code128
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

INK = colors.HexColor("#14323b")
RULE = colors.HexColor("#c5d1d4")
WASH = colors.HexColor("#eef3f3")

_base = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=_base["Title"], fontName="Helvetica-Bold", fontSize=18, textColor=INK, alignment=0, spaceAfter=2)
H2 = ParagraphStyle("H2", parent=_base["Heading2"], fontName="Helvetica-Bold", fontSize=12, textColor=INK, spaceBefore=12, spaceAfter=4)
BODY = ParagraphStyle("Body", parent=_base["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13)
SMALL = ParagraphStyle("Small", parent=BODY, fontSize=8.5, textColor=colors.HexColor("#4a5d63"))


def _p(text, style=BODY):
    return Paragraph(escape(str(text if text not in (None, "") else "-")).replace("\n", "<br/>"), style)


def _d(value):
    return value.strftime("%d %b %Y") if value else "-"


def _money(value):
    return "Rs. {:,.2f}".format(value or 0)


def _header(cfg, title):
    contact = [cfg["HOSPITAL_ADDRESS"]]
    if cfg.get("HOSPITAL_PHONE"):
        contact.append("Phone: " + cfg["HOSPITAL_PHONE"])
    if cfg.get("HOSPITAL_EMAIL"):
        contact.append("Email: " + cfg["HOSPITAL_EMAIL"])
    return [
        Paragraph(escape(cfg["HOSPITAL_NAME"]), H1),
        Paragraph(escape("  |  ".join(contact)), SMALL),
        Spacer(1, 6),
        Table([[""]], colWidths=[180 * mm], rowHeights=[1], style=[("LINEABOVE", (0, 0), (-1, 0), 1.2, INK)]),
        Paragraph(escape(title), H2),
    ]


def _grid(rows, widths, header=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), WASH), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    t.setStyle(TableStyle(style))
    return t


def _build(story, footer_text):
    buf = BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#4a5d63"))
        canvas.drawString(15 * mm, 10 * mm, footer_text)
        canvas.drawRightString(195 * mm, 10 * mm, "Page %d" % doc.page)
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=18 * mm, title=footer_text)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def patient_report_pdf(cfg, patient, visits, include_clinical=True):
    story = _header(cfg, "Patient report")
    info = [
        ["Patient ID", patient.patient_code, "Name", patient.name],
        ["Date of birth", _d(patient.date_of_birth), "Age", patient.age],
        ["Phone", patient.phone, "Registered", _d(patient.registered_at)],
        ["Address", _p(patient.address), "Emergency contact", _p(patient.emergency_contact)],
        ["Total visits", len(visits), "Status", "Active" if patient.is_active else "Inactive"],
    ]
    info = [[_p(c) if not hasattr(c, "wrap") else c for c in row] for row in info]
    story.append(_grid(info, [28 * mm, 62 * mm, 32 * mm, 58 * mm], header=False))
    story.append(Paragraph("Visit history (oldest first)", H2))
    if not visits:
        story.append(_p("No visits recorded."))
    for v in sorted(visits, key=lambda x: (x.visit_date, x.id)):
        rows = [
            ["Visit", "%s  (%s)" % (v.visit_code, v.visit_type)],
            ["Date", "%s%s" % (_d(v.visit_date), ("   Discharged: " + _d(v.discharge_date)) if v.discharge_date else "")],
            ["Doctor", "%s (%s)" % (v.doctor.name, v.doctor.doctor_code) if v.doctor else "-"],
            ["Nurse", v.nurse.name if v.nurse else "-"],
            ["Reason", v.reason],
        ]
        if include_clinical:
            rows += [["Diagnosis", v.diagnosis], ["Clinical notes", v.notes], ["Nursing notes", v.nursing_notes]]
            meds = ["%s %s - %s, %s, %s (qty %s)" % (m.medicine.name, m.medicine.strength_form or "", m.dosage or "", m.frequency or "", m.duration or "", m.quantity)
                    for m in v.medicines]
            rows.append(["Medicines", "\n".join(meds) if meds else "-"])
        rows.append(["Follow-up", _d(v.follow_up_date)])
        story.append(_grid([[_p(a, BODY), _p(b)] for a, b in rows], [32 * mm, 148 * mm], header=False))
        story.append(Spacer(1, 8))
    story.append(Paragraph("Printed on %s. Confidential patient information." % _d(date.today()), SMALL))
    return _build(story, "%s - patient report %s" % (cfg["HOSPITAL_NAME"], patient.patient_code))


def bill_pdf(cfg, bill):
    story = _header(cfg, "Payment bill")
    p = bill.patient
    meta = [
        ["Bill number", bill.bill_number, "Bill date", _d(bill.created_at)],
        ["Patient", "%s (%s)" % (p.name, p.patient_code), "Visit", bill.visit.visit_code if bill.visit else "-"],
        ["Status", bill.status.capitalize() + ((" - " + bill.status_reason) if bill.status_reason else ""), "", ""],
    ]
    story.append(_grid([[_p(c) for c in r] for r in meta], [28 * mm, 70 * mm, 24 * mm, 58 * mm], header=False))
    bc = code128.Code128(p.patient_code, barHeight=11 * mm, barWidth=0.38 * mm, humanReadable=True)
    bc.hAlign = "RIGHT"
    story += [Spacer(1, 6), bc, Spacer(1, 4)]
    rows = [["Description", "Qty", "Unit price", "Amount"]]
    for it in bill.items:
        rows.append([_p(it.description), it.quantity, _money(it.unit_price), _money(it.line_total)])
    t = _grid(rows, [108 * mm, 16 * mm, 28 * mm, 28 * mm])
    t.setStyle(TableStyle([("ALIGN", (1, 0), (-1, -1), "RIGHT")]))
    story.append(t)
    totals = [
        ["Subtotal", _money(bill.subtotal)], ["Discount", "- " + _money(bill.discount)], ["Tax", _money(bill.tax)],
        ["Total", _money(bill.total)], ["Amount paid", _money(bill.amount_paid)], ["Balance due", _money(bill.balance)],
    ]
    tt = Table(totals, colWidths=[40 * mm, 32 * mm], hAlign="RIGHT")
    tt.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                            ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
                            ("FONTNAME", (0, 5), (-1, 5), "Helvetica-Bold"), ("LINEABOVE", (0, 3), (-1, 3), 0.8, INK)]))
    story += [Spacer(1, 6), tt]
    story.append(Paragraph("Payments received", H2))
    if bill.payments:
        prow = [["Date", "Method", "Reference", "Amount"]]
        for pay in bill.payments:
            prow.append([_d(pay.paid_at), pay.method, pay.reference_no or "-", _money(pay.amount)])
        pt = _grid(prow, [36 * mm, 36 * mm, 70 * mm, 38 * mm])
        pt.setStyle(TableStyle([("ALIGN", (3, 0), (3, -1), "RIGHT")]))
        story.append(pt)
    else:
        story.append(_p("No payments recorded."))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Printed on %s." % _d(date.today()), SMALL))
    return _build(story, "%s - bill %s" % (cfg["HOSPITAL_NAME"], bill.bill_number))
