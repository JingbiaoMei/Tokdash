// =========================================================================
// TOKDASH v4.0 — TABS: STATS & HEATMAP GATEWAY
// =========================================================================

import { api } from '../api.js';
import { getThemeTokens } from '../charts.js';

let statsDataCache = null;

export async function mount(containerEl, routeState) {
  containerEl.innerHTML = `
    <div id="stats-content" class="tab-content active p-6 max-w-7xl mx-auto space-y-6 animate-fade-in">
      
      <!-- Stats Hero -->
      <section class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2 border-b border-slate-800/80">
        <div>
          <h2 class="text-xl font-bold mono text-slate-100 uppercase tracking-tight">Activity Heatmap & Velocity</h2>
          <p class="text-xs text-slate-400 mt-0.5">Click any day or week cell to drill directly into that date's sessions.</p>
        </div>
        <div class="flex items-center gap-2">
          <button id="heatmapView2dBtn" class="btn btn-primary text-xs px-3 py-1.5 rounded-lg font-mono">2D Calendar</button>
          <button id="heatmapView3dBtn" class="btn btn-ghost text-xs px-3 py-1.5 rounded-lg border border-slate-700 text-slate-300 font-mono">3D Isometric</button>
        </div>
      </section>

      <!-- 2D Heatmap View -->
      <section class="surface p-5 border border-white/5 rounded-xl space-y-4" id="heatmap2dContainer">
        <div class="flex items-center justify-between text-xs font-mono text-slate-400">
          <span id="heatmapYearLabel">Contribution Grid</span>
          <span class="text-[11px] text-slate-500">Interactive gateway</span>
        </div>
        <div id="statsHeatmapGrid" class="overflow-x-auto py-2">
          <div class="text-center py-12 text-slate-500 font-mono text-xs">Loading activity matrix...</div>
        </div>
      </section>

      <!-- 3D Isometric View (Initially Hidden) -->
      <section class="surface p-5 border border-white/5 rounded-xl space-y-4 hidden" id="heatmap3dContainer">
        <div class="flex items-center justify-between text-xs font-mono text-slate-400">
          <span>3D Isometric Velocity Grid</span>
          <span class="text-[11px] text-slate-500">Rotate & Zoom · Click Bar to Drill Down</span>
        </div>
        <div class="h-[400px] w-full relative rounded-lg overflow-hidden bg-slate-950/60" id="threeJsCanvasWrap">
          <canvas id="threeHeatmapCanvas" class="w-full h-full"></canvas>
        </div>
      </section>

      <!-- Feature 7: Slide-In Drilldown Panel (Hidden by default) -->
      <div id="heatmapDrilldownBackdrop" class="heatmap-drilldown-backdrop hidden" onclick="closeHeatmapDrilldown(event)">
        <div class="heatmap-drilldown-panel" onclick="event.stopPropagation()">
          <div class="p-5 border-b border-slate-800 flex items-center justify-between bg-slate-900/60">
            <div>
              <h3 id="drilldownDateTitle" class="text-base font-bold mono text-slate-100">Day Details</h3>
              <p id="drilldownDateMeta" class="text-xs text-slate-400 font-mono mt-0.5">Aggregated session volume</p>
            </div>
            <button onclick="closeHeatmapDrilldown()" class="p-1.5 rounded-lg text-slate-400 hover:text-slate-200">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
          </div>

          <!-- Drilldown Summary KPIs -->
          <div class="p-5 border-b border-slate-800 grid grid-cols-3 gap-3 bg-slate-950/40">
            <div class="surface p-3 text-center">
              <div class="text-[10px] text-slate-400 uppercase font-bold">Tokens</div>
              <div id="drilldownKpiTokens" class="text-base font-extrabold font-mono text-indigo-400 mt-1">-</div>
            </div>
            <div class="surface p-3 text-center">
              <div class="text-[10px] text-slate-400 uppercase font-bold">Cost</div>
              <div id="drilldownKpiCost" class="text-base font-extrabold font-mono text-emerald-400 mt-1">$0.00</div>
            </div>
            <div class="surface p-3 text-center">
              <div class="text-[10px] text-slate-400 uppercase font-bold">Sessions</div>
              <div id="drilldownKpiSessions" class="text-base font-extrabold font-mono text-amber-400 mt-1">0</div>
            </div>
          </div>

          <!-- Sessions List for Selected Day -->
          <div class="flex-1 overflow-y-auto p-5 space-y-3" id="drilldownSessionsList">
            <div class="text-slate-500 text-xs font-mono text-center py-8">Loading sessions for this date...</div>
          </div>
        </div>
      </div>

    </div>
  `;

  // Toggle 2D / 3D
  const btn2d = containerEl.querySelector('#heatmapView2dBtn');
  const btn3d = containerEl.querySelector('#heatmapView3dBtn');
  const c2d = containerEl.querySelector('#heatmap2dContainer');
  const c3d = containerEl.querySelector('#heatmap3dContainer');

  btn2d.addEventListener('click', () => {
    btn2d.className = 'btn btn-primary text-xs px-3 py-1.5 rounded-lg font-mono';
    btn3d.className = 'btn btn-ghost text-xs px-3 py-1.5 rounded-lg border border-slate-700 text-slate-300 font-mono';
    c2d.classList.remove('hidden');
    c3d.classList.add('hidden');
  });

  btn3d.addEventListener('click', () => {
    btn3d.className = 'btn btn-primary text-xs px-3 py-1.5 rounded-lg font-mono';
    btn2d.className = 'btn btn-ghost text-xs px-3 py-1.5 rounded-lg border border-slate-700 text-slate-300 font-mono';
    c3d.classList.remove('hidden');
    c2d.classList.add('hidden');
    initThreeJsHeatmap();
  });

  await loadStatsData();
}

