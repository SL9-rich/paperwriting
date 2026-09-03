'use strict';
const $ = s => document.querySelector(s),
    $$ = s => [...document.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
} [c]));
const para = t => String(t || '').split(/\n\s*\n/).map(t => t.trim()).filter(Boolean);
// Visual first-line indentation; keep whitespace out of persisted research text.
const indentForEditor = text => String(text || '').replace(/(^|\n[ \t]*\n)[ \t\u3000]*(?=\S)/g, '$1\u3000\u3000');
const plainEditorText = text => String(text).replace(/(^|\n[ \t]*\n)\u3000\u3000/g, '$1');
const pretty = v => `<pre>${esc(JSON.stringify(v,null,2))}</pre>`;
const btn = (text, id, primary = false) => `<button id="${id}" class="${primary?'primary':'ghost'}-button">${text}</button>`;
const field = (label, id, value = '', area = false) => `<label class="field">${esc(label)}${area?`<textarea id="${id}">${esc(value)}</textarea>`:`<input id="${id}" value="${esc(value)}">`}</label>`;
const fileLink = (path, name) => `<a class="ghost-button" href="/api/file?path=${encodeURIComponent(path)}" download="${esc(name)}">下载原文件</a>`;
let state, active = '01',
    view = 'workspace',
    type = 'thesis',
    integrated = false,
    zoom = 1,
    saveTimer, saveQueue = Promise.resolve(),
    agentSignature = '',
    lastInput = '',
    busy = false;
const materialTypes = [
    ['metadata', '基础信息', '作者与文档信息', 'i', 'indigo'],
    ['thesis', '论文主线', '研究问题与核心贡献', '⌁', 'green'],
    ['methods', '研究方法', '研究路线与实验设计', '◇', 'amber'],
    ['data', '实验数据与绘图', '数据文件与论文图库', '▥', 'blue'],
    ['literature', '参考论文', '文献证据与内容分析', '≡', 'rose'],
    ['template', '目标期刊', '投稿要求与排版模板', '▤', 'violet'],
    ['style', '写作风格', '个人风格与参考范式', 'Aa', 'indigo']
];

function toast(text, error = false) {
    const t = $('#toast');
    t.textContent = text;
    t.classList.add('visible');
    t.style.background = error ? '#994944' : '#38322e';
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove('visible'), error ? 9000 : 3500)
}
async function api(path, options = {}) {
    const r = await fetch(path, options);
    if (!r.ok) {
        let data;
        try {
            data = await r.json()
        } catch {}
        throw Error(data?.error || `请求失败 (${r.status})`)
    }
    return r.json()
}
async function action(action, values = {}) {
    return api('/api/action', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            action,
            ...values
        })
    })
}

function guard(fn) {
    return async e => {
        const button = e?.target?.closest?.('button');
        if (e?.type === 'click' && e.currentTarget === document && !Object.keys(button?.dataset || {}).length) return;
        try {
            if (button) button.disabled = true;
            await fn(e)
        } catch (err) {
            toast(err.message, true)
        } finally {
            if (button && button.isConnected) button.disabled = false
        }
    }
}

function on(id, fn) {
    const el = $('#' + id);
    if (el) el.onclick = guard(fn)
}

function assign(data) {
    if (data.paper) {
        state = data;
        if (!state.paper.sections.hasOwnProperty(active)) active = state.paper.structure[0].id;
        $('#projectTitle').textContent = state.paper.title
    }
}
async function reload() {
    assign(await api('/api/state'));
    render()
}

function save(mutator) {
    saveQueue = saveQueue.catch(() => {}).then(async () => {
        const p = structuredClone(state.paper);
        mutator(p);
        $('#saveState').className = 'save-state busy';
        $('#saveState').textContent = '保存中…';
        try {
            assign(await action('save', {
                paper: p,
                revision: state.revision
            }));
            $('#saveState').className = 'save-state';
            $('#saveState').innerHTML = '<i></i>已保存'
        } catch (err) {
            $('#saveState').className = 'save-state error';
            $('#saveState').textContent = '保存失败';
            throw err
        }
    });
    return saveQueue
}
async function flush() {
    clearTimeout(saveTimer);
    if (view === 'workspace' && !integrated && $('#editor').value !== lastInput) {
        const displayed = $('#editor').value,
            text = plainEditorText(displayed),
            id = active;
        await save(p => p.sections[id] = text);
        lastInput = displayed
    }
    await saveQueue
}

function showDialog(title, html) {
    $('#dialogTitle').textContent = title;
    $('#dialogBody').innerHTML = html;
    if (!$('#dialog').open) $('#dialog').showModal()
}

function closeDialog() {
    $('#dialog').close()
}

function uploadForm(kind, caption = '选择文件', accept = '.pdf', name = false) {
    return `<label class="upload-area">${caption}<input type="file" id="upload" accept="${accept}" multiple></label>${name?field('期刊名称','journalName'):''}<div class="actions">${btn('上传并保存','uploadButton',true)}</div>`
}

function bindUpload(kind) {
    on('uploadButton', async () => {
        const files = $('#upload').files;
        if (!files.length) throw Error('请选择文件');
        const body = new FormData();
        body.append('kind', kind);
        if ($('#journalName')) body.append('name', $('#journalName').value);
        for (const f of files) body.append('files', f);
        toast('正在读取和保存文件…');
        assign(await api('/api/upload', {
            method: 'POST',
            body
        }));
        renderSecondary();
        toast('文件已保存')
    })
}
async function navigate(next, sub = 'thesis') {
    await flush();
    assign(await api('/api/state'));
    view = next;
    type = sub;
    history.pushState({}, '', next === 'workspace' ? '/' : `/?view=${next}&type=${sub}`);
    render()
}

function render() {
    if (!state) return;
    $('#secondary').classList.toggle('translation-workspace', view === 'translate');
    $('#workspace').hidden = view !== 'workspace';
    $('#secondary').hidden = view === 'workspace';
    if (view === 'workspace') renderWorkspace();
    else renderSecondary();
    renderAgent(true)
}

