// ==UserScript==
// @name         Gemini Sender (Coloring Book)
// @namespace    coloringbook
// @version      1.0.0
// @description  Nhan prompt tu Playwright qua DOM, go vao o nhap Gemini bang duong soan thao that roi bam Send dung MOT lan.
// @match        https://gemini.google.com/*
// @run-at       document-idle
// @noframes
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  // ------------------------------------------------------------------
  // GIAO THUC VOI PLAYWRIGHT (bookgen/gemini_pool.py :: _send_via_extension)
  //
  //   html[data-mx-ready="1"]        userscript da san sang (Playwright do truoc khi giao viec)
  //   <div id="__mx_job">prompt      Playwright giao viec; userscript doc xong xoa ngay
  //   html[data-mx-status]           'working' -> 'clicked' | 'error'
  //   html[data-mx-error]            mo ta loi khi status='error'
  //
  // Chay o world USER_SCRIPT nen KHONG thay bien cua trang, nhung DOM thi
  // dung chung -> kenh nay thong ca hai chieu. Moi tab mot node rieng, khong
  // giam len nhau nhu localStorage.
  // ------------------------------------------------------------------

  const JOB_ID = '__mx_job';
  const root = document.documentElement;

  const PROMPT_BOX = [
    'div.ql-editor[contenteditable="true"]',
    'rich-textarea div[contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"]',
  ];
  const SEND_BUTTON = [
    'button[aria-label*="Send" i]',
    'button[aria-label*="Gửi" i]',
    'button.send-button',
    'button[mattooltip*="Send" i]',
    'button[aria-label*="Submit" i]',
    '.send-button-container button',
    'button[data-test-id="send-button"]',
  ];

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function setStatus(s, err) {
    if (err !== undefined) root.dataset.mxError = String(err).slice(0, 300);
    root.dataset.mxStatus = s;
  }

  function visible(el) {
    if (!el) return false;
    if (el.getClientRects().length === 0) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  }

  function findBox() {
    for (const sel of PROMPT_BOX) {
      for (const el of document.querySelectorAll(sel)) {
        if (visible(el)) return el;
      }
    }
    return null;
  }

  // Nut Send va nut Stop CUNG LA MOT NUT, chi doi aria-label sau khi gui.
  // Bam trung nut Stop la huy luon cau tra loi dang ve. Loc thang tay.
  function findSend() {
    for (const sel of SEND_BUTTON) {
      for (const b of document.querySelectorAll(sel)) {
        if (!visible(b)) continue;
        if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
        const aria = (b.getAttribute('aria-label') || '').toLowerCase();
        if (aria.includes('stop') || aria.includes('dừng')) continue;
        return b;
      }
    }
    return null;
  }

  // Day chinh la ly do userscript nay ton tai.
  //
  // Duong cu cua Playwright dat el.innerText = txt: chi sua DOM, con model noi
  // bo cua Quill (editor cua Gemini) khong he hay biet -> nut Send ket disabled,
  // phai chua chay bang tro go dau "." roi Backspace.
  //
  // execCommand('insertText') di qua dung pipeline soan thao cua Chrome: no ban
  // beforeinput/input that, Quill cap nhat Delta cua no dung nhu nguoi go tay.
  function insertPrompt(box, text) {
    box.focus();
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(box);      // chon het noi dung cu...
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('insertText', false, text);   // ...roi de che luon
  }

  async function waitSendEnabled(ms) {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) {
      const b = findSend();
      if (b) return b;
      await sleep(250);
    }
    return null;
  }

  async function runJob(text) {
    setStatus('working', '');

    const box = findBox();
    if (!box) throw new Error('khong tim thay o nhap prompt');

    insertPrompt(box, text);

    // Doi Quill/Angular tieu hoa xong roi moi doi chieu.
    await sleep(300);
    const got = (box.innerText || '').replace(/\s+/g, ' ').trim();
    if (got.length < Math.min(20, text.length)) {
      throw new Error('insertText khong vao duoc o nhap (con ' + got.length + ' ky tu)');
    }

    const btn = await waitSendEnabled(12000);
    if (!btn) throw new Error('nut Send khong bat sang trong 12s');

    // MOT lan. Khong retry, khong vong lap. Cu bam lan hai la trung nut Stop.
    btn.click();
    setStatus('clicked', '');
    // Tu day tro di TUYET DOI khong ghi 'error' nua: Playwright se hieu nham la
    // chua gui va chay duong du phong -> thanh cu bam thu hai.
  }

  let busy = false;
  async function pickUp() {
    if (busy) return;
    const node = document.getElementById(JOB_ID);
    if (!node) return;

    busy = true;
    const text = node.textContent || '';
    node.remove();                       // xoa ngay de khong nhan lai viec cu

    try {
      if (!text.trim()) throw new Error('job rong');
      await runJob(text);
    } catch (e) {
      setStatus('error', e && e.message ? e.message : e);
    } finally {
      busy = false;
    }
  }

  new MutationObserver(pickUp).observe(root, { childList: true });
  pickUp();                              // phong khi job duoc giao truoc luc script chay

  root.dataset.mxReady = '1';
})();
