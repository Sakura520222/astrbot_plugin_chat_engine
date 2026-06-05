
// Chat Engine WebUI — Frontend Application


const API_BASE = '';  // 同源，无需前缀

//  工具函数 

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
    setTimeout(() => el.remove(), 3000);
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function formatReplyContent(text) {
    if (!text) return text;
    // 将 [回复 xxx] 替换为 blockquote 样式
    return text.replace(
        /\[回复 ([^\]]+)\]/g,
        '<blockquote class="reply-quote">$1</blockquote>'
    );
}

//  Tab 切换 

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
        if (tab === 'memories') loadMemorySessionList();
        if (tab === 'proactive') loadProactiveSessionList();
        if (tab === 'tools') loadTools();
        if (tab === 'config') loadConfig();
    });
});


// 人格管理


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


// 会话管理


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
    currentSessionKey = key;
    try {
        const data = await api('GET', `/api/sessions/${encodeURIComponent(key)}`);
        const modal = document.getElementById('session-modal');
        const detail = document.getElementById('session-detail');

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
                        return `<img src="${escapeHtml(p.image_url.url)}" style="max-width:200px;max-height:200px;border-radius:6px;margin:4px 0;display:block;" />`;
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

// LLM 预览
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

        // 概览信息
        html += `<div class="preview-stats">
            <div class="stat-item"><span class="stat-label">Provider</span><span class="stat-value">${data.provider || '-'}</span></div>
            <div class="stat-item"><span class="stat-label">Modalities</span><span class="stat-value">${(data.modalities || []).join(', ') || '-'}</span></div>
            <div class="stat-item"><span class="stat-label">上下文条数</span><span class="stat-value">${data.context_count}</span></div>
            <div class="stat-item"><span class="stat-label">工具数</span><span class="stat-value">${data.tool_count}</span></div>
            <div class="stat-item"><span class="stat-label">估算 Token</span><span class="stat-value">${data.estimated_tokens}</span></div>
            ${data.filter_summary ? `<div class="stat-item"><span class="stat-label">模态过滤</span><span class="stat-value">图片=${data.filter_summary.fixed_image_blocks}, 音频=${data.filter_summary.fixed_audio_blocks}</span></div>` : ''}
        </div>`;

        // System Prompt
        if (data.system_prompt) {
            html += `<div class="preview-section">
                <div class="preview-section-title" onclick="this.nextElementSibling.classList.toggle('collapsed')">System Prompt (${data.system_prompt_length} 字)</div>
                <pre class="preview-code collapsed">${escapeHtml(data.system_prompt)}</pre>
            </div>`;
        }

        // 上下文消息
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
            html += `<div class="preview-msg">
                <span class="preview-role" style="color:${color}">${role.toUpperCase()}</span>
                <div class="preview-content">${content || '<em>(空)</em>'}</div>
            </div>`;
        }
        html += '</div>';

        // 工具列表
        if (data.tools && data.tools.length > 0) {
            html += `<div class="preview-section">
                <div class="preview-section-title" onclick="this.nextElementSibling.classList.toggle('collapsed')">工具列表 (${data.tool_count})</div>
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


// 工具管理


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


// 配置管理


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
    { key: 'split_mode', label: '分段模式', type: 'select', options: ['sentence', 'newline', 'smart'], hint: 'sentence: 按标点分段。newline: 仅按换行分段 (保持每行完整)。smart: 智能分段，保护对话文本不被劈断。' },
    { key: 'split_pattern', label: '分段匹配符号 (正则)', type: 'text', hint: '用于拆分 LLM 回复的正则表达式，匹配到的符号作为分段点。默认: [。！？\\n]' },
    { key: 'max_segments', label: '最大分段数', type: 'number', hint: '单次回复最多拆分成多少段。超过则合并后面的段落。' },
    { key: 'split_delay_ms', label: '分段发送间隔 (毫秒)', type: 'number', hint: '每段消息之间的发送延迟。' },
    { key: 'enable_text_clean', label: '启用文本清洗', type: 'checkbox', hint: '开启后，对 LLM 回复进行文本清洗，去除 Emoji、括号内容、句尾多余字符等。' },
    { key: 'clean_emoji', label: '去除 Emoji', type: 'checkbox', hint: '移除 LLM 回复中的所有 Emoji 表情符号。' },
    { key: 'clean_brackets', label: '去除括号内容', type: 'checkbox', hint: '移除 LLM 回复中括号及其内容（如动作描写、心理活动等）。支持 ()（）[]【】。' },
    { key: 'clean_trailing_chars', label: '清理句尾字符', type: 'checkbox', hint: '清理每句话末尾多余的标点或符号。' },
    { key: 'trailing_chars_pattern', label: '句尾清理字符 (正则)', type: 'text', hint: '匹配句尾需要清理的字符的正则表达式。默认: [~～\\.。!！?？…·•\\-—_\\s]+$' },
    { key: 'enable_memory', label: '启用记忆功能', type: 'checkbox', hint: '开启后，LLM 可通过工具主动记忆，并支持自动总结对话。' },
    { key: 'short_term_max_count', label: '短期记忆最大条数', type: 'number', hint: '短期记忆最多保留多少条。超出时自动总结优先清理旧条目。' },
    { key: 'short_term_max_chars', label: '每条短期记忆最大字符数', type: 'number', hint: '每条短期记忆建议不超过此字符数。' },
    { key: 'long_term_max_count', label: '长期记忆最大条数', type: 'number', hint: '长期记忆最多保留多少条。' },
    { key: 'long_term_retrieval_top_k', label: '长期记忆检索返回条数', type: 'number', hint: '每次 LLM 调用前检索长期记忆返回条数。' },
    { key: 'long_term_fetch_k', label: '长期记忆检索候选数', type: 'number', hint: '向量检索初始候选数。' },
    { key: 'long_term_enable_rerank', label: '启用长期记忆重排', type: 'checkbox', hint: '使用重排模型提高检索精度。需配置 RerankProvider。' },
    { key: 'long_term_similarity_threshold', label: '长期记忆相似度阈值', type: 'number', step: '0.05', hint: '相似度低于此值的检索结果直接丢弃 (0.0-1.0)。' },
    { key: 'memory_summary_interval', label: '自动总结触发轮数', type: 'number', hint: '每隔多少轮对话触发一次短期记忆自动总结。' },
    { key: 'memory_summary_recent_turns', label: '总结参考最近轮数', type: 'number', hint: '自动总结时参考最近几轮对话。' },
    { key: 'enable_auto_summary', label: '启用自动总结', type: 'checkbox', hint: '开启后，按配置轮数和上下文压缩时自动总结短期记忆。' },
    { key: 'enable_proactive', label: '启用主动回复', type: 'checkbox', hint: '开启后，支持 LLM 定时回复、超时主动发言、N 轮触发回复等功能。' },
    { key: 'proactive_timeout_minutes', label: '超时主动发言分钟数', type: 'number', hint: '用户未发言超过此分钟数后，AI 主动发起对话。需在会话设置中单独启用。' },
    { key: 'proactive_round_interval', label: 'N 轮触发回复（仅群聊）', type: 'number', hint: '每收到 N 条消息触发一次主动回复，仅对群聊生效。0 表示禁用。需在会话设置中单独启用。' },
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


// 记忆管理


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
            const icon = s.session_key.includes(':private:') ? '👤' : '👥';
            opt.textContent = `${icon} ${s.session_key}`;
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
    renderShortTermMemories();
    renderLongTermMemories();
}

function renderShortTermMemories() {
    const container = document.getElementById('short-term-list');
    if (!shortTermMemories.length) {
        container.innerHTML = '<div class="card"><div class="card-body" style="color:var(--text-dim)">暂无短期记忆。</div></div>';
        return;
    }
    container.innerHTML = shortTermMemories.map(m => `
        <div class="card" style="padding:8px 12px;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
                <div style="flex:1;min-width:0;">
                    <div style="font-size:12px;color:var(--text-dim);">[${escapeHtml(m.id.substring(0, 8))}] ${m.source === 'auto' ? '🤖' : m.source === 'manual' ? '✋' : '🔧'} ${m.updated_at ? new Date(m.updated_at).toLocaleString() : ''}</div>
                    <div style="margin-top:2px;">${escapeHtml(m.content)}</div>
                </div>
                <div style="display:flex;gap:4px;flex-shrink:0;">
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
        container.innerHTML = '<div class="card"><div class="card-body" style="color:var(--text-dim)">暂无长期记忆。</div></div>';
        return;
    }
    container.innerHTML = longTermMemories.map(m => `
        <div class="card" style="padding:8px 12px;${m.pinned ? 'border-left:3px solid var(--accent, #e67e22);' : ''}">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
                <div style="flex:1;min-width:0;">
                    <div style="font-size:12px;color:var(--text-dim);">[${escapeHtml(m.id.substring(0, 8))}] ${m.pinned ? '📌 ' : ''}${m.source === 'manual' ? '✋' : '🔧'} ${m.updated_at ? new Date(m.updated_at).toLocaleString() : ''}</div>
                    <div style="margin-top:2px;">${escapeHtml(m.content)}</div>
                </div>
                <div style="display:flex;gap:4px;flex-shrink:0;">
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

// 记忆类型 → URL 路径段 (short_term → short, long_term → long)
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


// 初始化


// 主动回复管理（独立 Tab）

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
            const icon = s.session_key.includes(':private:') ? '👤' : '👥';
            opt.textContent = `${icon} ${s.session_key}`;
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
        // 主动回复可能未启用，静默失败
    }
}

function updateProactiveToggles() {
    const sessionKey = currentProactiveSessionKey;
    const settings = proactiveSessions[sessionKey] || {};
    document.getElementById('proactive-timeout-toggle').checked = !!settings.timeout_enabled;
    // 轮数触发仅对群聊显示（私聊 session_key 包含 ":private:"）
    const isGroup = sessionKey && !sessionKey.includes(':private:');
    const roundLabel = document.getElementById('proactive-round-label');
    const roundToggle = document.getElementById('proactive-round-toggle');
    roundToggle.checked = isGroup && !!settings.round_enabled;
    roundLabel.style.display = isGroup ? '' : 'none';
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

//  认证管理


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
        // 认证已启用时显示退出按钮
        if (data.enabled) {
            const footer = document.getElementById('sidebar-footer');
            if (footer) footer.style.display = '';
        }
        return true;
    } catch (e) {
        return true; // 网络异常不阻塞
    }
}

async function logout() {
    try {
        await api('POST', '/api/auth/logout');
    } catch (e) {
        // 忽略登出请求错误
    }
    localStorage.removeItem('auth_token');
    window.location.href = '/login';
}

//  初始化

document.addEventListener('DOMContentLoaded', async () => {
    const ok = await checkAuth();
    if (ok) loadPersonas();
});
