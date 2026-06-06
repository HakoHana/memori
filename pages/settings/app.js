// Memory 插件 WebUI 配置页面逻辑

async function loadConfig {
    try {
        const resp = await fetch('/api/plugin/config');
        const data = await resp.json;
        const config = data.config || {};

        // 填入各字段
        setValue('llm_provider', config.llm_provider || '');
        setValue('trigger_msg_count', config.trigger_msg_count ?? 10);
        setValue('trigger_time_minutes', config.trigger_time_minutes ?? 360);
        setValue('immediate_capture', config.immediate_capture ?? true);
        setValue('warmup_enabled', config.warmup_enabled ?? true);
        setValue('idle_timeout_minutes', config.idle_timeout_minutes ?? 30);
        setValue('max_diary_tokens', config.max_diary_tokens ?? 500);
        setValue('persona_update_interval', config.persona_update_interval ?? 10);
        setValue('injection_position', config.injection_position || 'system_prompt_suffix');
        setValue('injection_template', config.injection_template || '');
        setValue('injection_use_tag', config.injection_use_tag ?? true);
        setValue('recall_count', config.recall_count ?? 5);
        setValue('recall_max_tokens', config.recall_max_tokens ?? 500);
        setValue('decay_enabled', config.decay_enabled ?? true);
        setValue('decay_rate', config.decay_rate ?? 0.99);
        setValue('search_imp_weight', config.search_imp_weight ?? 0.6);
        setValue('search_rank_weight', config.search_rank_weight ?? 0.4);

        // 高亮选中的 radio
        document.querySelectorAll('.radio-group label').forEach(el => {
            const radio = el.querySelector('input[type="radio"]');
            if (radio && radio.checked) {
                el.classList.add('selected');
            }
        });
    } catch (e) {
        showStatus('加载配置失败: ' + e.message, 'error');
    }
}

            } catch(e) {
async function saveConfig {
    const config = {
        llm_provider: getValue('llm_provider'),
        trigger_msg_count: parseInt(getValue('trigger_msg_count')) || 10,
        trigger_time_minutes: parseInt(getValue('trigger_time_minutes')) || 360,
        immediate_capture: getValue('immediate_capture') === true || getValue('immediate_capture') === 'true',
        warmup_enabled: getValue('warmup_enabled') === true || getValue('warmup_enabled') === 'true',
        idle_timeout_minutes: parseInt(getValue('idle_timeout_minutes')) || 30,
        max_diary_tokens: parseInt(getValue('max_diary_tokens')) || 500,
        persona_update_interval: parseInt(getValue('persona_update_interval')) || 10,
        injection_position: getValue('injection_position') || 'system_prompt_suffix',
        injection_template: getValue('injection_template') || '',
        injection_use_tag: getValue('injection_use_tag') === true || getValue('injection_use_tag') === 'true',
        recall_count: parseInt(getValue('recall_count')) || 5,
        recall_max_tokens: parseInt(getValue('recall_max_tokens')) || 500,
        decay_enabled: getValue('decay_enabled') === true || getValue('decay_enabled') === 'true',
        decay_rate: parseFloat(getValue('decay_rate')) || 0.99,
        search_imp_weight: parseFloat(getValue('search_imp_weight')) || 0.6,
        search_rank_weight: parseFloat(getValue('search_rank_weight')) || 0.4,
    };

    try {
        const resp = await fetch('/api/plugin/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });
        if (resp.ok) {
            showStatus(' 配置已保存！', 'success');
        } else {
            showStatus(' 保存失败: ' + (await resp.text), 'error');
        }
    } catch (e) {
        showStatus(' 保存失败: ' + e.message, 'error');
    }
}

// ── 工具函数 ──

function setValue(id, value) {
    const el = document.getElementById(id);
    if (!el) return;

    if (el.type === 'checkbox') {
        el.checked = Boolean(value);
    } else if (el.type === 'radio') {
        const radio = document.querySelector(`input[name="${id}"][value="${value}"]`);
        if (radio) radio.checked = true;
    } else {
        el.value = value;
    }
}

function getValue(id) {
    const el = document.getElementById(id);
    if (!el) return '';

    if (el.type === 'checkbox') {
        return el.checked;
    } else if (el.type === 'radio') {
        const checked = document.querySelector(`input[name="${id}"]:checked`);
        return checked ? checked.value : '';
    } else {
        return el.value;
    }
}

function showStatus(msg, type) {
    const el = document.getElementById('status');
    el.textContent = msg;
    el.className = 'status ' + type;
    el.style.display = 'block';
    setTimeout( => { el.style.display = 'none'; }, 3000);
}

// Radio 点击高亮
document.addEventListener('click', (e) => {
    const radio = e.target.closest('.radio-group label');
    if (radio) {
        document.querySelectorAll('.radio-group label').forEach(el => el.classList.remove('selected'));
        radio.classList.add('selected');
    }
});

// 页面加载时读取配置
document.addEventListener('DOMContentLoaded', loadConfig);
