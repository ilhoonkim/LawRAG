import React, { useEffect, useRef, useState } from 'react';
import { api, streamChat, dateText, createChatId, copyText } from './shared';
import { JusticeIcon, ProcessingIcon, TrashIcon } from './Icons';
import AnswerMarkdown from './AnswerMarkdown';

function readChats() {
  try {
    const data = JSON.parse(localStorage.getItem('lawrag.chats') || '[]');
    return Array.isArray(data) ? data.filter(c => typeof c.id === 'string' && typeof c.title === 'string' && Array.isArray(c.messages)).map(c => ({ ...c, messages: c.messages.filter(m => typeof m.content === 'string') })).slice(0,30) : [];
  } catch { return []; }
}
const stageLabels = {
  rewrite: '질문 재조정 중', search: '관련 법조항 검색 중',
  review: '법조항 후보군 검토 중', assess: '근거 충분성 확인 중',
  review_hierarchy: '상하위 연결 조문 검토 중',
  additional_search: '부족한 근거 추가 검색 중', answer: '근거 법조항 기반 답변 생성 중'
};
function groupedSources(evidence = []) {
  const groups = new Map();
  evidence.forEach((item, i) => {
    if (item.relation && !item.include_in_sources) return;
    if (!groups.has(item.law_name)) groups.set(item.law_name, { name: item.law_name, items: [], score: 0 });
    const group = groups.get(item.law_name);
    group.items.push({ ...item, citation: i + 1 });
    group.score = Math.max(group.score, item.score || 0);
  });
  return [...groups.values()].sort((a,b) => b.score - a.score).map(g => ({ ...g, items: g.items.sort((a,b) => (b.score || 0) - (a.score || 0)) }));
}
export default function Chat({ visible, status, onSource }) {
  const [chats, setChats] = useState(readChats);
  const [active, setActive] = useState(null);
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState(null);
  const [sourceGroup, setSourceGroup] = useState(null);
  const [model, setModel] = useState('');
  const [customModel, setCustomModel] = useState('');
  const [modelInfo, setModelInfo] = useState(null);
  const [storageError, setStorageError] = useState('');
  const [copied, setCopied] = useState(null);
  const messagesRef = useRef(null);
  const sourcesRef = useRef(null);
  const sourceTriggerRef = useRef(null);
  const newChatRef = useRef(null);
  const busyRef = useRef(false);
  const current = chats.find(c => c.id === active);
  const messages = current?.messages || [];
  const evidence = sourceGroup?.items || [];
  useEffect(() => { api('/api/models').then(info => { setModelInfo(info); setModel(prev => prev || info.default); }).catch(() => setModelInfo({ models: ['gpt-4o-mini','gpt-4o','gpt-4.1','gpt-5.4'], warning: '모델 목록 조회 실패 · 모델 ID를 직접 입력할 수 있습니다.' })); }, []);
  useEffect(() => { setSourceGroup(null); }, [active]);
  useEffect(() => {
    if (!sourceGroup?.citation) return;
    const card = sourcesRef.current?.querySelector('[aria-current="true"]');
    card?.focus({ preventScroll: true });
    card?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [sourceGroup]);
  useEffect(() => { try { localStorage.setItem('lawrag.chats', JSON.stringify(chats.slice(0,30))); } catch { setStorageError('대화 저장 공간이 부족합니다.'); } }, [chats]);
  useEffect(() => { if (messagesRef.current) messagesRef.current.scrollTop = messagesRef.current.scrollHeight; }, [chats, active, busy, stage]);
  async function send(e) {
    e.preventDefault();
    const value = question.trim();
    if (value.length < 2 || busyRef.current) return;
    const id = active || createChatId();
    busyRef.current = true; setBusy(true); setStage(null); setSourceGroup(null);
    const history = messages.filter(m => ['user','assistant'].includes(m.role)).slice(-6).map(m => ({ role: m.role, content: m.content.slice(0,6000) }));
    setChats(prev => active ? prev.map(c => c.id === id ? { ...c, messages: [...c.messages, { role: 'user', content: value }] } : c) : [{ id, title: value, messages: [{ role: 'user', content: value }] }, ...prev].slice(0,30));
    setActive(id); setQuestion('');
    try {
      const selectedModel = model === 'custom' ? customModel.trim() : (model || status?.chat_model);
      const result = await streamChat({ question: value, mode: 'hybrid', history, model: selectedModel || undefined }, setStage);
      setChats(prev => prev.map(c => c.id === id ? { ...c, messages: [...c.messages, { role: 'assistant', content: result.answer, evidence: result.evidence, as_of: result.as_of, model: selectedModel, missing_facts: result.assessment?.missing_facts, additional_search_rounds: result.additional_search_rounds }] } : c));
    } catch (error) {
      setChats(prev => prev.map(c => c.id === id ? { ...c, messages: [...c.messages, { role: 'error', content: error.message }] } : c));
    } finally { busyRef.current = false; setBusy(false); }
  }
  async function copy(text, index) { try { await copyText(text); setCopied(index); } catch { setCopied('error'); } }
  function openSources(group, messageIndex, trigger, citation = null) {
    sourceTriggerRef.current = trigger;
    setSourceGroup({ ...group, messageIndex, citation });
  }
  function closeSources() {
    setSourceGroup(null);
    sourceTriggerRef.current?.focus({ preventScroll: true });
  }
  function deleteChat(chat) {
    if (busyRef.current || !confirm(`이 대화를 삭제할까요?\n\n${chat.title.length > 120 ? chat.title.slice(0,120) + '…' : chat.title}`)) return;
    setChats(prev => prev.filter(item => item.id !== chat.id));
    if (active === chat.id) {
      setActive(null);
      setSourceGroup(null);
      setCopied(null);
    }
    newChatRef.current?.focus({ preventScroll: true });
  }
  return <div className={`chat-page ${sourceGroup ? 'has-sources' : ''}`} hidden={!visible}>
    <aside className="sidebar"><a className="brand" href="#chat">Law<span>RAG</span><small>법령 근거 기반 AI</small></a>
      <button className="nav-button" ref={newChatRef} disabled={busy} onClick={() => setActive(null)}>＋ 새 대화</button>
      <div className="sidebar-label">대화 이력 <button disabled={busy || !chats.length} onClick={() => { if (confirm('이 브라우저에 저장된 대화를 모두 삭제할까요?')) { setChats([]); setActive(null); } }}>전체 삭제</button></div>
      <nav id="history" aria-label="대화 이력">{chats.map(c => <div key={c.id} className={`history-entry ${c.id === active ? 'active' : ''}`}>
        <button className="history-title" title={c.title} aria-current={c.id === active ? 'page' : undefined} disabled={busy} onClick={() => setActive(c.id)}>{c.title}</button>
        <button className="history-delete" type="button" title="대화 삭제" aria-label={`${c.title} 대화 삭제`} disabled={busy} onClick={() => deleteChat(c)}><TrashIcon /></button>
      </div>)}</nav>
      <div className="sidebar-footer">법령을 찾고, 근거로 답합니다.<br />대화는 이 브라우저에 저장됩니다.</div>
    </aside>
    <main><header><span className="agent-pill">◉ 법령 검색 에이전트</span><div className="chat-header-actions"><span id="connection" role="status">{storageError || (status ? `${status.laws.length}개 법령 · ${status.llm_configured ? 'AI 연결 설정됨' : 'AI 키 미설정'}` : '서버 상태 확인 중')}</span><button className="mobile-new-chat" disabled={busy} onClick={() => setActive(null)}>새 대화</button></div></header>
      <div id="messages" ref={messagesRef} aria-live="polite">
        {!current && <div className="welcome"><div className="welcome-icon">§</div><h1>법령에 묻고,<br />근거로 확인하세요.</h1><p>관련 조문을 찾아 답변과 원문을 함께 제공합니다.</p><button id="example" onClick={() => setQuestion('선박안전법에 적용받지 않는 선박의 종류는?')}>선박안전법에 적용받지 않는 선박의 종류는? ↗</button></div>}
        {messages.map((m,i) => <article key={i} className={`message ${m.role}`}>
          {m.role === 'assistant' && <div className="message-label">LAW RAG · 법령 에이전트{m.model && <span className="answer-model"> · {m.model}</span>}</div>}
          {m.as_of && <div className="answer-date">기준일 {m.as_of} · 수집된 개정 버전 기준</div>}
          {m.role === 'assistant' ? <AnswerMarkdown content={m.content} evidence={m.evidence} onCitationClick={(number, trigger) => {
            const group = groupedSources(m.evidence).find(g => g.items.some(item => item.citation === number));
            if (group) openSources(group, i, trigger, number);
          }} /> : <div className="answer-text">{m.content}</div>}
          {m.evidence?.some(item => !item.relation || item.include_in_sources) && <div className="evidence-chips">{groupedSources(m.evidence).map(group => <button key={group.name} aria-label={`${group.name} 근거 ${group.items.length}개 보기`} aria-expanded={sourceGroup?.messageIndex === i && sourceGroup?.name === group.name} onClick={event => openSources(group, i, event.currentTarget)}><JusticeIcon />{group.name} <span>{group.items.length}</span></button>)}</div>}
          {m.role === 'assistant' && <button className="copy-button" onClick={() => copy(m.content,i)}>{copied === i ? '복사됨' : copied === 'error' ? '복사 권한을 확인하세요' : '답변 복사'}</button>}
        </article>)}
        {busy && <div className="message pipeline-progress" role="status" aria-label="답변 처리 단계" aria-atomic="true">
          <p className="current"><span className="stage-activity-icon"><ProcessingIcon /></span><span>{stage ? stageLabels[stage] || '답변 처리 중' : '서버 연결 중…'}</span></p>
          <div className="answer-skeleton" aria-hidden="true">
            <div className="skeleton-preview"><span className="skeleton-line" /><span className="skeleton-line" /><span className="skeleton-line" /></div>
            <div className="skeleton-paragraph"><span className="skeleton-line" /><span className="skeleton-line writing" /></div>
          </div>
        </div>}
      </div>
      <div className="composer-area"><form id="chat-form" onSubmit={send}>
        <textarea id="question" aria-label="질문" rows="2" placeholder="법령에 대해 질문해 주세요" maxLength={2000} minLength={2} required value={question} onChange={e => setQuestion(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); e.currentTarget.form.requestSubmit(); } }} />
        <div className="composer-bottom"><div className="model-picker"><label htmlFor="chat-model">모델</label><select id="chat-model" aria-label="답변 모델" disabled={busy} value={model} onChange={e => setModel(e.target.value)}>
          {!model && <option value="">서버 기본 모델</option>}
          {(modelInfo?.models || [status?.chat_model || 'gpt-4.1']).map(name => <option key={name} value={name}>{name}</option>)}
          <option value="custom">모델 ID 직접 입력</option>
        </select>{model === 'custom' && <input aria-label="모델 ID" placeholder="예: gpt-5.4" required pattern="[a-zA-Z0-9][a-zA-Z0-9._:\-]+" maxLength={100} value={customModel} disabled={busy} onChange={e => setCustomModel(e.target.value)} />}
        {modelInfo?.warning && <small>{modelInfo.warning}</small>}</div>
        <button id="send" type="submit" aria-label="질문 전송" disabled={busy}>↑</button></div>
      </form><small>기본은 현재 시행 중인 최신 수집 버전입니다. 과거 조회는 기준일을 지정하세요. 최신 개정 여부는 공식 원문을 확인하세요.</small></div>
    </main>
    {sourceGroup && <aside className="sources" aria-label="지식 출처" ref={sourcesRef}>
      <h2>지식 출처 <button aria-label="지식 출처 닫기" onClick={closeSources}>✕</button></h2>
      <strong className="source-law-name">{sourceGroup.name}</strong><p className="muted">관련 조문 {evidence.length}개 · {evidence.some(item => item.include_in_sources) ? '검색 근거·상하위 연결 포함' : '관련도 높은 순'}</p>
      {evidence.map((item,i) => <button key={`${item.version}-${item.number}-${i}`} className="source-card" aria-current={sourceGroup.citation === item.citation ? 'true' : undefined} onClick={() => onSource(item)}><strong><JusticeIcon />[{item.citation}] {item.number}{item.title ? `(${item.title})` : ''}</strong>
        <p className="source-relevance">{item.include_in_sources ? <b>상하위 연결 근거</b> : <>관련도 점수: <b>{((item.score || 0)*100).toFixed(1)}%</b></>}</p>
        {item.include_in_sources && <p>{item.referenced_by_law} {item.referenced_by}에서 연결 · 별도 검색 점수 없음</p>}
        <p className="source-effective">시행일: {dateText(item.effective).replaceAll('-', '.')}</p>
        <p>{item.evidence_role === 'supporting' ? '보조 근거' : '직접 근거'}{item.matched_units?.length ? ` · 검색 위치: ${item.matched_units.map(u => u.location).join(', ')}` : ''}</p><span className="source-open">원문 보기 ↗</span></button>)}
      <p className="score-note">관련도는 검색 점수이며 법적 정확도의 확률이 아닙니다.</p>
    </aside>}
  </div>;
}
