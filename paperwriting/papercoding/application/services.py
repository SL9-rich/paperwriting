from __future__ import annotations

import copy
import io
import re
import zipfile

from pypdf import PdfReader

from papercoding.infrastructure.storage import ConflictError, encode, now

from papercoding.domain.models import digest, paragraphs
from .contracts import PRINCIPLES, validate_candidates


def search(store, query, limit=8, mode="hybrid"):
    from papercoding.infrastructure.retrieval import retrieve

    return retrieve(store, query, limit, mode)["results"]


def add_pdf(store, filename, content):
    if not content.startswith(b"%PDF-"):
        raise ValueError("文件不是有效 PDF。")
    path = store.file(content, filename)
    reader = PdfReader(io.BytesIO(content))
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    quality = min(
        100, round(len(re.findall(r"[a-zA-Z\u3400-\u9fff]", text)) / max(1, len(reader.pages)) / 10)
    )
    return store.put_item(
        "literature",
        dict(
            filename=filename,
            path=path,
            text=text,
            quality=quality,
            status="extracted" if quality >= 10 else "needs_ocr",
            selected=False,
            analysis={},
        ),
    )


def analyze_pdf(store, provider, ident):
    item = next(x for x in store.items("literature") if x["id"] == ident)
    if len(item.get("text", "").strip()) < 100:
        raise ValueError("PDF 无可用文字层，需要先做 OCR。")
    result, _ = provider.json(
        PRINCIPLES
        + ' 分析文献，返回 {"title":"","summary":"","methods":"","findings":"","limitations":"","styleValue":"","warnings":[]}。不得凭文件名补写内容。',
        item["text"][:32000],
        4000,
    )
    item.update(analysis=result, status="ready", error="")
    store.put_item("literature", item, ident)
    return result


def style_profile(store, provider, requirements):
    selected = [x for x in store.items("literature") if x.get("selected") and x.get("text")]
    if not selected:
        raise ValueError("请先选择至少一篇文献加入风格分析。")
    result, _ = provider.json(
        PRINCIPLES
        + ' 总结可迁移风格，不复制独特长句。返回 {"globalProfile":"","sectionProfiles":{},"promptRules":[],"exampleExpressions":[]}。',
        encode(
            {
                "requirements": requirements,
                "sources": [
                    {"name": x["filename"], "text": x["text"][:6000]} for x in selected[:12]
                ],
            }
        ),
    )
    paper, revision = store.paper()
    paper["style"] = result
    store.save(paper, revision)
    return result


def add_template(store, name, files):
    texts, names, saved = [], [], []
    for filename, content in files:
        if filename.lower().endswith((".docx", ".zip")):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 100 * 1024 * 1024:
                    raise ValueError("模板解压后过大。")
                if (
                    filename.lower().endswith(".docx")
                    and "word/document.xml" not in archive.namelist()
                ):
                    raise ValueError("DOCX 缺少正文。")
                for info in archive.infolist():
                    names.append(info.filename.lower())
                    if info.file_size < 2_000_000 and (
                        info.filename.endswith((".cls", ".tex"))
                        or info.filename in {"word/document.xml", "word/styles.xml"}
                    ):
                        texts.append(archive.read(info).decode("utf-8", errors="replace"))
        else:
            texts.append(content[:2_000_000].decode("utf-8", errors="replace"))
        saved.append(dict(name=filename, path=store.file(content, filename)))
    if not saved:
        raise ValueError("请上传模板。")
    content = "\n".join(texts).lower()
    family = (
        "AGU"
        if "agujournal2019" in content or "agu word manuscript template" in content
        else "Copernicus"
        if "copernicus" in content or any("copernicus" in n for n in names)
        else "通用"
    )
    rules = {
        "requiredEndMatter": [
            "Acknowledgments",
            "Data availability",
            "Author contributions",
            "Competing interests",
        ],
        "sectionGuidance": ["Introduction", "Methods", "Results", "Discussion", "Conclusion"],
    }
    item = dict(name=name or "未命名期刊", family=family, files=saved, rules=rules)
    ident = store.put_item("template", item)
    paper, revision = store.paper()
    paper["template"] = dict(item, id=ident)
    store.save(paper, revision)
    return item


def context(paper, store):
    return dict(
        title=paper["title"],
        materials=paper["materials"],
        structure=paper["structure"],
        style=paper["style"],
        template=(paper.get("template") or {}).get("rules", {}),
        literature=[
            {
                "name": x["filename"],
                "analysis": x.get("analysis", {}),
                "excerpt": x.get("text", "")[:1500],
            }
            for x in store.items("literature")[:12]
        ],
    )


