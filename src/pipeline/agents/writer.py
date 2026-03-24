"""
Writer Agent — Phase 3 implementation.

Responsibility: Generate a tailored resume and cover letter (.docx) for a
specific job listing using the user's CV as the source of truth.

LangGraph patterns exercised here:
  - interrupt() for the pre-write interview and review gates
  - Cycles: the feedback revision loop (write → review → revise → review)
  - Structured LLM output as rendering input (not final artifact)
"""

from __future__ import annotations

import re
from pathlib import Path

import anthropic
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from pipeline.config import LLM_MODEL, settings
from pipeline.state import (
    CoverLetterContent,
    JobListing,
    PipelineState,
    ResumeContent,
)

# ── Stop words for gap extraction ──────────────────────────────────────────────

_STOP_WORDS = {
    "experience", "required", "strong", "skills", "plus", "with", "the",
    "and", "for", "our", "you", "will", "must", "have", "team", "using",
    "work", "build", "design", "maintain", "deploy", "manage", "collaborate",
    "familiarity", "preferred", "ability", "knowledge", "understanding",
    "including", "across", "within", "between", "through", "from", "into",
    "that", "this", "their", "they", "your", "platform", "systems", "service",
    "services", "applications", "application", "tools", "tool", "solutions",
    "solution", "environment", "environments", "data", "product", "products",
}


# ── Pure helper functions ──────────────────────────────────────────────────────


def _load_cv_text(cv_path: str) -> str:
    """
    Read CV content from disk. Handles both plain text and PDF.
    PDF import is lazy so tests using .txt fixtures don't need pypdf installed.
    Raises FileNotFoundError if the path does not exist.
    """
    path = Path(cv_path)
    if not path.exists():
        raise FileNotFoundError(f"CV not found at: {cv_path}")

    if path.suffix.lower() == ".pdf":
        import pypdf  # lazy — only needed at runtime with a real CV
        reader = pypdf.PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    return path.read_text(encoding="utf-8")


def _extract_job_gaps(cv_text: str, job_description: str) -> list[str]:
    """
    Identify keywords from the job description that are absent from the CV.

    Strategy:
      1. Tokenize the JD into words, strip punctuation.
      2. Keep words >= 4 chars that aren't in the stop-word list.
      3. For each candidate keyword, check if it appears anywhere in the CV
         (case-insensitive substring match).
      4. Return those that don't appear — these are the gaps.

    Returns a list of gap keyword strings (lowercased).
    """
    cv_lower = cv_text.lower()

    # Tokenize JD: split on whitespace, strip punctuation
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9+#.-]*", job_description)

    gaps: list[str] = []
    seen: set[str] = set()

    for token in tokens:
        word = token.lower().rstrip(".,;:!?()")
        if len(word) < 4:
            continue
        if word in _STOP_WORDS:
            continue
        if word in seen:
            continue
        seen.add(word)
        if word not in cv_lower:
            gaps.append(word)

    return gaps


def _build_resume_prompt(
    cv_text: str,
    job: JobListing,
    answers: dict[str, str],
) -> str:
    """Build the system+user prompt for resume generation."""
    answers_block = (
        "\n".join(f"- {k}: {v}" for k, v in answers.items())
        if answers
        else "None provided."
    )
    return (
        f"You are an expert resume writer. Tailor the candidate's CV for the following role.\n\n"
        f"TARGET ROLE: {job.title} at {job.company} ({job.location})\n\n"
        f"JOB DESCRIPTION:\n{job.description or 'Not provided.'}\n\n"
        f"CANDIDATE CV:\n{cv_text}\n\n"
        f"PRE-WRITE INTERVIEW ANSWERS:\n{answers_block}\n\n"
        "Return ONLY a valid JSON object matching this schema — no preamble, no markdown fences:\n"
        '{"name": str, "contact": str, "summary": str, '
        '"sections": [{"heading": str, "bullets": [str]}], "skills": [str]}'
    )


def _build_cover_letter_prompt(
    cv_text: str,
    job: JobListing,
    answers: dict[str, str],
) -> str:
    """Build the system+user prompt for cover letter generation."""
    answers_block = (
        "\n".join(f"- {k}: {v}" for k, v in answers.items())
        if answers
        else "None provided."
    )
    return (
        f"You are an expert cover letter writer. Write a compelling, concise cover letter.\n\n"
        f"TARGET ROLE: {job.title} at {job.company} ({job.location})\n\n"
        f"JOB DESCRIPTION:\n{job.description or 'Not provided.'}\n\n"
        f"CANDIDATE CV:\n{cv_text}\n\n"
        f"PRE-WRITE INTERVIEW ANSWERS:\n{answers_block}\n\n"
        "Return ONLY a valid JSON object matching this schema — no preamble, no markdown fences:\n"
        '{"opening": str, "body_paragraphs": [str], "closing": str}'
    )


