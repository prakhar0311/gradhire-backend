import uuid
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas


# =====================================================
# GradHire FAANG Resume Builder v4 (Production Ready)
# Supports categorized technical skills
# One-Page Optimized
# =====================================================

MAX_BULLETS_PER_ROLE = 4
MAX_PROJECTS = 2
MAX_EXPERIENCE = 3

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"

PAGE_WIDTH, PAGE_HEIGHT = LETTER
MARGIN = 50

LINE_SPACING = 14
SECTION_SPACING = 18
ITEM_SPACING = 10


def build_resume_pdf(data: dict) -> str:

    filename = f"/tmp/resume_{uuid.uuid4().hex}.pdf"

    c = canvas.Canvas(filename, pagesize=LETTER)

    y = PAGE_HEIGHT - MARGIN


    # =====================================================
    # Helpers
    # =====================================================

    def draw_text(text, x, y_pos, font=FONT, size=10):
        c.setFont(font, size)
        c.drawString(x, y_pos, text)


    def draw_right(text, y_pos, size=10):
        c.setFont(FONT, size)
        c.drawRightString(PAGE_WIDTH - MARGIN, y_pos, text)


    def wrap_text(text, max_width, font=FONT, size=10):

        words = text.split()

        lines = []
        current = ""

        for word in words:

            test = f"{current} {word}".strip()

            width = c.stringWidth(test, font, size)

            if width <= max_width:
                current = test
            else:
                lines.append(current)
                current = word

        if current:
            lines.append(current)

        return lines


    def draw_paragraph(text, indent=0):

        nonlocal y

        max_width = PAGE_WIDTH - MARGIN * 2 - indent

        lines = wrap_text(text, max_width)

        for line in lines:
            draw_text(line, MARGIN + indent, y)
            y -= LINE_SPACING


    def draw_bullet(text):

        nonlocal y

        bullet_x = MARGIN + 4
        text_x = MARGIN + 14

        c.setFont(FONT, 10)

        c.drawString(bullet_x, y, "•")

        lines = wrap_text(text, PAGE_WIDTH - MARGIN * 2 - 14)

        for i, line in enumerate(lines):

            if i == 0:
                c.drawString(text_x, y, line)
            else:
                y -= LINE_SPACING
                c.drawString(text_x, y, line)

        y -= LINE_SPACING


    def section(title):

        nonlocal y

        y -= SECTION_SPACING

        draw_text(title.upper(), MARGIN, y, FONT_BOLD, 12)

        y -= 4

        c.line(MARGIN, y, PAGE_WIDTH - MARGIN, y)

        y -= ITEM_SPACING


    # =====================================================
    # HEADER
    # =====================================================

    name = data.get("name", "Candidate Name")

    draw_text(name, MARGIN, y, FONT_BOLD, 20)

    y -= 20

    contact = data.get("contact", "")

    if contact:
        draw_text(contact, MARGIN, y, FONT, 10)
        y -= 16


    # =====================================================
    # SUMMARY
    # =====================================================

    summary = data.get("summary")

    if summary:
        section("Summary")
        draw_paragraph(summary)


    # =====================================================
    # SKILLS (UPDATED FOR CATEGORIZED SKILLS)
    # =====================================================

    skills = data.get("skills", {})

    if isinstance(skills, dict) and skills:

        section("Technical Skills")

        order = ["Languages", "Frameworks", "Tools", "Concepts"]

        for category in order:

            items = skills.get(category, [])

            if not items:
                continue

            category_line = f"{category}: {', '.join(items)}"

            draw_paragraph(category_line)


    # fallback if list (older format)
    elif isinstance(skills, list) and skills:

        section("Technical Skills")

        draw_paragraph(", ".join(skills))


    # =====================================================
    # EXPERIENCE
    # =====================================================

    experience = data.get("experience", [])[:MAX_EXPERIENCE]

    if experience:

        section("Experience")

        for job in experience:

            title = job.get("title", "Software Engineer")

            duration = job.get("duration", "")

            draw_text(title, MARGIN, y, FONT_BOLD, 11)

            if duration:
                draw_right(duration, y)

            y -= LINE_SPACING

            company = job.get("company", "")

            location = job.get("location", "")

            company_line = company

            if location:
                company_line += f" — {location}"

            draw_text(company_line, MARGIN, y, FONT, 10)

            y -= LINE_SPACING

            bullets = job.get("bullets", [])[:MAX_BULLETS_PER_ROLE]

            for bullet in bullets:
                draw_bullet(bullet)

            y -= 4


    # =====================================================
    # PROJECTS
    # =====================================================

    projects = data.get("projects", [])[:MAX_PROJECTS]

    if projects:

        section("Projects")

        for project in projects:

            title = project.get("title", "Project")

            draw_text(title, MARGIN, y, FONT_BOLD, 11)

            y -= LINE_SPACING

            bullets = project.get("bullets", [])[:MAX_BULLETS_PER_ROLE]

            for bullet in bullets:
                draw_bullet(bullet)

            y -= 4


    # =====================================================
    # EDUCATION
    # =====================================================

    education = data.get("education", [])

    if education:

        section("Education")

        for edu in education:

            degree = edu.get("degree", "")

            duration = edu.get("duration", "")

            draw_text(degree, MARGIN, y, FONT_BOLD, 11)

            if duration:
                draw_right(duration, y)

            y -= LINE_SPACING

            school = edu.get("school", "")

            location = edu.get("location", "")

            school_line = school

            if location:
                school_line += f" — {location}"

            draw_text(school_line, MARGIN, y, FONT, 10)

            y -= LINE_SPACING


    # =====================================================
    # SAVE
    # =====================================================

    c.save()

    return filename

