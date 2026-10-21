import React, { useEffect, useRef, useState } from 'react';
import { api } from './shared';

function readChats() {
  try {
    const data = JSON.parse(localStorage.getItem('lawrag.chats') || '[]');
    return Array.isArray(data) ? data.filter(c => typeof c.id === 'string' && typeof c.title === 'string' && Array.isArray(c.messages)).map(c => ({ ...c, messages: c.messages.filter(m => typeof m.content === 'string') })).slice(0,30) : [];
  } catch { return []; }
}
export default function Chat({ visible, status, onSource }) {
  const [chats, setChats] = useState(readChats);
  const [active, setActive] = useState(null);
  const [question, setQuestion] = useState('');
  const [mode, setMode] = useState('hybrid');
  const [threshold, setThreshold] = useState(.48);
  const [busy, setBusy] = useState(false);
  const [storageError, setStorageError] = useState('');
  const [copied, setCopied] = useState(null);
  const messagesRef = useRef(null);
  const busyRef = useRef(false);
  const current = chats.find(c => c.id === active);
  const messages = current?.messages || [];
  const evidence = [...messages].reverse().find(m => m.role === 'assistant')?.evidence || [];
  useEffect(() => { if (status?.threshold != null) setThreshold(status.threshold); }, [status?.threshold]);
  useEffect(() => { try { localStorage.setItem('lawrag.chats', JSON.stringify(chats.slice(0,30))); } catch { setStorageError('대화 저장 공간이 부족합니다.'); } }, [chats]);
  useEffect(() => { if (messagesRef.current) messagesRef.current.scrollTop = messagesRef.current.scrollHeight; }, [chats, active, busy]);
  async function send(e) {
    e.preventDefault();
    const value = question.trim();
    if (value.length < 2 || busyRef.current) return;
    busyRef.current = true; setBusy(true);
    const id = active || crypto.randomUUID();
    const history = messages.filter(m => ['user','assistant'].includes(m.role)).slice(-6).map(m => ({ role: m.role, content: m.content.slice(0,6000) }));
    setChats(prev => active ? prev.map(c => c.id === id ? { ...c, messages: [...c.messages, { role: 'user', content: value }] } : c) : [{ id, title: value, messages: [{ role: 'user', content: value }] }, ...prev].slice(0,30));
    setActive(id); setQuestion('');
    try {
      const result = await api('/api/chat', { question: value, mode, threshold: Number(threshold), history });
      setChats(prev => prev.map(c => c.id === id ? { ...c, messages: [...c.messages, { role: 'assistant', content: result.answer, evidence: result.evidence, as_of: result.as_of }] } : c));
    } catch (error) {
      setChats(prev => prev.map(c => c.id === id ? { ...c, messages: [...c.messages, { role: 'error', content: error.message }] } : c));
    } finally { busyRef.current = false; setBusy(false); }
  }
  async function copy(text, index) { try { await navigator.clipboard.writeText(text); setCopied(index); } catch { setCopied('error'); } }
  return <div className="chat-page" hidden={!visible}>
    <aside className="sidebar"><a className="brand" href="#chat">Law<span>RAG</span><small>법령 근거 기반 AI</small></a>
      <button className="nav-button" disabled={busy} onClick={() => setActive(null)}>＋ 새 대화</button>
      <div className="sidebar-label">대화 이력 <button disabled={busy} onClick={() => { if (confirm('이 브라우저에 저장된 대화를 모두 삭제할까요?')) { setChats([]); setActive(null); } }}>삭제</button></div>
      <nav id="history">{chats.map(c => <button key={c.id} className={c.id === active ? 'active' : ''} disabled={busy} onClick={() => setActive(c.id)}>{c.title}</button>)}</nav>
      <div className="sidebar-footer">법령을 찾고, 근거로 답합니다.<br />대화는 이 브라우저에 저장됩니다.</div>
    </aside>
    <main><header><span className="agent-pill">◉ 법령 검색 에이전트</span><span id="connection" role="status">{storageError || (status ? `${status.laws.length}개 법령 · ${status.llm_configured ? 'AI 연결 설정됨' : 'AI 키 미설정'}` : '서버 상태 확인 중')}</span><button className="mobile-new-chat" disabled={busy} onClick={() => setActive(null)}>새 대화</button></header>
      <div id="messages" ref={messagesRef} aria-live="polite">
        {!current && <div className="welcome"><div className="welcome-icon">§</div><h1>법령에 묻고,<br />근거로 확인하세요.</h1><p>관련 조문을 찾아 답변과 원문을 함께 제공합니다.</p><button id="example" onClick={() => setQuestion('선박안전법에 적용되지 않는 것은?')}>선박안전법에 적용되지 않는 것은? ↗</button></div>}
        {messages.map((m,i) => <article key={i} className={`message ${m.role}`}>
          {m.role === 'assistant' && <div className="message-label">LAW RAG · 법령 에이전트</div>}
          {m.as_of && <div className="answer-date">기준일 {m.as_of} · 수집된 개정 버전 기준</div>}
          <div className="answer-text">{m.content}</div>
          {!!m.evidence?.length && <div className="evidence-chips">{m.evidence.map((item,j) => <button key={j} onClick={() => onSource(item)}>▤ [{j+1}] {item.law_name} {item.number}{item.title ? `(${item.title})` : ''}</button>)}</div>}
          {m.role === 'assistant' && <button className="copy-button" onClick={() => copy(m.content,i)}>{copied === i ? '복사됨' : copied === 'error' ? '복사 권한을 확인하세요' : '답변 복사'}</button>}
        </article>)}
        {busy && <div className="message">관련 조문을 검색하고 답변을 생성하고 있습니다…</div>}
      </div>
      <div className="composer-area"><form id="chat-form" onSubmit={send}>
        <textarea id="question" aria-label="질문" rows="2" placeholder="법령에 대해 질문해 주세요" maxLength={2000} minLength={2} required value={question} onChange={e => setQuestion(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); e.currentTarget.form.requestSubmit(); } }} />
        <div className="composer-bottom"><select id="mode" aria-label="검색 방식" value={mode} onChange={e => setMode(e.target.value)}><option value="hybrid">키워드 + 임베딩</option><option value="keyword">키워드 검색</option></select><label className="threshold">근거 임계치 <input type="number" min="0" max="1" step="0.01" required value={threshold} onChange={e => setThreshold(e.target.value)} /></label><button id="send" type="submit" aria-label="질문 전송" disabled={busy}>↑</button></div>
      </form><small>기본은 현재 시행 중인 최신 수집 버전입니다. 과거 조회는 기준일을 지정하세요. 최신 개정 여부는 공식 원문을 확인하세요.</small></div>
    </main>
    <aside className="sources"><h2>지식 출처 <span>{evidence.length}</span></h2><p className="muted">답변에 사용한 법령·조문</p>
      {evidence.map((item,i) => <button key={i} className="source-card" onClick={() => onSource(item)}><strong>▤ {item.law_name} {item.number}</strong><p>{item.title}</p>{!item.relation && item.title_score != null && <p>제목 {(item.title_score*100).toFixed(1)} · 본문 {(item.body_score*100).toFixed(1)}</p>}<p>{item.relation ? `${item.referenced_by}의 참조 근거` : `관련도 ${(item.score*100).toFixed(1)}%`} · 시행 {item.effective || '미상'}</p></button>)}
      {!evidence.length && <div className="source-empty">질문하면 관련 근거가<br />여기에 표시됩니다.</div>}
      <p className="score-note">관련도는 검색 점수이며 법적 정확도의 확률이 아닙니다.</p>
    </aside>
  </div>;
}
