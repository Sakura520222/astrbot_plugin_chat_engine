// Chat Engine WebUI — Frontend Application v2


const API_BASE = '';

// Utility Functions

async function api(method, path, body = null) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    };
    const token = localStorage.getItem('auth_token');
    if (token) opts.headers['Authorization'] = `Bearer ${token}`;
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(`${API_BASE}${path}`, opts);
    if (resp.status === 401) {
        localStorage.removeItem('auth_token');
        window.location.href = '/login';
        throw new Error('未授权');
    }
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

function toast(msg, type = 'success') {
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => {
        el.style.opacity = '0';
        el.style.transform = 'translateY(12px)';
        el.style.transition = 'all 0.3s ease';
        setTimeout(() => el.remove(), 300);
    }, 3000);
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatReplyContent(text) {
    if (!text) return text;
    return text.replace(
        /\[回复 ([^\]]+)\]/g,
        '<blockquote class="reply-quote">$1</blockquote>'
    );
}

function sourceLabel(source) {
    if (source === 'auto') return '<span class="source-label source-auto">[自动]</span>';
    if (source === 'manual') return '<span class="source-label source-manual">[手动]</span>';
    return '<span class="source-label source-tool">[工具]</span>';
}

// Tab Switching

// UI 状态持久化：F5 刷新后恢复当前 tab 与已选会话
function setUiState(key, val) { try { localStorage.setItem(key, val); } catch (e) {} }
function getUiState(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

// 刷新按钮通用 helper：点击后切到「刷新中…」禁用态，完成后还原
async function refreshPage(btn, loader) {
    if (!btn) return loader();
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = '刷新中…';
    try { await loader(); }
    finally { btn.disabled = false; btn.textContent = orig; }
}

function activateTab(tab) {
    document.querySelectorAll('.nav-item').forEach(i => i.classList.toggle('active', i.dataset.tab === tab));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${tab}`));
    setUiState('ce_ui_tab', tab);

    if (tab === 'personas') loadPersonas();
    else if (tab === 'sessions') {
        const sp = parseInt(getUiState('ce_ui_sess_page'), 10);
        sessionPage = sp > 0 ? sp : 1;
        loadSessions();
    }
    else if (tab === 'memories') restoreMemoryTab();
    else if (tab === 'proactive') restoreProactiveTab();
    else if (tab === 'tools') loadTools();
    else if (tab === 'image-quota') loadImageQuota();
    else if (tab === 'config') loadConfig();
}

// 记忆页恢复：先加载会话下拉，再恢复上次选中的会话并加载记忆
async function restoreMemoryTab() {
    await loadMemorySessionList();
    const saved = getUiState('ce_ui_mem_session');
    const sel = document.getElementById('memory-session-select');
    if (saved && sel && [...sel.options].some(o => o.value === saved)) {
        sel.value = saved;
        await onMemorySessionChange();
    }
}

async function restoreProactiveTab() {
    await loadProactiveSessionList();
    const saved = getUiState('ce_ui_pro_session');
    const sel = document.getElementById('proactive-session-select');
    if (saved && sel && [...sel.options].some(o => o.value === saved)) {
        sel.value = saved;
        await onProactiveSessionChange();
    }
}

// 记忆 / 主动回复页一键刷新：刷下拉框 + 已选会话数据
async function refreshMemoryPage(btn) {
    await refreshPage(btn, async () => {
        await loadMemorySessionList();
        if (currentMemorySessionKey) await loadMemories();
    });
}

async function refreshProactivePage(btn) {
    await refreshPage(btn, async () => {
        await loadProactiveSessionList();
        if (currentProactiveSessionKey) await loadProactiveSettings();
    });
}

document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => activateTab(item.dataset.tab));
});


// Persona Management


let personas = [];

async function loadPersonas() {
    try {
        const data = await api('GET', '/api/personas');
        personas = data.personas || [];
        renderPersonas();
    } catch (e) {
        toast('加载人格失败: ' + e.message, 'error');
    }
}

function renderPersonas() {
    const container = document.getElementById('persona-list');
    if (!personas.length) {
        container.innerHTML = `
            <div class="empty-state">
                <p>暂无人格，请点击右上角新建</p>
            </div>`;
        return;
    }
    container.innerHTML = personas.map(p => `
        <div class="card">
            <div class="card-header">
                <div class="card-title">
                    ${escapeHtml(p.name)}
                    ${p.is_default ? '<span class="badge badge-default">默认</span>' : ''}
                </div>
                <div class="card-meta">${p.updated_at ? new Date(p.updated_at).toLocaleString() : ''}</div>
            </div>
            <div class="card-body">${escapeHtml(p.system_prompt)}</div>
            <div class="card-actions">
                ${!p.is_default ? `<button class="btn btn-small btn-secondary" onclick="setDefaultPersona(${p.id})">设为默认</button>` : ''}
                <button class="btn btn-small btn-secondary" onclick="editPersona(${p.id})">编辑</button>
                <button class="btn btn-small btn-danger" onclick="deletePersona(${p.id}, '${escapeHtml(p.name)}')">删除</button>
            </div>
        </div>
    `).join('');
}

function showPersonaModal(persona = null) {
    const modal = document.getElementById('persona-modal');
    const title = document.getElementById('modal-title');
    const idField = document.getElementById('persona-id');
    const nameField = document.getElementById('persona-name');
    const promptField = document.getElementById('persona-prompt');
    const defaultField = document.getElementById('persona-default');

    if (persona) {
        title.textContent = '编辑人格';
        idField.value = persona.id;
        nameField.value = persona.name;
        promptField.value = persona.system_prompt;
        defaultField.checked = persona.is_default;
    } else {
        title.textContent = '新建人格';
        idField.value = '';
        nameField.value = '';
        promptField.value = '';
        defaultField.checked = false;
    }

    modal.classList.remove('hidden');
}

function hidePersonaModal() {
    document.getElementById('persona-modal').classList.add('hidden');
}

function editPersona(id) {
    const persona = personas.find(p => p.id === id);
    if (persona) showPersonaModal(persona);
}

async function savePersona(e) {
    e.preventDefault();
    const id = document.getElementById('persona-id').value;
    const name = document.getElementById('persona-name').value.trim();
    const system_prompt = document.getElementById('persona-prompt').value;
    const is_default = document.getElementById('persona-default').checked;

    try {
        if (id) {
            await api('PUT', `/api/personas/${id}`, { name, system_prompt, is_default });
            toast('人格已更新');
        } else {
            await api('POST', '/api/personas', { name, system_prompt, is_default });
            toast('人格已创建');
        }
        hidePersonaModal();
        loadPersonas();
    } catch (e) {
        toast('保存失败: ' + e.message, 'error');
    }
}

async function deletePersona(id, name) {
    if (!confirm(`确定要删除人格「${name}」吗？`)) return;
    try {
        await api('DELETE', `/api/personas/${id}`);
        toast('人格已删除');
        loadPersonas();
    } catch (e) {
        toast('删除失败: ' + e.message, 'error');
    }
}

async function setDefaultPersona(id) {
    try {
        await api('POST', `/api/personas/${id}/set_default`);
        toast('已设为默认人格');
        loadPersonas();
    } catch (e) {
        toast('设置失败: ' + e.message, 'error');
    }
}


// Session Management


let sessionPage = 1;
const sessionPageSize = 20;

async function loadSessions() {
    try {
        const data = await api('GET', `/api/sessions?page=${sessionPage}&page_size=${sessionPageSize}`);
        renderSessions(data.sessions || [], data.total || 0);
    } catch (e) {
        toast('加载会话失败: ' + e.message, 'error');
    }
}

function renderSessions(sessions, total) {
    const container = document.getElementById('session-list');
    if (!sessions.length) {
        container.innerHTML = `
            <div class="empty-state">
                <p>暂无会话记录</p>
            </div>`;
        document.getElementById('session-pagination').innerHTML = '';
        return;
    }

    container.innerHTML = sessions.map(s => {
        const isGroup = !s.session_key.includes(':private:');
        const tag = isGroup ? '<span class="tag">群</span>' : '<span class="tag">私</span>';
        const archiveBadge = s.archive_count ? `<span class="badge-archive">${s.archive_count} 个归档</span>` : '';
        return `
            <div class="card">
                <div class="card-header">
                    <div class="card-title">${tag} ${escapeHtml(s.session_key)} ${archiveBadge}</div>
                    <div class="card-meta">
                        ${s.message_count} 条消息 · Token ${(s.total_tokens || 0).toLocaleString()} · ${s.updated_at ? new Date(s.updated_at).toLocaleString() : ''}
                    </div>
                </div>
                <div class="card-actions">
                    <button class="btn btn-small btn-secondary" data-session-key="${escapeHtml(s.session_key)}" onclick="viewSession(this.dataset.sessionKey)">查看上下文</button>
                    <button class="btn btn-small btn-secondary" data-session-key="${escapeHtml(s.session_key)}" onclick="viewArchives(this.dataset.sessionKey)">归档列表</button>
                    <button class="btn btn-small btn-danger" data-session-key="${escapeHtml(s.session_key)}" onclick="deleteSession(this.dataset.sessionKey)">删除</button>
                </div>
            </div>
        `;
    }).join('');

    // Pagination
    const totalPages = Math.ceil(total / sessionPageSize);
    const pagination = document.getElementById('session-pagination');
    let pagHtml = '';
    if (sessionPage > 1) {
        pagHtml += `<button onclick="goSessionPage(${sessionPage - 1})">上一页</button>`;
    }
    for (let i = 1; i <= Math.min(totalPages, 10); i++) {
        pagHtml += `<button class="${i === sessionPage ? 'active' : ''}" onclick="goSessionPage(${i})">${i}</button>`;
    }
    if (sessionPage < totalPages) {
        pagHtml += `<button onclick="goSessionPage(${sessionPage + 1})">下一页</button>`;
    }
    pagination.innerHTML = pagHtml;
}

function goSessionPage(page) {
    sessionPage = page;
    setUiState('ce_ui_sess_page', String(page));
    loadSessions();
}

async function viewSession(key) {
    currentSessionKey = key;
    try {
        const data = await api('GET', `/api/sessions/${encodeURIComponent(key)}`);
        const modal = document.getElementById('session-modal');
        const detail = document.getElementById('session-detail');

        const messagesHtml = (data.messages || []).map(msg => {
            const role = msg.role || 'unknown';
            let content = '';
            if (typeof msg.content === 'string') {
                content = formatReplyContent(escapeHtml(msg.content));
            } else if (Array.isArray(msg.content)) {
                content = msg.content.map(p => {
                    if (p.type === 'text') {
                        return formatReplyContent(escapeHtml(p.text || ''));
                    }
                    if (p.type === 'image_url' && p.image_url && p.image_url.url) {
                        return `<img src="${escapeHtml(p.image_url.url)}" style="max-width:200px;max-height:200px;border-radius:8px;margin:4px 0;display:block;" alt="image" />`;
                    }
                    return '';
                }).filter(Boolean).join('\n');
            }
            const roleLabel = { user: 'USER', assistant: 'ASSISTANT', system: 'SYSTEM', tool: 'TOOL', observed: 'OBSERVED' }[role] || role.toUpperCase();
            return `
                <div class="msg-bubble msg-${role}">
                    <div class="msg-role">${roleLabel}</div>
                    ${content || '<em>(无文本内容)</em>'}
                </div>
            `;
        }).join('');

        // Token 用量统计条（估算）
        const tokenBar = `
            <div class="token-stats-bar">
                <span class="token-stats-title">Token 用量（估算）</span>
                <span class="token-stats-item">输入 <strong>${(data.prompt_tokens || 0).toLocaleString()}</strong></span>
                <span class="token-stats-item">输出 <strong>${(data.completion_tokens || 0).toLocaleString()}</strong></span>
                <span class="token-stats-item">总计 <strong>${(data.total_tokens || 0).toLocaleString()}</strong></span>
            </div>`;

        if (!data.messages || !data.messages.length) {
            detail.innerHTML = `
                ${tokenBar}
                <div class="empty-state">
                    <p>此会话暂无消息记录</p>
                </div>`;
        } else {
            detail.innerHTML = tokenBar + messagesHtml;
        }

        modal.classList.remove('hidden');
    } catch (e) {
        toast('加载会话失败: ' + e.message, 'error');
    }
}

async function clearSession() {
    if (!currentSessionKey) return;
    if (!confirm('确定清空当前会话上下文？\nToken 计数将一并归零，且不会归档（不可恢复）。')) return;
    try {
        await api('POST', `/api/sessions/${encodeURIComponent(currentSessionKey)}/clear`);
        toast('已清空上下文');
        await viewSession(currentSessionKey);
        loadSessions();
    } catch (e) {
        toast('清空失败: ' + e.message, 'error');
    }
}

function hideSessionModal() {
    document.getElementById('session-modal').classList.add('hidden');
}

// LLM Preview
let currentSessionKey = null;

async function showLlmPreview() {
    if (!currentSessionKey) return;
    try {
        const data = await api('GET', `/api/sessions/${encodeURIComponent(currentSessionKey)}/llm-preview`);
        const container = document.getElementById('llm-preview-content');

        const roleColors = {
            system: 'var(--warning)',
            user: 'var(--primary)',
            assistant: 'var(--success)',
            tool: 'var(--text-dim)',
        };

        let html = '';

        // Overview stats
        html += `<div class="preview-stats">
            <div class="stat-item"><span class="stat-label">Provider</span><div class="stat-value">${data.provider || '-'}</div></div>
            <div class="stat-item"><span class="stat-label">Modalities</span><div class="stat-value">${(data.modalities || []).join(', ') || '-'}</div></div>
            <div class="stat-item"><span class="stat-label">上下文条数</span><div class="stat-value">${data.context_count}</div></div>
            <div class="stat-item"><span class="stat-label">工具数</span><div class="stat-value">${data.tool_count}</div></div>
            <div class="stat-item"><span class="stat-label">估算 Token</span><div class="stat-value">${data.estimated_tokens}</div></div>
            ${data.filter_summary ? `<div class="stat-item"><span class="stat-label">模态过滤</span><div class="stat-value">图片=${data.filter_summary.fixed_image_blocks}, 音频=${data.filter_summary.fixed_audio_blocks}</div></div>` : ''}
        </div>`;

        // System Prompt
        if (data.system_prompt) {
            html += `<div class="preview-section">
                <div class="preview-section-title" onclick="this.nextElementSibling.classList.toggle('collapsed')">
                    System Prompt (${data.system_prompt_length} 字)
                </div>
                <pre class="preview-code collapsed">${escapeHtml(data.system_prompt)}</pre>
            </div>`;
        }

        // Context messages
        html += `<div class="preview-section">
            <div class="preview-section-title">上下文消息 (${data.context_count} 条)</div>`;

        for (const msg of data.contexts || []) {
            const role = msg.role || 'unknown';
            const color = roleColors[role] || 'var(--text-dim)';
            let content = '';
            const c = msg.content;
            if (typeof c === 'string') {
                content = escapeHtml(c);
            } else if (Array.isArray(c)) {
                content = c.map(p => {
                    if (p.type === 'text') return escapeHtml(p.text || '');
                    if (p.type === 'image_url') return `<span class="img-tag">[image]</span>`;
                    if (p.type === 'image_ref') return `<span class="img-tag">[image_ref:${p.image_id}]</span>`;
                    return `<span class="img-tag">[${p.type}]</span>`;
                }).filter(Boolean).join('\n');
            }
            html += `<div class="preview-msg ${role}">
                <span class="preview-role" style="color:${color}">${role.toUpperCase()}</span>
                <div class="preview-content">${content || '<em>(空)</em>'}</div>
            </div>`;
        }
        html += '</div>';

        // Tool list
        if (data.tools && data.tools.length > 0) {
            html += `<div class="preview-section">
                <div class="preview-section-title" onclick="this.nextElementSibling.classList.toggle('collapsed')">
                    工具列表 (${data.tool_count})
                </div>
                <div class="preview-tools collapsed">${(data.tools || []).map(t => `<span class="tool-tag">${escapeHtml(t)}</span>`).join('')}</div>
            </div>`;
        }

        container.innerHTML = html;
        document.getElementById('llm-preview-modal').classList.remove('hidden');
    } catch (e) {
        toast('LLM 预览失败: ' + e.message, 'error');
    }
}

function hideLlmPreview() {
    document.getElementById('llm-preview-modal').classList.add('hidden');
}

async function deleteSession(key) {
    if (!confirm(`确定要删除会话「${key}」吗？`)) return;
    try {
        await api('DELETE', `/api/sessions/${encodeURIComponent(key)}`);
        toast('会话已删除');
        loadSessions();
    } catch (e) {
        toast('删除失败: ' + e.message, 'error');
    }
}


// Archive Management


let currentArchiveSessionKey = null;

async function viewArchives(key) {
    currentArchiveSessionKey = key;
    try {
        const data = await api('GET', `/api/sessions/${encodeURIComponent(key)}/archives`);
        renderArchives(key, data.archives || []);
    } catch (e) {
        toast('加载归档失败: ' + e.message, 'error');
    }
}

function renderArchives(sessionKey, archives) {
    const container = document.getElementById('archives-list');
    document.getElementById('archives-modal-title').textContent = `归档会话 — ${sessionKey}`;

    if (!archives.length) {
        container.innerHTML = `
            <div class="empty-state">
                <p>暂无归档会话。使用 /new 命令可归档当前会话。</p>
            </div>`;
        document.getElementById('archives-modal').classList.remove('hidden');
        return;
    }

    container.innerHTML = archives.map((a, idx) => `
        <div class="archive-card" style="margin-bottom:10px;">
            <div class="archive-card-header">
                <div style="flex:1;min-width:0;">
                    <div class="archive-card-title">${idx + 1}. ${escapeHtml(a.title)}</div>
                    <div class="archive-card-meta">
                        ${a.message_count} 条消息 · 更新于 ${a.updated_at ? new Date(a.updated_at).toLocaleString() : ''}
                    </div>
                </div>
                <div class="archive-card-actions">
                    <button class="btn btn-small btn-secondary" data-archive-id="${a.id}" onclick="viewArchiveDetail(this.dataset.archiveId)">查看</button>
                    <button class="btn btn-small btn-primary" data-archive-id="${a.id}" data-archive-title="${escapeHtml(a.title)}" onclick="restoreArchive(this.dataset.archiveId, this.dataset.archiveTitle)">恢复</button>
                    <button class="btn btn-small btn-danger" data-archive-id="${a.id}" data-archive-title="${escapeHtml(a.title)}" onclick="deleteArchive(this.dataset.archiveId, this.dataset.archiveTitle)">删除</button>
                </div>
            </div>
        </div>
    `).join('');

    document.getElementById('archives-modal').classList.remove('hidden');
}

function hideArchivesModal() {
    document.getElementById('archives-modal').classList.add('hidden');
}

async function viewArchiveDetail(archiveId) {
    try {
        const data = await api('GET', `/api/sessions/${encodeURIComponent(currentArchiveSessionKey)}/archives/${archiveId}`);
        const detail = document.getElementById('archive-detail-content');
        document.getElementById('archive-detail-title').textContent = `归档: ${data.title}`;

        detail.innerHTML = (data.messages || []).map(msg => {
            const role = msg.role || 'unknown';
            let content = '';
            if (typeof msg.content === 'string') {
                content = formatReplyContent(escapeHtml(msg.content));
            } else if (Array.isArray(msg.content)) {
                content = msg.content.map(p => {
                    if (p.type === 'text') {
                        return formatReplyContent(escapeHtml(p.text || ''));
                    }
                    if (p.type === 'image_url' && p.image_url && p.image_url.url) {
                        return `<img src="${escapeHtml(p.image_url.url)}" style="max-width:200px;max-height:200px;border-radius:8px;margin:4px 0;display:block;" alt="image" />`;
                    }
                    return '';
                }).filter(Boolean).join('\n');
            }
            const roleLabel = { user: 'USER', assistant: 'ASSISTANT', system: 'SYSTEM', tool: 'TOOL', observed: 'OBSERVED' }[role] || role.toUpperCase();
            return `
                <div class="msg-bubble msg-${role}">
                    <div class="msg-role">${roleLabel}</div>
                    ${content || '<em>(无文本内容)</em>'}
                </div>
            `;
        }).join('');

        if (!data.messages || !data.messages.length) {
            detail.innerHTML = `
                <div class="empty-state">
                    <p>此归档暂无消息记录</p>
                </div>`;
        }

        document.getElementById('archive-detail-modal').classList.remove('hidden');
    } catch (e) {
        toast('加载归档详情失败: ' + e.message, 'error');
    }
}

function hideArchiveDetail() {
    document.getElementById('archive-detail-modal').classList.add('hidden');
}

async function restoreArchive(archiveId, title) {
    if (!confirm(`确定要恢复归档「${title}」吗？当前活跃会话将被自动归档。`)) return;
    try {
        const data = await api('POST', `/api/sessions/${encodeURIComponent(currentArchiveSessionKey)}/archives/${archiveId}/restore`);
        toast(`已恢复会话: ${data.title}`);
        hideArchivesModal();
        loadSessions();
    } catch (e) {
        toast('恢复失败: ' + e.message, 'error');
    }
}

async function deleteArchive(archiveId, title) {
    if (!confirm(`确定要删除归档「${title}」吗？此操作不可撤销。`)) return;
    try {
        await api('DELETE', `/api/sessions/${encodeURIComponent(currentArchiveSessionKey)}/archives/${archiveId}`);
        toast('归档已删除');
        viewArchives(currentArchiveSessionKey);
    } catch (e) {
        toast('删除失败: ' + e.message, 'error');
    }
}


// Tool Management


async function loadTools() {
    try {
        const data = await api('GET', '/api/tools');
        renderTools(data.tools || []);
    } catch (e) {
        toast('加载工具失败: ' + e.message, 'error');
    }
}

function renderTools(tools) {
    const container = document.getElementById('tool-list');
    if (!tools.length) {
        container.innerHTML = `
            <div class="empty-state">
                <p>未发现已注册的工具</p>
            </div>`;
        return;
    }

    // Group by source
    const groups = { builtin: [], plugin: [], mcp: [] };
    tools.forEach(t => {
        const src = t.source || 'builtin';
        if (groups[src]) groups[src].push(t);
        else groups.plugin.push(t);
    });

    const sourceLabels = {
        builtin: '内置工具',
        plugin: '插件工具',
        mcp: 'MCP 工具',
    };

    let html = '';
    for (const [source, items] of Object.entries(groups)) {
        if (!items.length) continue;
        html += `<div class="tool-group-title">${sourceLabels[source] || source} (${items.length})</div>`;
        html += items.map(t => `
            <div class="card tool-card">
                <label class="tool-switch">
                    <input type="checkbox" ${t.enabled ? 'checked' : ''} onchange="toggleTool('${escapeHtml(t.name)}', this.checked)">
                    <span class="slider"></span>
                </label>
                <div class="tool-card-info">
                    <div class="tool-card-title">
                        ${escapeHtml(t.name)}
                        <span class="badge badge-${t.source}">${t.source}</span>
                        ${!t.enabled ? '<span class="badge badge-disabled">已禁用</span>' : ''}
                    </div>
                    <div class="tool-card-desc">${escapeHtml(t.description || '无描述')}</div>
                </div>
            </div>
        `).join('');
    }

    container.innerHTML = html;
}

async function toggleTool(name, enabled) {
    const action = enabled ? 'enable' : 'disable';
    try {
        await api('POST', `/api/tools/${encodeURIComponent(name)}/${action}`);
    } catch (e) {
        toast(`操作失败: ${e.message}`, 'error');
        loadTools();
    }
}

async function refreshTools() {
    try {
        const data = await api('POST', '/api/tools/refresh');
        toast(`已扫描到 ${data.count || 0} 个工具`);
        loadTools();
    } catch (e) {
        toast('刷新失败: ' + e.message, 'error');
    }
}


// Config Management


// 配置分组：color 对应 CSS .group-<color> 顶条色
const CONFIG_GROUPS = [
    { id: 'context', label: '上下文与压缩', color: 'primary', subtitle: '对话上下文保留与压缩策略' },
    { id: 'tool', label: '工具与命令', color: 'teal', subtitle: 'Function Calling 与命令代执行' },
    { id: 'memory', label: '记忆系统', color: 'success', subtitle: '短期 / 长期记忆与自动总结', restart: true },
    { id: 'proactive', label: '主动回复', color: 'warning', subtitle: '超时主动发言、轮数与 AI 判断触发', restart: true },
    { id: 'debounce', label: '消息抖动', color: 'pink', subtitle: '短时间多消息合并处理' },
    { id: 'split', label: '分段发送', color: 'yellow', subtitle: '回复拆分与打字节奏模拟' },
    { id: 'clean', label: '文本清洗', color: 'purple', subtitle: 'Emoji / 括号 / 句尾清理' },
    { id: 'image', label: '画图 (OpenAI 兼容)', color: 'rose', subtitle: 'LLM 工具调用生成图片，修改需重启生效', restart: true },
    { id: 'webui', label: 'WebUI 与数据库', color: 'gray', subtitle: '端口 / 数据库修改需重启；认证项实时生效' },
];

const CONFIG_FIELDS = [
    { key: 'compression_mode', group: 'context', label: '上下文压缩模式', type: 'select', options: ['turn_limit', 'token'], hint: 'turn_limit: 超过轮数限制丢弃旧消息。token: 达到 Token 阈值时 LLM 总结旧消息。' },
    { key: 'max_turns', group: 'context', label: '最大保留轮数', type: 'number', hint: '轮数模式下最大保留的对话轮次。' },
    { key: 'token_threshold_ratio', group: 'context', label: 'Token 阈值比例', type: 'number', step: '0.05', hint: 'Token 模式下触发压缩的阈值比例 (0.5-0.95)。' },
    { key: 'keep_recent_turns', group: 'context', label: '压缩后保留轮数', type: 'number', hint: 'Token 模式下压缩后保留最近多少轮。' },
    { key: 'fallback_max_context_tokens', group: 'context', label: '最大上下文 Token (自动检测)', type: 'number', hint: '自动从 Provider 获取，通常无需手动修改。' },
    { key: 'user_id_format', group: 'context', label: '用户标识格式', type: 'text', hint: '群聊中用户消息前缀。{NAME}=昵称, {ID}=用户ID。' },
    { key: 'require_at_in_group', group: 'context', label: '群聊需要 @Bot', type: 'checkbox', hint: '开启后群聊只有 @Bot 才触发回复。' },
    { key: 'enable_passive_record', group: 'context', label: '被动记录群聊消息', type: 'checkbox', hint: '开启后，群聊中未触发回复的消息也会记录到上下文，丰富 LLM 对群聊的感知。仅对群聊生效。' },
    { key: 'enable_tool_calls', group: 'tool', label: '启用 Tool Calls', type: 'checkbox', hint: '是否启用原生 Function Calling。' },
    { key: 'max_tool_rounds', group: 'tool', label: '最大工具调用轮数', type: 'number', hint: '单次对话中工具调用最大循环次数。' },
    { key: 'enable_command_execution', group: 'tool', label: '启用命令执行', type: 'checkbox', hint: '开启后，用户可通过自然语言让 LLM 代为执行其他插件注册的命令。每个命令自身的权限定义会被尊重（管理员限定命令仅管理员可执行）。' },
    { key: 'enable_memory', group: 'memory', label: '启用记忆功能', type: 'checkbox', restart: true, hint: '开启后，LLM 可通过工具主动记忆，并支持自动总结对话。修改需重启插件生效。' },
    { key: 'short_term_max_count', group: 'memory', label: '短期记忆最大条数', type: 'number', hint: '短期记忆最多保留多少条。超出时自动总结优先清理旧条目。' },
    { key: 'short_term_max_chars', group: 'memory', label: '每条短期记忆最大字符数', type: 'number', hint: '每条短期记忆建议不超过此字符数。' },
    { key: 'long_term_max_count', group: 'memory', label: '长期记忆最大条数', type: 'number', hint: '长期记忆最多保留多少条。' },
    { key: 'long_term_retrieval_top_k', group: 'memory', label: '长期记忆检索返回条数', type: 'number', hint: '每次 LLM 调用前检索长期记忆返回条数。' },
    { key: 'long_term_fetch_k', group: 'memory', label: '长期记忆检索候选数', type: 'number', hint: '向量检索初始候选数。' },
    { key: 'long_term_enable_rerank', group: 'memory', label: '启用长期记忆重排', type: 'checkbox', hint: '使用重排模型提高检索精度。需配置 RerankProvider。' },
    { key: 'long_term_similarity_threshold', group: 'memory', label: '长期记忆相似度阈值', type: 'number', step: '0.05', hint: '相似度低于此值的检索结果直接丢弃 (0.0-1.0)。' },
    { key: 'memory_summary_interval', group: 'memory', label: '自动总结触发轮数', type: 'number', hint: '每隔多少轮对话触发一次短期记忆自动总结。' },
    { key: 'memory_summary_recent_turns', group: 'memory', label: '总结参考最近轮数', type: 'number', hint: '自动总结时参考最近几轮对话。' },
    { key: 'enable_auto_summary', group: 'memory', label: '启用自动总结', type: 'checkbox', hint: '开启后，按配置轮数和上下文压缩时自动总结短期记忆。' },
    { key: 'enable_proactive', group: 'proactive', label: '启用主动回复', type: 'checkbox', restart: true, hint: '开启后，支持 LLM 定时回复、超时主动发言、N 轮触发回复等功能。修改需重启插件生效。' },
    { key: 'proactive_timeout_minutes', group: 'proactive', label: '超时主动发言分钟数', type: 'number', hint: '用户未发言超过此分钟数后，AI 主动发起对话。需在会话设置中单独启用。' },
    { key: 'proactive_timeout_probability', group: 'proactive', label: '超时触发概率 (%)', type: 'number', hint: '每次超时命中时以此概率(0~100)决定是否实际触发。30 表示约三成概率触发，100 则每次必触发。' },
    { key: 'proactive_timeout_max_consecutive', group: 'proactive', label: '最大连续主动次数', type: 'number', hint: '连续主动回复的最大次数，达到后不再触发直到用户再次发言。0 表示不限制。' },
    { key: 'proactive_round_interval', group: 'proactive', label: 'N 轮触发回复（仅群聊）', type: 'number', hint: '每收到 N 条消息触发一次主动回复，仅对群聊生效。0 表示禁用。需在会话设置中单独启用。' },
    { key: 'proactive_ai_judge_interval', group: 'proactive', label: 'AI 判断触发消息条数（仅群聊）', type: 'number', hint: '群聊中每收到 N 条消息，让 AI 判断一次是否适合主动插话。0 表示禁用。需在会话设置中单独启用。与「N 轮触发」二选一。' },
    { key: 'proactive_ai_judge_cooldown', group: 'proactive', label: 'AI 判断触发后冷却秒数', type: 'number', hint: 'AI 判断为「该回复」并发送后进入冷却的时间（秒）。冷却期间消息照常计数但不触发判断。0 表示无冷却。' },
    { key: 'proactive_ai_judge_context_messages', group: 'proactive', label: 'AI 判断参考消息条数', type: 'number', hint: 'AI 判断是否插话时参考群聊最近 N 条消息（含被动消息）。冷却到期时累计的消息会一次性提交判断，建议此值 >= 触发条数。默认 10。' },
    { key: 'proactive_ai_judge_window_ms', group: 'proactive', label: 'AI 判断消停窗口（毫秒）', type: 'number', hint: '类似消息防抖：每收到一条消息就重置窗口，群聊消息消停（窗口到期）后才一次性触发 AI 判断。推荐 1500~3000。默认 2000。' },
    { key: 'enable_message_debounce', group: 'debounce', label: '启用消息抖动', type: 'checkbox', hint: '开启后，短时间内的多条消息会合并为一次 LLM 调用，减少冗余回复。适用于群聊中用户快速连发消息的场景。' },
    { key: 'debounce_window_ms', group: 'debounce', label: '抖动等待窗口 (毫秒)', type: 'number', hint: '收到消息后等待多少毫秒，若期间无新消息则开始处理。推荐 1500~3000。' },
    { key: 'debounce_max_messages', group: 'debounce', label: '最大缓冲消息数', type: 'number', hint: '缓冲区最多收集多少条消息，超出后立即处理不再等待。' },
    { key: 'debounce_scope', group: 'debounce', label: '抖动适用范围', type: 'select', options: ['group', 'private', 'all'], hint: 'group: 仅群聊生效。private: 仅私聊生效。all: 所有会话生效。' },
    { key: 'debounce_merge_mode', group: 'debounce', label: '消息合并模式', type: 'select', options: ['concat', 'numbered'], hint: 'concat: 直接拼接消息（保留发送者标识）。numbered: 为每条消息添加 [N] 序号前缀。' },
    { key: 'debounce_separator', group: 'debounce', label: '消息分隔符', type: 'text', hint: '合并多条消息时使用的分隔符。默认换行符 \\n。' },
    { key: 'debounce_absorb_passive', group: 'debounce', label: '被动消息并入抖动', type: 'checkbox', hint: '开启后，当某条消息触发抖动（激活回复）时，抖动窗口内到达的被动消息（未@Bot）会一并并入缓冲参与合并处理，而非单独走被动记录。无活跃缓冲时仍按被动记录处理。' },
    { key: 'enable_split_send', group: 'split', label: '启用分段发送', type: 'checkbox', hint: '将 LLM 回复按标点符号拆分为多条消息分段发送，模拟真人打字节奏。' },
    { key: 'split_mode', group: 'split', label: '分段模式', type: 'select', options: ['sentence', 'newline', 'smart'], hint: 'sentence: 按标点分段。newline: 仅按换行分段 (保持每行完整)。smart: 智能分段，保护对话文本不被劈断。' },
    { key: 'split_pattern', group: 'split', label: '分段匹配符号 (正则)', type: 'text', hint: '用于拆分 LLM 回复的正则表达式，匹配到的符号作为分段点。默认: [。！？\\n]' },
    { key: 'max_segments', group: 'split', label: '最大分段数', type: 'number', hint: '单次回复最多拆分成多少段。超过则合并后面的段落。' },
    { key: 'split_delay_ms', group: 'split', label: '分段发送间隔 (毫秒)', type: 'number', hint: '每段消息之间的发送延迟。' },
    { key: 'enable_text_clean', group: 'clean', label: '启用文本清洗', type: 'checkbox', hint: '开启后，对 LLM 回复进行文本清洗，去除 Emoji、括号内容、句尾多余字符等。' },
    { key: 'clean_emoji', group: 'clean', label: '去除 Emoji', type: 'checkbox', hint: '移除 LLM 回复中的所有 Emoji 表情符号。' },
    { key: 'clean_brackets', group: 'clean', label: '去除括号内容', type: 'checkbox', hint: '移除 LLM 回复中括号及其内容（如动作描写、心理活动等）。支持 ()（）[]【】。' },
    { key: 'clean_trailing_chars', group: 'clean', label: '清理句尾字符', type: 'checkbox', hint: '清理每句话末尾多余的标点或符号。' },
    { key: 'trailing_chars_pattern', group: 'clean', label: '句尾清理字符 (正则)', type: 'text', hint: '匹配句尾需要清理的字符的正则表达式。默认: [~～\\.。!！?？…·•\\-—_\\s]+$' },
    { key: 'enable_image_generation', group: 'image', label: '启用画图工具', type: 'checkbox', restart: true, hint: '开启后，LLM 可通过 generate_image 工具调用 OpenAI 兼容 API 生成图片并发送到聊天。修改需重启插件生效。' },
    { key: 'image_gen_api_base', group: 'image', label: '画图 API 地址 (OpenAI 兼容)', type: 'text', restart: true, hint: 'OpenAI 兼容的图片生成 API 地址。例如 https://api.openai.com（是否带 /v1 均可，会自动补全）。' },
    { key: 'image_gen_api_key', group: 'image', label: '画图 API Key', type: 'password', sensitive: true, restart: true, hint: 'OpenAI 兼容图片生成 API 的密钥。留空保存表示不修改。' },
    { key: 'image_gen_model', group: 'image', label: '画图模型名称', type: 'text', restart: true, hint: '图片生成模型名称，默认 gpt-image-2。' },
    { key: 'image_gen_size', group: 'image', label: '图片尺寸', type: 'select', options: ['1024x1024', '1024x1536', '1536x1024', 'auto'], restart: true, hint: '生成图片的尺寸。auto 由模型自动决定。不同模型支持的尺寸可能不同，调用失败时请尝试其他尺寸。' },
    { key: 'image_gen_quality', group: 'image', label: '图片质量', type: 'select', options: ['auto', 'low', 'medium', 'high'], restart: true, hint: '生成图片的质量等级。auto 由模型自动决定。不同模型支持的质量等级可能不同。' },
    { key: 'image_gen_timeout', group: 'image', label: '画图请求超时 (秒)', type: 'number', restart: true, hint: '调用图片生成 API 的超时时间。图片生成通常较慢，建议 60~180 秒。' },
    { key: 'image_gen_admin_only', group: 'image', label: '画图/改图仅管理员可用', type: 'checkbox', hint: '开启后，画图和改图工具仅管理员可调用。关闭则允许普通用户使用，受下方每日配额约束。管理员始终不受配额限制。' },
    { key: 'image_gen_quota_dimension', group: 'image', label: '普通用户配额维度', type: 'select', options: ['user', 'session'], hint: 'user: 每个用户每天各 N 次（群聊中互不影响）。session: 每个会话每天共享 N 次（群里所有人共用）。仅当「仅管理员可用」关闭时生效。' },
    { key: 'image_gen_daily_quota', group: 'image', label: '普通用户每日配额', type: 'number', hint: '普通用户每天可使用画图+改图的总次数（共用额度）。仅当「仅管理员可用」关闭时生效。管理员不受此限制。' },
    { key: 'web_port', group: 'webui', label: 'WebUI 端口', type: 'number', restart: true, hint: '插件管理 WebUI 的独立端口。修改后需重启插件。' },
    { key: 'db_type', group: 'webui', label: '数据库类型', type: 'select', options: ['sqlite', 'mysql'], restart: true, hint: 'sqlite: 本地文件数据库。mysql: 远程 MySQL 数据库。修改后需重启插件。' },
    { key: 'mysql_url', group: 'webui', label: 'MySQL 连接 URL', type: 'text', restart: true, hint: '格式: mysql+aiomysql://user:password@host:port/dbname。修改后需重启插件。' },
    { key: 'web_auth_enabled', group: 'webui', label: '启用 WebUI 登录认证', type: 'checkbox', hint: '开启后，访问 WebUI 管理面板需要先登录。' },
    { key: 'web_username', group: 'webui', label: 'WebUI 登录用户名', type: 'text', hint: 'WebUI 管理面板的登录用户名。' },
    { key: 'web_password', group: 'webui', label: 'WebUI 登录密码', type: 'password', sensitive: true, hint: 'WebUI 管理面板的登录密码。留空保存表示不修改。' },
];

let currentConfig = {};

async function loadConfig() {
    try {
        currentConfig = await api('GET', '/api/config');
        renderConfig();
    } catch (e) {
        toast('加载配置失败: ' + e.message, 'error');
    }
}

function renderConfig() {
    const container = document.getElementById('config-form');
    const html = CONFIG_GROUPS.map(group => {
        const fields = CONFIG_FIELDS.filter(f => f.group === group.id);
        if (!fields.length) return '';
        const restartBadge = group.restart ? '<span class="group-restart-badge">需重启</span>' : '';
        return `<div class="config-group group-${group.color}">
            <div class="config-group-bar"></div>
            <div class="config-group-header">
                <div class="config-group-title">${group.label}${restartBadge}</div>
                <div class="config-group-subtitle">${group.subtitle}</div>
            </div>
            <div class="config-group-body">
                ${fields.map(renderConfigField).join('')}
            </div>
        </div>`;
    }).join('');
    container.innerHTML = html + `<div class="config-actions">
        <button class="btn btn-secondary" onclick="confirmReloadConfig()">放弃修改</button>
        <button class="btn btn-primary" onclick="saveConfig()">保存配置</button>
    </div>`;

    // 滑动开关文字同步
    CONFIG_FIELDS.filter(f => f.type === 'checkbox').forEach(f => {
        const el = document.getElementById(`cfg-${f.key}`);
        if (!el) return;
        el.addEventListener('change', () => {
            const txt = el.closest('.cfg-switch')?.querySelector('.cfg-switch-text');
            if (txt) txt.textContent = el.checked ? '已开启' : '已关闭';
        });
    });
}

function renderConfigField(field) {
    const raw = currentConfig[field.key];
    const value = (raw === undefined || raw === null) ? '' : raw;
    let inputHtml = '';
    if (field.type === 'select') {
        inputHtml = `<select id="cfg-${field.key}">${field.options.map(o => `<option value="${o}" ${value === o ? 'selected' : ''}>${o}</option>`).join('')}</select>`;
    } else if (field.type === 'checkbox') {
        inputHtml = `<label class="cfg-switch">
            <input type="checkbox" id="cfg-${field.key}" ${value ? 'checked' : ''}>
            <span class="cfg-slider"></span>
            <span class="cfg-switch-text">${value ? '已开启' : '已关闭'}</span>
        </label>`;
    } else if (field.type === 'number') {
        inputHtml = `<input type="number" id="cfg-${field.key}" value="${escapeHtml(String(value))}" ${field.step ? `step="${field.step}"` : ''}>`;
    } else if (field.type === 'password') {
        inputHtml = `<input type="password" id="cfg-${field.key}" placeholder="留空表示不修改" autocomplete="new-password">`;
    } else {
        inputHtml = `<input type="text" id="cfg-${field.key}" value="${escapeHtml(String(value))}">`;
    }
    const secretFlag = field.sensitive
        ? (currentConfig[`${field.key}_set`] ? '<span class="secret-flag secret-set">已设置</span>' : '<span class="secret-flag secret-unset">未设置</span>')
        : '';
    return `<div class="config-item">
        <label>${field.label}</label>
        <div class="hint">${field.hint}</div>
        <div class="config-input-row">${inputHtml}${secretFlag}</div>
    </div>`;
}

async function refreshConfigPage(btn = null) {
    if (!confirm('放弃当前未保存的修改，并重新加载配置？')) return;
    await refreshPage(btn, loadConfig);
}

function confirmReloadConfig() {
    refreshConfigPage();
}

async function saveConfig() {
    const data = {};
    const restartChanged = [];
    CONFIG_FIELDS.forEach(field => {
        const el = document.getElementById(`cfg-${field.key}`);
        if (!el) return;
        let val;
        if (field.type === 'checkbox') val = el.checked;
        else if (field.type === 'number') val = parseFloat(el.value) || 0;
        else val = el.value;
        // 敏感字段留空表示不修改，跳过提交
        if (field.sensitive && !String(val).trim()) return;
        data[field.key] = val;
        if (field.restart && String(currentConfig[field.key]) !== String(val)) {
            restartChanged.push(field.label);
        }
    });

    if (restartChanged.length) {
        const ok = confirm(`以下配置需重启插件后才会生效：\n  · ${restartChanged.join('\n  · ')}\n\n仍要保存吗？`);
        if (!ok) return;
    }

    try {
        await api('PUT', '/api/config', data);
        toast(restartChanged.length ? '配置已保存（部分项需重启插件生效）' : '配置已保存');
        await loadConfig();
    } catch (e) {
        toast('保存失败: ' + e.message, 'error');
    }
}


// Image Quota Management


let imageQuotaData = null;

async function loadImageQuota() {
    try {
        imageQuotaData = await api('GET', '/api/image-quota');
        renderImageQuota();
    } catch (e) {
        toast('加载图片配额失败: ' + e.message, 'error');
    }
}

function renderImageQuota() {
    if (!imageQuotaData) return;
    const { items = [], daily_quota, dimension, admin_only, date } = imageQuotaData;
    const dimLabel = dimension === 'session' ? '按会话' : '按用户';

    const summary = document.getElementById('image-quota-summary');
    summary.innerHTML = `
        <div style="display:flex;gap:24px;flex-wrap:wrap;align-items:center;">
            <span><strong>日期:</strong> ${escapeHtml(date)}</span>
            <span><strong>配额维度:</strong> ${dimLabel}</span>
            <span><strong>每日上限:</strong> ${daily_quota} 次/天</span>
            <span><strong>访问限制:</strong> ${admin_only ? '仅管理员可用' : '允许普通用户'}</span>
        </div>`;

    const container = document.getElementById('image-quota-list');
    if (!items.length) {
        container.innerHTML = `<div class="empty-state"><p>今日暂无普通用户使用记录。</p></div>`;
        return;
    }
    const limit = Number(daily_quota) || 1;
    container.innerHTML = items.map((it, idx) => {
        const pct = Math.min(100, (it.used_count / limit) * 100);
        const barColor = it.used_count >= limit ? '#e74c3c' : '#3498db';
        return `<div class="card">
            <div class="card-header">
                <div class="card-title">${escapeHtml(it.identifier)} <span class="badge">${escapeHtml(it.dimension)}</span></div>
                <div class="card-meta">${it.updated_at ? new Date(it.updated_at).toLocaleString() : ''}</div>
            </div>
            <div class="card-body">
                已用 <strong>${it.used_count}</strong> / ${daily_quota} 次
                <div style="margin-top:8px;height:6px;background:#eee;border-radius:3px;overflow:hidden;">
                    <div style="width:${pct}%;height:100%;background:${barColor};"></div>
                </div>
            </div>
            <div class="card-actions">
                <button class="btn btn-small btn-danger" data-quota-idx="${idx}">重置配额</button>
            </div>
        </div>`;
    }).join('');

    // 用 data 属性 + addEventListener 读取 quota_key,避免内联 onclick 嵌入不可信值(XSS)
    container.querySelectorAll('button[data-quota-idx]').forEach(btn => {
        btn.addEventListener('click', () => {
            const item = items[Number(btn.dataset.quotaIdx)];
            if (item) resetImageQuota(item.quota_key);
        });
    });
}

async function resetImageQuota(quotaKey) {
    if (!confirm('确定重置该配额记录吗？用量将归零。')) return;
    try {
        await api('POST', '/api/image-quota/reset', { quota_key: quotaKey });
        toast('配额已重置');
        await loadImageQuota();
    } catch (e) {
        toast('重置失败: ' + e.message, 'error');
    }
}


// Memory Management


let currentMemorySessionKey = null;
let shortTermMemories = [];
let longTermMemories = [];

async function loadMemorySessionList() {
    try {
        const data = await api('GET', '/api/sessions?page=1&page_size=100');
        const select = document.getElementById('memory-session-select');
        const currentVal = select.value;
        select.innerHTML = '<option value="">选择会话...</option>';
        (data.sessions || []).forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.session_key;
            const isGroup = !s.session_key.includes(':private:');
            opt.textContent = `${isGroup ? '[群]' : '[私]'} ${s.session_key}`;
            select.appendChild(opt);
        });
        if (currentVal) {
            select.value = currentVal;
        }
    } catch (e) {
        toast('加载会话列表失败: ' + e.message, 'error');
    }
}

async function onMemorySessionChange() {
    const select = document.getElementById('memory-session-select');
    currentMemorySessionKey = select.value;
    setUiState('ce_ui_mem_session', currentMemorySessionKey || '');
    if (!currentMemorySessionKey) {
        document.getElementById('memory-content').style.display = 'none';
        document.getElementById('memory-empty').style.display = '';
        return;
    }
    document.getElementById('memory-content').style.display = '';
    document.getElementById('memory-empty').style.display = 'none';
    await loadMemories();
}

async function loadMemories() {
    if (!currentMemorySessionKey) return;
    const key = encodeURIComponent(currentMemorySessionKey);
    try {
        const [shortData, longData] = await Promise.all([
            api('GET', `/api/memories/${key}/short`),
            api('GET', `/api/memories/${key}/long`),
        ]);
        shortTermMemories = shortData.memories || [];
        longTermMemories = longData.memories || [];
        renderMemories();
    } catch (e) {
        toast('加载记忆失败: ' + e.message, 'error');
    }
}

function renderMemories() {
    const shortTitle = document.querySelector('#short-term-list')?.closest('.memory-panel')?.querySelector('h4');
    const longTitle = document.querySelector('#long-term-list')?.closest('.memory-panel')?.querySelector('h4');
    if (shortTitle) shortTitle.innerHTML = `短期记忆 <span class="memory-count-badge">${shortTermMemories.length}</span>`;
    if (longTitle) longTitle.innerHTML = `长期记忆 <span class="memory-count-badge">${longTermMemories.length}</span>`;
    renderShortTermMemories();
    renderLongTermMemories();
}

function renderShortTermMemories() {
    const container = document.getElementById('short-term-list');
    if (!shortTermMemories.length) {
        container.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:8px 0;">暂无短期记忆</div>';
        return;
    }
    container.innerHTML = shortTermMemories.map(m => `
        <div class="memory-card">
            <div class="memory-card-header">
                <div style="flex:1;min-width:0;">
                    <div class="memory-card-meta">[${escapeHtml(m.id.substring(0, 8))}] ${sourceLabel(m.source)} ${m.updated_at ? new Date(m.updated_at).toLocaleString() : ''}</div>
                    <div class="memory-card-content">${escapeHtml(m.content)}</div>
                </div>
                <div class="memory-card-actions">
                    <button class="btn btn-small btn-secondary" onclick="editMemory('short_term','${escapeHtml(m.id)}')">编辑</button>
                    <button class="btn btn-small btn-danger" onclick="deleteMemory('short_term','${escapeHtml(m.id)}')">删除</button>
                </div>
            </div>
        </div>
    `).join('');
}

function renderLongTermMemories() {
    const container = document.getElementById('long-term-list');
    if (!longTermMemories.length) {
        container.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:8px 0;">暂无长期记忆</div>';
        return;
    }
    container.innerHTML = longTermMemories.map(m => `
        <div class="memory-card ${m.pinned ? 'memory-card-pinned' : ''}">
            <div class="memory-card-header">
                <div style="flex:1;min-width:0;">
                    <div class="memory-card-meta">[${escapeHtml(m.id.substring(0, 8))}] ${m.pinned ? '<span class="source-label" style="color:var(--warning)">[置顶]</span>' : ''} ${sourceLabel(m.source)} ${m.updated_at ? new Date(m.updated_at).toLocaleString() : ''}</div>
                    <div class="memory-card-content">${escapeHtml(m.content)}</div>
                </div>
                <div class="memory-card-actions">
                    <button class="btn btn-small btn-secondary" onclick="editMemory('long_term','${escapeHtml(m.id)}')">编辑</button>
                    <button class="btn btn-small btn-danger" onclick="deleteMemory('long_term','${escapeHtml(m.id)}')">删除</button>
                </div>
            </div>
        </div>
    `).join('');
}

function showAddMemoryModal(type) {
    document.getElementById('memory-modal-title').textContent = type === 'short_term' ? '添加短期记忆' : '添加长期记忆';
    document.getElementById('memory-type').value = type;
    document.getElementById('memory-edit-id').value = '';
    document.getElementById('memory-content-input').value = '';
    document.getElementById('memory-pinned').checked = false;
    document.getElementById('memory-pinned-group').style.display = type === 'long_term' ? '' : 'none';
    document.getElementById('memory-modal').classList.remove('hidden');
}

function editMemory(type, id) {
    const list = type === 'short_term' ? shortTermMemories : longTermMemories;
    const mem = list.find(m => m.id === id);
    if (!mem) return;
    document.getElementById('memory-modal-title').textContent = '编辑记忆';
    document.getElementById('memory-type').value = type;
    document.getElementById('memory-edit-id').value = id;
    document.getElementById('memory-content-input').value = mem.content;
    document.getElementById('memory-pinned').checked = !!mem.pinned;
    document.getElementById('memory-pinned-group').style.display = type === 'long_term' ? '' : 'none';
    document.getElementById('memory-modal').classList.remove('hidden');
}

function hideMemoryModal() {
    document.getElementById('memory-modal').classList.add('hidden');
}

function memTypePath(type) { return type.replace('_term', ''); }

async function saveMemory(e) {
    e.preventDefault();
    if (!currentMemorySessionKey) return;
    const type = document.getElementById('memory-type').value;
    const editId = document.getElementById('memory-edit-id').value;
    const content = document.getElementById('memory-content-input').value.trim();
    const pinned = type === 'long_term' && document.getElementById('memory-pinned').checked;
    const key = encodeURIComponent(currentMemorySessionKey);
    const path = memTypePath(type);

    try {
        if (editId) {
            await api('PUT', `/api/memories/${key}/${path}/${editId}`, { content, pinned });
            toast('记忆已更新');
        } else {
            await api('POST', `/api/memories/${key}/${path}`, { content, pinned });
            toast('记忆已添加');
        }
        hideMemoryModal();
        await loadMemories();
    } catch (e) {
        toast('保存失败: ' + e.message, 'error');
    }
}

async function deleteMemory(type, id) {
    if (!confirm('确定要删除这条记忆吗？')) return;
    if (!currentMemorySessionKey) return;
    const key = encodeURIComponent(currentMemorySessionKey);
    const path = memTypePath(type);
    try {
        await api('DELETE', `/api/memories/${key}/${path}/${id}`);
        toast('记忆已删除');
        await loadMemories();
    } catch (e) {
        toast('删除失败: ' + e.message, 'error');
    }
}


// Proactive Management


let proactiveSessions = {};
let currentProactiveSessionKey = null;

async function loadProactiveSessionList() {
    try {
        const data = await api('GET', '/api/sessions?page=1&page_size=100');
        const select = document.getElementById('proactive-session-select');
        const currentVal = select.value;
        select.innerHTML = '<option value="">选择会话...</option>';
        (data.sessions || []).forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.session_key;
            const isGroup = !s.session_key.includes(':private:');
            opt.textContent = `${isGroup ? '[群]' : '[私]'} ${s.session_key}`;
            select.appendChild(opt);
        });
        if (currentVal) {
            select.value = currentVal;
        }
    } catch (e) {
        toast('加载会话列表失败: ' + e.message, 'error');
    }
}

async function onProactiveSessionChange() {
    const select = document.getElementById('proactive-session-select');
    currentProactiveSessionKey = select.value;
    setUiState('ce_ui_pro_session', currentProactiveSessionKey || '');
    if (!currentProactiveSessionKey) {
        document.getElementById('proactive-settings').style.display = 'none';
        document.getElementById('proactive-empty').style.display = '';
        return;
    }
    document.getElementById('proactive-settings').style.display = '';
    document.getElementById('proactive-empty').style.display = 'none';
    await loadProactiveSettings();
}

async function loadProactiveSettings() {
    try {
        const data = await api('GET', '/api/proactive/sessions');
        proactiveSessions = {};
        (data.sessions || []).forEach(s => {
            proactiveSessions[s.session_key] = s;
        });
        updateProactiveToggles();
    } catch (e) {
        // Proactive may not be enabled, silent fail
    }
}

function updateProactiveToggles() {
    const sessionKey = currentProactiveSessionKey;
    const settings = proactiveSessions[sessionKey] || {};
    document.getElementById('proactive-timeout-toggle').checked = !!settings.timeout_enabled;
    const isGroup = sessionKey && !sessionKey.includes(':private:');
    const roundLabel = document.getElementById('proactive-round-label');
    const roundToggle = document.getElementById('proactive-round-toggle');
    roundToggle.checked = isGroup && !!settings.round_enabled;
    roundLabel.style.display = isGroup ? '' : 'none';
    const aiJudgeLabel = document.getElementById('proactive-ai-judge-label');
    const aiJudgeToggle = document.getElementById('proactive-ai-judge-toggle');
    aiJudgeToggle.checked = isGroup && !!settings.ai_judge_enabled;
    aiJudgeLabel.style.display = isGroup ? '' : 'none';
}

async function toggleProactiveTimeout() {
    if (!currentProactiveSessionKey) return;
    const enabled = document.getElementById('proactive-timeout-toggle').checked;
    const key = encodeURIComponent(currentProactiveSessionKey);
    try {
        await api('PUT', `/api/proactive/${key}/timeout`, { enabled });
        toast(enabled ? '已启用超时主动发言' : '已关闭超时主动发言');
    } catch (e) {
        toast('设置失败: ' + e.message, 'error');
        document.getElementById('proactive-timeout-toggle').checked = !enabled;
    }
}

async function toggleProactiveRound() {
    if (!currentProactiveSessionKey) return;
    const enabled = document.getElementById('proactive-round-toggle').checked;
    const key = encodeURIComponent(currentProactiveSessionKey);
    try {
        await api('PUT', `/api/proactive/${key}/round`, { enabled });
        toast(enabled ? '已启用轮数触发' : '已关闭轮数触发');
    } catch (e) {
        toast('设置失败: ' + e.message, 'error');
        document.getElementById('proactive-round-toggle').checked = !enabled;
    }
}

async function toggleProactiveAiJudge() {
    if (!currentProactiveSessionKey) return;
    const enabled = document.getElementById('proactive-ai-judge-toggle').checked;
    const key = encodeURIComponent(currentProactiveSessionKey);
    try {
        await api('PUT', `/api/proactive/${key}/ai_judge`, { enabled });
        toast(enabled ? '已启用 AI 判断插话' : '已关闭 AI 判断插话');
    } catch (e) {
        toast('设置失败: ' + e.message, 'error');
        document.getElementById('proactive-ai-judge-toggle').checked = !enabled;
    }
}

// Auth


async function checkAuth() {
    try {
        const resp = await fetch('/api/auth/status', {
            headers: { 'Authorization': `Bearer ${localStorage.getItem('auth_token') || ''}` },
        });
        const data = await resp.json();
        if (data.enabled && !data.authenticated) {
            window.location.href = '/login';
            return false;
        }
        if (data.enabled) {
            const footer = document.getElementById('sidebar-footer');
            if (footer) footer.style.display = '';
        }
        return true;
    } catch (e) {
        return true;
    }
}

async function logout() {
    try {
        await api('POST', '/api/auth/logout');
    } catch (e) {
        // Ignore logout request errors
    }
    localStorage.removeItem('auth_token');
    window.location.href = '/login';
}

// Init

document.addEventListener('DOMContentLoaded', async () => {
    const ok = await checkAuth();
    if (!ok) return;
    const savedTab = getUiState('ce_ui_tab') || 'personas';
    const exists = document.querySelector(`.nav-item[data-tab="${savedTab}"]`);
    activateTab(exists ? savedTab : 'personas');
});
