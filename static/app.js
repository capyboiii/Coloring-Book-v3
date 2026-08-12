// State management
let state = {
  config: {},
  lulu_specs: {},
  current_book: ""
};

const TRIM_PRESETS = {
  "us_letter": { width: 8.5, height: 11.0 },
  "square_small": { width: 8.5, height: 8.5 },
  "trade": { width: 6.0, height: 9.0 },
  "square_medium": { width: 8.25, height: 8.25 }
};

const PAPER_THICKNESS = {
  "60_white": 0.002252,
  "60_cream": 0.0025,
  "70_white": 0.0023,
  "80_white": 0.003
};

document.addEventListener("DOMContentLoaded", () => {
  initApp();
  
  // Keyboard Navigation for Raw Inspector
  document.addEventListener("keydown", (e) => {
    const activeTab = document.querySelector('.tab-content.active');
    if (!activeTab || activeTab.id !== 'tab-inspector') return;
    
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      prevInspectorImage();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      nextInspectorImage();
    } else if (e.key === "a" || e.key === "A") {
      e.preventDefault();
      updateInspectorStatus("approved");
      nextInspectorImage();
    }
  });
});

async function initApp() {
  await loadConfig();
  await loadBooksList();
  await loadGallery();
}

// ---------------- TAB NAVIGATION ----------------
function switchTab(tabId) {
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  
  const selectedBtn = Array.from(document.querySelectorAll('.tab-btn')).find(b => b.getAttribute('onclick').includes(tabId));
  if (selectedBtn) selectedBtn.classList.add('active');
  
  const content = document.getElementById(tabId);
  if (content) content.classList.add('active');

  if (tabId === 'tab-gallery') {
    loadGallery();
  } else if (tabId === 'tab-inspector') {
    loadInspector();
  }
}

// ---------------- CONFIG MANAGEMENT & SYNC ----------------
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    state.config = data.config;
    state.lulu_specs = data.lulu_specs;
    state.current_book = data.current_book;

    syncConfigToUI();
  } catch (err) {
    console.error("Error loading config:", err);
  }
}

function syncConfigToUI() {
  const cfg = state.config || {};
  const print = cfg.print || {};
  const book = cfg.book || {};
  const cover_text = cfg.cover_text || {};

  document.getElementById('book-title').value = book.title || "";
  document.getElementById('book-subtitle').value = book.subtitle || "";
  document.getElementById('book-author').value = book.author || "";
  document.getElementById('book-num-images').value = book.num_images || 30;
  document.getElementById('blank-verso').value = book.blank_verso !== false ? "true" : "false";
  document.getElementById('cover-back-blurb').value = cover_text.back_blurb || "";

  // Sync Trim Preset Dropdown
  const tw = print.trim_width || 8.5;
  const th = print.trim_height || 11.0;
  const matchPreset = Object.keys(TRIM_PRESETS).find(k => TRIM_PRESETS[k].width === tw && TRIM_PRESETS[k].height === th);
  
  const trimSelect = document.getElementById('trim-size-select');
  if (matchPreset) {
    trimSelect.value = matchPreset;
    document.getElementById('custom-trim-row').style.display = 'none';
  } else {
    trimSelect.value = 'custom';
    document.getElementById('custom-trim-row').style.display = 'flex';
  }
  document.getElementById('trim-width').value = tw;
  document.getElementById('trim-height').value = th;

  // Sync Paper Dropdown
  const pt = print.paper_thickness || 0.002252;
  const matchPaper = Object.keys(PAPER_THICKNESS).find(k => Math.abs(PAPER_THICKNESS[k] - pt) < 0.0001);
  if (matchPaper) {
    document.getElementById('paper-type-select').value = matchPaper;
  }

  // Sync Binding Dropdown
  document.getElementById('binding-type-select').value = print.binding || "perfect";

  // Sync Backend & Filters
  document.getElementById('backend-type').value = cfg.backend || "api";
  document.getElementById('process-pure-bw').value = cfg.process?.pure_bw ? "true" : "false";

  // Sync Subjects List (Textarea)
  const subjects = cfg.subjects;
  document.getElementById('subjects-list-textarea').value = (Array.isArray(subjects) && subjects.length > 0) ? subjects.join("\n") : "";

  updateSpecsSummary();
}

