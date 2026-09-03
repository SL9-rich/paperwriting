"""Custom browser UI served by Python. No JavaScript build toolchain."""

import json
from pathlib import Path
from urllib.parse import quote

from papercoding.config import ROOT, load_config, data_directory, embeddings_enabled
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from papercoding.infrastructure.storage import Store
from papercoding.domain.models import ConflictError, now
from papercoding.infrastructure.llm import Provider
from papercoding.application.jobs import Jobs
from papercoding.application.services import (
    PRINCIPLES,
    generate_outline,
    write_full,
    analyze_pdf,
    style_profile,
    translate_batch,
    translate_text,
    merge_translations,
    add_pdf,
    add_template,
    digest,
    paragraphs,
)
from papercoding.infrastructure.retrieval import retrieve, build_index, index_status
from papercoding.application.workflow import (
    create_run,
    execute as execute_run,
    decide,
    run_record,
    backup_state,
)
from papercoding.infrastructure.exporting import docx_bytes, markdown


def create_app(data_dir=None, provider=None):
    load_config()
    store = Store(Path(data_dir) if data_dir is not None else data_directory())
    model = provider or Provider()
    jobs = Jobs(store)

    def state():
        paper, revision = store.paper()
        return dict(
            paper=paper,
            revision=revision,
            literature=store.items("literature"),
            templates=store.items("template"),
            assets=store.items("asset"),
            chats=store.items("chat")[:30],
            jobs=store.jobs(),
            runs=store.runs(),
            versions=store.versions(),
            embeddings_enabled=embeddings_enabled(),
            model=getattr(model, "model", "") or "未配置",
            configured=bool(getattr(model, "configured", False)),
        )

    async def home(request):
        return FileResponse(ROOT / "frontend/index.html")

    async def get_state(request):
        return JSONResponse(await run_in_threadpool(state))

    def action(body):
        kind = body.get("action")
        paper, revision = store.paper()
        if kind == "save":
            if body.get("revision") != revision:
                raise ConflictError(
                    "论文已由其他操作更新。你的输入仍保留在页面，请先复制内容，再刷新核对。"
                )
            store.save(body["paper"], revision)
        elif kind == "version":
            store.version(body.get("label", "手动保存"))
        elif kind == "restore":
            store.restore(body["id"], body["revision"])
        elif kind == "delete_version":
            store.delete_version(body["id"])
        elif kind == "apply":
            decide(store, body["id"], True)
        elif kind == "reject":
            decide(store, body["id"], False)
        elif kind == "revise":
            instruction, allowed = body["instruction"], body["allowed"]
            if not instruction.strip() or not allowed:
                raise ValueError("请填写要求并选择允许修改的章节。")
            ident = create_run(store, instruction, allowed)
            jobs.submit(
                "章节修改",
                lambda progress: execute_run(store, model, ident, progress),
                context={"run_id": ident},
            )
        elif kind == "resume_run":
            ident = body["id"]
            run = run_record(store, ident)
            if run["payload"].get("engine") != "langgraph-v1" or run["status"] not in [
                "failed",
                "interrupted",
            ]:
                raise ValueError("仅支持继续中断或失败的 LangGraph 任务。")
            jobs.submit(
                "继续章节修改",
                lambda progress: execute_run(store, model, ident, progress),
                context={"run_id": ident},
            )
        elif kind == "discuss":
            question, selected = body["instruction"], body["section"]
            text = paper["sections"][selected]

            def discuss(progress):
                answer, _ = model.json(
                    PRINCIPLES + ' 讨论问题，不修改正文。返回 {"answer":""}。',
                    json.dumps(
                        {"question": question, "text": text, "materials": paper["materials"]},
                        ensure_ascii=False,
                    ),
                )
                store.put_item(
                    "chat", dict(question=question, answer=answer.get("answer", ""), created=now())
                )
                return {"answer": answer.get("answer", "")}

            jobs.submit("讨论与总结", discuss)
        elif kind == "outline":
            jobs.submit("提纲生成", lambda progress: generate_outline(store, model))
        elif kind == "write":
            jobs.submit("全文生成", lambda progress: write_full(store, model))
        elif kind == "analyze":
            jobs.submit("文献分析", lambda progress: analyze_pdf(store, model, body["id"]))
        elif kind == "style":
            jobs.submit(
                "风格分析",
                lambda progress: style_profile(store, model, body.get("requirements", "")),
            )
        elif kind == "select_literature":
            item = next(x for x in store.items("literature") if x["id"] == body["id"])
            item["selected"] = bool(body["selected"])
            store.put_item("literature", item, item["id"])
        elif kind == "delete_item":
            store.delete_item(body["id"])
        elif kind == "update_asset":
            item = next(x for x in store.items("asset") if x["id"] == body["id"])
            item.update(
                caption=str(body.get("caption", "")), section_id=str(body.get("section_id", ""))
            )
            if item["section_id"] and item["section_id"] not in paper["sections"]:
                raise ValueError("章节不存在。")
            store.put_item("asset", item, item["id"])
        elif kind == "translate_batch":
            jobs.submit(
                "批量翻译",
                lambda progress: translate_batch(
                    store, model, body["ids"], bool(body.get("direct")), progress
                ),
                context={
                    "sections": body["ids"],
                    "section_titles": {
                        s["id"]: s["title"] for s in paper["structure"] if s["id"] in body["ids"]
                    },
                    "mode": "batch",
                },
            )
        elif kind == "translate_one":
            section, index = body["section"], int(body["index"])
            if index < 0 or (section == "__paper_title__" and index != 0):
                raise ValueError("无效段落编号。")
            source = (
                paper["title"]
                if section == "__paper_title__"
                else paragraphs(paper["sections"][section])[index]
            )
            input_text = body.get("english", "") if body.get("polish") else source
            section_title = (
                "论文标题"
                if section == "__paper_title__"
                else next(s["title"] for s in paper["structure"] if s["id"] == section)
            )
            target = {
                "section": section,
                "section_title": section_title,
                "index": index,
                "source_hash": digest(source),
                "mode": "polish" if body.get("polish") else "translate",
            }

            def single(progress):
                progress(
                    0,
                    f"{section_title} · {'标题' if section == '__paper_title__' else '第 ' + str(index + 1) + ' 段'} · {'润色中' if body.get('polish') else '翻译中'}",
                )
                entry = translate_text(
                    model, input_text, paper.get("glossary", ""), bool(body.get("polish"))
                )
                entry.update(source=source, source_hash=digest(source))
                return dict(single_translation=entry, section=section, index=index)

            jobs.submit("英文润色" if body.get("polish") else "段落翻译", single, context=target)
        elif kind == "import_translation":
            job = next(j for j in store.jobs() if j["id"] == body["id"])
            if job["status"] != "success":
                raise ValueError("任务尚未成功完成。")
            if "translations" in job["result"]:
                merge_translations(
                    store, job["result"]["translations"], bool(body.get("overwrite"))
                )
            else:
                result = job["result"]
                section = result["section"]
                index = result["index"]
                entry = result["single_translation"]
                source = (
                    paper["title"]
                    if section == "__paper_title__"
                    else paragraphs(paper["sections"][section])[index]
                )
                if digest(source) != entry["source_hash"]:
                    raise ConflictError("原文已改变，请重新翻译。")
                items = paper["translations"].setdefault(section, [])
                while len(items) <= index:
                    items.append({})
                entry = dict(entry, approved=items[index].get("approved", ""))
                items[index] = entry
                store.save(paper, revision)
        elif kind == "confirm_translation":
            if body.get("revision") != revision:
                raise ConflictError("论文已更新，请刷新后确认英文。")
            section = body["section"]
            index = int(body["index"])
            source = (
                paper["title"]
                if section == "__paper_title__"
                else paragraphs(paper["sections"][section])[index]
            )
            if not body.get("text", "").strip():
                raise ValueError("英文不能为空。")
            items = paper["translations"].setdefault(section, [])
            while len(items) <= index:
                items.append({})
            job = next((j for j in store.jobs() if j["id"] == body.get("job_id")), None)
            if body.get("job_id"):
                if not job or job["status"] != "success":
                    raise ValueError("翻译任务尚未完成。")
                result = job["result"]
                candidate = result.get("single_translation") or (
                    result.get("translations", {}).get(section, [])[index]
                    if section in result.get("translations", {})
                    else {}
                )
                if candidate.get("source_hash") != digest(source):
                    raise ConflictError("原文已改变，请重新翻译后确认。")
                target = result.get("_context", {})
                if result.get("single_translation") and (
                    result.get("section") != section or result.get("index") != index
                ):
                    raise ValueError("任务不属于当前段落。")
            previous = items[index]
            if body.get("polish"):
                items[index] = dict(
                    previous,
                    source=source,
                    source_hash=digest(source),
                    polished=body["text"],
                    polishCandidate="",
                    updatedAt=now(),
                )
            else:
                items[index] = dict(
                    source=source,
                    source_hash=digest(source),
                    approved=body["text"],
                    candidate="",
                    warnings=[],
                    updatedAt=now(),
                )
            store.save(paper, revision)
            store.put_item(
                "translation_history",
                dict(section=section, index=index, entry=items[index], created=now()),
            )
            if job:
                result = dict(job["result"])
                confirmed = list(result.get("_confirmed", []))
                token = f"{section}:{index}:{'polish' if body.get('polish') else 'translate'}"
                if token not in confirmed:
                    confirmed.append(token)
                result["_confirmed"] = confirmed
                store.job_update(job["id"], "success", 100, "已完成", result)
        elif kind == "search":
            return retrieve(store, body.get("query", ""), mode=body.get("mode", "hybrid"))
        elif kind == "rag_status":
            return index_status(store)
        elif kind == "rag_rebuild":
            jobs.submit("文献向量索引", lambda progress: build_index(store, progress))
        elif kind == "steps":
            return {"steps": store.steps(body["id"])}
        else:
            raise ValueError("不支持的操作。")
        return state()

    async def post_action(request):
        body = await request.json()
        result = await run_in_threadpool(action, body)
        return JSONResponse(result)

    async def upload(request):
        form = await request.form(max_files=20, max_fields=5)
        uploads = form.getlist("files")
        if not uploads:
            raise ValueError("请选择文件。")
        content = []
        for file in uploads:
            data = await file.read(30 * 1024 * 1024 + 1)
            if len(data) > 30 * 1024 * 1024:
                raise ValueError("单个文件不能超过 30 MB。")
            content.append((file.filename, data))
        kind = str(form.get("kind", "literature"))

        def save_files():
            if kind == "template":
                add_template(store, str(form.get("name", "")), content)
            else:
                for name, data in content:
                    if kind == "literature":
                        add_pdf(store, name, data)
                    else:
                        suffix = Path(name).suffix.lower()
                        if suffix not in {
                            ".csv",
                            ".tsv",
                            ".xlsx",
                            ".docx",
                            ".png",
                            ".jpg",
                            ".jpeg",
                        }:
                            raise ValueError("不支持该文件类型。")
                        figure = suffix in {".png", ".jpg", ".jpeg"}
                        if figure:
                            import io
                            from PIL import Image

                            Image.open(io.BytesIO(data)).verify()
                        store.put_item(
                            "asset",
                            dict(
                                filename=name,
                                path=store.file(data, name),
                                kind="figure" if figure else "data",
                                caption="",
                                section_id="",
                            ),
                        )
            return state()

        return JSONResponse(await run_in_threadpool(save_files))

    async def download(request):
        path = request.query_params.get("path", "")
        data = await run_in_threadpool(store.read_file, path)
        image = Path(path).suffix.lower() in {".png", ".jpg", ".jpeg"}
        mime = (
            "image/png"
            if Path(path).suffix.lower() == ".png"
            else "image/jpeg"
            if image
            else "application/octet-stream"
        )
        return Response(
            data,
            media_type=mime,
            headers={
                "Content-Disposition": ("inline" if image else "attachment")
                + "; filename*=UTF-8''"
                + quote(Path(path).name)
            },
        )

    async def export(request):
        paper, _ = store.paper()
        kind = request.query_params.get("kind", "docx")
        if kind == "markdown":
            return Response(
                markdown(paper),
                media_type="text/markdown",
                headers={"Content-Disposition": 'attachment; filename="paper.md"'},
            )
        if kind == "backup":

            def backup():
                import io
                import zipfile

                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
                    for name, data in backup_state(store).items():
                        z.writestr(name, data)
                    for p in (store.root / "files").iterdir():
                        if p.is_file():
                            z.write(p, "files/" + p.name)
                return buffer.getvalue()

            return Response(
                await run_in_threadpool(backup),
                media_type="application/zip",
                headers={"Content-Disposition": 'attachment; filename="papercoding-backup.zip"'},
            )
        data = await run_in_threadpool(docx_bytes, store, paper, kind == "english")
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": 'attachment; filename="paper.docx"'},
        )

    async def table(request):
        ident = request.query_params["id"]
        item = next(x for x in store.items("asset") if x["id"] == ident)

        def read():
            import io
            import pandas as pd

            data = io.BytesIO(store.read_file(item["path"]))
            name = item["filename"].lower()
            if name.endswith(".docx"):
                from docx import Document

                return {"text": "\n".join(p.text for p in Document(data).paragraphs)}
            frame = (
                pd.read_excel(data)
                if name.endswith(".xlsx")
                else pd.read_csv(data, sep="\t" if name.endswith(".tsv") else ",")
            )
            return {
                "columns": list(frame.columns),
                "numeric": list(frame.select_dtypes("number").columns),
                "rows": json.loads(frame.head(100).to_json(orient="records")),
            }

        return JSONResponse(await run_in_threadpool(read))

    async def plot(request):
        body = await request.json()

        def draw():
            import io
            import pandas as pd
            import matplotlib

            matplotlib.use("Agg")
            from matplotlib.figure import Figure

            item = next(x for x in store.items("asset") if x["id"] == body["id"])
            data = io.BytesIO(store.read_file(item["path"]))
            frame = (
                pd.read_excel(data)
                if item["filename"].lower().endswith(".xlsx")
                else pd.read_csv(
                    data, sep="\t" if item["filename"].lower().endswith(".tsv") else ","
                )
            )
            fig = Figure(figsize=(8, 4), layout="constrained")
            ax = fig.subplots()
            for y in body["ys"]:
                if body["kind"] == "scatter":
                    ax.scatter(frame[body["x"]], frame[y], label=y)
                elif body["kind"] == "bar":
                    ax.bar(frame[body["x"]].astype(str), frame[y], label=y, alpha=0.7)
                else:
                    ax.plot(frame[body["x"]], frame[y], label=y)
            ax.legend()
            ax.set_xlabel(body["x"])
            output = io.BytesIO()
            fig.savefig(output, format="png", dpi=180)
            store.put_item(
                "asset",
                dict(
                    filename="plot.png",
                    path=store.file(output.getvalue(), "plot.png"),
                    kind="figure",
                    section_id="",
                    caption="",
                ),
            )
            return state()

        return JSONResponse(await run_in_threadpool(draw))

    routes = [
        Route("/", home),
        Route("/api/state", get_state),
        Route("/api/action", post_action, methods=["POST"]),
        Route("/api/upload", upload, methods=["POST"]),
        Route("/api/file", download),
        Route("/api/export", export),
        Route("/api/table", table),
        Route("/api/plot", plot, methods=["POST"]),
        Route("/health", lambda request: JSONResponse({"status": "ok"})),
        Mount("/static", app=StaticFiles(directory=ROOT / "frontend/static")),
    ]
    app = Starlette(routes=routes)

    async def boundaries(request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": "不允许跨站修改本地数据。"}, status_code=403)
        try:
            result = await call_next(request)
        except ConflictError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except (ValueError, KeyError, StopIteration, IndexError) as error:
            return JSONResponse({"error": str(error) or "无效请求"}, status_code=400)
        except Exception:
            import logging

            logging.exception("PaperCoding request failed")
            return JSONResponse(
                {"error": "操作未完成，请检查文件格式或稍后重试。"}, status_code=500
            )
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Cache-Control"] = "no-store"
        return result

    from starlette.middleware.base import BaseHTTPMiddleware

    app.add_middleware(BaseHTTPMiddleware, dispatch=boundaries)
    app.state.store = store
    app.state.jobs = jobs
    return app
