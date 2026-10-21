import React, { useEffect, useRef } from 'react';

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
export const statusLabels = { uncollected: '미수집', queued: '수집 대기', running: '수집 중', completed: '수집 완료', failed: '수집 실패', interrupted: '수집 중단' };
export const rowKey = row => `${row.kind}:${row.id}`;
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
