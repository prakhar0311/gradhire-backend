# app/services/resume_builder.py

import tempfile
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    ListFlowable,
    ListItem
)
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER


def build_resume_pdf(data: dict) -> str:

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    temp_file.close()

    # =========================
    # STYLES
    # =========================

    header_style = ParagraphStyle(
        "Header",
        fontSize=18,
        spaceAfter=6,
        alignment=TA_CENTER
    )

    contact_style = ParagraphStyle(
        "Contact",
        fontSize=10,
        spaceAfter=12,
        alignment=TA_CENTER
    )

    section_style = ParagraphStyle(
        "Section",
        fontSize=12,
        spaceBefore=10,
        spaceAfter=6
    )

    body_style = ParagraphStyle(
        "Body",
        fontSize=10,
        leading=14
    )

    # =========================
    # DOCUMENT
    # =========================

    doc = SimpleDocTemplate(
        temp_file.name,
        pagesize=LETTER,
        leftMargin=50,
        rightMargin=50,
        topMargin=50,
        bottomMargin=50,
    )

    elements = []

    # =========================
    # HEADER
    # =========================

    elements.append(
        Paragraph(data.get("name", "Candidate Name"), header_style)
    )

    contact = data.get("contact", "")
    if contact:
        elements.append(Paragraph(contact, contact_style))

    # =========================
    # EMPTY FALLBACK
    # =========================

    has_content = (
        data.get("summary") or
        data.get("skills") or
        data.get("experience_improvements")
    )

    if not has_content:
        elements.append(
            Paragraph("No optimized content available.", body_style)
        )
        doc.build(elements)
        return temp_file.name

    # =========================
    # SUMMARY
    # =========================

    summary = data.get("summary", "")

    if summary:
        elements.append(Paragraph("<b>SUMMARY</b>", section_style))
        elements.append(Paragraph(summary, body_style))
        elements.append(Spacer(1, 8))

    # =========================
    # SKILLS
    # =========================

    skills = data.get("skills", [])

    if skills:
        elements.append(Paragraph("<b>SKILLS</b>", section_style))

        skills_text = ", ".join(skills)
        elements.append(Paragraph(skills_text, body_style))
        elements.append(Spacer(1, 8))

    # =========================
    # EXPERIENCE
    # =========================

    experience = data.get("experience_improvements", [])

    if experience:
        elements.append(Paragraph("<b>EXPERIENCE</b>", section_style))

        bullets = [
            ListItem(Paragraph(b, body_style))
            for b in experience
        ]

        elements.append(ListFlowable(bullets, bulletType="bullet"))
        elements.append(Spacer(1, 8))

    # =========================
    # PROJECTS
    # =========================

    projects = data.get("project_improvements", [])

    if projects:
        elements.append(Paragraph("<b>PROJECTS</b>", section_style))

        bullets = [
            ListItem(Paragraph(b, body_style))
            for b in projects
        ]

        elements.append(ListFlowable(bullets, bulletType="bullet"))

    # =========================
    # BUILD PDF
    # =========================

    doc.build(elements)

    return temp_file.name
