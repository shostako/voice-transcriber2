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
  const startBtn = document.getElementById('start-btn');
  let lastResult = null;   // {text, raw, polished}
  let showingRaw = false;
  let selectedFile = null;
  let isProcessing = false;

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
    if (isProcessing) return;
    dropZone.classList.add('dragover');
  });

  dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
  });

  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    if (isProcessing) return;
    const files = e.dataTransfer.files;
    if (files.length > 0) {
      selectFile(files[0]);
    }
  });

  // Click to select (avoid double-trigger from label)
  dropZone.addEventListener('click', (e) => {
    if (isProcessing) return;
    // labelやinput自体からのクリックは無視（labelがinputを開くので）
    if (e.target.tagName === 'LABEL' || e.target.tagName === 'INPUT') {
      return;
    }
    fileInput.click();
  });

  fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) {
      selectFile(e.target.files[0]);
    }
  });

  startBtn.addEventListener('click', startTranscription);

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

  function formatFileSize(bytes) {
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function setProcessing(processing) {
    isProcessing = processing;
    fileInput.disabled = processing;
    hintInput.disabled = processing;
    polishInput.disabled = processing;
    startBtn.disabled = processing || !selectedFile;
    startBtn.textContent = processing ? '文字起こし中…' : '文字起こし開始';
    dropZone.classList.toggle('processing', processing);
    dropZone.setAttribute('aria-disabled', processing ? 'true' : 'false');
  }

  function selectFile(file) {
    // Validate file type
    if (!(file.type.startsWith('audio/') || file.type.startsWith('video/'))) {
      selectedFile = null;
      startBtn.disabled = true;
      fileName.textContent = '';
      alert('音声または動画ファイルを選択してください。');
      fileInput.value = '';
      return;
    }

    // ドロップとファイル選択を行き来しても、同じファイルを再選択できるようにする
    fileInput.value = '';
    selectedFile = file;
    fileName.textContent = `選択済み: ${file.name}（${formatFileSize(file.size)}）`;
    startBtn.disabled = false;
  }

  async function startTranscription() {
    if (!selectedFile || isProcessing) return;

    const file = selectedFile;
    setProcessing(true);
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
        selectedFile = null;
        fileInput.value = '';
        fileName.textContent = `完了: ${file.name}`;
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
      setProcessing(false);
    }
  }
});