function onTrimSelectChange() {
  const val = document.getElementById('trim-size-select').value;
  const customRow = document.getElementById('custom-trim-row');

  if (val === 'custom') {
    customRow.style.display = 'flex';
  } else {
    customRow.style.display = 'none';
    const preset = TRIM_PRESETS[val];
    if (preset) {
      document.getElementById('trim-width').value = preset.width;
      document.getElementById('trim-height').value = preset.height;
    }
  }
  saveConfigFromUI();
}

function onPaperSelectChange() {
  saveConfigFromUI();
}

function updateSpecsSummary() {
  const specs = state.lulu_specs || {};
  const book = state.config.book || {};
  const print = state.config.print || {};

  document.getElementById('summary-book-title').textContent = book.title || "Untitled";
  document.getElementById('summary-trim-size').textContent = `${print.trim_width || 8.5} x ${print.trim_height || 11.0} in`;
  document.getElementById('summary-total-pages').textContent = `${specs.calculated_pages || 32} trang`;
  document.getElementById('summary-spine-width').textContent = `${specs.spine_width_in || 0} in (${specs.spine_width_mm || 0} mm)`;

  const statusEl = document.getElementById('summary-lulu-status');
  if (specs.lulu_compatible) {
    statusEl.textContent = "ĐẠT ✓";
    statusEl.style.color = "var(--accent-emerald)";
  } else {
    statusEl.textContent = "CHƯA ĐẠT ✗";
    statusEl.style.color = "var(--accent-rose)";
  }

  document.getElementById('calc-interior-size').textContent = `${specs.interior_size_in || ''} (${specs.interior_px_300dpi || ''})`;
  document.getElementById('calc-cover-size').textContent = `${specs.cover_size_in || ''} (${specs.cover_px_300dpi || ''})`;
  document.getElementById('calc-spine-detail').textContent = `${specs.spine_width_in || 0} in (${specs.spine_width_mm || 0} mm)`;
}

async function saveConfigFromUI() {
  const paperKey = document.getElementById('paper-type-select').value;
  const paper_thick = PAPER_THICKNESS[paperKey] || 0.002252;

  const payload = {
    print: {
      trim_width: parseFloat(document.getElementById('trim-width').value),
      trim_height: parseFloat(document.getElementById('trim-height').value),
      paper_thickness: paper_thick,
      binding: document.getElementById('binding-type-select').value
    },
    book: {
      title: document.getElementById('book-title').value,
      subtitle: document.getElementById('book-subtitle').value,
      author: document.getElementById('book-author').value,
      num_images: parseInt(document.getElementById('book-num-images').value),
      blank_verso: document.getElementById('blank-verso').value === "true"
    },
    cover_text: {
      back_blurb: document.getElementById('cover-back-blurb').value
    },
    backend: document.getElementById('backend-type').value,
    process: {
      pure_bw: document.getElementById('process-pure-bw').value === "true"
    }
  };

  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    state.lulu_specs = data.lulu_specs;
    state.config = {...state.config, ...payload};
    updateSpecsSummary();
  } catch (err) {
    console.error("Error saving config:", err);
  }
}

async function saveSubjectsFromTextarea() {
  const text = document.getElementById('subjects-list-textarea').value;
  const subjects = text.split("\n").map(s => s.trim()).filter(s => s.length > 0);
  
  await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({subjects: subjects})
  });
}

// ---------------- BOOK SELECTOR ----------------
async function loadBooksList() {
  try {
    const res = await fetch('/api/books');
    const data = await res.json();
    const select = document.getElementById('active-book-select');
    select.innerHTML = data.books.map(b => `
      <option value="${b.slug}" ${b.slug === state.current_book ? 'selected' : ''}>
        ${b.slug} (${b.raw_count} ảnh, ${b.has_interior_pdf ? 'PDF ✓' : 'chưa PDF'})
      </option>
    `).join('');
  } catch (err) {
    console.error("Error loading books:", err);
  }
}

async function switchBook(slug) {
  if (!slug) return;
  try {
    logToTerminal(`[BOOK] Đang tự động tải thông tin cuốn sách: "${slug}"...`);
    const res = await fetch('/api/books/select', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({slug: slug})
    });
    const data = await res.json();

    state.config = data.config;
    state.lulu_specs = data.lulu_specs;
    state.current_book = data.active_book;

    syncConfigToUI();
    await loadBooksList();
    await loadGallery();
    if (typeof loadInspector === 'function') {
      await loadInspector();
    }

    logToTerminal(`[BOOK] ✅ Đã tự động cập nhật toàn bộ thông tin cho cuốn "${data.config?.book?.title || slug}"!`);
  } catch (err) {
    console.error("Error switching book:", err);
    logToTerminal(`[ERROR] Lỗi khi chuyển cuốn sách: ${err}`);
  }
}

