# app/services/pdf_generator.py

import uuid
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    ListFlowable,
    ListItem
)
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle


def generate_resume_pdf(resume_data: dict, output_path: str | None = None):
    """
    Production-ready FAANG resume PDF generator.

    Uses clean ATS-optimized formatting.
    One-page optimized for new grads.

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
                "location": str,
                "duration": str,
                "bullets": [str]
            }
        ],
        "projects": [
            {
                "title": str,
                "bullets": [str]
            }
        ],
        "education": [
            {
                "degree": str,
                "school": str,
                "duration": str
            }
        ]
    }
    """

    if not output_path:
        output_path = f"/tmp/resume_{uuid.uuid4().hex}.pdf"

    # =====================
    # Styles
    # =====================

    header_style = ParagraphStyle(
        name="Header",
        fontName="Helvetica-Bold",
        fontSize=18,
        spaceAfter=6,
    )

    contact_style = ParagraphStyle(
        name="Contact",
        fontName="Helvetica",
        fontSize=10,
        spaceAfter=12,
    )

    section_style = ParagraphStyle(
        name="Section",
        fontName="Helvetica-Bold",
        fontSize=11,
        spaceBefore=8,
        spaceAfter=4,
    )

    body_style = ParagraphStyle(
        name="Body",
        fontName="Helvetica",
        fontSize=10,
        leading=14,
    )

    bullet_style = ParagraphStyle(
        name="Bullet",
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        leftIndent=4,
    )

    # =====================
    # Document
    # =====================

    doc = SimpleDocTemplate(
        output_path,
        pagesize=LETTER,
        leftMargin=50,
        rightMargin=50,
        topMargin=40,
        bottomMargin=40,
    )

    elements = []

    # =====================
    # Header
    # =====================

    name = resume_data.get("name", "Candidate Name")
    elements.append(Paragraph(name, header_style))

    contact = resume_data.get("contact", "")
    if contact:
        elements.append(Paragraph(contact, contact_style))

    # =====================
    # Summary
    # =====================

    summary = resume_data.get("summary")
    if summary:
        elements.append(Paragraph("SUMMARY", section_style))
        elements.append(Paragraph(summary, body_style))

    # =====================
    # Skills
    # =====================

    skills = resume_data.get("skills", [])
    if skills:
        elements.append(Paragraph("SKILLS", section_style))
        elements.append(
            Paragraph(", ".join(skills), body_style)
        )

    # =====================
    # Experience
    # =====================

    experience = resume_data.get("experience", [])

    if experience:
        elements.append(Paragraph("EXPERIENCE", section_style))

        for job in experience:

            title_line = f"<b>{job.get('title','')}</b>"
            elements.append(Paragraph(title_line, body_style))

            meta = " — ".join(
                filter(None, [
                    job.get("company"),
                    job.get("location"),
                    job.get("duration")
                ])
            )

            if meta:
                elements.append(Paragraph(meta, body_style))

            bullets = [
                ListItem(
                    Paragraph(bullet, bullet_style)
                )
                for bullet in job.get("bullets", [])[:4]
            ]

            elements.append(
                ListFlowable(
                    bullets,
                    bulletType="bullet",
                    leftIndent=14
                )
            )

    # =====================
    # Projects
    # =====================

    projects = resume_data.get("projects", [])

    if projects:
        elements.append(Paragraph("PROJECTS", section_style))

        for project in projects:

            elements.append(
                Paragraph(
                    f"<b>{project.get('title','')}</b>",
                    body_style
                )
            )

            bullets = [
                ListItem(
                    Paragraph(bullet, bullet_style)
                )
                for bullet in project.get("bullets", [])[:4]
            ]

            elements.append(
                ListFlowable(
                    bullets,
                    bulletType="bullet",
                    leftIndent=14
                )
            )

    # =====================
    # Education
    # =====================

    education = resume_data.get("education", [])

    if education:
        elements.append(Paragraph("EDUCATION", section_style))

        for edu in education:

            elements.append(
                Paragraph(
                    f"<b>{edu.get('degree','')}</b>",
                    body_style
                )
            )

            meta = " — ".join(
                filter(None, [
                    edu.get("school"),
                    edu.get("duration")
                ])
            )

            if meta:
                elements.append(Paragraph(meta, body_style))

    # =====================
    # Build
    # =====================

    doc.build(elements)

    return output_path