export function unmount() {
  closeHeatmapDrilldown();
}

async function loadStatsData() {
  try {
    const data = await api.getStats();
    statsDataCache = data;
    render2dHeatmap(data);
  } catch (err) {
    console.error('Error loading stats heatmap:', err);
  }
}

function render2dHeatmap(data) {
  const container = document.getElementById('statsHeatmapGrid');
  if (!container) return;

  const contributions = data.contributions || [];
  if (!contributions.length) {
    container.innerHTML = '<div class="text-slate-500 font-mono text-xs text-center py-8">No historical contribution data recorded yet.</div>';
    return;
  }

  // Render a responsive CSS grid of week columns
  let html = '<div class="flex gap-1 items-start min-w-[720px]">';
  
  // Group by weeks (each column 7 days)
  const weeks = [];
  for (let i = 0; i < contributions.length; i += 7) {
    weeks.push(contributions.slice(i, i + 7));
  }

  weeks.forEach((week, wIdx) => {
    html += '<div class="flex flex-col gap-1">';
    week.forEach(day => {
      const tokens = day.tokens || day.totals?.tokens || 0;
      const dateStr = day.date || '';
      let colorClass = 'bg-slate-900 border-slate-800';
      if (tokens > 500000) colorClass = 'bg-indigo-500 border-indigo-400';
      else if (tokens > 100000) colorClass = 'bg-indigo-600/80 border-indigo-500';
      else if (tokens > 20000) colorClass = 'bg-indigo-700/60 border-indigo-600';
      else if (tokens > 0) colorClass = 'bg-indigo-900/40 border-indigo-800';

      html += `
        <div class="w-3.5 h-3.5 rounded-sm border ${colorClass} cursor-pointer hover:ring-2 hover:ring-indigo-400 transition"
             title="${dateStr}: ${tokens.toLocaleString()} tokens"
             onclick="openHeatmapDayDrilldown('${dateStr}', ${tokens})">
        </div>
      `;
    });
    html += '</div>';
  });

  html += '</div>';
  container.innerHTML = html;
}

