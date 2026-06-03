// ============================================================
// Chat Engine WebUI — Frontend Application
// ============================================================

const API_BASE = '';  // 同源，无需前缀

// ---- 工具函数 ----

async function api(method, path, body = null) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(`${API_BASE}${path}`, opts);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

function toast(msg, type = 'success') {
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ---- Tab 切换 ----

document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
        const tab = item.dataset.tab;
        document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        item.classList.add('active');
        document.getElementById(`tab-${tab}`).classList.add('active');

        // 加载对应 Tab 数据
        if (tab === 'personas') loadPersonas();
        if (tab === 'sessions') loadSessions();
        if (tab === 'tools') loadTools();
        if (tab === 'config') loadConfig();
    });
});

// ============================================================
// 人格管理
// ============================================================

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
        container.innerHTML = '<div class="card"><div class="card-body">暂无人格，请点击右上角新建。</div></div>';
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

// ============================================================
// 会话管理
// ============================================================

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
        container.innerHTML = '<div class="card"><div class="card-body">暂无会话记录。</div></div>';
        document.getElementById('session-pagination').innerHTML = '';
        return;
    }

    container.innerHTML = sessions.map(s => {
        const isGroup = !s.session_key.includes(':private:');
        const icon = isGroup ? '👥' : '👤';
        return `
            <div class="card">
                <div class="card-header">
                    <div class="card-title">${icon} ${escapeHtml(s.session_key)}</div>
                    <div class="card-meta">
                        ${s.message_count} 条消息 · ${s.updated_at ? new Date(s.updated_at).toLocaleString() : ''}
                    </div>
                </div>
                <div class="card-actions">
                    <button class="btn btn-small btn-secondary" onclick="viewSession('${escapeHtml(s.session_key)}')">查看上下文</button>
                    <button class="btn btn-small btn-danger" onclick="deleteSession('${escapeHtml(s.session_key)}')">删除</button>
                </div>
            </div>
        `;
    }).join('');

    // 分页
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
    loadSessions();
}

async function viewSession(key) {
    try {
        const data = await api('GET', `/api/sessions/${encodeURIComponent(key)}`);
        const modal = document.getElementById('session-modal');
        const detail = document.getElementById('session-detail');

        detail.innerHTML = (data.messages || []).map(msg => {
            const role = msg.role || 'unknown';
            let content = '';
            if (typeof msg.content === 'string') {
                content = escapeHtml(msg.content);
            } else if (Array.isArray(msg.content)) {
                content = msg.content
                    .filter(p => p.type === 'text')
                    .map(p => escapeHtml(p.text || ''))
                    .join('\n');
            }
            const roleLabel = { user: 'USER', assistant: 'ASSISTANT', system: 'SYSTEM', tool: 'TOOL' }[role] || role.toUpperCase();
            return `
                <div class="msg-bubble msg-${role}">
                    <div class="msg-role">${roleLabel}</div>
                    ${content || '<em>(无文本内容)</em>'}
                </div>
            `;
        }).join('');

        if (!data.messages || !data.messages.length) {
            detail.innerHTML = '<div class="card"><div class="card-body">此会话暂无消息记录。</div></div>';
        }

        modal.classList.remove('hidden');
    } catch (e) {
        toast('加载会话失败: ' + e.message, 'error');
    }
}