function renderWorkspace() {
    const p = state.paper;
    const ready = {
        metadata: !!Object.values(p.metadata).find(Boolean),
        thesis: !!p.materials.thesis,
        methods: !!Object.values(p.materials.methods || {}).find(Boolean),
        data: !!state.assets.length,
        literature: !!state.literature.length,
        template: !!p.template,
        style: !!Object.keys(p.style || {}).length
    };
    $('#materialList').innerHTML = materialTypes.map(([id, title, desc, icon, color]) => `<button class="material-card" data-material="${id}"><span class="material-icon ${color}">${icon}</span><span><strong>${title}</strong><small>${desc}</small></span><span class="material-card-end"><i class="material-status-dot ${ready[id]?'complete':'incomplete'}" title="${ready[id]?'已有内容':'待补充'}"></i><b>›</b></span></button>`).join('');
    const n = Object.values(ready).filter(Boolean).length;
    $('#readiness').textContent = `${n} / 7`;
    $('#readinessBar').style.width = `${n/7*100}%`;
    $('#chapterTabs').innerHTML = p.structure.map((s, i) => `<button data-chapter="${esc(s.id)}" class="${s.id===active?'active':''}">${i?`<span>${String(i).padStart(2,'0')}</span>`:''}${esc(s.title)}</button>`).join('') + '<button class="chapter-structure-button" id="structureButton" aria-label="管理章节">＋</button>';
    const index = p.structure.findIndex(s => s.id === active),
        section = p.structure[index];
    $('#chapterNumber').textContent = index ? `第 ${index} 章` : '前置部分 · 摘要';
    $('#wordCount').textContent = `中文稿 · ${(p.sections[active]||'').length} 字`;
    $('#chapterTitle').textContent = integrated ? p.title : section.title;
    $('#pageCount').textContent = String(index + 1).padStart(2, '0');
    $('#paperTemplate').textContent = p.template?.name || '研究论文 · 自由写作';
    $('#editor').hidden = integrated;
    $('#integratedText').hidden = !integrated;
    $('#chapterView').classList.toggle('active', !integrated);
    $('#integratedView').classList.toggle('active', integrated);
    if (!integrated) {
        $('#editor').value = indentForEditor(p.sections[active]);
        lastInput = $('#editor').value;
        fitEditor()
    } else $('#integratedText').innerHTML = p.structure.map(s => `<h3>${esc(s.title)}</h3>${para(p.sections[s.id]).map(text=>`<p class="body-paragraph">${esc(text)}</p>`).join('')}`).join('');
    const figs = state.assets.filter(a => a.kind === 'figure' && a.section_id === active);
    $('#figureArea').innerHTML = figs.length ? figs.map(f => `<figure><img class="asset-image" src="/api/file?path=${encodeURIComponent(f.path)}" alt="${esc(f.caption||f.filename)}"><figcaption>${esc(f.caption||f.filename)}</figcaption></figure>`).join('') : `<div class="figure-placeholder"><div class="placeholder-symbol">＋</div><div><strong>为本章添加图表</strong><p>上传正式图片，设置图注与插入位置。</p></div><button data-view="materials" data-type="data">添加图片</button></div>`;
    const outline = p.outline?.sections?.find(s => (s.id || s.number) === active);
    $('#handoff').innerHTML = `<div class="handoff-title"><span>章节交接卡</span><small>${outline?'来自已保存提纲':'写作提示'}</small></div><div class="handoff-grid"><div><span>本章重点</span><p>${esc(outline?.purpose||'围绕研究问题组织论证，区分事实与判断。')}</p></div><div><span>下一章计划</span><p>${esc(outline?.transition||'完成本章后，检查章节之间的衔接。')}</p></div><div><span>等待补充</span><p>${esc(outline?.placeholders?.join('；')||'请检查实验数据、图片和引用是否齐全。')}</p></div><div><span>结论边界</span><p>${esc(p.outline?.coreLogic?.boundary||'仅表达现有证据支持的结论。')}</p></div></div>`;
    on('structureButton', structureDialog);
}

function fitEditor() {
    const e = $('#editor');
    e.style.height = 'auto';
    e.style.height = Math.max(360, e.scrollHeight + 4) + 'px'
}

function renderAgent(force = false) {
    if (!state) return;
    const signature = JSON.stringify([active, state.chats, state.jobs, state.runs]);
    if (!force && signature === agentSignature) return;
    agentSignature = signature;
    const running = state.jobs.filter(j => ['queued', 'running'].includes(j.status));
    $('#jobStatus').innerHTML = running.map(j => `<div class="job-card">${esc(j.kind)} · ${esc(j.phase)}<progress max="100" value="${j.progress}"></progress></div>`).join('');
    const title = state.paper.structure.find(s => s.id === active)?.title || '';
    $('#chatContent').innerHTML = `<div class="context-pill">正在讨论 · ${esc(title)}</div><div class="message agent"><span class="mini-avatar">✦</span><p>我会围绕你的研究主线协助写作。<br><br>你可以先讨论本章，也可以选择“修改正文”，确认范围后生成可审阅的候选稿。</p></div>` + state.chats.slice(0, 8).reverse().map(c => `<div class="message user"><p>${esc(c.question)}</p></div><div class="message agent"><span class="mini-avatar">✦</span><p>${esc(c.answer)}</p></div>`).join('');
    const pending = state.runs.filter(r => r.status === 'waiting_for_approval');
    $('#candidateCards').innerHTML = pending.map(r => `<div class="candidate-card"><span>候选稿 · 尚未应用</span><h3>${esc(r.payload.instruction)}</h3><p>${r.payload.candidates.length} 个章节等待你审阅</p><button data-review="${r.id}">查看修改对比 →</button></div>`).join('');
    const failed = state.jobs.find(j => j.status === 'error' || j.status === 'interrupted');
    if (failed) $('#candidateCards').innerHTML += `<div class="candidate-card"><strong>任务未完成 · ${esc(failed.kind)}</strong><p>${esc(failed.error||failed.phase)}</p><button data-view="trace">查看记录</button></div>`;
}

function structureDialog() {
    showDialog('调整章节结构', `<p class="hint">摘要固定在最前；调整前会先保存当前编辑。</p><div>${state.paper.structure.map((s,i)=>`<div class="record"><strong>${i?String(i).padStart(2,'0')+' · ':''}${esc(s.title)}</strong><div class="actions">${i?`<button class="ghost-button" data-move="${esc(s.id)}" data-direction="-1" ${i===1?'disabled':''}>↑ 上移</button><button class="ghost-button" data-move="${esc(s.id)}" data-direction="1" ${i===state.paper.structure.length-1?'disabled':''}>↓ 下移</button><button class="ghost-button" data-rename="${esc(s.id)}">重命名</button><button class="ghost-button danger" data-deletechapter="${esc(s.id)}">删除</button>`:'<span class="hint">摘要 · 固定</span>'}</div></div>`).join('')}</div>${field('新增章节名称','newChapter')}<div class="actions">${btn('添加章节','addChapter',true)}${btn('按模板建议','templateStructure')}</div>`);
    on('addChapter', async () => {
        const title = $('#newChapter').value.trim();
        if (!title) throw Error('请输入章节名称');
        const id = crypto.randomUUID().slice(0, 8);
        await save(p => {
            p.structure.push({
                id,
                title
            });
            p.sections[id] = ''
        });
        active = id;
        renderWorkspace();
        structureDialog()
    });
    on('templateStructure', async () => {
        const suggested = state.paper.template?.rules?.sectionGuidance?.filter(t => t !== '作者自定义正文部分') || ['Introduction', 'Methods', 'Results', 'Discussion', 'Conclusion'];
        if (!confirm('按当前模板建议重建目录？原稿会先保存为版本。')) return;
        await action('version', {
            label: '调整章节结构前备份'
        });
        await save(p => {
            const structure = [{
                id: '01',
                title: 'Abstract'
            }];
            const sections = {
                '01': p.sections['01']
            };
            for (const title of suggested) {
                const existing = p.structure.find(s => s.title.toLowerCase() === title.toLowerCase());
                const id = existing?.id || crypto.randomUUID().slice(0, 8);
                structure.push({
                    id,
                    title
                });
                sections[id] = p.sections[id] || ''
            }
            p.structure = structure;
            p.sections = sections
        });
        active = '01';
        renderWorkspace();
        structureDialog()
    });
}