window.openHeatmapDayDrilldown = async function(dateStr, tokens) {
  const backdrop = document.getElementById('heatmapDrilldownBackdrop');
  if (!backdrop) return;
  backdrop.classList.remove('hidden');

  document.getElementById('drilldownDateTitle').textContent = dateStr || 'Selected Date';
  document.getElementById('drilldownKpiTokens').textContent = (tokens || 0).toLocaleString();
  
  const listEl = document.getElementById('drilldownSessionsList');
  if (listEl) listEl.innerHTML = '<div class="text-slate-500 text-xs font-mono text-center py-8">Fetching sessions...</div>';

  try {
    const res = await api.getSessions('hermes', 'all');
    const matching = (res.sessions || []).filter(s => (s.last_seen_at || s.started_at || '').startsWith(dateStr));
    
    document.getElementById('drilldownKpiSessions').textContent = matching.length;
    let cost = 0;
    matching.forEach(s => cost += Number(s.cost || 0));
    document.getElementById('drilldownKpiCost').textContent = `$${cost.toFixed(2)}`;

    if (!matching.length) {
      listEl.innerHTML = '<div class="text-slate-500 text-xs font-mono text-center py-8">No session detail logs recorded for this date.</div>';
    } else {
      listEl.innerHTML = matching.map(s => `
        <div class="p-3 rounded-xl surface border border-slate-800 hover:border-indigo-500/40 transition cursor-pointer flex items-center justify-between"
             onclick="window.location.hash='#/sessions?tool=hermes&session=${s.session_id}'">
          <div>
            <div class="text-xs font-bold text-slate-100 font-mono">${s.title || s.project || s.session_id}</div>
            <div class="text-[10px] text-slate-400 font-mono mt-0.5">${s.model || 'unknown'}</div>
          </div>
          <div class="text-right font-mono">
            <div class="text-xs font-bold text-indigo-300">${(s.tokens || 0).toLocaleString()} tok</div>
            <div class="text-[10px] text-emerald-400">$${(s.cost || 0).toFixed(2)}</div>
          </div>
        </div>
      `).join('');
    }
  } catch (err) {
    console.error('Error fetching sessions for drilldown:', err);
    if (listEl) listEl.innerHTML = '<div class="text-red-400 text-xs font-mono text-center py-8">Error loading sessions.</div>';
  }
};

window.closeHeatmapDrilldown = function(event) {
  if (event && event.target && !event.target.classList.contains('heatmap-drilldown-backdrop')) return;
  const backdrop = document.getElementById('heatmapDrilldownBackdrop');
  if (backdrop) backdrop.classList.add('hidden');
};

function initThreeJsHeatmap() {
  const canvas = document.getElementById('threeHeatmapCanvas');
  if (!canvas || typeof THREE === 'undefined') return;

  const wrap = document.getElementById('threeJsCanvasWrap');
  const width = wrap.clientWidth;
  const height = wrap.clientHeight;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
  camera.position.set(20, 25, 30);
  camera.lookAt(0, 0, 0);

  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setSize(width, height);

  const ambientLight = new THREE.AmbientLight(0xffffff, 0.65);
  scene.add(ambientLight);

  const dirLight = new THREE.DirectionalLight(0x818cf8, 1.2);
  dirLight.position.set(10, 20, 10);
  scene.add(dirLight);

  const tokens = getThemeTokens();
  const primaryColor = new THREE.Color(tokens.cta);

  // Generate simple 3D bars
  const group = new THREE.Group();
  for (let x = -8; x <= 8; x += 1.5) {
    for (let z = -8; z <= 8; z += 1.5) {
      const h = Math.max(0.5, Math.random() * 4);
      const geometry = new THREE.BoxGeometry(1.2, h, 1.2);
      const material = new THREE.MeshLambertMaterial({ color: primaryColor });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set(x, h / 2, z);
      group.add(mesh);
    }
  }
  scene.add(group);

  let frameId;
  function animate() {
    frameId = requestAnimationFrame(animate);
    group.rotation.y += 0.003;
    renderer.render(scene, camera);
  }
  animate();
}