def _parse_resume_json(raw: str) -> ResumeContent:
    """
    Parse raw LLM output into a ResumeContent model.
    Raises pydantic.ValidationError if required fields are missing or malformed.
    Strips markdown code fences if the model added them despite instructions.
    """
    cleaned = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```$", "", cleaned.strip(), flags=re.MULTILINE)
    return ResumeContent.model_validate_json(cleaned.strip())


def _parse_cover_letter_json(raw: str) -> CoverLetterContent:
    """
    Parse raw LLM output into a CoverLetterContent model.
    Raises pydantic.ValidationError if required fields are missing or malformed.
    Strips markdown code fences if present.
    """
    cleaned = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```$", "", cleaned.strip(), flags=re.MULTILINE)
    return CoverLetterContent.model_validate_json(cleaned.strip())


def _add_hyperlink(paragraph, text: str, url: str) -> None:
    """
    Insert a hyperlink run into a paragraph using raw OOXML manipulation.
    python-docx does not natively support hyperlink runs; this is the
    standard workaround via relationship + XML element construction.
    """
    part = paragraph.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    run_elem = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    rpr.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rpr.append(underline)
    run_elem.append(rpr)

    t = OxmlElement("w:t")
    t.text = text
    run_elem.append(t)
    hyperlink.append(run_elem)
    paragraph._p.append(hyperlink)


def _render_resume_docx(content: ResumeContent, output_path: str) -> str:
    """
    Render a ResumeContent model to a .docx file using python-docx.
    Returns the output_path on success.

    Layout mirrors the master resume: name as large heading, contact line,
    horizontal rule, summary, then sections with heading + bullet list.
    One-page heuristic: warns (via print) if character count exceeds 3,500.
    """
    doc = Document()

    # Remove default margins slightly to give more room
    for section in doc.sections:
        section.top_margin = Pt(36)
        section.bottom_margin = Pt(36)
        section.left_margin = Pt(54)
        section.right_margin = Pt(54)

    # Name
    name_para = doc.add_paragraph()
    name_run = name_para.add_run(content.name)
    name_run.bold = True
    name_run.font.size = Pt(14)

    # Contact line
    doc.add_paragraph(content.contact)

    # Horizontal rule via bottom border on an empty paragraph
    rule_para = doc.add_paragraph()
    pPr = rule_para._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "000000")
    pBdr.append(bottom)
    pPr.append(pBdr)

    # Summary
    summary_heading = doc.add_paragraph()
    summary_heading.add_run("PROFESSIONAL SUMMARY").bold = True
    doc.add_paragraph(content.summary)

    # Skills
    skills_heading = doc.add_paragraph()
    skills_heading.add_run("SKILLS & TECHNOLOGIES").bold = True
    doc.add_paragraph(" | ".join(content.skills))

    # Experience sections
    for section_obj in content.sections:
        heading_para = doc.add_paragraph()
        heading_para.add_run(section_obj.heading.upper()).bold = True
        for bullet in section_obj.bullets:
            doc.add_paragraph(bullet, style="List Bullet")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)

    # One-page heuristic warning
    total_chars = (
        len(content.summary) + len(" ".join(content.skills))
        + sum(len(b) for s in content.sections for b in s.bullets)
    )
    if total_chars > 3500:
        print(
            f"[writer] Warning: resume content is ~{total_chars} chars — "
            "may exceed one page. Review the rendered .docx."
        )

    return output_path


def _render_cover_letter_docx(content: CoverLetterContent, output_path: str) -> str:
    """
    Render a CoverLetterContent model to a .docx file using python-docx.
    Returns the output_path on success.

    Layout: opening paragraph(s), body paragraphs with spacing, closing.
    """
    doc = Document()

    for section in doc.sections:
        section.top_margin = Pt(54)
        section.bottom_margin = Pt(54)
        section.left_margin = Pt(72)
        section.right_margin = Pt(72)

    # Opening — may contain \n line breaks within the string
    for line in content.opening.split("\n"):
        line = line.strip()
        if line:
            doc.add_paragraph(line)

    doc.add_paragraph("")  # spacer

    # Body paragraphs
    for para in content.body_paragraphs:
        p = doc.add_paragraph(para)
        p.paragraph_format.space_after = Pt(8)

    doc.add_paragraph("")  # spacer

    # Closing
    for line in content.closing.split("\n"):
        line = line.strip()
        if line:
            doc.add_paragraph(line)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)

    return output_path