function review(id) {
    const run = state.runs.find(r => r.id === id);
    if (!run) return;
    const stale = run.payload.base_revision !== state.revision;
    showDialog('候选稿修改对比', `${stale?'<p class="warn">生成后论文或材料已改变。请重新生成，避免覆盖新内容。</p>':''}${run.payload.candidates.map(c=>`<h3>${esc(state.paper.structure.find(s=>s.id===c.section_id)?.title||c.section_id)}</h3><div class="split-translation"><div><p class="hint">当前正文</p><div class="source-text">${esc(state.paper.sections[c.section_id])}</div></div><div><p class="hint">候选正文</p><div class="source-text" style="background:#f2effd">${esc(c.revised_text)}</div></div></div>${c.review?`<details open><summary>Evidence Reviewer 审查</summary>${pretty(c.review)}</details>`:''}`).join('')}<label><input type="checkbox" id="reviewCheck"> 我已检查候选稿、证据和审查意见</label><div class="actions">${btn('应用到正文','applyCandidate',true)}${btn('放弃候选稿','rejectCandidate')}</div>`);
    $('#applyCandidate').disabled = true;
    $('#reviewCheck').onchange = () => $('#applyCandidate').disabled = stale || !$('#reviewCheck').checked;
    on('applyCandidate', async () => {
        assign(await action('apply', {
            id
        }));
        closeDialog();
        render();
        toast('已应用，旧稿已自动备份')
    });
    on('rejectCandidate', async () => {
        assign(await action('reject', {
            id
        }));
        closeDialog();
        renderAgent(true);
        toast('已放弃此候选稿')
    });
}
async function send() {
    await flush();
    const instruction = $('#instruction').value.trim();
    if (!instruction) throw Error('请输入讨论或修改要求');
    if ($('#agentMode').value === 'discuss') {
        assign(await action('discuss', {
            instruction,
            section: active
        }));
        $('#instruction').value = '';
        renderAgent(true);
        toast('助手正在处理，请稍候')
    } else {
        showDialog('确认修改范围', `<p class="hint">${esc(instruction)}</p>${state.paper.structure.map(s=>`<label class="field"><input type="checkbox" class="scope-check" value="${esc(s.id)}" ${s.id===active?'checked':''}> ${esc(s.title)}</label>`).join('')}<div class="actions">${btn('确认范围并生成候选稿','confirmScope',true)}</div>`);
        on('confirmScope', async () => {
            const allowed = $$('.scope-check:checked').map(el => el.value);
            if (!allowed.length) throw Error('请选择至少一个章节');
            assign(await action('revise', {
                instruction,
                allowed
            }));
            $('#instruction').value = '';
            closeDialog();
            renderAgent(true);
            toast('已开始生成，完成后在右侧审阅')
        })
    }
}
const pageNames = {
    materials: '研究材料',
    translate: '中英转换',
    versions: '版本历史',
    rag: '文献证据检索',
    trace: '运行记录',
    settings: '数据与设置'
};

function renderSecondary() {
    if (view === 'workspace') return;
    $('#secondaryTitle').textContent = pageNames[view];
    $('#secondaryEyebrow').textContent = {
        materials: 'RESEARCH MATERIALS',
        translate: 'BILINGUAL MANUSCRIPT',
        versions: 'VERSION HISTORY',
        rag: 'REFERENCE EVIDENCE',
        trace: 'AGENT TRACE',
        settings: 'PROJECT SETTINGS'
    } [view];
    if (view === 'materials') renderMaterials();
    if (view === 'translate') renderTranslation();
    if (view === 'versions') renderVersions();
    if (view === 'rag') renderSearch();
    if (view === 'trace') renderTrace();
    if (view === 'settings') renderSettings();
}

function renderMaterials() {
    const tabs = [...materialTypes, ['outline', '提纲与全文', '写作蓝图']];
    $('#secondaryContent').innerHTML = `<div class="material-page-grid"><nav class="material-subnav">${tabs.map(([id,title])=>`<button data-material="${id}" class="${type===id?'active':''}">${title}</button>`).join('')}</nav><div id="materialBody"></div></div>`;
    const body = $('#materialBody'),
        p = state.paper;
    if (type === 'thesis' || type === 'methods') {
        body.innerHTML = `<section class="card"><h3>${type==='thesis'?'论文主线':'研究方法'}</h3><p class="hint">保留你的研究目标与事实依据，清楚标记尚待验证的内容。</p>${type==='thesis'?field('研究问题、核心创新与结论边界','thesis',p.materials.thesis,true):'<div class="two-fields">'+Object.entries({objective:'研究目标',dataset:'数据集',route:'研究路线',modules:'方法模块',experiments:'实验设计',metrics:'评价指标',reproducibility:'复现信息',...Object.fromEntries(Object.keys(p.materials.methods||{}).filter(k=>!['objective','dataset','route','modules','experiments','metrics','reproducibility'].includes(k)).map(k=>[k,k]))}).map(([key,label])=>field(label,'method-'+key,p.materials.methods?.[key]||'',true)).join('')+'</div>'}${field('补充证据与待补材料','notes',p.materials.notes,true)}${btn('保存研究材料','saveMaterial',true)}</section>`;
        on('saveMaterial', async () => {
            await save(p => {
                if (type === 'thesis') p.materials.thesis = $('#thesis').value;
                else
                    for (const e of $$('[id^="method-"]')) p.materials.methods[e.id.slice(7)] = e.value;
                p.materials.notes = $('#notes').value
            });
            toast('研究材料已保存')
        });
    } else if (type === 'metadata') {
        const fields = {
            authors: '作者列表',
            affiliations: '作者机构',
            correspondingAuthor: '通信作者',
            email: '通信邮箱',
            orcid: 'ORCID',
            name: '姓名',
            studentId: '学号',
            institution: '学校 / 机构',
            courseName: '课程',
            instructor: '导师 / 教师'
        };
        const required = p.template?.rules?.requiredEndMatter || ['Acknowledgments', 'Data availability', 'Author contributions', 'Competing interests'];
        body.innerHTML = `<section class="card"><h3>文档基础信息</h3><p class="hint">用于论文与 Word 导出。只填写适用于当前文档的信息。</p><div class="two-fields">${Object.entries(fields).map(([k,l])=>field(l,'meta-'+k,p.metadata[k])).join('')}</div>${field('已核验参考文献（每条空一行）','references',p.references,true)}${required.map((h,i)=>field(h,'end-'+i,p.end_matter[h],true)).join('')}${btn('保存基础信息','saveMetadata',true)}</section>`;
        on('saveMetadata', async () => {
            await save(p => {
                for (const k of Object.keys(fields)) p.metadata[k] = $('#meta-' + k).value;
                p.references = $('#references').value;
                required.forEach((h, i) => p.end_matter[h] = $('#end-' + i).value)
            });
            toast('基础信息已保存')
        });
    } else if (type === 'literature' || type === 'style') {
        body.innerHTML = `<section class="card"><h3>${type==='style'?'写作风格参考库':'参考文献'}</h3><p class="hint">保存 PDF，提取文字并进行内容分析。扫描版 PDF 需要先完成 OCR。</p>${uploadForm('literature','添加 PDF 文献 · 单篇最多 30 MB')}<div>${state.literature.map(l=>`<div class="record"><h4>${esc(l.filename)}</h4><span class="tag">${esc(l.status||'已保存')}</span><p class="hint">已提取 ${l.text?.length||0} 字符</p>${!l.path?'<p class="warn">原文件不可用，请重新上传。</p>':fileLink(l.path,l.filename)}<label class="field"><input type="checkbox" data-selectlit="${l.id}" ${l.selected?'checked':''}> 纳入写作风格</label><div class="actions"><button class="ghost-button" data-analyze="${l.id}">分析 / 重新分析</button><button class="ghost-button danger" data-deleteitem="${l.id}">删除记录</button></div><details><summary>内容分析</summary>${pretty(l.analysis||{})}</details><details><summary>查看提取文本</summary><pre>${esc(l.text||'')}</pre></details></div>`).join('')}</div></section><section class="card"><h3>个人写作风格</h3>${field('风格要求与禁用表达','styleRequirements','',true)}${btn('生成风格方案','generateStyle',true)}${Object.keys(p.style||{}).length?pretty(p.style):'<p class="hint">选择参考文献后，提炼可迁移的表达习惯与分章规则。</p>'}</section>`;
        bindUpload('literature');
        on('generateStyle', async () => {
            assign(await action('style', {
                requirements: $('#styleRequirements').value
            }));
            toast('风格分析已启动，可在运行记录查看结果')
        });
    } else if (type === 'template') {
        body.innerHTML = `<section class="card"><h3>目标期刊</h3><p class="hint">上传官方 DOCX 模板用于 Word 导出，也可以一并保存 LaTeX 模板。</p>${uploadForm('template','添加期刊模板','.docx,.zip,.tex,.cls,.sty',true)}</section><section class="card"><h3>已保存模板</h3>${state.templates.map(t=>`<div class="record"><h4>${esc(t.name)} <span class="tag">${esc(t.family)}</span>${t.id===p.template?.id?'<span class="tag">当前使用</span>':''}</h4><div class="actions"><button class="ghost-button" data-template="${t.id}">使用此模板</button>${t.files.filter(f=>f.path).map(f=>fileLink(f.path,f.name)).join('')}</div><details><summary>模板规则</summary>${pretty(t.rules)}</details></div>`).join('')}</section>`;
        bindUpload('template');
    } else if (type === 'data') {
        body.innerHTML = `<section class="card"><h3>实验数据与论文图库</h3><p class="hint">保存 CSV / TSV / XLSX / Word 和图片。数据可以预览绘图，图片可插入指定章节。</p>${uploadForm('asset','添加数据或图片','.csv,.tsv,.xlsx,.docx,.png,.jpg,.jpeg')}</section>${state.assets.map(a=>`<section class="card"><h3>${esc(a.filename)}</h3>${a.kind==='figure'?`<img class="asset-image" src="/api/file?path=${encodeURIComponent(a.path)}" alt="${esc(a.caption||a.filename)}">${field('图注','caption-'+a.id,a.caption)}<label class="field">插入章节<select id="figsection-${a.id}"><option value="">暂不插入</option>${p.structure.map(s=>`<option value="${s.id}" ${a.section_id===s.id?'selected':''}>${esc(s.title)}</option>`).join('')}</select></label><button class="primary-button" data-savefigure="${a.id}">保存图注与位置</button>`:`<button class="ghost-button" data-table="${a.id}">预览数据 / 绘图</button><div id="table-${a.id}"></div>`}<div class="actions">${fileLink(a.path,a.filename)}<button class="ghost-button danger" data-deleteitem="${a.id}">删除记录</button></div></section>`).join('')}`;
        bindUpload('asset');
    } else if (type === 'outline') {
        body.innerHTML = `<section class="card"><h3>从研究材料生成分章蓝图</h3><p class="hint">先确认章节要解决的问题、证据与边界，再生成全文候选稿。</p>${btn('生成分章提纲','generateOutline',true)}${p.outline?field('提纲内容（JSON，可编辑）','outlineJSON',JSON.stringify(p.outline,null,2),true)+`<div class="actions">${btn('保存提纲调整','saveOutline')}${btn('按提纲生成全文候选稿','generateFull',true)}</div>`:'<p class="hint">请先填写主线与研究方法。</p>'}</section>`;
        on('generateOutline', async () => {
            assign(await action('outline'));
            toast('正在生成提纲，请稍后刷新或查看运行记录')
        });
        on('saveOutline', async () => {
            const outline = JSON.parse($('#outlineJSON').value);
            if (JSON.stringify(outline.sections.map(s => s.id)) !== JSON.stringify(p.structure.map(s => s.id))) throw Error('提纲章节编号与目录不一致');
            await save(p => p.outline = outline);
            toast('提纲已保存')
        });
        on('generateFull', async () => {
            if (!confirm('已确认提纲，开始生成全文候选稿？')) return;
            assign(await action('write'));
            toast('全文生成已启动，完成后在工作台审阅')
        });
    }
}
const translationDrafts = new Map();