async function openNewBookModal() {
  const title = prompt("Nhập tên cuốn sách mới (ví dụ: Cute Dino Coloring):");
  if (!title) return;
  try {
    const res = await fetch('/api/books/create', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: title})
    });
    const data = await res.json();
    logToTerminal(`[BOOK] ✅ Đã tạo sách mới thành công: "${data.title}"`);
    await switchBook(data.slug);
  } catch (err) {
    console.error("Error creating book:", err);
    logToTerminal(`[ERROR] Lỗi khi tạo sách mới: ${err}`);
  }
}

// ---------------- AI SUBJECT GENERATOR ----------------
async function generateAISubjects() {
  const title = document.getElementById('book-title').value;
  const count = document.getElementById('book-num-images').value;
  logToTerminal(`[AI STUDIO] Đang nhờ Gemini tạo ${count} cảnh cho cuốn "${title}"...`);

  try {
    const res = await fetch('/api/subjects/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: title, count: count})
    });
    const data = await res.json();
    if (data.subjects) {
      document.getElementById('subjects-list-textarea').value = data.subjects.join("\n");
      logToTerminal(`[AI STUDIO] ✅ Đã tạo xong ${data.subjects.length} cảnh minh họa!`);
    }
  } catch (err) {
    logToTerminal(`[ERROR] Lỗi sinh chủ đề AI: ${err}`);
  }
}

// ---------------- TASK RUNNER & SSE LOGS ----------------
async function runTaskCommand(command) {
  switchTab('tab-pipeline');
  logToTerminal(`[COMMAND] Khởi chạy bước '${command}'...`);
  
  try {
    const res = await fetch('/api/tasks/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command: command})
    });
    const data = await res.json();
    
    // Connect to SSE stream
    const eventSource = new EventSource(`/api/tasks/stream/${data.task_id}`);
    
    eventSource.onmessage = (event) => {
      const msgData = JSON.parse(event.data);
      if (msgData.type === 'log') {
        logToTerminal(msgData.message);
      } else if (msgData.type === 'done') {
        eventSource.close();
        logToTerminal(`[DONE] Hoàn thành bước '${command}'.`);
        loadGallery();
        loadBooksList();
      }
    };

    eventSource.onerror = (err) => {
      eventSource.close();
    };

  } catch (err) {
    logToTerminal(`[ERROR] Lỗi khởi chạy: ${err}`);
  }
}

function logToTerminal(msg) {
  const terminal = document.getElementById('log-terminal');
  const line = document.createElement('div');
  line.className = 'log-line';
  if (msg.includes('ERROR') || msg.includes('THẤT BẠI')) line.classList.add('error');
  if (msg.includes('START') || msg.includes('COMMAND')) line.classList.add('start');
  if (msg.includes('SUCCESS') || msg.includes('OK') || msg.includes('✓')) line.classList.add('success');
  line.textContent = msg;
  terminal.appendChild(line);
  terminal.scrollTop = terminal.scrollHeight;
}

function clearConsole() {
  document.getElementById('log-terminal').innerHTML = '<div class="log-line start">[SYSTEM] Terminal log cleared.</div>';
}