function hideSessionModal() {
    document.getElementById('session-modal').classList.add('hidden');
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

// ============================================================
// 工具管理
// ============================================================

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
        container.innerHTML = '<div class="card"><div class="card-body">未发现已注册的工具。</div></div>';
        return;
    }

    // 按来源分组
    const groups = { builtin: [], plugin: [], mcp: [] };
    tools.forEach(t => {
        const src = t.source || 'builtin';
        if (groups[src]) groups[src].push(t);
        else groups.plugin.push(t);
    });

    const sourceLabels = {
        builtin: '🛠 内置工具',
        plugin: '🔌 插件工具',
        mcp: '🌐 MCP 工具',
    };

    let html = '';
    for (const [source, items] of Object.entries(groups)) {
        if (!items.length) continue;
        html += `<h4 style="margin: 16px 0 8px; color: var(--text-dim); font-size: 13px;">${sourceLabels[source] || source} (${items.length})</h4>`;
        html += items.map(t => `
            <div class="card" style="display: flex; align-items: center; gap: 16px;">
                <label class="tool-switch">
                    <input type="checkbox" ${t.enabled ? 'checked' : ''} onchange="toggleTool('${escapeHtml(t.name)}', this.checked)">
                    <span class="slider"></span>
                </label>
                <div style="flex: 1; min-width: 0;">
                    <div class="card-title" style="font-size: 14px;">
                        ${escapeHtml(t.name)}
                        <span class="badge badge-${t.source}">${t.source}</span>
                        ${!t.enabled ? '<span class="badge badge-disabled">已禁用</span>' : ''}
                    </div>
                    <div class="card-body" style="max-height: 40px; margin-top: 4px;">${escapeHtml(t.description || '无描述')}</div>
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
        loadTools(); // 刷新恢复状态
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

// ============================================================
// 配置管理
// ============================================================

const CONFIG_FIELDS = [
    { key: 'compression_mode', label: '上下文压缩模式', type: 'select', options: ['turn_limit', 'token'], hint: 'turn_limit: 超过轮数限制丢弃旧消息。token: 达到 Token 阈值时 LLM 总结旧消息。' },
    { key: 'max_turns', label: '最大保留轮数', type: 'number', hint: '轮数模式下最大保留的对话轮次。' },
    { key: 'token_threshold_ratio', label: 'Token 阈值比例', type: 'number', step: '0.05', hint: 'Token 模式下触发压缩的阈值比例 (0.5-0.95)。' },
    { key: 'keep_recent_turns', label: '压缩后保留轮数', type: 'number', hint: 'Token 模式下压缩后保留最近多少轮。' },
    { key: 'fallback_max_context_tokens', label: '最大上下文 Token (自动检测)', type: 'number', hint: '自动从 Provider 获取，通常无需手动修改。' },
    { key: 'user_id_format', label: '用户标识格式', type: 'text', hint: '群聊中用户消息前缀。{NAME}=昵称, {ID}=用户ID。' },
    { key: 'require_at_in_group', label: '群聊需要 @Bot', type: 'checkbox', hint: '开启后群聊只有 @Bot 才触发回复。' },
    { key: 'enable_tool_calls', label: '启用 Tool Calls', type: 'checkbox', hint: '是否启用原生 Function Calling。' },
    { key: 'max_tool_rounds', label: '最大工具调用轮数', type: 'number', hint: '单次对话中工具调用最大循环次数。' },
    { key: 'enable_passive_record', label: '被动记录群聊消息', type: 'checkbox', hint: '开启后，群聊中未触发回复的消息也会记录到上下文，丰富 LLM 对群聊的感知。仅对群聊生效。' },
    { key: 'enable_split_send', label: '启用分段发送', type: 'checkbox', hint: '将 LLM 回复按标点符号拆分为多条消息分段发送，模拟真人打字节奏。' },
    { key: 'split_pattern', label: '分段匹配符号 (正则)', type: 'text', hint: '用于拆分 LLM 回复的正则表达式，匹配到的符号作为分段点。默认: [。！？\\n]' },
    { key: 'max_segments', label: '最大分段数', type: 'number', hint: '单次回复最多拆分成多少段。超过则合并后面的段落。' },
    { key: 'split_delay_ms', label: '分段发送间隔 (毫秒)', type: 'number', hint: '每段消息之间的发送延迟。' },
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
    let html = CONFIG_FIELDS.map(field => {
        const value = currentConfig[field.key] ?? field.default ?? '';
        let inputHtml = '';

        if (field.type === 'select') {
            inputHtml = `<select id="cfg-${field.key}">
                ${field.options.map(o => `<option value="${o}" ${value === o ? 'selected' : ''}>${o}</option>`).join('')}
            </select>`;
        } else if (field.type === 'checkbox') {
            inputHtml = `<label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                <input type="checkbox" id="cfg-${field.key}" ${value ? 'checked' : ''}>
                <span>${value ? '已开启' : '已关闭'}</span>
            </label>`;
        } else if (field.type === 'number') {
            inputHtml = `<input type="number" id="cfg-${field.key}" value="${value}" ${field.step ? `step="${field.step}"` : ''}>`;
        } else {
            inputHtml = `<input type="text" id="cfg-${field.key}" value="${escapeHtml(String(value))}">`;
        }

        return `
            <div class="config-item">
                <label>${field.label}</label>
                <div class="hint">${field.hint}</div>
                ${inputHtml}
            </div>
        `;
    }).join('');

    html += '<div class="config-actions"><button class="btn btn-primary" onclick="saveConfig()">保存配置</button></div>';
    container.innerHTML = html;

    // checkbox 实时文字更新
    CONFIG_FIELDS.filter(f => f.type === 'checkbox').forEach(f => {
        const el = document.getElementById(`cfg-${f.key}`);
        if (el) {
            el.addEventListener('change', () => {
                el.nextElementSibling.textContent = el.checked ? '已开启' : '已关闭';
            });
        }
    });
}

async function saveConfig() {
    const data = {};
    CONFIG_FIELDS.forEach(field => {
        const el = document.getElementById(`cfg-${field.key}`);
        if (!el) return;
        if (field.type === 'checkbox') {
            data[field.key] = el.checked;
        } else if (field.type === 'number') {
            data[field.key] = parseFloat(el.value) || 0;
        } else {
            data[field.key] = el.value;
        }
    });

    try {
        await api('PUT', '/api/config', data);
        toast('配置已保存');
    } catch (e) {
        toast('保存失败: ' + e.message, 'error');
    }
}

// ============================================================
// 初始化
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    loadPersonas();
});