function translationKey(section, index, mode) {
    return `${section}:${index}:${mode}`
}

function translationContext(job) {
    const result = job.result || {},
        c = result._context || {};
    return {
        section: c.section ?? result.section,
        index: c.index ?? result.index,
        mode: c.mode || (job.kind.includes('润色') ? 'polish' : result.translations ? 'batch' : 'translate'),
        ...c
    }
}

function translationJobLabel(job) {
    const c = translationContext(job);
    if (c.section) {
        const title = c.section_title || (c.section === '__paper_title__' ? '论文标题' : state.paper.structure.find(s => s.id === c.section)?.title) || c.section;
        return c.section === '__paper_title__' ? '论文标题' : `${title} · 第 ${Number(c.index)+1} 段`
    }
    if (c.sections?.length) return c.sections.map(id => c.section_titles?.[id] || state.paper.structure.find(s => s.id === id)?.title || id).join('、') + ' · 批量翻译';
    if (job.result?.translations) return Object.keys(job.result.translations).map(id => id === '__paper_title__' ? '论文标题' : state.paper.structure.find(s => s.id === id)?.title || id).join('、') + ' · 批量翻译';
    return '旧记录未保存章节与段落位置'
}

function translationTask(section, index, mode) {
    return state.jobs.find(j => {
        const c = translationContext(j);
        if (c.mode === 'batch') return mode === 'translate' && (c.sections?.includes(section) || section === '__paper_title__' || j.result?.translations?.[section]);
        return c.section === section && Number(c.index) === index && c.mode === mode
    })
}

function translationCandidate(job, section, index) {
    if (!job || job.status !== 'success') return null;
    return job.result?.single_translation || job.result?.translations?.[section]?.[index] || null
}

function translationValue(section, index, mode) {
    const key = translationKey(section, index, mode);
    if (translationDrafts.has(key)) return translationDrafts.get(key);
    const saved = state.paper.translations[section]?.[index] || {},
        job = translationTask(section, index, mode);
    const candidate = translationCandidate(job, section, index);
    if (candidate && !job.result?._confirmed?.includes(key) && !(saved.updatedAt && Date.parse(saved.updatedAt) > Date.parse(job.updated))) return candidate.candidate || candidate.approved || '';
    return mode === 'polish' ? (saved.polishCandidate || saved.polished || '') : (saved.candidate || saved.approved || '')
}