// ---------------- GALLERY & PDF VIEWER ----------------
async function loadGallery() {
  try {
    const res = await fetch('/api/gallery');
    const data = await res.json();

    // Render PDFs
    const pdfContainer = document.getElementById('pdf-list-container');
    if (data.pdfs.length === 0) {
      pdfContainer.innerHTML = `<p style="color: var(--text-muted); font-size: 0.875rem;">Chưa có file PDF nào. Vui lòng chạy bước "Dựng PDF Lulu" ở tab 2.</p>`;
    } else {
      pdfContainer.innerHTML = data.pdfs.map(pdf => `
        <div style="background: rgba(0,0,0,0.3); border: 1px solid var(--border-color); padding: 14px 18px; border-radius: 8px; display: flex; align-items: center; justify-content: space-between; flex: 1; min-width: 280px;">
          <div>
            <div style="font-weight: 700; font-size: 0.95rem; color: #fff;">📄 ${pdf.name}</div>
            <div style="font-size: 0.75rem; color: var(--text-muted);">${pdf.size_mb} MB</div>
          </div>
          <a href="${pdf.url}" target="_blank" class="btn btn-secondary" style="padding: 6px 14px; font-size: 0.825rem;">Tải Về / Xem PDF</a>
        </div>
      `).join('');
    }

    // Render Image Grid
    const grid = document.getElementById('gallery-grid-container');
    const images = data.proc_images.length > 0 ? data.proc_images : data.raw_images;

    if (images.length === 0) {
      grid.innerHTML = `<p style="color: var(--text-muted); font-size: 0.875rem; grid-column: 1/-1;">Chưa có ảnh nào. Vui lòng chạy bước "Gen Ảnh AI" ở tab 2.</p>`;
    } else {
      grid.innerHTML = images.map(img => `
        <div class="gallery-card">
          <div class="gallery-img-wrapper">
            <img src="${img.url}" alt="${img.name}" loading="lazy">
          </div>
          <div class="gallery-info">
            <span>${img.name}</span>
            <span>${img.size_kb} KB</span>
          </div>
        </div>
      `).join('');
    }

  } catch (err) {
    console.error("Error loading gallery:", err);
  }
}

// ---------------- GEMINI ACCOUNT MODAL ----------------
function openGeminiModal() {
  document.getElementById('gemini-modal').style.display = 'block';
  updateGeminiStatusUI();
}

function closeGeminiModal() {
  document.getElementById('gemini-modal').style.display = 'none';
}

async function updateGeminiStatusUI() {
  const statusEl = document.getElementById('gemini-status-text');
  statusEl.innerText = "Đang kiểm tra...";
  statusEl.style.color = "var(--text-primary)";
  
  const profilesListEl = document.getElementById('gemini-profiles-list');
  if (profilesListEl) {
    profilesListEl.innerHTML = "<div style='color: var(--text-muted);'>Đang tải...</div>";
  }
  
  try {
    const res = await fetch('/api/gemini/profiles');
    const data = await res.json();
    
    let statusHtml = "";
    
    // API Status
    if (data.api && data.api.has_key) {
      statusHtml += `<div style="color: var(--accent-emerald);">✅ API Key: Đã được cấu hình (Sẵn sàng)</div>`;
    } else {
      statusHtml += `<div style="color: var(--accent-rose);">❌ API Key: Chưa cấu hình</div>`;
    }
    statusEl.innerHTML = statusHtml;
    
    // Render profiles
    if (profilesListEl) {
      if (data.profiles && data.profiles.length > 0) {
        profilesListEl.innerHTML = data.profiles.map((p, idx) => `
          <div style="background: rgba(0,0,0,0.2); padding: 10px; border-radius: 6px; display: flex; justify-content: space-between; align-items: center;">
            <div>
              <div style="font-weight: 600;">${p.name}</div>
              <div style="font-size: 0.75rem; color: var(--text-muted);">${p.exists ? 'Đã khởi tạo' : 'Mới (Chưa đăng nhập)'}</div>
            </div>
            <div style="display: flex; gap: 8px;">
              <button class="btn btn-secondary" style="font-size: 0.8rem; padding: 6px 10px; background-color: #4285F4; color: white; border: none;" onclick="launchGeminiChrome('${p.path.replace(/\\/g, '\\\\')}')">🌐 Đăng Nhập</button>
              <button class="btn btn-danger" style="font-size: 0.8rem; padding: 6px 10px;" onclick="deleteGeminiProfile('${p.path.replace(/\\/g, '\\\\')}')">🗑️ Xóa</button>
            </div>
          </div>
        `).join('');
      } else {
        profilesListEl.innerHTML = `<div style="color: var(--text-muted);">Chưa có tài khoản nào. Hãy thêm một tài khoản mới.</div>`;
      }
    }
  } catch (err) {
    statusEl.innerHTML = `<span style="color: var(--accent-rose);">Lỗi khi lấy trạng thái: ${err.message}</span>`;
    if (profilesListEl) profilesListEl.innerHTML = "";
  }
}

