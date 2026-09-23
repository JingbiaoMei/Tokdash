// =========================================================================
// TOKDASH v4.0 — TABS: ACHIEVEMENTS (104 BADGES)
// =========================================================================

import { api } from '../api.js';

let allBadges = [];
let activeCategory = 'all';
let unlockedOnly = false;
let searchQuery = '';

export async function mount(containerEl, routeState) {
  containerEl.innerHTML = `
    <div id="achievements-content" class="tab-content active p-6 max-w-7xl mx-auto space-y-6 animate-fade-in">
      
      <!-- Hero Banner -->
      <section class="surface p-6 rounded-xl border border-white/5 relative overflow-hidden" style="background: linear-gradient(135deg, rgba(30, 27, 75, 0.85) 0%, rgba(15, 23, 42, 0.95) 100%);">
        <div class="flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
          <div class="flex items-center gap-4">
            <div class="w-12 h-12 rounded-2xl bg-amber-500/20 border border-amber-500/40 flex items-center justify-center text-amber-300 text-2xl">
              🏆
            </div>
            <div>
              <div class="flex items-center gap-2">
                <h2 class="text-xl sm:text-2xl font-extrabold mono text-slate-100">Hall of Achievements</h2>
                <span class="px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30 font-mono">
                  <span id="achievementsHeroCount">0</span> / 104 Unlocked
                </span>
              </div>
              <p class="text-xs text-slate-400 mt-1">Mastery badges across telemetry volume, token speed, cost efficiency, model discovery, and multi-agent coordination.</p>
            </div>
          </div>
          <div class="flex items-center gap-3 w-full md:w-auto">
            <div class="w-full md:w-48 bg-slate-900/80 rounded-full h-3 border border-white/10 overflow-hidden relative">
              <div id="achievementsHeroProgressBar" class="h-full bg-gradient-to-r from-amber-500 to-indigo-500 transition-all duration-700" style="width: 0%;"></div>
            </div>
            <span id="achievementsHeroPct" class="font-mono text-xs font-bold text-amber-400 min-w-[3rem] text-right">0.0%</span>
          </div>
        </div>
      </section>

      <!-- Category Filter Pills & Search -->
      <section class="surface p-4 border border-white/5 rounded-xl space-y-3">
        <div class="flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-3">
          <div class="flex items-center gap-1.5 overflow-x-auto pb-1 lg:pb-0" id="achievementsCategoryPills">
            <button type="button" class="query-preset-chip active !border-indigo-500 !text-indigo-300 !bg-indigo-500/20" data-cat="all">All (104)</button>
            <button type="button" class="query-preset-chip" data-cat="pioneer">🚀 Pioneer (18)</button>
            <button type="button" class="query-preset-chip" data-cat="token">⚡ Token Titan (18)</button>
            <button type="button" class="query-preset-chip" data-cat="cost">💎 Cost Slayer (18)</button>
            <button type="button" class="query-preset-chip" data-cat="tool">🛠️ Tool Sorcerer (18)</button>
            <button type="button" class="query-preset-chip" data-cat="model">🧠 Model Connoisseur (16)</button>
            <button type="button" class="query-preset-chip" data-cat="secret">👑 Secret & Legendary (16)</button>
          </div>
          <div class="flex items-center gap-2">
            <input type="text" id="achievementsSearchInput" class="ui-input text-xs py-1.5 px-3 rounded-lg font-mono w-full sm:w-56" placeholder="Search 104 badges..." />
            <button id="achievementsUnlockedOnlyBtn" type="button" class="btn btn-ghost text-xs px-2.5 py-1.5 rounded-lg border border-slate-700 text-slate-300 font-mono whitespace-nowrap">
              Unlocked Only
            </button>
          </div>
        </div>
      </section>

      <!-- Badges Grid -->
      <section>
        <div id="achievementsGrid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          <div class="text-center py-12 text-slate-500 font-mono text-xs col-span-full">Evaluating achievement telemetry...</div>
        </div>
      </section>

    </div>
  `;

  // Bind Events
  containerEl.querySelectorAll('#achievementsCategoryPills button').forEach(chip => {
    chip.addEventListener('click', () => {
      containerEl.querySelectorAll('#achievementsCategoryPills button').forEach(c => c.classList.remove('active', '!border-indigo-500', '!text-indigo-300', '!bg-indigo-500/20'));
      chip.classList.add('active', '!border-indigo-500', '!text-indigo-300', '!bg-indigo-500/20');
      activeCategory = chip.dataset.cat;
      renderBadgesGrid();
    });
  });

  const searchInput = containerEl.querySelector('#achievementsSearchInput');
  searchInput.addEventListener('input', (e) => {
    searchQuery = e.target.value.toLowerCase().trim();
    renderBadgesGrid();
  });

  const unlockedBtn = containerEl.querySelector('#achievementsUnlockedOnlyBtn');
  unlockedBtn.addEventListener('click', () => {
    unlockedOnly = !unlockedOnly;
    unlockedBtn.classList.toggle('bg-indigo-500/20', unlockedOnly);
    unlockedBtn.classList.toggle('text-indigo-300', unlockedOnly);
    unlockedBtn.classList.toggle('border-indigo-500', unlockedOnly);
    renderBadgesGrid();
  });

  await loadAchievements();
}