function renderTranslation() {
    const p = state.paper;
    const ids = ['__paper_title__', ...p.structure.map(s => s.id)];
    if (!ids.includes(window.translationSection)) window.translationSection = p.structure[0]?.id || '__paper_title__';
    const selected = window.translationSection;
    const title = p.structure.find(s => s.id === selected)?.title || '论文标题';
    $('#secondary').classList.add('translation-workspace');
    const rows = [{
        section: '__paper_title__',
        index: 0,
        text: p.title
    }];
    if (selected !== '__paper_title__') para(p.sections[selected]).forEach((text, index) => rows.push({
        section: selected,
        index,
        text
    }));
    $('#secondaryContent').innerHTML = `<div class="tr-shell"><div class="tr-toolbar"><strong>${esc(title)}</strong><div class="actions">${btn('历史记录','translationHistory')}${btn('翻译全文','translateAll')}${btn('翻译当前章节','translateSection',true)}${btn('导出英文 Word','exportEnglish')}</div></div><div class="tr-status" id="translationStatus" role="status" aria-live="polite">选择段落开始翻译，结果会显示在对应位置。</div><div class="tr-body"><aside class="tr-nav"><h3>章节</h3>${ids.map((id,i)=>`<button data-tsection="${esc(id)}" class="${selected===id?'active':''}"><span>${i?String(i).padStart(2,'0'):''}</span>${esc(id==='__paper_title__'?'论文标题':p.structure.find(s=>s.id===id).title)}</button>`).join('')}${field('固定术语表','glossary',p.glossary,true)}${btn('保存术语表','saveGlossary')}<label class="tr-direct"><input type="checkbox" id="directSave"> 批量结果直接确认</label><p class="hint">单段任务的进度、错误与候选稿显示在对应段落中。</p></aside><section class="tr-editor"><div class="tr-head"><div>中文原稿<strong>${esc(title)}</strong></div><div>英文基础稿<strong>翻译、编辑与确认</strong></div><div>写作风格润色稿<strong>独立保留润色版本</strong></div><div>润色说明<strong>进度与调整提示</strong></div></div>${rows.map(({section,index,text})=>{const row=section+'-'+index;return ` < article class = "tr-row"
    data - trrow = "${esc(row)}" > < div class = "tr-source" > < span > $ {
        section === '__paper_title__' ? '论文标题' : esc(title) + ' · 第 ' + (index + 1) + ' 段'
    } < /span><p>${esc(text)}</p > < /div>${['translate','polish'].map(mode=>`<div class="tr-target ${mode==='polish'?'tr-polish':''}"><div class="tr-target-header"><span data-trstate="${translationKey(section,index,mode)}">尚未${mode==='polish'?'润色':'翻译'}</span > < button class = "ghost-button"
    data - trun = "${mode}"
    data - section = "${esc(section)}"
    data - index = "${index}" > $ {
        mode === 'polish' ? '按风格润色' : section === '__paper_title__' ? '翻译标题' : '翻译本段'
    } < /button></div > < div class = "tr-inline-status"
    data - trstatus = "${translationKey(section,index,mode)}" > < /div><textarea aria-label="${esc(section==='__paper_title__'?'论文标题':title+'第'+(index+1)+'段')}${mode==='polish'?'润色稿':'英文稿'}" data-trtext="${translationKey(section,index,mode)}" placeholder="${mode==='polish'?'基础英文生成后，可在此生成润色候选稿':'英文候选稿将在这里生成'}">${esc(translationValue(section,index,mode))}</textarea > < button class = "tr-approve"
    data - tconfirm = "${mode}"
    data - section = "${esc(section)}"
    data - index = "${index}" > $ {
        mode === 'polish' ? '确认并保存润色稿' : '确认并保存英文'
    } < /button></div > `).join('')}<div class="tr-notes"><span>本段润色说明</span><div data-trnotes="${translationKey(section,index,'polish')}"><p>完成润色后，提示会显示在这里。</p></div></div></article>`
}).join('')
}
$ {
    rows.length === 1 && selected !== '__paper_title__' ? '<p class="hint">此章节还没有正文。</p>' : ''
} < /section></div > < /div>`;
$('#translateSection').disabled = selected === '__paper_title__';
on('saveGlossary', async () => {
    await save(p => p.glossary = $('#glossary').value);
    toast('术语表已保存')
});
const batch = async ids => {
    if (!confirm('开始翻译所选章节？结果将按章节和段落显示。')) return;
    assign(await action('translate_batch', {
        ids,
        direct: $('#directSave').checked
    }));
    renderTranslationJobs();
    toast('批量翻译已启动')
};
on('translateSection', () => batch([selected]));
on('translateAll', () => batch(p.structure.map(s => s.id)));
on('exportEnglish', () => download('english'));
on('translationHistory', translationHistory);
for (const textarea of $$('[data-trtext]')) textarea.addEventListener('input', () => {
    translationDrafts.set(textarea.dataset.trtext, textarea.value);
    const row = textarea.closest('.tr-row');
    row.querySelectorAll('[data-tconfirm]').forEach(b => b.disabled = !row.querySelector(`[data-trtext$=":${b.dataset.tconfirm}"]`).value.trim())
});
renderTranslationJobs();
}

function renderTranslationJobs() {
    const status = $('#translationStatus');
    if (!status) return;
    const selected = window.translationSection;
    const related = state.jobs.filter(j => {
        const c = translationContext(j);
        return c.section === selected || c.section === '__paper_title__' || c.sections?.includes(selected) || j.result?.translations?.[selected]
    });
    const running = related.filter(j => ['queued', 'running'].includes(j.status));
    status.textContent = running.length ? running.map(j => `${translationJobLabel(j)} · ${j.kind} · ${j.phase}`).join('；') : '当前章节 · 结果与状态显示在对应段落，全部任务见“历史记录”。';
    for (const textarea of $$('[data-trtext]')) {
        const key = textarea.dataset.trtext;
        const [section, indexText, mode] = key.split(':');
        const index = Number(indexText),
            job = translationTask(section, index, mode),
            saved = state.paper.translations[section]?.[index] || {};
        const row = textarea.closest('.tr-target'),
            label = row.querySelector('[data-trstate]'),
            inline = row.querySelector('[data-trstatus]'),
            run = row.querySelector('[data-trun]'),
            approve = row.querySelector('[data-tconfirm]');
        const candidate = translationCandidate(job, section, index);
        const pending = job && ['queued', 'running'].includes(job.status);
        run.disabled = !!pending;
        run.textContent = pending ? (mode === 'polish' ? '润色中…' : '翻译中…') : (mode === 'polish' ? '按风格润色' : section === '__paper_title__' ? '翻译标题' : '翻译本段');
        if (!translationDrafts.has(key) && document.activeElement !== textarea) textarea.value = translationValue(section, index, mode);
        approve.disabled = !textarea.value.trim();
        const confirmed = mode === 'polish' ? saved.polished : saved.approved;
        label.textContent = pending ? '正在处理' : candidate && !job.result?._confirmed?.includes(key) ? '候选稿 · 待确认' : confirmed ? '已确认' : mode === 'polish' ? '尚未润色' : '尚未翻译';
        inline.innerHTML = pending ? `<span>${esc(translationJobLabel(job))} · ${esc(job.phase)}</span>` : job && ['error', 'interrupted'].includes(job.status) ? `<p class="warn">${esc(translationJobLabel(job))}：${esc(job.error||job.phase)}</p>` : '';
        if (mode === 'polish') {
            const notes = document.querySelector(`[data-trnotes="${key}"]`);
            const hints = candidate?.warnings || saved.polishChanges || [];
            if (notes) notes.innerHTML = hints.length ? `<ul>${hints.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>` : candidate ? '<p>已生成润色候选稿。请对照基础英文检查术语、数字与原意。</p>' : '<p>完成润色后，提示会显示在这里。</p>'
        }
    }
}

