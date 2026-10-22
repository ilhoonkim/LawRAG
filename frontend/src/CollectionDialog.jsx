import React, { useEffect, useRef, useState } from 'react';
import { api, Badge, collectionItem, dateText, EmptyRow, Modal, Pagination, rowKey, selectable } from './shared';

export default function CollectionDialog({ initialQuery, initialKind, onClose, onQueued, refreshToken }) {
  const [query,setQuery] = useState(initialQuery);
  const [kind,setKind] = useState(initialKind);
  const [search,setSearch] = useState(null);
  const [candidates,setCandidates] = useState({names:[],total:0,page:1});
  const [selectedName,setSelectedName] = useState('');
  const [versions,setVersions] = useState({items:[],total:0,page:1});
  const [selected,setSelected] = useState(new Set());
  const [loading,setLoading] = useState(false);
  const [submitting,setSubmitting] = useState(false);
  const [message,setMessage] = useState('');
  const [plan,setPlan] = useState(null);
  const generation = useRef(0);
  const allRef = useRef(null);
  // 이미 수집한 버전도 상하위법 보완의 출발점으로 선택할 수 있습니다.
  const canSelect = r => r.kind === 'law' ? !['queued','running'].includes(r.collection_status) : selectable(r);
  const eligible = versions.items.filter(canSelect);
  const all = eligible.length > 0 && eligible.every(r => selected.has(rowKey(r)));
  useEffect(() => { if(allRef.current) allRef.current.indeterminate = selected.size > 0 && !all; }, [selected,all]);
  useEffect(() => () => { generation.current++; }, []);
  async function findNames(page=1, criteria=search) {
    const id=++generation.current;
    setPlan(null);
    setSearch(criteria);setLoading(true);setSelectedName('');setVersions({items:[],total:0,page:1});setSelected(new Set());setCandidates({names:[],total:0,page});setMessage('법령명을 검색하고 있습니다…');
    try {
      const result=await api(`/api/management/names?${new URLSearchParams({query:criteria.query,kind:criteria.kind,page:String(page),source:'api'})}`);
      if(id!==generation.current)return;
      setCandidates({names:[...new Set(result.items.map(r=>r.name))],total:result.total,page});
      setMessage(result.items.length?'법령명을 선택하면 개정 버전을 조회합니다.':'검색 결과가 없습니다.');
    } catch(e) {if(id===generation.current)setMessage(e.message);} finally {if(id===generation.current)setLoading(false);}
  }
  async function findVersions(name,page=1) {
    const id=++generation.current;
    setPlan(null);
    setLoading(true);setSelectedName(name);setSelected(new Set());setVersions({items:[],total:0,page});setMessage('개정 버전을 조회하고 있습니다…');
    try {
      const result=await api(`/api/management/revisions?${new URLSearchParams({query:name,kind:search.kind,page:String(page),source:'api'})}`);
      if(id!==generation.current)return;
      const items=result.items;
      const checked=new Set(items.filter(selectable).map(rowKey));
      setVersions({items,total:result.total,page});setSelected(checked);
      setMessage(checked.size?`이 페이지의 미수집 버전 ${checked.size}건을 선택했습니다. 상하위법 확인을 눌러 진행하세요.`:'이 페이지에 미수집 버전이 없습니다. 저장된 법령을 선택하면 상하위법을 보완할 수 있습니다.');
    } catch(e) {if(id===generation.current)setMessage(e.message);} finally {if(id===generation.current)setLoading(false);}
  }
  // Poll updates only remove unavailable selections; they never re-check items the user unchecked.
  useEffect(() => {
    if (!versions.items.length || loading) return;
    let stale=false;
    const id=generation.current;
    api('/api/management/revision-status',{items:versions.items.map(collectionItem)}).then(result=>{
      if(stale || id!==generation.current)return;
      const mapped=new Map(result.items.map(r=>[rowKey(r),r]));
      const items=versions.items.map(r=>({...r,...mapped.get(rowKey(r))}));
      setVersions(v=>({...v,items}));setSelected(prev=>new Set([...prev].filter(key=>items.some(r=>rowKey(r)===key&&canSelect(r)))));
    }).catch(e=>{if(!stale)setMessage(e.message);});
    return ()=>{stale=true;};
  },[refreshToken]);
  function toggle(row,checked) {setPlan(null);setSelected(prev=>{const next=new Set(prev);if(checked)next.add(rowKey(row));else next.delete(rowKey(row));return next;});}
  async function start() {
    const rows=versions.items.filter(r=>selected.has(rowKey(r))&&canSelect(r));
    if(!rows.length||submitting)return;
    setSubmitting(true);
    const id=generation.current;
    try {
      if(!plan){
        setMessage('상하위법 체계와 함께 수집할 버전을 확인하고 있습니다…');
        const preview=await api('/api/management/collection-plan',{items:rows.map(collectionItem)});
        if(id!==generation.current)return;
        setPlan(preview);setMessage(`전체 ${preview.items.length}개 버전 중 미수집 ${preview.new_count}개입니다. 아래 목록을 확인하고 함께 수집하세요.`);
        return;
      }
      const result=await api('/api/management/collections',{plan_id:plan.plan_id});
      if(id!==generation.current)return;
      setPlan(null);
      setSelected(new Set());setVersions(prev=>({...prev,items:prev.items.map(r=>rows.some(x=>rowKey(x)===rowKey(r))?{...r,collection_status:'queued'}:r)}));
      setMessage(result.queued?`${result.queued}건을 수집 대기열에 등록했습니다. 수집 이력에서 진행 상황을 확인하세요.`:'이미 저장된 버전을 재사용하고 상하위법 관계를 갱신했습니다.');onQueued();
    } catch(e) {if(id===generation.current){setPlan(null);setMessage(e.message);}} finally {if(id===generation.current)setSubmitting(false);}
  }
  return <Modal title="법령 수집" id="collection-dialog" className="collection-dialog" onClose={onClose}>
    <div className="collection-content"><ol className="collection-steps"><li>법령명 검색</li><li>개정 버전 조회</li><li>미수집 대상 선택·수집</li></ol>
      <form className="revision-toolbar" onSubmit={e=>{e.preventDefault();findNames(1,{query:query.trim(),kind});}}>
        <select aria-label="수집할 구분" value={kind} onChange={e=>setKind(e.target.value)} disabled={submitting}><option value="law">법령</option><option value="admrul">행정규칙</option></select>
        <input aria-label="수집할 법령명" placeholder="법령명을 입력하세요" minLength={2} maxLength={100} required value={query} onChange={e=>setQuery(e.target.value)} disabled={submitting}/><button type="submit" disabled={loading||submitting}>검색</button>
      </form>
      <div className="collection-candidates">{candidates.names.map(name=><button key={name} disabled={loading||submitting} onClick={()=>findVersions(name)}>{name} · 버전 조회</button>)}</div>
      <Pagination page={candidates.page} total={candidates.total} onChange={p=>findNames(p)} label="법령 검색 페이지" disabled={loading||submitting}/>
      <div className="collection-version-heading"><h3>{selectedName?`${selectedName} 개정 버전`:'개정 버전'}</h3><span className="table-total">개정 버전 {versions.total}건</span></div>
      <p className="collection-explanation">시행 예정은 제외합니다. 법령은 상하위 체계의 법률·시행령·시행규칙을 함께 확인합니다. 이미 수집한 버전도 선택하면 빠진 상하위법을 보완할 수 있습니다. 행정규칙은 자동으로 추가하지 않습니다.</p>
      <div className="table-scroll"><table className="law-table"><thead><tr><th><input ref={allRef} type="checkbox" aria-label="조회된 버전 모두 선택" checked={all} disabled={!eligible.length||submitting} onChange={e=>{setPlan(null);setSelected(new Set(e.target.checked?eligible.map(rowKey):[]));}}/></th><th>법령명</th><th>공포·발령일자</th><th>시행일자</th><th>수집 상태</th></tr></thead><tbody>
        {versions.items.map(row=><tr key={rowKey(row)}><td><input type="checkbox" aria-label={`${row.name} ${dateText(row.effective)} 수집`} disabled={!canSelect(row)||submitting} checked={selected.has(rowKey(row))} onChange={e=>toggle(row,e.target.checked)}/></td><td>{row.name}</td><td>{dateText(row.promulgated)}</td><td>{dateText(row.effective)}</td><td><Badge status={row.version?'completed':row.collection_status}/>{!row.version&&row.collection_status==='failed'&&<p className="collection-failure">{row.reason||'실패 사유가 기록되지 않았습니다. 수집 이력을 확인하세요.'}</p>}</td></tr>)}
        {!versions.items.length&&<EmptyRow columns={5}>{loading?'조회 중입니다…':selectedName?'조회된 개정 버전이 없습니다.':'법령명을 검색하고 버전을 조회하세요.'}</EmptyRow>}
      </tbody></table></div>
      <Pagination page={versions.page} total={versions.total} onChange={p=>findVersions(selectedName,p)} label="수집할 버전 페이지" disabled={loading||submitting}/>
      {plan&&<section aria-label="상하위법 동반 수집 목록"><h3>함께 수집할 법령</h3>
        <p className="muted">체계도에 제공된 버전을 우선 사용합니다. 누락되거나 선택 기간에 맞지 않는 버전만 연혁으로 보완합니다. 저장된 버전은 다시 수집하지 않습니다.</p>
        {plan.warnings.map(w=><p key={w} className="collection-failure">{w}</p>)}
        <div className="table-scroll"><table className="law-table"><thead><tr><th>법령명</th><th>공포일</th><th>시행일</th><th>처리</th></tr></thead><tbody>{plan.items.map(r=><tr key={`${r.kind}-${r.mst}-${r.effective}`}><td>{r.name}{r.requested?' (선택)':''}</td><td>{dateText(r.promulgated)}</td><td>{dateText(r.effective)}</td><td>{r.collected?'저장됨 · 건너뛰기':'수집 예정'}</td></tr>)}</tbody></table></div>
      </section>}
      <p id="collection-status" role="status">{message}</p><div className="collection-footer"><span className="muted">본문 수집 후 제목·본문 임베딩을 생성합니다.</span><span>{selected.size}건 선택</span><button className="primary-button" disabled={!selected.size||loading||submitting} onClick={start}>{submitting?'처리 중…':plan?'함께 수집 시작':'상하위법 확인'}</button></div>
    </div>
  </Modal>;
}