export function unmount() {}

async function loadAchievements() {
  try {
    const usage = await api.getUsage({ period: 'all' });
    const totalTokens = usage.total_tokens || 0;
    const totalCost = usage.total_cost || 0;
    const modelsCount = (usage.combined_models || []).length;

    // Generate 104 badge definitions
    allBadges = generate104Badges(totalTokens, totalCost, modelsCount);

    const unlockedCount = allBadges.filter(b => b.unlocked).length;
    const pct = ((unlockedCount / 104) * 100).toFixed(1);

    document.getElementById('achievementsHeroCount').textContent = unlockedCount;
    document.getElementById('achievementsHeroPct').textContent = `${pct}%`;
    const bar = document.getElementById('achievementsHeroProgressBar');
    if (bar) bar.style.width = `${pct}%`;

    renderBadgesGrid();
  } catch (err) {
    console.error('Error loading achievements:', err);
  }
}

function renderBadgesGrid() {
  const grid = document.getElementById('achievementsGrid');
  if (!grid) return;

  let filtered = allBadges;
  if (activeCategory !== 'all') {
    filtered = filtered.filter(b => b.category === activeCategory);
  }
  if (unlockedOnly) {
    filtered = filtered.filter(b => b.unlocked);
  }
  if (searchQuery) {
    filtered = filtered.filter(b => b.title.toLowerCase().includes(searchQuery) || b.description.toLowerCase().includes(searchQuery));
  }

  if (!filtered.length) {
    grid.innerHTML = '<div class="text-slate-500 font-mono text-xs text-center py-12 col-span-full">No achievements match your filters.</div>';
    return;
  }

  grid.innerHTML = filtered.map(b => {
    const statusColor = b.unlocked ? 'border-amber-500/40 bg-amber-950/15' : 'border-slate-800 bg-slate-950/40 opacity-70';
    const iconGlow = b.unlocked ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 shadow-[0_0_12px_rgba(245,158,11,0.25)]' : 'bg-slate-800 text-slate-500 border-slate-700';

    return `
      <div class="surface p-4 rounded-xl border ${statusColor} flex flex-col justify-between space-y-3">
        <div class="flex items-start gap-3">
          <div class="w-10 h-10 rounded-xl border flex items-center justify-center text-lg flex-shrink-0 ${iconGlow}">
            ${b.icon}
          </div>
          <div>
            <div class="text-xs font-bold text-slate-100 font-mono flex items-center gap-1.5">
              <span>${escapeHtml(b.title)}</span>
              ${b.unlocked ? '<span class="text-[9px] px-1.5 py-0.2 rounded bg-emerald-500/15 text-emerald-400 font-mono border border-emerald-500/25">UNLOCKED</span>' : ''}
            </div>
            <p class="text-[11px] text-slate-400 mt-0.5 leading-snug">${escapeHtml(b.description)}</p>
          </div>
        </div>
        <div class="pt-2 border-t border-slate-800/80 flex items-center justify-between text-[10px] font-mono text-slate-500">
          <span>Tier ${b.tier}</span>
          <span class="${b.unlocked ? 'text-amber-400 font-bold' : ''}">${b.progress}</span>
        </div>
      </div>
    `;
  }).join('');
}

function generate104Badges(totalTokens, totalCost, modelsCount) {
  const badges = [];
  const categories = [
    { key: 'pioneer', prefix: '🚀 Pioneer', count: 18 },
    { key: 'token', prefix: '⚡ Token Titan', count: 18 },
    { key: 'cost', prefix: '💎 Cost Slayer', count: 18 },
    { key: 'tool', prefix: '🛠️ Tool Sorcerer', count: 18 },
    { key: 'model', prefix: '🧠 Model Connoisseur', count: 16 },
    { key: 'secret', prefix: '👑 Secret & Legendary', count: 16 },
  ];

  const icons = ['🚀', '⚡', '💎', '🛠️', '🧠', '👑', '🔮', '🌟', '🎯', '🔥', '🛡️', '⚔️', '🌊', '🪐', '🧬', '🌌'];

  categories.forEach(cat => {
    for (let i = 1; i <= cat.count; i++) {
      const tier = Math.ceil(i / 3);
      const reqTokens = i * 250000;
      const isUnlocked = totalTokens >= reqTokens || (cat.key === 'pioneer' && i <= 5) || (cat.key === 'model' && i <= modelsCount);

      badges.push({
        id: `${cat.key}_${i}`,
        category: cat.key,
        title: `${cat.prefix} ${i}`,
        description: `Process ${reqTokens.toLocaleString()} telemetry tokens with autonomous agent workflows.`,
        icon: icons[i % icons.length],
        tier: tier,
        unlocked: isUnlocked,
        progress: isUnlocked ? 'Completed' : `${Math.min(100, ((totalTokens / reqTokens) * 100)).toFixed(0)}%`
      });
    }
  });

  return badges;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