function translationHistory() {
    const jobs = state.jobs.filter(j => j.kind.includes('翻译') || j.kind.includes('润色'));
    showDialog('翻译历史', jobs.length ? jobs.map(j => `<div class="record"><h4>${esc(translationJobLabel(j))}</h4><span class="tag">${esc(j.kind)} · ${esc(j.phase)}</span><p class="hint">${esc(new Date(j.created).toLocaleString('zh-CN'))}</p>${j.error?`<p class="warn">${esc(j.error)}</p>`:''}${j.result?.translations&&j.status==='success'?`<button class="ghost-button" data-importtrans="${j.id}">导入这批翻译结果</button>`:''}${translationContext(j).section?`<button class="ghost-button" data-tlocate="${j.id}">定位到段落</button>`:''}</div>`).join('') : '<p class="hint">还没有翻译记录。</p>')
}
async function translationClick(el) {
    if (el.dataset.tsection) {
        window.translationSection = el.dataset.tsection;
        renderTranslation();
        return true
    }
    if (el.dataset.tlocate) {
        const job = state.jobs.find(j => j.id === el.dataset.tlocate),
            c = translationContext(job);
        window.translationSection = c.section;
        closeDialog();
        renderTranslation();
        document.querySelector(`[data-trrow="${c.section}-${c.index}"]`)?.scrollIntoView({
            block: 'center',
            behavior: 'smooth'
        });
        return true
    }
    if (el.dataset.trun) {
        const section = el.dataset.section,
            index = Number(el.dataset.index),
            mode = el.dataset.trun;
        const base = $(`[data-trtext="${translationKey(section,index,'translate')}"]`).value;
        if (mode === 'polish' && !base.trim()) throw Error('请先翻译本段，再进行润色。');
        assign(await action('translate_one', {
            section,
            index,
            polish: mode === 'polish',
            english: base
        }));
        renderTranslationJobs();
        toast(`${section==='__paper_title__'?'论文标题':state.paper.structure.find(s=>s.id===section)?.title+' · 第 '+(index+1)+' 段'}：${mode==='polish'?'润色':'翻译'}已启动`);
        return true
    }
    if (el.dataset.tconfirm) {
        const section = el.dataset.section,
            index = Number(el.dataset.index),
            mode = el.dataset.tconfirm,
            key = translationKey(section, index, mode);
        const job = translationTask(section, index, mode);
        const candidate = translationCandidate(job, section, index);
        const text = $(`[data-trtext="${key}"]`).value;
        assign(await action('confirm_translation', {
            section,
            index,
            text,
            polish: mode === 'polish',
            revision: state.revision,
            job_id: candidate ? job.id : undefined
        }));
        translationDrafts.delete(key);
        renderTranslationJobs();
        toast(`${mode==='polish'?'润色稿':'英文稿'}已保存`);
        return true
    }
    return false;
}

function renderVersions() {
    $('#secondaryContent').innerHTML = state.versions.length ? state.versions.map(v => {
        const p = JSON.parse(v.payload);
        return `<section class="card"><h3>${esc(v.label)}</h3><p class="hint">${esc(v.created)}</p><details><summary>预览完整正文</summary>${p.structure.map(s=>`<h4>${esc(s.title)}</h4><div class="article-text">${esc(p.sections[s.id])}</div>`).join('')}</details><div class="actions"><button class="primary-button" data-restore="${v.id}">恢复此版本</button><button class="ghost-button danger" data-deleteversion="${v.id}">删除快照</button></div></section>`
    }).join('') : '<section class="card"><p class="hint">还没有保存版本。</p></section>'
}

function renderSearch() {
    $('#secondaryContent').innerHTML = `<section class="card"><h3>从原始文献寻找证据</h3><p class="hint">关键词与中英文语义检索融合，保留来源和片段编号。语义相似不代表证据已经核实。</p><p class="hint" id="ragStatus">正在读取索引状态…</p>${field('关键词、问题或研究主张','searchQuery')}<label>检索方式 <select id="searchMode"><option value="hybrid">混合检索（关键词 + 向量）</option><option value="keyword">仅关键词</option><option value="vector">仅向量</option></select></label><div class="actions">${btn('检索证据','searchButton',true)}${btn('重建向量索引','rebuildIndex')}</div><p class="hint" id="searchNotice"></p></section><div id="searchResults"></div>`;
    const status = async () => {
        const data = await action('rag_status');
        if ($('#ragStatus')) $('#ragStatus').textContent = `已索引 ${data.indexed} / ${data.chunks} 个片段 · 待更新 ${data.pending} 个`
    };
    status().catch(e => {
        if ($('#ragStatus')) $('#ragStatus').textContent = e.message
    });
    on('rebuildIndex', async () => {
        assign(await action('rag_rebuild'));
        toast('已加入后台索引任务，可在运行记录中查看进度');
        await status()
    });
    on('searchButton', async () => {
        const button = $('#searchButton');
        button.disabled = true;
        $('#searchNotice').textContent = '正在检索；新增文献首次生成向量可能需要一些时间…';
        try {
            const data = await action('search', {
                query: $('#searchQuery').value,
                mode: $('#searchMode').value
            });
            if (!$('#searchResults')) return;
            $('#searchNotice').textContent = data.warning || ({
                hybrid: '本次使用关键词与向量排名融合。',
                keyword: '本次仅使用关键词检索。',
                vector: '本次仅使用语义向量检索。'
            } [data.actualMode]);
            $('#searchResults').innerHTML = data.results.length ? data.results.map(r => `<section class="card"><h3>${esc(r.sourceName)}</h3><p class="hint">${esc(r.chunkId)} · ${(r.retrievalMethods||[]).map(x=>x==='keyword'?'关键词':'向量').join(' + ')}${r.keywordScore!=null?' · 关键词命中 '+r.keywordScore:''}${r.vectorScore!=null?' · 余弦相似度 '+r.vectorScore.toFixed(3):''}${data.actualMode==='hybrid'?' · RRF '+r.score.toFixed(4):''}</p><div class="article-text">${esc(r.content)}</div></section>`).join('') : '<section class="card">没有找到匹配证据，请调整问题或补充文献。</section>';
            await status();
        } catch (error) {
            if ($('#searchNotice')) $('#searchNotice').textContent = error.message;
            throw error
        } finally {
            button.disabled = false
        }
    });
}

function renderTrace() {
    $('#secondaryContent').innerHTML = `<div class="stat-grid"><div class="stat"><strong>${state.runs.length}</strong><span>写作运行</span></div><div class="stat"><strong>${state.runs.filter(r=>r.status==='approved').length}</strong><span>已应用候选稿</span></div><div class="stat"><strong>${state.runs.reduce((n,r)=>n+(r.payload.tokens||0),0)}</strong><span>模型 Token</span></div></div><section class="card"><h3>后台任务</h3>${state.jobs.map(j=>`<div class="record"><strong>${esc(j.kind)}</strong><span class="tag">${esc(j.status)}</span><p>${esc(j.phase)} · ${j.progress}%</p>${j.error?`<p class="warn">${esc(j.error)}</p>`:''}</div>`).join('')||'<p class="hint">暂无任务。</p>'}</section>${state.runs.map(r=>`<section class="card"><h3>${esc(r.payload.instruction)}</h3><span class="tag">${esc(r.status)}</span>${r.payload.engine==='langgraph-v1'?`<p class="hint">LangGraph · ${esc(r.payload.graph_stage||'等待执行')} · 进度已保存</p>${r.payload.error?`<p class="warn">${esc(r.payload.error)}</p>`:''}`:''}<div class="actions">${r.payload.engine==='langgraph-v1'&&['interrupted','failed'].includes(r.status)?`<button class="primary-button" data-resumerun="${r.id}">从检查点继续</button>`:''}<button class="ghost-button" data-steps="${r.id}">查看审查过程</button>${r.status==='waiting_for_approval'?`<button class="primary-button" data-review="${r.id}">审阅候选稿</button>`:''}</div><div id="steps-${r.id}"></div></section>`).join('')}`
}

