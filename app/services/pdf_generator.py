# app/services/pdf_generator.py

from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    ListFlowable,
    ListItem
)
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

# Unicode font for ATS-safe resumes
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


def generate_resume_pdf(resume_data: dict, output_path: str):
    """
    Generate ATS-friendly resume PDF.

    resume_data format:

    {
        "name": str,
        "contact": str,
        "summary": str,
        "skills": [str],
        "experience": [
            {
                "title": str,
                "company": str,
                "bullets": [str]
            }
        ],
        "education": [str]
    }
    """

    styles = getSampleStyleSheet()

    header_style = ParagraphStyle(
        "Header",
        fontName="STSong-Light",
        fontSize=18,
        spaceAfter=10,
        alignment=TA_LEFT,
    )

    section_style = ParagraphStyle(
        "Section",
        fontName="STSong-Light",
        fontSize=12,
        spaceAfter=6,
        spaceBefore=10,
        leading=14,
    )

    body_style = ParagraphStyle(
        "Body",
        fontName="STSong-Light",
        fontSize=10,
        leading=14,
    )

    doc = SimpleDocTemplate(
        output_path,
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

    if resume_data.get("name"):
        elements.append(Paragraph(resume_data["name"], header_style))

    if resume_data.get("contact"):
        elements.append(Paragraph(resume_data["contact"], body_style))
        elements.append(Spacer(1, 12))

    # =========================
    # SUMMARY
    # =========================

    if resume_data.get("summary"):
        elements.append(Paragraph("<b>Summary</b>", section_style))
        elements.append(Paragraph(resume_data["summary"], body_style))
        elements.append(Spacer(1, 8))

    # =========================
    # SKILLS
    # =========================

    if resume_data.get("skills"):
        elements.append(Paragraph("<b>Skills</b>", section_style))

        skills_text = ", ".join(resume_data["skills"])
        elements.append(Paragraph(skills_text, body_style))
        elements.append(Spacer(1, 8))

    # =========================
    # EXPERIENCE
    # =========================

    if resume_data.get("experience"):
        elements.append(Paragraph("<b>Experience</b>", section_style))

        for job in resume_data["experience"]:

            if job.get("title"):
                elements.append(
                    Paragraph(f"<b>{job['title']}</b>", body_style)
                )

            if job.get("company"):
                elements.append(
                    Paragraph(job["company"], body_style)
                )

            bullets = []

            for bullet in job.get("bullets", []):
                bullets.append(
                    ListItem(Paragraph(bullet, body_style))
                )

            elements.append(
                ListFlowable(
                    bullets,
                    bulletType="bullet",
                    leftIndent=20
                )
            )

            elements.append(Spacer(1, 6))

    # =========================
    # EDUCATION
    # =========================

    if resume_data.get("education"):
        elements.append(Paragraph("<b>Education</b>", section_style))

        for edu in resume_data["education"]:
            elements.append(Paragraph(edu, body_style))

    # =========================
    # BUILD PDF
    # =========================

    doc.build(elements)
