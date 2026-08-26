document.addEventListener('DOMContentLoaded', () => {
  const dropZone = document.getElementById('drop-zone');
  const fileInput = document.getElementById('file-input');
  const fileName = document.getElementById('file-name');
  const loadingOverlay = document.getElementById('loading-overlay');
  const resultSection = document.getElementById('result-section');
  const transcriptionText = document.getElementById('transcription-text');
  const copyBtn = document.getElementById('copy-btn');
  const downloadBtn = document.getElementById('download-btn');
  const hintInput = document.getElementById('hint-input');
  const polishInput = document.getElementById('polish-input');
  const rawBtn = document.getElementById('raw-btn');
  let lastResult = null;   // {text, raw, polished}
  let showingRaw = false;

  try {
    const savedPolish = localStorage.getItem('transcribe-polish');
    if (savedPolish !== null) polishInput.checked = savedPolish === '1';
  } catch (_) {}
  polishInput.addEventListener('change', () => {
    try { localStorage.setItem('transcribe-polish', polishInput.checked ? '1' : '0'); } catch (_) {}
  });

  function renderResult() {
    if (!lastResult) return;
    transcriptionText.textContent = showingRaw ? lastResult.raw : lastResult.text;
    const mark = lastResult.polish_partial ? '*' : '';   // 一部未整形の印
    rawBtn.querySelector('span').textContent = (showingRaw ? 'Polished' : 'Raw') + mark;
    rawBtn.title = lastResult.polish_partial ? '一部の塊は整形できず原文のまま。整形前の原文と切り替え' : '整形前の原文と切り替え';
    rawBtn.classList.toggle('hidden', !lastResult.polished);
  }

  rawBtn.addEventListener('click', () => {
    showingRaw = !showingRaw;
    renderResult();
  });

  // 用語ヒントは端末に覚えさせる
  try {
    const saved = localStorage.getItem('transcribe-hint');
    if (saved) hintInput.value = saved;
  } catch (_) {}
  hintInput.addEventListener('input', () => {
    try { localStorage.setItem('transcribe-hint', hintInput.value); } catch (_) {}
  });

  // Drag & Drop
  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
  });

  dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
  });

  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    const files = e.dataTransfer.files;
    if (files.length > 0) {
      handleFile(files[0]);
    }
  });

  // Click to upload (avoid double-trigger from label)
  dropZone.addEventListener('click', (e) => {
    // labelやinput自体からのクリックは無視（labelがinputを開くので）
    if (e.target.tagName === 'LABEL' || e.target.tagName === 'INPUT') {
      return;
    }
    fileInput.click();
  });

  fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) {
      handleFile(e.target.files[0]);
    }
  });

  // Copy button
  copyBtn.addEventListener('click', async () => {
    const text = transcriptionText.innerText;
    try {
      await navigator.clipboard.writeText(text);
      const span = copyBtn.querySelector('span');
      const originalText = span.textContent;
      span.textContent = 'Copied!';
      setTimeout(() => {
        span.textContent = originalText;
      }, 2000);
    } catch (err) {
      console.error('Copy failed:', err);
    }
  });

  // Download button
  downloadBtn.addEventListener('click', () => {
    const text = transcriptionText.innerText;
    const blob = new Blob([text], { type: 'text/plain; charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `transcription_${new Date().toISOString().slice(0, 10)}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  });

  async function handleFile(file) {
    // Validate file type
    if (!(file.type.startsWith('audio/') || file.type.startsWith('video/'))) {
      alert('音声または動画ファイルを選択してください。');
      return;
    }

    fileName.textContent = `選択: ${file.name}`;
    loadingOverlay.classList.remove('hidden');
    resultSection.classList.add('hidden');

    const formData = new FormData();
    formData.append('file', file);
    const hint = hintInput.value.trim();
    if (hint) formData.append('prompt', hint);
    formData.append('polish', polishInput.checked ? 'true' : 'false');

    try {
      const response = await fetch('/transcribe', {
        method: 'POST',
        body: formData
      });

      const data = await response.json();

      if (response.ok) {
        lastResult = data;
        showingRaw = false;
        renderResult();
        resultSection.classList.remove('hidden');
        // Scroll to result
        resultSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } else {
        const errorMsg = data.error || data.detail || '不明なエラーが発生しました';
        alert(`エラー: ${errorMsg}`);
      }
    } catch (error) {
      console.error('Error:', error);
      alert(`通信エラーが発生しました。\n${error.message}`);
    } finally {
      loadingOverlay.classList.add('hidden');
    }
  }
});