function renderSettings() {
    $('#secondaryContent').innerHTML = `<section class="card"><h3>模型与本机数据</h3><p class="hint">生成模型：${esc(state.model)} · 模型端点${state.configured?'已配置':'未配置'}（不代表连接已验证）。请在项目 .env 中填写 LLM_BASE_URL、LLM_MODEL 和服务要求的 LLM_API_KEY，重启后生效。</p><p class="hint">本地语义检索：${state.embeddings_enabled?'已启用；首次索引可能下载模型':'未启用；默认不下载模型，混合检索将降级为关键词'}。启用方法见 README。</p><p class="hint">本项目不附带论文、数据、模板或模型权重。请添加自己的研究材料。模型调用会将必要的文本发送到你配置的 API 服务。</p>${btn('下载完整数据备份','backup',true)}<p class="hint">备份包含私人论文、文献、图片和任务检查点，不包含 API 密钥。请勿将备份上传到公开仓库。</p></section>`;
    on('backup', () => download('backup'));
}
async function download(kind) {
    await flush();
    const r = await fetch('/api/export?kind=' + kind);
    if (!r.ok) {
        const d = await r.json();
        throw Error(d.error)
    }
    const blob = await r.blob(),
        url = URL.createObjectURL(blob),
        a = document.createElement('a');
    a.href = url;
    a.download = kind === 'backup' ? 'papercoding-backup.zip' : kind === 'markdown' ? 'paper.md' : kind === 'english' ? 'paper-english.docx' : 'paper.docx';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast('文件已生成')
}
async function showTable(id) {
    const data = await api('/api/table?id=' + id);
    const el = $('#table-' + id);
    if (data.text) {
        el.innerHTML = `<pre>${esc(data.text)}</pre>`;
        return
    }
    el.innerHTML = `<div class="scroll-table"><table><thead><tr>${data.columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${data.rows.map(row=>`<tr>${data.columns.map(c=>`<td>${esc(row[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div><div class="actions"><select id="plotx-${id}">${data.columns.map(c=>`<option>${esc(c)}</option>`).join('')}</select><select id="ploty-${id}">${data.numeric.map(c=>`<option>${esc(c)}</option>`).join('')}</select><select id="plotkind-${id}"><option value="line">折线图</option><option value="bar">柱状图</option><option value="scatter">散点图</option></select><button class="primary-button" data-plot="${id}">生成并保存图片</button></div>`
}
document.addEventListener('click', guard(async e => {
    const el = e.target.closest('button,a');
    if (!el) return;
    if (await translationClick(el)) return;
    if (el.dataset.view) {
        e.preventDefault();
        return navigate(el.dataset.view, el.dataset.type || 'thesis')
    }
    if (el.dataset.material) {
        await navigate('materials', el.dataset.material);
        return
    }
    if (el.dataset.chapter) {
        await flush();
        active = el.dataset.chapter;
        integrated = false;
        renderWorkspace();
        renderAgent(true);
        return
    }
    if (el.dataset.prompt) {
        $('#instruction').value = el.dataset.prompt;
        $('#agentMode').value = el.dataset.mode || 'discuss';
        $('#instruction').focus();
        return
    }
    if (el.dataset.review) {
        await flush();
        assign(await api('/api/state'));
        review(el.dataset.review);
        return
    }
    if (el.dataset.move) {
        const id = el.dataset.move,
            dir = Number(el.dataset.direction);
        await save(p => {
            const i = p.structure.findIndex(s => s.id === id);
            if (i === 0 || i + dir < 1 || i + dir >= p.structure.length) return;
            [p.structure[i], p.structure[i + dir]] = [p.structure[i + dir], p.structure[i]]
        });
        renderWorkspace();
        structureDialog();
        return
    }
    if (el.dataset.rename) {
        const id = el.dataset.rename,
            title = prompt('章节名称', state.paper.structure.find(s => s.id === id).title);
        if (!title?.trim()) return;
        await save(p => p.structure.find(s => s.id === id).title = title.trim());
        renderWorkspace();
        structureDialog();
        return
    }
    if (el.dataset.deletechapter) {
        const id = el.dataset.deletechapter;
        if (id === '01' || !confirm('删除此章节？当前稿将先保存为版本。')) return;
        await action('version', {
            label: '删除章节前备份'
        });
        await save(p => {
            p.structure = p.structure.filter(s => s.id !== id);
            delete p.sections[id]
        });
        active = '01';
        renderWorkspace();
        structureDialog();
        return
    }
    if (el.dataset.analyze) {
        assign(await action('analyze', {
            id: el.dataset.analyze
        }));
        toast('文献分析已启动');
        return
    }
    if (el.dataset.deleteitem) {
        if (!confirm('删除这条记录？底层文件仍保留供备份使用。')) return;
        assign(await action('delete_item', {
            id: el.dataset.deleteitem
        }));
        renderSecondary();
        return
    }
    if (el.dataset.template) {
        const template = state.templates.find(t => t.id === el.dataset.template);
        await save(p => p.template = template);
        renderSecondary();
        toast('已切换期刊模板');
        return
    }
    if (el.dataset.savefigure) {
        const id = el.dataset.savefigure;
        assign(await action('update_asset', {
            id,
            caption: $('#caption-' + id).value,
            section_id: $('#figsection-' + id).value
        }));
        toast('图注与位置已保存');
        return
    }
    if (el.dataset.restore) {
        if (!confirm('恢复此版本？当前稿会先备份。')) return;
        assign(await action('restore', {
            id: el.dataset.restore,
            revision: state.revision
        }));
        renderSecondary();
        toast('已恢复版本');
        return
    }
    if (el.dataset.deleteversion) {
        if (!confirm('永久删除这个历史快照？不会删除当前正文。')) return;
        assign(await action('delete_version', {
            id: el.dataset.deleteversion
        }));
        renderSecondary();
        return
    }
    if (el.dataset.transone !== undefined || el.dataset.polish !== undefined) {
        const i = Number(el.dataset.transone ?? el.dataset.polish);
        assign(await action('translate_one', {
            section: window.translationSection,
            index: i,
            polish: el.dataset.polish !== undefined,
            english: $('#english-' + i).value
        }));
        renderTranslationJobs();
        toast('任务已启动，完成后导入结果');
        return
    }
    if (el.dataset.confirmtrans !== undefined) {
        const i = Number(el.dataset.confirmtrans);
        assign(await action('confirm_translation', {
            section: window.translationSection,
            index: i,
            text: $('#english-' + i).value,
            revision: state.revision
        }));
        toast('英文已确认保存');
        return
    }
    if (el.dataset.importtrans) {
        const job = state.jobs.find(j => j.id === el.dataset.importtrans);
        let overwrite = false;
        if (job.result.translations) {
            overwrite = confirm('是否允许替换已有确认英文？确定：允许替换；取消：仅导入尚未确认的章节。')
        }
        assign(await action('import_translation', {
            id: el.dataset.importtrans,
            overwrite
        }));
        closeDialog();
        renderTranslation();
        toast('翻译结果已导入');
        return
    }
    if (el.dataset.resumerun) {
        assign(await action('resume_run', {
            id: el.dataset.resumerun
        }));
        renderTrace();
        toast('已加入继续执行队列，已完成步骤将复用');
        return
    }
    if (el.dataset.steps) {
        const data = await action('steps', {
            id: el.dataset.steps
        });
        $('#steps-' + el.dataset.steps).innerHTML = pretty(data.steps);
        return
    }
    if (el.dataset.table) return showTable(el.dataset.table);
    if (el.dataset.plot) {
        const id = el.dataset.plot,
            ys = [$('#ploty-' + id).value];
        if (!ys[0]) throw Error('没有可绘制的数值列');
        assign(await api('/api/plot', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                id,
                x: $('#plotx-' + id).value,
                ys,
                kind: $('#plotkind-' + id).value
            })
        }));
        renderSecondary();
        toast('图片已保存到论文图库')
    }
}));
document.addEventListener('change', guard(async e => {
    if (e.target.dataset.selectlit) assign(await action('select_literature', {
        id: e.target.dataset.selectlit,
        selected: e.target.checked
    }))
}));
$('#editor').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.isComposing && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        const editor = e.currentTarget;
        editor.setRangeText('\n\n\u3000\u3000', editor.selectionStart, editor.selectionEnd, 'end');
        editor.dispatchEvent(new Event('input', {
            bubbles: true
        }))
    }
});
$('#editor').addEventListener('input', () => {
    fitEditor();
    $('#wordCount').textContent = `中文稿 · ${$('#editor').value.length} 字`;
    $('#saveState').className = 'save-state busy';
    $('#saveState').textContent = '尚未保存';
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => flush().catch(err => toast(err.message, true)), 850)
});
$('#editor').addEventListener('blur', () => flush().catch(err => toast(err.message, true)));
window.addEventListener('beforeunload', e => {
    if (view === 'workspace' && !integrated && $('#editor').value !== lastInput) {
        e.preventDefault();
        e.returnValue = ''
    }
});
on('closeDialog', closeDialog);
$('#dialog').addEventListener('click', e => {
    if (e.target === $('#dialog')) closeDialog()
});
on('projectTitle', async () => {
    await flush();
    showDialog('修改论文标题', field('当前项目', 'renameTitle', state.paper.title) + btn('保存标题', 'commitTitle', true));
    on('commitTitle', async () => {
        const title = $('#renameTitle').value.trim();
        if (!title) throw Error('标题不能为空');
        await save(p => p.title = title);
        closeDialog();
        render();
        toast('标题已更新')
    })
});
on('saveVersion', async () => {
    await flush();
    showDialog('保存论文版本', field('版本名称', 'versionLabel', '') + '<p class="hint">保存当前标题、正文、章节结构和材料状态。</p>' + btn('保存版本', 'commitVersion', true));
    on('commitVersion', async () => {
        assign(await action('version', {
            label: $('#versionLabel').value
        }));
        closeDialog();
        toast('版本已保存')
    })
});
on('exportButton', async () => {
    await flush();
    showDialog('导出论文', `<p class="hint">当前模板：${esc(state.paper.template?.name||'未选择，请先在目标期刊中上传 DOCX 模板')}</p><div class="actions">${btn('下载当前稿 Word','downloadDocx',true)}${btn('下载 Markdown','downloadMd')}${btn('导出英文 Word','downloadEn')}</div><p class="hint">PDF：下载 Word 后在 Word 或 LibreOffice 中导出。正式投稿前请检查模板格式、公式与声明。</p>`);
    on('downloadDocx', () => download('docx'));
    on('downloadMd', () => download('markdown'));
    on('downloadEn', () => download('english'))
});
on('versionsButton', () => navigate('versions'));
on('backHome', () => navigate('workspace'));
on('chapterView', async () => {
    integrated = false;
    renderWorkspace()
});
on('integratedView', async () => {
    await flush();
    integrated = true;
    renderWorkspace()
});
on('send', send);
$('#instruction').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        send().catch(err => toast(err.message, true))
    }
});