# ── Node stubs (implemented in Commits 6–8) ───────────────────────────────────


def load_job(state: PipelineState) -> dict:
    """Fetch job record from DB by current_job_id. Commit 8 target."""
    return {}


def fetch_cv(state: PipelineState) -> dict:
    """Read CV from CV_PATH, extract text. Cached after first call. Commit 8 target."""
    return {}


def research_company(state: PipelineState) -> dict:
    """
    Web search for headcount, mission, recent news.
    Results cached in company_cache table. Commit 8 target.
    """
    return {}


def pre_write_interview(state: PipelineState) -> dict:
    """
    Human-in-the-loop gate: extract gaps between CV and JD, surface targeted
    questions, wait for user answers before document generation begins.

    interrupt() returns the answers dict supplied by Command(resume=...).
    """
    job = next(
        (j for j in state.get("shortlist", []) if j.id == state.get("current_job_id")),
        None,
    )
    cv_text = state.get("cv_text") or ""

    gaps: list[str] = []
    if job and job.description:
        gaps = _extract_job_gaps(cv_text, job.description)

    # Build one targeted question per gap (cap at 5 to keep the interview short)
    questions = [
        f"The JD mentions '{g}' — describe any experience you have with this, or press Enter to skip."
        for g in gaps[:5]
    ]

    answers = interrupt({
        "gaps": gaps,
        "questions": questions,
        "message": "Answer the following before document generation begins.",
    })

    return {"interview_answers": answers if isinstance(answers, dict) else {}}


def write_resume(state: PipelineState) -> dict:
    """
    LLM call (Anthropic API): generate resume content as structured JSON,
    then render immediately to .docx. One call per round.

    Rendering happens here rather than in the separate render_resume_docx node
    so that the revision loop (apply_feedback → write_resume) re-renders in
    a single step without an extra node hop.
    """
    job = next(
        (j for j in state.get("shortlist", []) if j.id == state.get("current_job_id")),
        None,
    )
    if job is None:
        return {"errors": state.get("errors", []) + ["write_resume: job not found in shortlist"]}

    cv_text = state.get("cv_text") or ""
    answers = state.get("interview_answers") or {}

    prompt = _build_resume_prompt(cv_text, job, answers)

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=LLM_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text
    content = _parse_resume_json(raw)

    output_dir = str(settings.output_dir)
    output_path = f"{output_dir}/{job.id}_resume.docx"
    _render_resume_docx(content, output_path)

    return {"resume_content": content, "resume_path": output_path}


def write_cover_letter(state: PipelineState) -> dict:
    """
    LLM call (Anthropic API): generate cover letter content as structured JSON,
    then render immediately to .docx.
    """
    job = next(
        (j for j in state.get("shortlist", []) if j.id == state.get("current_job_id")),
        None,
    )
    if job is None:
        return {"errors": state.get("errors", []) + ["write_cover_letter: job not found in shortlist"]}

    cv_text = state.get("cv_text") or ""
    answers = state.get("interview_answers") or {}

    prompt = _build_cover_letter_prompt(cv_text, job, answers)

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=LLM_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text
    content = _parse_cover_letter_json(raw)

    output_dir = str(settings.output_dir)
    output_path = f"{output_dir}/{job.id}_cover_letter.docx"
    _render_cover_letter_docx(content, output_path)

    return {"cover_letter_content": content, "cover_letter_path": output_path}


def render_resume_docx(state: PipelineState) -> dict:
    """Deterministic python-docx rendering from structured JSON. Commit 6 target."""
    return {}


def render_cover_letter_docx(state: PipelineState) -> dict:
    """Deterministic python-docx rendering from structured JSON. Commit 6 target."""
    return {}


def review_interrupt(state: PipelineState) -> dict:
    """
    Human-in-the-loop gate: show generated doc paths, wait for the user to
    approve, provide feedback, or abort.

    interrupt() returns the string supplied by Command(resume=...):
      "approve" or None → persist_documents
      "abort"           → exits with a warning (handled by CLI)
      any other string  → stored as human_feedback → routes to apply_feedback
    """
    feedback = interrupt({
        "resume_path": state.get("resume_path"),
        "cover_letter_path": state.get("cover_letter_path"),
        "message": "Review documents. Reply: 'approve', 'abort', or type feedback.",
    })

    if feedback is None or str(feedback).strip().lower() == "approve":
        return {"human_feedback": None, "human_approved": True}
    elif str(feedback).strip().lower() == "abort":
        return {
            "human_feedback": None,
            "human_approved": False,
            "warnings": state.get("warnings", []) + ["Application aborted by user at review gate."],
        }
    else:
        return {"human_feedback": str(feedback), "human_approved": False}


