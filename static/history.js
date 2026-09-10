const number = new Intl.NumberFormat('ja-JP');

function duration(seconds) {
  if (seconds == null) return '—';
  const value = Number(seconds);
  const minutes = Math.floor(value / 60);
  const rest = Math.round(value % 60);
  return `${minutes}分${rest}秒`;
}

function dateTime(value) {
  return new Intl.DateTimeFormat('ja-JP', {
    timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  }).format(new Date(value));
}

function money(value) {
  return `$${Number(value || 0).toFixed(6)}`;
}

function renderRow(row) {
  const tr = document.createElement('tr');
  const cells = [
    dateTime(row.created_at),
    row.status === 'success' ? '成功' : `失敗${row.error_type ? ` (${row.error_type})` : ''}`,
    duration(row.audio_duration_seconds),
    `${number.format(row.raw_characters || 0)} → ${number.format(row.output_characters || 0)}`,
    row.transcription_model,
    row.transcription_segments ?? '—',
    row.polish_requested ? (row.polish_succeeded ? (row.polish_partial ? '一部成功' : '成功') : '未整形') : 'OFF',
    row.processing_seconds == null ? '—' : `${Number(row.processing_seconds).toFixed(1)}秒`,
    money(row.total_cost_usd),
  ];
  cells.forEach((value) => {
    const td = document.createElement('td');
    td.textContent = value;
    tr.appendChild(td);
  });
  if (row.status === 'error') tr.classList.add('error-row');
  return tr;
}

async function loadHistory() {
  const status = document.getElementById('history-status');
  try {
    const response = await fetch('/api/history?limit=200');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const summary = data.summary;
    document.getElementById('requests').textContent = number.format(summary.requests);
    document.getElementById('result-count').textContent = `${number.format(summary.successes)} / ${number.format(summary.errors)}`;
    document.getElementById('audio-time').textContent = duration(summary.audio_duration_seconds);
    document.getElementById('characters').textContent = number.format(summary.output_characters);
    document.getElementById('cost').textContent = money(summary.total_cost_usd);
    const body = document.getElementById('history-body');
    body.replaceChildren(...data.items.map(renderRow));
    status.textContent = data.items.length ? `直近${data.items.length}件を表示` : '履歴はまだない。次の文字起こしから記録される。';
  } catch (error) {
    status.textContent = `履歴を読み込めなかった: ${error.message}`;
  }
}

loadHistory();
