import React, { useEffect, useState } from 'react';
import CollectionDialog from './CollectionDialog';
import { api, Badge, dateText, EmptyRow, kindLabel, Modal, Pagination, statusLabels, timeText } from './shared';

function HistoryDialog({ onClose, refreshToken, onPending }) {
  const [page,setPage]=useState(1);
  const [data,setData]=useState({items:[],total:0,last_completed:null});
  const [error,setError]=useState('');
  const [loading,setLoading]=useState(true);
  useEffect(()=>{let stale=false;setLoading(true);api(`/api/management/history?page=${page}`).then(result=>{if(!stale){setData(result);setError('');onPending(result.pending>0);}}).catch(e=>{if(!stale)setError(e.message);}).finally(()=>{if(!stale)setLoading(false);});return()=>{stale=true;};},[page,refreshToken,onPending]);
  return <Modal title="수집 이력" id="history-dialog" className="history-dialog" onClose={onClose}><div className="history-content">
    <div className="last-collection"><span aria-hidden="true">▤</span><strong>마지막 수집 완료 일시</strong><span>{timeText(data.last_completed)}</span></div>
    <p role="status">{error}</p><div className="table-scroll"><table className="law-table"><thead><tr><th>수집일시</th><th>구분</th><th>법령명</th><th>공포·발령일자</th><th>시행일자</th><th>수집 상태</th><th>처리 사유</th></tr></thead><tbody>
      {data.items.map(row=><tr key={row.id}><td>{timeText(row.finished_at||row.started_at||row.requested_at)}</td><td>{kindLabel(row.kind)}</td><td>{row.name}</td><td>{dateText(row.promulgated)}</td><td>{dateText(row.effective)}</td><td><Badge status={row.status}/></td><td className="failure-reason">{row.reason||'—'}{row.hierarchy_status&&<p>{row.hierarchy_status==='completed'?'상하위법 함께 수집 완료':`상하위법 수집 미완료 · 미수집 ${row.hierarchy_missing}건`}</p>}{row.hierarchy_warnings?.map(w=><p key={w}>{w}</p>)}</td></tr>)}
      {!data.items.length&&<EmptyRow columns={7}>{loading?'이력을 불러오는 중입니다…':'수집 이력이 없습니다.'}</EmptyRow>}
    </tbody></table></div><Pagination page={page} total={data.total} onChange={setPage} label="수집 이력 페이지" disabled={loading}/><div className="dialog-footer"><button className="primary-button" onClick={onClose}>확인</button></div>
  </div></Modal>;
}
function VersionDialog({ row, onClose, onArticles }) {
  const [indexing,setIndexing]=useState(false);
  const [indexMessage,setIndexMessage]=useState('');
  async function reindex(){setIndexing(true);setIndexMessage('제목·본문 임베딩을 생성하고 있습니다…');try{await api(`/api/management/versions/${row.version}/reindex`,{});setIndexMessage('제목·본문 재색인이 완료되었습니다.');}catch(e){setIndexMessage(e.message);}finally{setIndexing(false);}}
  const entries=[['법령명',row.name],['구분',kindLabel(row.kind)],['공포·발령일',dateText(row.promulgated)],['시행일',dateText(row.effective)],['수집일',timeText(row.collected_at)],['식별자',row.id||'기존 수집 자료'],['수집 상태',statusLabels[row.collection_status]],['실패 사유',row.reason||'—']];
  return <Modal title="버전 상세" className="version-dialog" onClose={onClose}><div className="version-content"><dl id="version-metadata">{entries.map(([label,value])=><React.Fragment key={label}><dt>{label}</dt><dd>{value}</dd></React.Fragment>)}</dl>
    <p>{row.active?'현재 기본 검색에 사용하는 버전입니다.':''}</p><div className="version-actions"><button disabled={!row.version} onClick={()=>{onArticles(row);onClose();}}>조문 보기</button>{row.version&&<a href={`/documents/${row.version}.html`} target="_blank" rel="noopener noreferrer">HTML 원문 ↗</a>}</div>
    <p role="status">{indexMessage}</p><button disabled={!row.version||indexing} onClick={reindex}>{indexing?'재색인 중…':'제목·본문 재색인'}</button><p className="muted">재색인은 저장된 XML을 사용하며 임베딩 API 비용이 발생합니다.</p>
    <p className="muted">기본 검색은 현재 시행 중인 최신 수집 버전을 사용합니다. 과거 기준일은 질문에 YYYY-MM-DD 형식으로 지정하세요. 미조회 개정 여부는 공식 원문 확인이 필요합니다.</p>
  </div></Modal>;
}
export default function LawManagement({ visible, onSource, onStatusChange }) {
  const [categories,setCategories]=useState([]);
  const [categoryError,setCategoryError]=useState('');
  const [query,setQuery]=useState('');
  const [kind,setKind]=useState('law');
  const [filter,setFilter]=useState({query:'',kind:'law'});
  const [page,setPage]=useState(1);
  const [revisions,setRevisions]=useState({items:[],total:0});
  const [loading,setLoading]=useState(false);
  const [message,setMessage]=useState('');
  const [selected,setSelected]=useState(null);
  const [articlePage,setArticlePage]=useState(1);
  const [articles,setArticles]=useState(null);
  const [articleError,setArticleError]=useState('');
  const [articleLoading,setArticleLoading]=useState(false);
  const [collectionOpen,setCollectionOpen]=useState(false);
  const [historyOpen,setHistoryOpen]=useState(false);
  const [detail,setDetail]=useState(null);
  const [tick,setTick]=useState(0);
  const [watch,setWatch]=useState(false);
  useEffect(()=>{let stale=false;api('/api/management/categories').then(r=>{if(!stale)setCategories(r.items);}).catch(e=>{if(!stale)setCategoryError(e.message);});api('/api/management/history').then(r=>{if(!stale)setWatch(r.pending>0);}).catch(e=>{if(!stale)setMessage(e.message);});return()=>{stale=true;};},[tick]);
  useEffect(()=>{
    const controller=new AbortController();setLoading(true);
    api(`/api/management/revisions?${new URLSearchParams({query:filter.query,kind:filter.kind,page:String(page),source:'collected'})}`,undefined,{signal:controller.signal}).then(r=>{setRevisions(r);}).catch(e=>{if(e.name!=='AbortError'){setRevisions({items:[],total:0});setMessage(e.message);}}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[filter,page,tick]);
  useEffect(()=>{
    if(!selected?.version){setArticles(null);return;}
    const controller=new AbortController();setArticleLoading(true);setArticleError('');setArticles(null);
    api(`/api/management/versions/${selected.version}/articles?page=${articlePage}`,undefined,{signal:controller.signal}).then(setArticles).catch(e=>{if(e.name!=='AbortError')setArticleError(e.message);}).finally(()=>{if(!controller.signal.aborted)setArticleLoading(false);});
    return()=>controller.abort();
  },[selected,articlePage]);
  useEffect(()=>{
    if(!watch)return;
    let stale=false,timer;
    async function poll(){try{const h=await api('/api/management/history');if(stale)return;setTick(t=>t+1);if(h.pending){timer=setTimeout(poll,1500);}else{setWatch(false);setMessage('수집 작업이 종료되었습니다. 수집 이력에서 성공·실패 결과를 확인하세요.');onStatusChange();}}catch(e){if(!stale){setWatch(false);setMessage(`진행 상태 확인 실패: ${e.message} 수집 이력에서 다시 확인하세요.`);}}}
    timer=setTimeout(poll,1200);return()=>{stale=true;clearTimeout(timer);};
  },[watch,onStatusChange]);
  function search(next={query:query.trim(),kind}) {setFilter(next);setPage(1);setSelected(null);setMessage('');}
  function select(row){setSelected(row);setArticlePage(1);}
  return <section className="laws-page" hidden={!visible} aria-label="법령관리">
    <aside className="category-panel"><div className="category-heading"><h2>카테고리</h2></div>
      {categoryError&&<p id="category-error" role="status">{categoryError}</p>}
      {['law','admrul'].map(type=><details key={type} open><summary>{kindLabel(type)}</summary><nav aria-label={`${kindLabel(type)} 카테고리`}>{categories.filter(c=>c.kind===type).map(c=><button key={c.id} className={`category-item ${filter.kind===type&&filter.query===c.name?'selected':''}`} onClick={()=>{setKind(type);setQuery(c.name);search({query:c.name,kind:type});}}>{c.name}</button>)}{!categories.some(c=>c.kind===type)&&<p className="category-empty">등록된 항목이 없습니다.</p>}</nav></details>)}
      <p className="category-help">수집한 법령의 이름을 선택하면 개정 연혁을 확인할 수 있습니다.</p>
    </aside>
    <div className="management-workspace"><section className="revision-panel" aria-labelledby="revisions-title"><div className="panel-heading"><h1 id="revisions-title">{filter.query||kindLabel(filter.kind)} 개정 연혁</h1><div className="panel-actions"><button id="open-collection" onClick={()=>setCollectionOpen(true)}>수집하기</button><button onClick={()=>setHistoryOpen(true)}>수집 이력</button></div></div>
      <form className="revision-toolbar" onSubmit={e=>{e.preventDefault();search();}}><select aria-label="법령 구분" value={kind} onChange={e=>setKind(e.target.value)}><option value="law">법령</option><option value="admrul">행정규칙</option></select><input placeholder="법령명을 입력하세요" aria-label="검색할 법령명" maxLength={100} value={query} onChange={e=>setQuery(e.target.value)}/><button type="submit" disabled={loading}>검색</button><button type="button" onClick={()=>{setQuery('');setKind('law');search({query:'',kind:'law'});}}>초기화</button><span className="table-total">총 {revisions.total}건</span></form>
      <p className="management-status" role="status">{message}</p><div className="table-scroll"><table className="law-table revisions-table"><thead><tr><th>법령명</th><th>공포·발령일자</th><th>시행일자</th><th>수집일자</th><th>수집 상태</th><th>상세</th></tr></thead><tbody>
        {revisions.items.map(row=><tr key={row.version} className={selected?.version===row.version?'selected-row':''}><td><button className="row-name" onClick={()=>select(row)}>{row.name}</button>{row.active&&<span className="active-version">기본 검색</span>}</td><td>{dateText(row.promulgated)}</td><td>{dateText(row.effective)}</td><td>{timeText(row.collected_at)}</td><td><Badge status={row.collection_status}/></td><td><button className="more-button" aria-label={`${row.name} 버전 상세`} onClick={()=>setDetail(row)}>⋮</button></td></tr>)}
        {!revisions.items.length&&<EmptyRow columns={6}>{loading?'목록을 불러오는 중입니다…':'수집한 연혁이 없습니다. 수집하기에서 법령을 검색해 주세요.'}</EmptyRow>}
      </tbody></table></div><Pagination page={page} total={revisions.total} onChange={p=>{setPage(p);setSelected(null);}} label="개정 연혁 페이지" disabled={loading}/>
    </section>
    <section className="articles-panel" aria-labelledby="articles-title"><div className="panel-heading"><h2 id="articles-title">조문 목록</h2><span className="table-total">총 {articles?.total||0}건</span></div>
      <div className="article-meta">{articles?`${articles.law.name}　공포·발령일 ${dateText(articles.law.promulgated)}　시행일 ${dateText(articles.law.effective)}${articles.law.active?'　· 기본 검색 버전':''}`:'개정 연혁에서 수집된 버전을 선택하세요.'}</div>
      <div className="table-scroll"><table className="law-table articles-table"><thead><tr><th>법령명</th><th>편·장·절</th><th>조문</th><th>조문 내용</th></tr></thead><tbody>
        {articles?.items.map(article=><tr key={article.section_id}><td>{articles.law.name}</td><td>{article.chapter||'—'}</td><td><button className="row-name" onClick={()=>onSource({...article,law_name:articles.law.name})}>{article.number}</button></td><td><div className="article-excerpt">{article.body}</div></td></tr>)}
        {!articles?.items.length&&<EmptyRow columns={4}>{articleError|| (articleLoading?'조문을 불러오는 중입니다…':'수집된 버전을 선택하면 조문을 확인할 수 있습니다.')}</EmptyRow>}
      </tbody></table></div><Pagination page={articlePage} total={articles?.total||0} onChange={setArticlePage} label="조문 페이지" disabled={articleLoading}/>
    </section><p className="management-note">기본 검색은 현재 시행 중인 최신 수집 버전을 자동 선택합니다. 과거 조회는 질문에 기준일을 지정하세요. 임베딩 수집에는 API 비용이 발생할 수 있습니다.</p>
    </div>
    {collectionOpen&&<CollectionDialog initialQuery={filter.query} initialKind={filter.kind} onClose={()=>setCollectionOpen(false)} onQueued={()=>{setWatch(true);setTick(t=>t+1);}} refreshToken={tick}/>}
    {historyOpen&&<HistoryDialog onClose={()=>setHistoryOpen(false)} refreshToken={tick} onPending={setWatch}/>}
    {detail&&<VersionDialog row={detail} onClose={()=>setDetail(null)} onArticles={select}/>}
  </section>;
}
