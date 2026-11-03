/* ============================================================
   ROLETA — Telegram Mini App (hospedado no GitHub Pages)
   Os prêmios e o sorteio ficam no BACKEND. Aqui só desenhamos e animamos.
   ============================================================ */

// ─── CONFIGURAÇÃO ────────────────────────────────────────────
// ⚠️ TROQUE PELA URL DO SEU BACKEND (termina em /api, sem barra no final)
const API_BASE_URL = 'https://SEU-BACKEND.onrender.com/api';
// Teste local: const API_BASE_URL = 'http://localhost:8000/api';

// Link para o botão "Comprar tokens" (seu @ no Telegram). Deixe '' para esconder.
const SUPPORT_URL = 'https://t.me/SEU_USUARIO';

// Só usado se o servidor estiver fora do ar e você abrir fora do Telegram (modo demo)
const DEMO_PRIZES = [
    { id: 1, name: '10 Tokens', color: '#ff2d78', icon: 'fa-coins', probability: 20, type: 'tokens' },
    { id: 2, name: '5 Tokens', color: '#ff6b9d', icon: 'fa-coins', probability: 25, type: 'tokens' },
    { id: 3, name: 'Cupom 10%', color: '#e91e63', icon: 'fa-tag', probability: 20, type: 'texto' },
    { id: 4, name: 'Conteúdo VIP', color: '#ad1457', icon: 'fa-crown', probability: 10, type: 'link' },
    { id: 5, name: 'Tente de novo', color: '#7a0f3d', icon: 'fa-xmark', probability: 15, type: 'nada' },
    { id: 6, name: 'ESPECIAL', color: '#ffd700', icon: 'fa-gem', probability: 5, type: 'texto' }
];

// ─── ESTADO ──────────────────────────────────────────────────
const state = {
    prizes: [],
    tokens: 0,
    isVip: false,
    vipUntil: null,
    dailyAvailable: false,
    dailyResetsAt: null,
    isSpinning: false,
    currentRotation: 0,
    demo: false,
    offline: false,
    refreshing: false
};

const $ = (s) => document.querySelector(s);

// ─── TELEGRAM ────────────────────────────────────────────────
const tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
const inTelegram = !!(tg && tg.initData);

if (tg) {
    try {
        tg.ready();
        tg.expand();
        if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); // evita fechar sem querer ao arrastar
        tg.setHeaderColor('#0a0a0f');
        tg.setBackgroundColor('#0a0a0f');
    } catch (e) { /* versões antigas */ }
}

const tgUser = tg && tg.initDataUnsafe && tg.initDataUnsafe.user;
$('#userName').textContent = tgUser ? tgUser.first_name : 'Visitante';

function haptic(kind) {
    try {
        if (!tg || !tg.HapticFeedback) return;
        if (kind === 'success' || kind === 'error' || kind === 'warning') tg.HapticFeedback.notificationOccurred(kind);
        else tg.HapticFeedback.impactOccurred(kind || 'light');
    } catch (e) { /* ignore */ }
}

// ─── UTILS ───────────────────────────────────────────────────
function showLoading(show) { $('#loadingOverlay').classList.toggle('show', show); }

function showError(title, message) {
    $('#errorTitle').textContent = title;
    $('#errorMessage').textContent = message;
    $('#errorModal').classList.add('show');
    haptic('error');
}

function closeError() { $('#errorModal').classList.remove('show'); }

function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
}

function pad(n) { return String(n).padStart(2, '0'); }

