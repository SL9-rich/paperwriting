import io

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches

from papercoding.domain.models import digest, paragraphs


def english_paper(paper):
    import copy

    result = copy.deepcopy(paper)
    for section_id in ["__paper_title__"] + [s["id"] for s in paper["structure"]]:
        sources = (
            [paper["title"]]
            if section_id == "__paper_title__"
            else paragraphs(paper["sections"][section_id])
        )
        entries = paper["translations"].get(section_id, [])
        if len(entries) != len(sources):
            raise ValueError("英文尚未全部确认，或原文段落已改变。")
        for source, entry in zip(sources, entries):
            if not (entry.get("polished") or entry.get("approved")) or entry.get(
                "source_hash"
            ) != digest(source):
                raise ValueError("存在未确认或与当前原文不一致的英文，请在翻译页重新检查。")
        value = "\n\n".join(e.get("polished") or e["approved"] for e in entries)
        if section_id == "__paper_title__":
            result["title"] = value
        else:
            result["sections"][section_id] = value
    return result


def markdown(paper):
    return (
        "# "
        + paper["title"]
        + "\n\n"
        + "\n\n".join(
            "## " + s["title"] + "\n\n" + paper["sections"][s["id"]] for s in paper["structure"]
        )
    )


def docx_bytes(store, paper, english=False):
    paper = english_paper(paper) if english else paper
    template = paper.get("template") or {}
    candidates = [
        f
        for f in template.get("files", [])
        if f["name"].lower().endswith(".docx") and f.get("path")
    ]
    if not candidates:
        raise ValueError("请先在目标期刊中选择可用的 DOCX 模板。")
    document = Document(io.BytesIO(store.read_file(candidates[0]["path"])))
    body = document._element.body
    # Preserve the final section properties and the rest of the template package.
    for element in list(body):
        if element.tag != qn("w:sectPr"):
            body.remove(element)
    names = {s.name for s in document.styles}
    ids = {s.style_id: s.name for s in document.styles}

    def style(*choices):
        for name in choices:
            if name in names:
                return name
            if name in ids:
                return ids[name]
        return "Normal"

    def add(text, *styles):
        p = document.add_paragraph(style=style(*styles))
        r = p.add_run(str(text))
        r.font.name = "Times New Roman"
        r._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "Microsoft YaHei")
        return p

    add(paper["title"], "MStitle", "Title")
    meta = paper.get("metadata", {})
    for key in [
        "authors",
        "affiliations",
        "correspondingAuthor",
        "email",
        "orcid",
        "name",
        "studentId",
        "institution",
        "courseName",
        "instructor",
    ]:
        if meta.get(key):
            add(meta[key], "Authors" if key in {"authors", "name"} else "Affiliation", "Normal")
    figures = [x for x in store.items("asset") if x.get("kind") == "figure" and x.get("section_id")]
    for section in paper["structure"]:
        add(section["title"], "Heading-Main", "Heading1", "Heading 1")
        for text in paragraphs(paper["sections"][section["id"]]):
            paragraph = add(text.lstrip(" \t\u3000"), "Text", "Normal")
            # OOXML character units: 100 = one character; override template hanging indent.
            indent = paragraph._p.get_or_add_pPr().get_or_add_ind()
            for attribute in ("firstLine", "hanging", "hangingChars"):
                indent.attrib.pop(qn("w:" + attribute), None)
            indent.set(qn("w:firstLineChars"), "200")
        for figure in figures:
            if figure["section_id"] == section["id"]:
                document.add_picture(io.BytesIO(store.read_file(figure["path"])), width=Inches(5.8))
                add(figure.get("caption") or figure["filename"], "Caption", "Normal")
    required = template.get("rules", {}).get("requiredEndMatter", [])
    headings = list(dict.fromkeys(required + list(paper.get("end_matter", {}))))
    for heading in headings:
        add(heading, "Heading-Main", "Heading1", "Heading 1")
        add(
            paper.get("end_matter", {}).get(heading) or "[Pending author confirmation]",
            "Text",
            "Normal",
        )
    if paper.get("references"):
        add("References", "Heading-Main", "Heading1", "Heading 1")
        for text in paragraphs(paper["references"]):
            add(text, "Reference", "Normal")
    document.core_properties.title = paper["title"]
    document.core_properties.author = meta.get("authors") or meta.get("name", "")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
