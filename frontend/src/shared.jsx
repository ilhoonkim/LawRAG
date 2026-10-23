import React, { useEffect, useRef } from 'react';

export function createChatId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // randomUUID needs HTTPS/localhost; getRandomValues also works on LAN HTTP.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}

export async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return; } catch { /* Try the HTTP-compatible path. */ }
  }
  const previousFocus = document.activeElement;
  const field = document.createElement('textarea');
  field.value = text;
  field.readOnly = true;
  field.style.cssText = 'position:fixed;left:0;top:0;opacity:0;pointer-events:none';
  document.body.appendChild(field);
  try {
    field.select();
    if (!document.execCommand('copy')) throw new Error('복사 권한을 확인하세요.');
  } finally {
    field.remove();
    previousFocus?.focus({ preventScroll: true });
  }
}

export async function api(path, body, options = {}) {
  const response = await fetch(path, body === undefined ? options : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), ...options
  });
  let data;
  try { data = await response.json(); } catch { throw new Error('서버 응답을 읽지 못했습니다.'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '입력값 또는 서버 상태를 확인하세요.');
  return data;
}
export const kindLabel = kind => kind === 'admrul' ? '행정규칙' : '법령';
export const dateText = value => /^\d{8}$/.test(value || '') ? `${value.slice(0,4)}-${value.slice(4,6)}-${value.slice(6,8)}` : value || '—';
export function timeText(value) {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.valueOf()) ? '—' : new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
  }).format(d);
}
export const statusLabels = { uncollected: '미수집', queued: '수집 대기', running: '수집 중', completed: '수집 완료', failed: '수집 실패', interrupted: '수집 중단', skipped: '건너뜀' };
export const rowKey = row => `${row.kind}:${row.id}:${row.effective||''}`;
export const collectionItem = row => ({ mst: row.id, kind: row.kind, name: row.name, promulgated: row.promulgated || '', effective: row.effective || '' });
export const selectable = row => !row.version && !['completed', 'queued', 'running'].includes(row.collection_status) && /^\d+$/.test(row.id);
export function Badge({ status }) { return <span className={`status-badge status-${status}`}>{statusLabels[status] || status}</span>; }
export function EmptyRow({ columns, children }) { return <tr><td colSpan={columns} className="empty-table">{children}</td></tr>; }
export function Pagination({ page, total, onChange, label, disabled = false }) {
  const pages = Math.ceil(total / 10);
  if (pages <= 1) return null;
  const start = Math.max(1, Math.min(page - 2, pages - 4));
  const numbers = Array.from({ length: Math.min(5, pages - start + 1) }, (_, i) => start + i);
  return <nav className="pagination" aria-label={label}>
    <button disabled={disabled || page === 1} onClick={() => onChange(page - 1)} aria-label="이전 페이지">‹</button>
    {numbers.map(n => <button key={n} className={n === page ? 'current' : ''} aria-current={n === page ? 'page' : undefined} disabled={disabled} onClick={() => onChange(n)}>{n}</button>)}
    <button disabled={disabled || page === pages} onClick={() => onChange(page + 1)} aria-label="다음 페이지">›</button>
  </nav>;
}
export function Modal({ title, onClose, className = '', children, id }) {
  const ref = useRef(null);
  useEffect(() => {
    const node = ref.current;
    node.showModal();
    return () => node.close();
  }, []);
  return <dialog ref={ref} id={id} className={className} aria-label={title} onCancel={e => { e.preventDefault(); onClose(); }}>
    <div className="dialog-head"><strong>{title}</strong><button onClick={onClose} aria-label={`${title} 닫기`}>✕</button></div>
    {children}
  </dialog>;
}

export async function streamChat(body, onStage, onAnswerText = () => {}) {
  const response = await fetch('/api/chat/stream', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : '입력값 또는 서버 상태를 확인하세요.');
  }
  if (!response.body) throw new Error('스트리밍 응답을 읽을 수 없습니다.');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let pending = '', result;
  function consume(line) {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.type === 'stage') onStage(event.stage);
    if (event.type === 'answer_text' && typeof event.text === 'string') onAnswerText(event.text);
    if (event.type === 'result') result = event.result;
    if (event.type === 'error') throw new Error(event.detail);
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      pending += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const lines = pending.split('\n'); pending = lines.pop();
      lines.forEach(consume);
      if (done) break;
    }
    consume(pending);
    if (!result) throw new Error('답변 수신이 중단되었습니다. 다시 시도해 주세요.');
    return result;
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
