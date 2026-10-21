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
  const generation = useRef(0);
  const allRef = useRef(null);
  const eligible = versions.items.filter(selectable);
  const all = eligible.length > 0 && eligible.every(r => selected.has(rowKey(r)));
  useEffect(() => { if(allRef.current) allRef.current.indeterminate = selected.size > 0 && !all; }, [selected,all]);
  useEffect(() => () => { generation.current++; }, []);
  async function findNames(page=1, criteria=search) {
    const id=++generation.current;
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
    setLoading(true);setSelectedName(name);setSelected(new Set());setVersions({items:[],total:0,page});setMessage('개정 버전을 조회하고 있습니다…');
    try {
      const result=await api(`/api/management/revisions?${new URLSearchParams({query:name,kind:search.kind,page:String(page),source:'api'})}`);
      if(id!==generation.current)return;
      const items=result.items;
      const checked=new Set(items.filter(selectable).map(rowKey));
      setVersions({items,total:result.total,page});setSelected(checked);
      setMessage(checked.size?`이 페이지의 미수집 버전 ${checked.size}건을 선택했습니다. 수집 시작을 눌러 진행하세요.`:'이 페이지에 미수집 버전이 없습니다. 다른 페이지도 확인할 수 있습니다.');
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
      setVersions(v=>({...v,items}));setSelected(prev=>new Set([...prev].filter(key=>items.some(r=>rowKey(r)===key&&selectable(r)))));
    }).catch(e=>{if(!stale)setMessage(e.message);});
    return ()=>{stale=true;};
  },[refreshToken]);
  function toggle(row,checked) {setSelected(prev=>{const next=new Set(prev);if(checked)next.add(rowKey(row));else next.delete(rowKey(row));return next;});}
  async function start() {
    const rows=versions.items.filter(r=>selected.has(rowKey(r))&&selectable(r));
    if(!rows.length||submitting)return;
    setSubmitting(true);
    try {
      const result=await api('/api/management/collections',{items:rows.map(collectionItem)});
      setSelected(new Set());setVersions(prev=>({...prev,items:prev.items.map(r=>rows.some(x=>rowKey(x)===rowKey(r))?{...r,collection_status:'queued'}:r)}));
      setMessage(`${result.queued}건을 수집 대기열에 등록했습니다. 수집 이력에서 진행 상황을 확인하세요.`);onQueued();
    } catch(e) {setMessage(e.message);} finally {setSubmitting(false);}
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
      <p className="collection-explanation">현재 페이지의 미수집 버전만 자동 체크됩니다. 페이지를 바꾸면 선택이 초기화됩니다. 필요한 대상만 선택하세요.</p>
      <div className="table-scroll"><table className="law-table"><thead><tr><th><input ref={allRef} type="checkbox" aria-label="조회된 미수집 버전 모두 선택" checked={all} disabled={!eligible.length||submitting} onChange={e=>setSelected(new Set(e.target.checked?eligible.map(rowKey):[]))}/></th><th>법령명</th><th>공포·발령일자</th><th>시행일자</th><th>수집 상태</th></tr></thead><tbody>
        {versions.items.map(row=><tr key={rowKey(row)}><td><input type="checkbox" aria-label={`${row.name} ${dateText(row.effective)} 수집`} disabled={!selectable(row)||submitting} checked={selected.has(rowKey(row))} onChange={e=>toggle(row,e.target.checked)}/></td><td>{row.name}</td><td>{dateText(row.promulgated)}</td><td>{dateText(row.effective)}</td><td><Badge status={row.version?'completed':row.collection_status}/></td></tr>)}
        {!versions.items.length&&<EmptyRow columns={5}>{loading?'조회 중입니다…':selectedName?'조회된 개정 버전이 없습니다.':'법령명을 검색하고 버전을 조회하세요.'}</EmptyRow>}
      </tbody></table></div>
      <Pagination page={versions.page} total={versions.total} onChange={p=>findVersions(selectedName,p)} label="수집할 버전 페이지" disabled={loading||submitting}/>
      <p id="collection-status" role="status">{message}</p><div className="collection-footer"><span className="muted">본문 수집 후 제목·본문 임베딩을 생성합니다.</span><span>{selected.size}건 선택</span><button className="primary-button" disabled={!selected.size||loading||submitting} onClick={start}>{submitting?'등록 중…':'수집 시작'}</button></div>
    </div>
  </Modal>;
}