function formatDuration(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

function totalSpins() { return state.tokens + (state.dailyAvailable ? 1 : 0); }

// ─── API ─────────────────────────────────────────────────────
async function apiFetch(endpoint, options = {}) {
    const headers = { 'Content-Type': 'application/json', ...options.headers };
    if (tg && tg.initData) headers['X-Telegram-Init-Data'] = tg.initData;

    const response = await fetch(`${API_BASE_URL}${endpoint}`, { ...options, headers });
    let data = {};
    try { data = await response.json(); } catch (e) { /* resposta sem JSON */ }
    if (!response.ok) throw new Error(data.error || 'Erro na API');
    return data;
}

function applyUser(data) {
    state.tokens = data.tokens || 0;
    state.isVip = !!data.is_vip;
    state.vipUntil = data.vip_until || null;
    state.dailyAvailable = !!data.daily_available;
    state.dailyResetsAt = data.daily_resets_at || null;
    renderStatus();
    updateSpinButton();
}

async function loadUserData() {
    try {
        applyUser(await apiFetch('/user/me'));
        state.offline = false;
    } catch (error) {
        console.warn('Falha ao carregar usuário:', error);
        if (inTelegram) {
            state.offline = true;
            $('#spinInfo').textContent = error.message || 'Servidor indisponível.';
        } else if (!state.demo) {
            // Fora do Telegram e sem servidor: modo demonstração local
            state.demo = true;
            applyUser({ tokens: 5, is_vip: false });
        }
    }
    updateSpinButton();
}

async function loadPrizes() {
    try {
        const data = await apiFetch('/prizes');
        state.prizes = data.prizes;
    } catch (error) {
        console.warn('Falha ao carregar prêmios:', error);
        state.prizes = DEMO_PRIZES;
        if (!inTelegram) state.demo = true;
    }
}

async function loadHistory() {
    if (state.demo) return;
    try {
        const data = await apiFetch('/spins/history');
        renderHistory(data.spins || []);
    } catch (error) { /* mantém o que já está */ }
}

// ─── RENDER: status, botão, histórico ────────────────────────
function renderStatus() {
    $('#tokenCount').textContent = state.tokens;

    const badge = $('#vipBadge');
    badge.hidden = !state.isVip;
    if (state.isVip && state.vipUntil) {
        $('#vipText').textContent = 'VIP até ' + new Date(state.vipUntil).toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
    }

    $('#dailyCard').hidden = !state.isVip;
    tickDaily();
}

function tickDaily() {
    if (!state.isVip || !state.dailyResetsAt) return;
    const diff = new Date(state.dailyResetsAt) - new Date();

    if (diff <= 0) {
        if (!state.refreshing && !state.isSpinning) {
            state.refreshing = true;
            loadUserData().finally(() => { state.refreshing = false; });
        }
        return;
    }
    const value = $('#dailyValue');
    if (state.dailyAvailable) {
        value.textContent = 'Disponível';
        value.className = 'status__value ok';
        $('#dailySub').textContent = 'Expira em ' + formatDuration(diff);
    } else {
        value.textContent = 'Usado hoje';
        value.className = 'status__value used';
        $('#dailySub').textContent = 'Renova em ' + formatDuration(diff);
    }
}
setInterval(tickDaily, 1000);

function updateSpinButton() {
    const btn = $('#spinBtn');
    const info = $('#spinInfo');
    const text = btn.querySelector('.btn-text');
    const buy = $('#buyBtn');
    buy.hidden = true;

    if (state.isSpinning) {
        btn.disabled = true;
        text.innerHTML = '<i class="fas fa-spinner fa-spin"></i> GIRANDO...';
        return;
    }
    if (state.offline) {
        btn.disabled = true;
        text.innerHTML = '<i class="fas fa-plug"></i> INDISPONÍVEL';
        $('#btnCost').textContent = 'tente novamente em instantes';
        return;
    }
    if (totalSpins() <= 0) {
        btn.disabled = true;
        text.innerHTML = '<i class="fas fa-lock"></i> SEM RODADAS';
        $('#btnCost').textContent = state.isVip ? 'volte amanhã' : 'compre tokens';
        info.textContent = state.isVip
            ? 'Seu token diário renova à meia-noite.'
            : 'Você não tem tokens. Compre para continuar girando!';
        if (SUPPORT_URL) { buy.href = SUPPORT_URL; buy.hidden = false; }
        return;
    }
    btn.disabled = false;
    text.innerHTML = '<i class="fas fa-play"></i> GIRAR A ROLETA';
    $('#btnCost').textContent = state.dailyAvailable ? 'TOKEN DIÁRIO · GRÁTIS' : '1 TOKEN';
    info.innerHTML = `Disponível: <strong>${totalSpins()}</strong> rodada(s)`;
}

const TYPE_ICONS = { tokens: 'fa-coins', texto: 'fa-comment', link: 'fa-link', arquivo: 'fa-file', nada: 'fa-xmark' };

function renderHistory(spins) {
    const box = $('#historyList');
    if (!spins.length) {
        box.innerHTML = '<p class="empty-history">Nenhuma rodada ainda. Seja o primeiro a girar!</p>';
        return;
    }
    box.innerHTML = spins.map((s) => `
        <div class="history-item">
            <div class="history-prize">
                <i class="fas ${TYPE_ICONS[s.prize_type] || 'fa-gift'}"></i>
                <span>${escapeHtml(s.prize_name)}</span>
            </div>
            <span class="history-date">${new Date(s.created_at).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</span>
        </div>
    `).join('');
}

const demoHistory = [];

// ─── ROLETA (CANVAS) ─────────────────────────────────────────
const canvas = $('#rouletteCanvas');
const ctx = canvas.getContext('2d');
const centerX = canvas.width / 2;
const centerY = canvas.height / 2;
const radius = canvas.width / 2 - 14;
const TWO_PI = Math.PI * 2;

const ICON_CHARS = {
    'fa-coins': '\uf51e', 'fa-tag': '\uf02b', 'fa-crown': '\uf521', 'fa-gem': '\uf3a5',
    'fa-file': '\uf15b', 'fa-link': '\uf0c1', 'fa-comment': '\uf075', 'fa-box-open': '\uf49e',
    'fa-gift': '\uf06b', 'fa-ticket': '\uf145', 'fa-xmark': '\uf00d', 'fa-star': '\uf005',
    'fa-heart': '\uf004', 'fa-fire': '\uf06d', 'fa-bolt': '\uf0e7', 'fa-video': '\uf03d',
    'fa-image': '\uf03e', 'fa-camera': '\uf030'
};

function isLight(hex) {
    let h = hex.replace('#', '');
    if (h.length === 3) h = h.split('').map((c) => c + c).join('');
    const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    return (r * 299 + g * 587 + b * 114) / 1000 > 150;
}

function drawRoulette(prizes, rotation = 0) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const total = prizes.reduce((s, p) => s + p.probability, 0);
    let angle = rotation;

    prizes.forEach((prize) => {
        const slice = (prize.probability / total) * TWO_PI;
        const fg = isLight(prize.color) ? '#0a0a0f' : '#ffffff';

        ctx.beginPath();
        ctx.moveTo(centerX, centerY);
        ctx.arc(centerX, centerY, radius, angle, angle + slice);
        ctx.closePath();
        ctx.fillStyle = prize.color;
        ctx.fill();
        ctx.strokeStyle = '#0a0a0f';
        ctx.lineWidth = 4;
        ctx.stroke();

        ctx.save();
        ctx.translate(centerX, centerY);
        ctx.rotate(angle + slice / 2);
        ctx.textBaseline = 'middle';
        ctx.fillStyle = fg;

        // Ícone na borda externa
        const iconChar = ICON_CHARS[prize.icon];
        let textEnd = radius - 24;
        if (iconChar) {
            ctx.textAlign = 'center';
            ctx.font = '900 28px "Font Awesome 6 Free"';
            ctx.fillText(iconChar, radius - 34, 0);
            textEnd = radius - 64;
        }

        // Nome: reduz a fonte até caber entre o centro e o ícone
        const maxWidth = textEnd - radius * 0.34;
        let size = 24;
        ctx.textAlign = 'right';
        ctx.font = `700 ${size}px Poppins, sans-serif`;
        while (ctx.measureText(prize.name).width > maxWidth && size > 11) {
            size -= 1;
            ctx.font = `700 ${size}px Poppins, sans-serif`;
        }
        ctx.fillText(prize.name, textEnd, 0, maxWidth);
        ctx.restore();

        angle += slice;
    });

    // Borda dourada + sombra interna
    ctx.beginPath();
    ctx.arc(centerX, centerY, radius + 5, 0, TWO_PI);
    ctx.strokeStyle = '#ffd700';
    ctx.lineWidth = 6;
    ctx.stroke();

    const g = ctx.createRadialGradient(centerX, centerY, radius * 0.7, centerX, centerY, radius);
    g.addColorStop(0, 'rgba(0,0,0,0)');
    g.addColorStop(1, 'rgba(0,0,0,0.35)');
    ctx.beginPath();
    ctx.arc(centerX, centerY, radius, 0, TWO_PI);
    ctx.fillStyle = g;
    ctx.fill();
}