async function deleteGeminiProfile(profilePath) {
  if (!confirm("Bạn có chắc chắn muốn xóa tài khoản / Profile Chrome này khỏi hệ thống?")) return;
  
  try {
    const res = await fetch('/api/gemini/delete-profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: profilePath, delete_files: true })
    });
    const data = await res.json();
    if (res.ok) {
      updateGeminiStatusUI();
    } else {
      alert("Lỗi khi xóa profile: " + data.detail);
    }
  } catch (err) {
    alert("Lỗi kết nối: " + err.message);
  }
}

async function addGeminiProfile() {
  const nameInput = document.getElementById('new-profile-name');
  const name = nameInput.value.trim();
  if (!name) {
    alert("Vui lòng nhập tên tài khoản (vd: account1)");
    return;
  }
  
  try {
    const res = await fetch('/api/gemini/add-profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name })
    });
    const data = await res.json();
    if (res.ok) {
      nameInput.value = "";
      updateGeminiStatusUI();
    } else {
      alert("Lỗi: " + data.detail);
    }
  } catch (err) {
    alert("Lỗi kết nối: " + err.message);
  }
}

async function saveGeminiApiKey() {
  const apiKey = document.getElementById('gemini-api-key-input').value.trim();
  if (!apiKey) {
    alert("Vui lòng nhập API Key.");
    return;
  }
  
  try {
    const res = await fetch('/api/gemini/apikey', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey })
    });
    const data = await res.json();
    
    if (res.ok) {
      alert("Đã lưu API Key thành công!");
      document.getElementById('gemini-api-key-input').value = "";
      updateGeminiStatusUI();
    } else {
      alert("Lỗi: " + data.detail);
    }
  } catch (err) {
    alert("Lỗi kết nối: " + err.message);
  }
}