def generate_outline(store, provider):
    paper, revision = store.paper()
    if not str(paper["materials"].get("thesis", "")).strip() or not any(
        str(v).strip() for v in paper["materials"].get("methods", {}).values()
    ):
        raise ValueError("请先填写论文主线与研究方法。")
    result, _ = provider.json(
        PRINCIPLES
        + ' 按给出的章节顺序生成分章蓝图。返回 {"title":"建议标题","coreLogic":{"problem":"","innovation":"","boundary":""},"sections":[{"id":"01","title":"Abstract","purpose":"","keyPoints":[],"evidence":[],"placeholders":[],"transition":""}],"warnings":[]}。',
        encode(context(paper, store)),
        7000,
    )
    if not isinstance(result.get("sections"), list) or [
        x.get("id") for x in result["sections"]
    ] != [x["id"] for x in paper["structure"]]:
        raise ValueError("模型提纲未遵守章节目录，请重试。")
    for x, expected in zip(result["sections"], paper["structure"]):
        x["title"] = expected["title"]
    paper["outline"] = result
    store.save(paper, revision)
    return result


def write_full(store, provider):
    paper, base_revision = store.paper()
    if not paper.get("outline"):
        raise ValueError("请先生成并确认提纲。")
    ids = [s["id"] for s in paper["structure"]]
    run_id = store.new_run("按已确认提纲生成全文", ids, base_revision)
    try:
        result, tokens = provider.json(
            PRINCIPLES
            + ' 根据提纲写完整论文。章节编号逐字一致。返回 {"sections":[{"section_id":"01","revised_text":"完整正文"}],"warnings":[]}。',
            encode(dict(context(paper, store), outline=paper["outline"])),
            12000,
        )
        validate_candidates(result.get("sections"), ids)
        store.update_run(
            run_id,
            "waiting_for_approval",
            candidates=result["sections"],
            warnings=result.get("warnings", []),
            tokens=tokens,
            calls=1,
        )
        store.step(run_id, "全文生成", {"tokens": tokens, "warnings": result.get("warnings", [])})
        return {"run_id": run_id}
    except Exception as error:
        store.update_run(run_id, "failed", error=str(error))
        raise


def revise(store, provider, instruction, allowed, progress=lambda *args: None):
    from papercoding.application.workflow import revise as graph_revise

    return graph_revise(store, provider, instruction, allowed, progress)


def translate_text(provider, text, glossary="", polish=False):
    formulas = re.findall(r"\$\$[\s\S]*?\$\$", text)
    protected = text
    for i, formula in enumerate(formulas):
        protected = protected.replace(formula, f"[[PC_FORMULA_{i}]]", 1)
    task = "润色英文，保持原意" if polish else "忠实中译英"
    result, _ = provider.json(
        PRINCIPLES
        + f' {task}。数字、引用、单位和 [[PC_FORMULA_N]] 占位符必须保持。返回 {{"translated_text":"","warnings":[]}}。',
        encode({"text": protected, "glossary": glossary}),
        5000,
    )
    translated = result.get("translated_text")
    if not isinstance(translated, str) or not translated.strip():
        raise ValueError("模型没有返回翻译。")
    for i, formula in enumerate(formulas):
        token = f"[[PC_FORMULA_{i}]]"
        if translated.count(token) != 1:
            raise ValueError("翻译损坏了公式占位符，已拒绝保存。")
        translated = translated.replace(token, formula)
    return dict(
        source=text,
        source_hash=digest(text),
        candidate=translated,
        approved="",
        warnings=result.get("warnings", []),
        updatedAt=now(),
    )


def translate_batch(store, provider, ids, direct_save=False, progress=lambda *args: None):
    paper, _ = store.paper()
    sources = {"__paper_title__": [paper["title"]]}
    sources.update({i: paragraphs(paper["sections"][i]) for i in ids})
    result, done = {}, 0
    total = sum(len(v) for v in sources.values())
    for section_id, texts in sources.items():
        result[section_id] = []
        for text in texts:
            entry = translate_text(provider, text, paper["glossary"])
            if direct_save:
                entry["approved"], entry["candidate"] = entry["candidate"], ""
            result[section_id].append(entry)
            done += 1
            progress(
                round(done / total * 100),
                f"翻译 {done}/{total}",
                {"translations": copy.deepcopy(result)},
            )
    return {"translations": result}


def merge_translations(store, result, overwrite=False):
    paper, revision = store.paper()
    for section_id, entries in result.items():
        if section_id != "__paper_title__" and section_id not in paper["sections"]:
            raise ConflictError("原章节已删除，请重新翻译。")
        texts = (
            [paper["title"]]
            if section_id == "__paper_title__"
            else paragraphs(paper["sections"][section_id])
        )
        if len(entries) != len(texts) or any(
            e.get("source_hash") != digest(t) for e, t in zip(entries, texts)
        ):
            raise ConflictError("原文已改变或任务未完成，请重新翻译；没有覆盖现有英文。")
        previous = paper["translations"].get(section_id, [])
        if not overwrite and any(e.get("approved") or e.get("polished") for e in previous):
            raise ConflictError("已有确认英文，请勾选允许替换后再导入结果。")
    for section_id, entries in result.items():
        paper["translations"][section_id] = entries
    store.save(paper, revision)
    store.put_item("translation_history", {"created": now(), "result": result})