function animateSpin(targetPrize) {
    return new Promise((resolve) => {
        const total = state.prizes.reduce((s, p) => s + p.probability, 0);
        let centerOfSlice = 0;
        let acc = 0;
        for (const p of state.prizes) {
            const slice = (p.probability / total) * TWO_PI;
            if (p.id === targetPrize.id) { centerOfSlice = acc + slice / 2; break; }
            acc += slice;
        }

        // Rotação final para o meio da fatia parar sob o ponteiro (topo = -90°)
        let target = (-Math.PI / 2 - centerOfSlice) % TWO_PI;
        if (target < 0) target += TWO_PI;
        let delta = (target - state.currentRotation) % TWO_PI;
        if (delta < 0) delta += TWO_PI;

        const start = state.currentRotation;
        const end = start + 6 * TWO_PI + delta;
        const duration = 4500;
        const t0 = performance.now();
        const ease = (t) => 1 - Math.pow(1 - t, 4);
        let lastTick = -1;

        function frame(now) {
            const p = Math.min((now - t0) / duration, 1);
            const rot = start + (end - start) * ease(p);
            drawRoulette(state.prizes, rot);

            // vibração leve a cada fatia que passa pelo ponteiro
            const tick = Math.floor(rot / (TWO_PI / Math.max(state.prizes.length, 1)));
            if (tick !== lastTick && p < 0.92) { haptic('light'); lastTick = tick; }

            if (p < 1) requestAnimationFrame(frame);
            else { state.currentRotation = end % TWO_PI; resolve(); }
        }
        requestAnimationFrame(frame);
    });
}