def apply_feedback(state: PipelineState) -> dict:
    """
    Targeted LLM call to revise resume based on user feedback.
    Passes the previous resume JSON + feedback as a revision prompt —
    cheaper and more accurate than full regeneration.
    Increments revision_round. Re-renders both docs.
    """
    job = next(
        (j for j in state.get("shortlist", []) if j.id == state.get("current_job_id")),
        None,
    )
    feedback = state.get("human_feedback") or ""
    prev_content = state.get("resume_content")
    prev_json = prev_content.model_dump_json() if prev_content else "{}"

    revision_prompt = (
        f"The candidate has reviewed their tailored resume and provided this feedback:\n\n"
        f"FEEDBACK: {feedback}\n\n"
        f"Here is the current resume JSON:\n{prev_json}\n\n"
        "Apply the feedback and return ONLY an updated JSON object with the same schema. "
        "No preamble, no markdown fences."
    )

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=LLM_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": revision_prompt}],
    )
    raw = message.content[0].text
    content = _parse_resume_json(raw)

    output_dir = str(settings.output_dir)
    job_id = state.get("current_job_id", "unknown")
    resume_path = f"{output_dir}/{job_id}_resume.docx"
    _render_resume_docx(content, resume_path)

    return {
        "resume_content": content,
        "resume_path": resume_path,
        "revision_round": state.get("revision_round", 0) + 1,
        "human_feedback": None,  # clear feedback after applying
    }


def persist_documents(state: PipelineState) -> dict:
    """Update jobs row with doc paths, set status → docs_ready. Commit 8 target."""
    return {}


def should_revise(state: PipelineState) -> str:
    """
    Conditional edge: route back to write nodes if feedback given and
    revision budget remains, otherwise exit.
    """
    revision_round = state.get("revision_round", 0)
    human_feedback = state.get("human_feedback")
    max_rounds = settings.max_revision_rounds

    if human_feedback and revision_round < max_rounds:
        return "apply_feedback"
    if revision_round >= max_rounds:
        return "warn_and_exit"
    return "persist_documents"


def warn_and_exit(state: PipelineState) -> dict:
    """Surface warning when max revision rounds are reached without approval."""
    return {
        "warnings": state.get("warnings", [])
        + [
            f"Max revision rounds ({settings.max_revision_rounds}) reached — "
            "documents saved as draft. Re-run writer for this job_id to continue."
        ]
    }


# ── Graph assembly ─────────────────────────────────────────────────────────────


def build_writer_graph() -> StateGraph:
    """
    Compile the Writer subgraph.

    The revision loop is the key LangGraph pattern here:
      write_resume / write_cover_letter → render → review_interrupt
        → apply_feedback (if feedback) → write again (cycle)
        → persist_documents (if approved)
        → warn_and_exit (if max rounds exceeded)
    """
    graph = StateGraph(PipelineState)

    graph.add_node("load_job", load_job)
    graph.add_node("fetch_cv", fetch_cv)
    graph.add_node("research_company", research_company)
    graph.add_node("pre_write_interview", pre_write_interview)
    graph.add_node("write_resume", write_resume)
    graph.add_node("write_cover_letter", write_cover_letter)
    graph.add_node("render_resume_docx", render_resume_docx)
    graph.add_node("render_cover_letter_docx", render_cover_letter_docx)
    graph.add_node("review_interrupt", review_interrupt)
    graph.add_node("apply_feedback", apply_feedback)
    graph.add_node("persist_documents", persist_documents)
    graph.add_node("warn_and_exit", warn_and_exit)

    graph.set_entry_point("load_job")
    graph.add_edge("load_job", "fetch_cv")
    graph.add_edge("fetch_cv", "research_company")
    graph.add_edge("research_company", "pre_write_interview")
    graph.add_edge("pre_write_interview", "write_resume")
    graph.add_edge("write_resume", "write_cover_letter")
    graph.add_edge("write_cover_letter", "render_resume_docx")
    graph.add_edge("render_resume_docx", "render_cover_letter_docx")
    graph.add_edge("render_cover_letter_docx", "review_interrupt")

    graph.add_conditional_edges(
        "review_interrupt",
        should_revise,
        {
            "apply_feedback": "apply_feedback",
            "persist_documents": "persist_documents",
            "warn_and_exit": "warn_and_exit",
        },
    )
    graph.add_edge("apply_feedback", "write_resume")
    graph.add_edge("persist_documents", END)
    graph.add_edge("warn_and_exit", END)

    return graph