async function launchGeminiChrome(profilePath) {
  if (!confirm("Hệ thống sẽ mở một cửa sổ Chrome. Vui lòng đăng nhập Google, vào gemini.google.com, sau đó ĐÓNG CỬA SỔ TRÌNH DUYỆT ĐÓ lại (để hệ thống có quyền truy cập profile). Bạn có muốn tiếp tục?")) return;
  
  try {
    const payload = profilePath ? { profile_path: profilePath } : {};
    const res = await fetch('/api/gemini/launch-chrome', { 
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (res.ok) {
      alert("Đang mở Chrome... Hãy đăng nhập xong và ĐÓNG cửa sổ Chrome lại nhé.");
      setTimeout(updateGeminiStatusUI, 3000); // refresh status after 3s
    } else {
      alert("Lỗi: " + data.detail);
    }
  } catch (err) {
    alert("Lỗi kết nối: " + err.message);
  }
}

// Global click handler to close modals
window.onclick = function(event) {
  const geminiModal = document.getElementById('gemini-modal');
  if (event.target == geminiModal) {
    closeGeminiModal();
  }
  
  // Also close new book modal if it exists
  const newBookModal = document.getElementById('new-book-modal');
  if (newBookModal && event.target == newBookModal) {
    if (typeof closeNewBookModal === 'function') closeNewBookModal();
  }
}

// ====================================================
// RAW IMAGE INSPECTOR & FILTER LOGIC
// ====================================================

let inspectorState = {
  items: [],
  filteredItems: [],
  currentIndex: 0,
  currentFilter: "all",
  viewMode: "raw", // "raw", "proc", "split"
};

async function loadInspector() {
  try {
    const res = await fetch('/api/raw-inspector/details');
    const data = await res.json();
    inspectorState.items = data.items || [];
    
    const summary = data.summary || {};
    document.getElementById('count-all').textContent = summary.total || 0;
    document.getElementById('count-pending').textContent = summary.pending || 0;
    document.getElementById('count-approved').textContent = summary.approved || 0;
    document.getElementById('count-needs_review').textContent = summary.needs_review || 0;
    document.getElementById('count-rejected').textContent = summary.rejected || 0;
    
    filterInspector(inspectorState.currentFilter);
  } catch (err) {
    console.error("Error loading inspector details:", err);
  }
}

function filterInspector(filterName) {
  const previousKey = inspectorState.filteredItems[inspectorState.currentIndex]?.key;
  inspectorState.currentFilter = filterName;
  document.querySelectorAll('.filter-tab-btn').forEach(btn => {
    if (btn.dataset.filter === filterName) btn.classList.add('active');
    else btn.classList.remove('active');
  });

  if (filterName === 'all') {
    inspectorState.filteredItems = [...inspectorState.items];
  } else {
    inspectorState.filteredItems = inspectorState.items.filter(item => item.status === filterName);
  }

  if (previousKey) {
    const foundIdx = inspectorState.filteredItems.findIndex(x => x.key === previousKey);
    if (foundIdx !== -1) {
      inspectorState.currentIndex = foundIdx;
    } else if (inspectorState.currentIndex >= inspectorState.filteredItems.length) {
      inspectorState.currentIndex = Math.max(0, inspectorState.filteredItems.length - 1);
    }
  } else if (inspectorState.currentIndex >= inspectorState.filteredItems.length) {
    inspectorState.currentIndex = Math.max(0, inspectorState.filteredItems.length - 1);
  }

  renderInspectorCarousel();
  renderInspectorCurrent();
}

function renderInspectorCarousel() {
  const strip = document.getElementById('insp-thumbnail-strip');
  strip.innerHTML = '';
  
  if (inspectorState.filteredItems.length === 0) {
    strip.innerHTML = `<div style="color:var(--text-muted); padding: 10px; font-size: 0.8rem;">Không có trang nào trong bộ lọc này.</div>`;
    return;
  }

  inspectorState.filteredItems.forEach((item, idx) => {
    const card = document.createElement('div');
    card.className = `thumb-card status-${item.status}`;
    if (idx === inspectorState.currentIndex) card.classList.add('active');
    
    let imgHtml = '';
    if (item.has_raw) {
      imgHtml = `<img src="${item.raw_url}?t=${Date.now()}" class="thumb-img" alt="${item.key}" loading="lazy">`;
    } else {
      imgHtml = `<div class="thumb-placeholder">Chưa sinh</div>`;
    }
    
    const label = item.type === 'cover' ? (item.key === 'cover_front' ? 'Bìa Trước' : 'Bìa Sau') : `Trang ${item.index}`;
    card.innerHTML = `${imgHtml}<div class="thumb-label">${label}</div>`;
    
    card.onclick = () => {
      inspectorState.currentIndex = idx;
      renderInspectorCarousel();
      renderInspectorCurrent();
    };
    
    strip.appendChild(card);
  });
}

function renderInspectorCurrent() {
  const item = inspectorState.filteredItems[inspectorState.currentIndex];
  const counterEl = document.getElementById('insp-counter');
  
  if (!item) {
    counterEl.textContent = "Không có ảnh nào";
    document.getElementById('insp-viewport').innerHTML = `<div class="viewport-placeholder"><p style="color:var(--text-muted);">Không tìm thấy ảnh phù hợp với bộ lọc.</p></div>`;
    return;
  }

  counterEl.textContent = `Đang xem ${inspectorState.currentIndex + 1} / ${inspectorState.filteredItems.length} (${item.key})`;
  document.getElementById('insp-current-filename').textContent = item.name;
  document.getElementById('insp-page-tag').textContent = item.type === 'cover' ? (item.key === 'cover_front' ? 'Bìa trước' : 'Bìa sau') : `#${item.index}`;
  document.getElementById('insp-page-type-tag').textContent = item.type === 'cover' ? 'Bìa Sách' : `Trang Ruột số ${item.index}`;
  document.getElementById('insp-prompt-text').value = item.subject || "";

  // Update Status Badge
  const badge = document.getElementById('insp-status-badge');
  badge.textContent = item.status.toUpperCase().replace('_', ' ');
  if (item.status === 'approved') badge.style.background = 'var(--accent-emerald)';
  else if (item.status === 'needs_review') badge.style.background = 'var(--accent-amber)';
  else if (item.status === 'rejected') badge.style.background = 'var(--accent-rose)';
  else badge.style.background = 'var(--bg-card)';

  // Update Status Action Buttons Active State
  document.querySelectorAll('.btn-status-act').forEach(b => b.classList.remove('active'));
  if (item.status === 'approved') document.querySelector('.btn-status-approve').classList.add('active');
  if (item.status === 'needs_review') document.querySelector('.btn-status-needs').classList.add('active');
  if (item.status === 'rejected') document.querySelector('.btn-status-reject').classList.add('active');

  // Update Info Summary Box
  document.getElementById('insp-info-size').textContent = item.size_kb > 0 ? `${item.size_kb} KB` : "Chưa có file";
  document.getElementById('insp-info-raw-status').textContent = item.has_raw ? "Đã sinh ảnh raw ✓" : "Chưa sinh ❌";
  document.getElementById('insp-info-proc-status').textContent = item.has_proc ? "Đã xử lý nét 300DPI ✓" : "Chưa xử lý ❌";

  // Render Viewport
  renderInspectorViewport(item);
}

function setInspectorViewMode(mode) {
  inspectorState.viewMode = mode;
  document.querySelectorAll('.view-mode-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById(`btn-view-${mode}`);
  if (btn) btn.classList.add('active');
  
  const item = inspectorState.filteredItems[inspectorState.currentIndex];
  if (item) renderInspectorViewport(item);
}

function renderInspectorViewport(item) {
  const viewport = document.getElementById('insp-viewport');
  const t = Date.now();

  if (inspectorState.viewMode === 'raw') {
    if (item.has_raw) {
      viewport.innerHTML = `<img src="${item.raw_url}?t=${t}" class="inspector-stage-img" alt="${item.key}">`;
    } else {
      viewport.innerHTML = `<div class="viewport-placeholder"><p style="color:var(--accent-amber);">❌ Chưa sinh ảnh raw cho trang này</p></div>`;
    }
  } else if (inspectorState.viewMode === 'proc') {
    if (item.has_proc) {
      viewport.innerHTML = `<img src="${item.proc_url}?t=${t}" class="inspector-stage-img" alt="${item.key}">`;
    } else {
      viewport.innerHTML = `<div class="viewport-placeholder"><p style="color:var(--accent-cyan);">⚡ Chưa có ảnh nét đen trắng (Bấm 'Xử Lý Nét' để tạo)</p></div>`;
    }
  } else if (inspectorState.viewMode === 'split') {
    const rawContent = item.has_raw ? `<img src="${item.raw_url}?t=${t}" class="inspector-stage-img" style="max-height:400px;" alt="${item.key}">` : `<p style="color:var(--text-muted);">Chưa có Raw</p>`;
    const procContent = item.has_proc ? `<img src="${item.proc_url}?t=${t}" class="inspector-stage-img" style="max-height:400px;" alt="${item.key}">` : `<p style="color:var(--text-muted);">Chưa có Nét</p>`;
    
    viewport.innerHTML = `
      <div class="split-viewport-wrapper">
        <div class="split-pane">
          <span class="split-pane-label">📷 Ảnh Raw Gốc</span>
          ${rawContent}
        </div>
        <div class="split-pane">
          <span class="split-pane-label">⚡ Ảnh Nét Đen Trắng</span>
          ${procContent}
        </div>
      </div>
    `;
  }
}

function prevInspectorImage() {
  if (inspectorState.filteredItems.length === 0) return;
  if (inspectorState.currentIndex > 0) {
    inspectorState.currentIndex--;
  } else {
    inspectorState.currentIndex = inspectorState.filteredItems.length - 1;
  }
  renderInspectorCarousel();
  renderInspectorCurrent();
}

function nextInspectorImage() {
  if (inspectorState.filteredItems.length === 0) return;
  if (inspectorState.currentIndex < inspectorState.filteredItems.length - 1) {
    inspectorState.currentIndex++;
  } else {
    inspectorState.currentIndex = 0;
  }
  renderInspectorCarousel();
  renderInspectorCurrent();
}

async function updateInspectorStatus(status) {
  const item = inspectorState.filteredItems[inspectorState.currentIndex];
  if (!item) return;
  
  try {
    const res = await fetch('/api/raw-inspector/update-status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: item.key, status: status })
    });
    if (res.ok) {
      item.status = status;
      const mainItem = inspectorState.items.find(x => x.key === item.key);
      if (mainItem) mainItem.status = status;
      
      const counts = { pending:0, approved:0, needs_review:0, rejected:0 };
      inspectorState.items.forEach(x => { counts[x.status] = (counts[x.status] || 0) + 1; });
      document.getElementById('count-pending').textContent = counts.pending || 0;
      document.getElementById('count-approved').textContent = counts.approved || 0;
      document.getElementById('count-needs_review').textContent = counts.needs_review || 0;
      document.getElementById('count-rejected').textContent = counts.rejected || 0;

      renderInspectorCarousel();
      renderInspectorCurrent();
    }
  } catch (err) {
    console.error("Error updating status:", err);
  }
}

async function saveInspectorSubject() {
  const item = inspectorState.filteredItems[inspectorState.currentIndex];
  if (!item) return;
  const newSubj = document.getElementById('insp-prompt-text').value.trim();
  
  try {
    const res = await fetch('/api/raw-inspector/update-subject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: item.key, subject: newSubj })
    });
    if (res.ok) {
      item.subject = newSubj;
      alert(`Đã lưu prompt mới cho ${item.key}!`);
    }
  } catch (err) {
    console.error("Error updating subject:", err);
  }
}

async function regenerateInspectorImage() {
  const item = inspectorState.filteredItems[inspectorState.currentIndex];
  if (!item) return;
  const prompt = document.getElementById('insp-prompt-text').value.trim();
  
  const btn = document.getElementById('btn-insp-regen');
  const oldText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = "⏳ Đang gọi Gemini sinh lại...";
  
  try {
    const res = await fetch('/api/raw-inspector/regenerate-single', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: item.key, prompt: prompt })
    });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      // Auto process single image to update 300DPI lineart if raw regenerated
      try {
        await fetch('/api/raw-inspector/process-single', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: item.key })
        });
      } catch (procErr) {
        console.warn("Auto process after regen warning:", procErr);
      }

      await loadInspector();
      alert(`🎉 Đã sinh lại & tự động thay thế ảnh ${item.key}.png thành công!`);
    } else {
      alert(`⚠️ Lỗi: ${data.detail || "Không thể sinh lại ảnh"}`);
    }
  } catch (err) {
    alert(`Lỗi mạng/hệ thống: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = oldText;
  }
}

async function processInspectorImageSingle() {
  const item = inspectorState.filteredItems[inspectorState.currentIndex];
  if (!item) return;
  
  const btn = document.getElementById('btn-insp-process');
  const oldText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = "⏳ Đang xử lý nét...";
  
  try {
    const res = await fetch('/api/raw-inspector/process-single', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: item.key })
    });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      item.has_proc = true;
      item.proc_url = data.proc_url;
      renderInspectorCurrent();
    } else {
      alert(`⚠️ Lỗi: ${data.detail || "Không thể xử lý nét ảnh"}`);
    }
  } catch (err) {
    alert(`Lỗi: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = oldText;
  }
}