// ─── GIRO ────────────────────────────────────────────────────
function pickDemoPrize() {
    const total = state.prizes.reduce((s, p) => s + p.probability, 0);
    let r = Math.random() * total;
    for (const p of state.prizes) { r -= p.probability; if (r <= 0) return p; }
    return state.prizes[state.prizes.length - 1];
}

async function handleSpin() {
    if (state.isSpinning || state.offline) return;
    if (totalSpins() <= 0) {
        showError('Sem rodadas', 'Você não tem rodadas disponíveis agora.');
        return;
    }

    state.isSpinning = true;
    updateSpinButton();
    $('#resultSection').classList.remove('show');
    haptic('medium');

    try {
        let prize;
        if (state.demo) {
            prize = pickDemoPrize();
            await animateSpin(prize);
            state.tokens = Math.max(0, state.tokens - 1);
            demoHistory.unshift({ prize_name: prize.name, prize_type: prize.type, created_at: new Date().toISOString() });
            renderHistory(demoHistory.slice(0, 10));
        } else {
            showLoading(true);
            const res = await apiFetch('/spins', { method: 'POST' });
            showLoading(false);
            prize = state.prizes.find((p) => p.id === res.prize_id) || state.prizes[0];
            await animateSpin(prize);
            state.tokens = res.tokens_left;
            state.dailyAvailable = !!res.daily_available;
            loadHistory();
        }
        renderStatus();
        showResult(prize);
    } catch (error) {
        showError('Erro', error.message || 'Não foi possível girar. Tente novamente.');
        loadUserData();
    } finally {
        state.isSpinning = false;
        showLoading(false);
        updateSpinButton();
    }
}

function showResult(prize) {
    const lost = prize.type === 'nada';
    $('#resultIcon').className = `fas ${prize.icon || 'fa-gift'}`;
    $('#resultTitle').textContent = lost ? 'Não foi dessa vez!' : 'Você ganhou!';
    $('#resultPrize').textContent = prize.name;
    $('#resultNote').innerHTML = state.demo
        ? '<i class="fas fa-flask"></i> Modo demonstração: nada foi enviado.'
        : '<i class="fas fa-paper-plane"></i> ' + (lost ? 'Enviamos o resultado no seu chat com o bot.' : 'O prêmio foi enviado para o seu chat com o bot.');
    $('#btnClose').hidden = !inTelegram;
    $('#resultSection').classList.add('show');
    $('#resultSection').scrollIntoView({ behavior: 'smooth', block: 'center' });
    haptic(lost ? 'warning' : 'success');
}

// ─── EVENTOS ─────────────────────────────────────────────────
$('#spinBtn').addEventListener('click', handleSpin);
$('#centerBtn').addEventListener('click', handleSpin);
$('#btnClose').addEventListener('click', () => { if (tg) tg.close(); });

// Atualiza saldo ao voltar para o app (ex.: depois de comprar tokens)
document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !state.isSpinning && !state.demo) loadUserData();
});

// ─── INICIALIZAÇÃO ───────────────────────────────────────────
async function init() {
    await loadPrizes();
    await loadUserData();
    try { await document.fonts.load('900 28px "Font Awesome 6 Free"'); } catch (e) { /* ok */ }
    try { await document.fonts.load('700 20px Poppins'); } catch (e) { /* ok */ }
    drawRoulette(state.prizes, state.currentRotation);
    await loadHistory();
}

init();
