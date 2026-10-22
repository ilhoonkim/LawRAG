import React, { useEffect, useRef, useState } from 'react';
import { SettingsIcon } from './Icons';

export default function SearchSettings({ mode, fusion, threshold, disabled, onApply }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState({ mode, fusion, threshold });
  const root = useRef(null), trigger = useRef(null);
  useEffect(() => {
    if (!open) return;
    root.current?.querySelector('select')?.focus();
    const outside = e => { if (!root.current?.contains(e.target)) setOpen(false); };
    const escape = e => { if (e.key === 'Escape') { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [open]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  return <div className="chat-settings" ref={root}>
    <button className="settings-trigger" type="button" ref={trigger} title="검색 설정" aria-label="검색 설정" aria-expanded={open} aria-controls="search-options-panel" disabled={disabled} onClick={() => {
      setDraft({ mode, fusion, threshold }); setOpen(value => !value);
    }}><SettingsIcon /></button>
    {open && <section id="search-options-panel" className="search-options-panel" role="dialog" aria-label="검색 설정">
      <div className="search-options-heading"><strong>검색 설정</strong><button type="button" aria-label="검색 설정 닫기" onClick={() => { setOpen(false); trigger.current?.focus(); }}>✕</button></div>
      <form onSubmit={e => { e.preventDefault(); onApply({ ...draft, threshold: Number(draft.threshold) }); setOpen(false); trigger.current?.focus(); }}>
        <label>검색 방식<select aria-label="검색 방식" value={draft.mode} onChange={e => setDraft({ ...draft, mode: e.target.value })}><option value="hybrid">키워드 + 임베딩</option><option value="keyword">키워드 검색</option></select></label>
        <label>검색 결합 방식<select aria-label="검색 결합 방식" value={draft.fusion} onChange={e => setDraft({ ...draft, fusion: e.target.value })}><option value="weighted">가중합</option><option value="rrf">RRF 순위 결합</option></select></label>
        <label>근거 임계치<input aria-label="근거 임계치" type="number" min="0" max="1" step="0.01" required value={draft.threshold} onChange={e => setDraft({ ...draft, threshold: e.target.value })} /></label>
        <p>임계치를 높이면 더 엄격하게 근거를 고릅니다. RRF는 법령명·조문 제목·본문의 검색 순위를 결합합니다.</p>
        <button className="apply-search-options" type="submit">적용</button>
      </form>
    </section>}
  </div>;
}