async function uploadReplacementImage(fileInput) {
  const file = fileInput.files[0];
  if (!file) return;
  const item = inspectorState.filteredItems[inspectorState.currentIndex];
  if (!item) return;
  
  const dropHint = document.querySelector('.drop-hint');
  const oldHintText = dropHint ? dropHint.innerHTML : "";
  if (dropHint) dropHint.innerHTML = "⏳ Đang tải ảnh mới lên...";
  
  const formData = new FormData();
  formData.append('key', item.key);
  formData.append('file', file);
  
  try {
    const res = await fetch('/api/raw-inspector/replace', {
      method: 'POST',
      body: formData
    });
    const data = await res.json();
    if (res.ok) {
      // Auto process single image to update 300DPI lineart if raw changed
      try {
        await fetch('/api/raw-inspector/process-single', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: item.key })
        });
      } catch (procErr) {
        console.warn("Auto process warning:", procErr);
      }
      
      await loadInspector();
      if (dropHint) dropHint.innerHTML = "✅ Tải ảnh & Làm sạch thành công!";
      setTimeout(() => {
        if (dropHint) dropHint.innerHTML = oldHintText || "📤 Bấm để chọn file PNG/JPG thay cho trang này";
      }, 2500);
    } else {
      alert(`Lỗi: ${data.detail || "Không thể upload ảnh"}`);
      if (dropHint) dropHint.innerHTML = oldHintText;
    }
  } catch (err) {
    alert(`Lỗi upload: ${err.message}`);
    if (dropHint) dropHint.innerHTML = oldHintText;
  } finally {
    fileInput.value = "";
  }
}

async function deleteInspectorImage() {
  const item = inspectorState.filteredItems[inspectorState.currentIndex];
  if (!item) return;
  
  if (!confirm(`Bạn có chắc chắn muốn xóa ảnh ${item.name}?`)) return;
  
  try {
    const res = await fetch('/api/raw-inspector/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: item.key })
    });
    if (res.ok) {
      await loadInspector();
    }
  } catch (err) {
    alert(`Lỗi xóa ảnh: ${err.message}`);
  }
}
