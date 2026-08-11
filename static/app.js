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
  
  try {
    const res = await fetch('/api/gemini/status');
    const data = await res.json();
    
    let statusHtml = "";
    
    // API Status
    if (data.api_key_set) {
      statusHtml += `<div style="color: var(--accent-emerald);">✅ API Key: Đã được cấu hình (Sẵn sàng cho chế độ API)</div>`;
    } else {
      statusHtml += `<div style="color: var(--accent-rose);">❌ API Key: Chưa cấu hình (Chế độ API sẽ lỗi)</div>`;
    }
    
    // Chrome Status
    if (data.chrome_running) {
      statusHtml += `<div style="color: var(--accent-emerald); margin-top: 5px;">✅ Chrome Automation: Đang chạy ở port 9222 (Sẵn sàng cho chế độ Web)</div>`;
    } else {
      statusHtml += `<div style="color: var(--text-muted); margin-top: 5px;">⚪ Chrome Automation: Chưa chạy</div>`;
    }
    
    statusEl.innerHTML = statusHtml;
  } catch (err) {
    statusEl.innerHTML = `<span style="color: var(--accent-rose);">Lỗi khi lấy trạng thái: ${err.message}</span>`;
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

async function launchGeminiChrome() {
  if (!confirm("Hệ thống sẽ mở một cửa sổ Chrome mới (có thể mất vài giây). Vui lòng đăng nhập vào tài khoản Google, sau đó vào gemini.google.com và ĐỂ MỞ CỬA SỔ ĐÓ. Bạn có muốn tiếp tục?")) return;
  
  try {
    const res = await fetch('/api/gemini/launch-chrome', { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      alert("Đang mở Chrome... Hãy đăng nhập và giữ cửa sổ Chrome luôn mở.");
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