function setZoom(value) {
    zoom = Math.max(.6, Math.min(1.5, value));
    $('#paperCanvas').style.zoom = zoom;
    $('#zoomReset').textContent = Math.round(zoom * 100) + '%';
    localStorage.setItem('pc-web-zoom', zoom)
}
on('zoomIn', () => setZoom(zoom + .1));
on('zoomOut', () => setZoom(zoom - .1));
on('zoomReset', () => setZoom(1));
$('#paperPanel').addEventListener('wheel', e => {
    if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        setZoom(zoom + (e.deltaY > 0 ? -.05 : .05))
    }
}, {
    passive: false
});
for (const handle of $$('.panel-resizer')) {
    handle.addEventListener('pointerdown', e => {
        e.preventDefault();
        handle.setPointerCapture(e.pointerId);
        handle._drag = true
    });
    handle.addEventListener('pointermove', e => {
        if (!handle._drag) return;
        const side = handle.dataset.side,
            width = side === 'left' ? e.clientX : innerWidth - e.clientX;
        const clamped = Math.max(180, Math.min(480, width));
        $('#workspace').style.setProperty(`--${side}-panel-width`, clamped + 'px');
        localStorage.setItem('pc-web-' + side, clamped)
    });
    handle.addEventListener('pointerup', () => handle._drag = false);
    handle.addEventListener('pointercancel', () => handle._drag = false);
    handle.addEventListener('keydown', e => {
        if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
        const side = handle.dataset.side,
            current = parseInt(getComputedStyle($('#workspace')).getPropertyValue(`--${side}-panel-width`)) || 250;
        const delta = (e.key === 'ArrowRight' ? 12 : -12) * (side === 'left' ? 1 : -1);
        $('#workspace').style.setProperty(`--${side}-panel-width`, Math.min(480, Math.max(180, current + delta)) + 'px')
    })
}
const grip = $('.zoom-grip');
grip.addEventListener('pointerdown', e => {
    grip.setPointerCapture(e.pointerId);
    grip._drag = [e.clientX - $('#zoom').offsetLeft, e.clientY - $('#zoom').offsetTop]
});
grip.addEventListener('pointermove', e => {
    if (!grip._drag) return;
    $('#zoom').style.left = Math.max(0, Math.min(innerWidth - 160, e.clientX - grip._drag[0])) + 'px';
    $('#zoom').style.top = Math.max(82, Math.min(innerHeight - 45, e.clientY - grip._drag[1])) + 'px';
    $('#zoom').style.bottom = 'auto'
});
grip.addEventListener('pointerup', () => grip._drag = null);
window.addEventListener('popstate', guard(async () => {
    await flush();
    const params = new URLSearchParams(location.search);
    view = params.get('view') || 'workspace';
    type = params.get('type') || 'thesis';
    assign(await api('/api/state'));
    render()
}));
async function poll() {
        try {
            const data = await api('/api/state');
            const traceChanged = JSON.stringify([state.jobs, state.runs]) !== JSON.stringify([data.jobs, data.runs]);
            state.jobs = data.jobs;
            state.runs = data.runs;
            state.chats = data.chats;
            renderAgent();
            if (view === 'translate') renderTranslationJobs();
            if (view === 'trace' && traceChanged) renderTrace();
            if (data.revision !== state.revision) {
                $('#saveState').textContent = '有更新 · 点击刷新';
                $('#saveState').style.cursor = 'pointer';
                $('#saveState').onclick = guard(async () => {
                    if (!confirm('重新读取最新论文与材料？请先保存页面中尚未提交的表单。')) return;
                    await flush();
                    assign(await api('/api/state'));
                    render();
                    $('#saveState').innerHTML = '<i></i>已保存';
                    $('#saveState').onclick = null
                })
            }
        } catch {}
        setTimeout(poll, 4000)
    }
    (async () => {
        try {
            assign(await api('/api/state'));
            const params = new URLSearchParams(location.search);
            view = pageNames[params.get('view')] ? params.get('view') : 'workspace';
            type = params.get('type') || 'thesis';
            for (const side of ['left', 'right']) {
                const width = Number(localStorage.getItem('pc-web-' + side));
                if (width >= 180 && width <= 480) $('#workspace').style.setProperty(`--${side}-panel-width`, width + 'px')
            }
            render();
            setZoom(Number(localStorage.getItem('pc-web-zoom')) || 1);
            poll()
        } catch (err) {
            toast('页面加载失败：' + err.message, true);
            $('#saveState').textContent = '连接失败'
        }
    })();