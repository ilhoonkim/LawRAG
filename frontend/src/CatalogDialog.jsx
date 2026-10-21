import React, { useEffect, useState } from 'react';
import { api, Badge, EmptyRow, kindLabel, Modal } from './shared';

export default function CatalogDialog({ onClose, onQueued, refreshToken, onSelect }) {
  const [items,setItems]=useState([]);
  const [submitting,setSubmitting]=useState(false);
  const [loading,setLoading]=useState(true);
  const [message,setMessage]=useState('');
  const pending=items.some(i=>['queued','running'].includes(i.status));
  useEffect(()=>{
    let stale=false;
    api('/api/management/catalog/real-estate').then(result=>{if(!stale){setItems(result.items);setLoading(false);}}).catch(e=>{if(!stale){setMessage(e.message);setLoading(false);}});
    return()=>{stale=true;};
  },[refreshToken]);
  async function collect(){
    if(submitting||pending)return;
    setSubmitting(true);
    try{
      const result=await api('/api/management/catalog/real-estate/collect',{});
      setMessage(`${result.queued}개 항목의 조회·수집을 시작했습니다. 이미 저장된 버전은 건너뛰며, 항목별 실패 사유는 이력에 남습니다.`);
      onQueued();
    }catch(e){setMessage(e.message);}finally{setSubmitting(false);}
  }
  return <Modal title="부동산 거래 관련 지정 목록" className="catalog-dialog" onClose={onClose}>
    <div className="catalog-content">
      <div className="catalog-summary"><strong>중복 제거 · 26개</strong><span>법령·법규명령 24개 / 등기 관련 지침 2개</span></div>
      <p className="muted">이 목록은 사용자가 지정한 원래 명칭입니다. 일괄 수집은 각 항목의 API 연혁을 조회해 시행일이 오늘 이하인 최신 버전 1개를 저장합니다. 과거 명칭의 현행 여부는 별도로 확인해야 하며, 다른 명칭으로 자동 대체하지 않습니다.</p>
      <div className="table-scroll"><table className="law-table catalog-table"><thead><tr><th>번호</th><th>구분</th><th>명칭</th><th>저장 버전</th><th>처리 상태</th><th>비고·처리 사유</th></tr></thead><tbody>
        {items.map((item,i)=><tr key={`${item.kind}:${item.name}`}><td>{i+1}</td><td>{kindLabel(item.kind)}</td><td><button className="row-name" onClick={()=>onSelect(item)}>{item.name}</button></td><td>{item.collected_versions}</td><td><Badge status={item.status}/></td><td className="catalog-note">{item.reason||item.note||'—'}</td></tr>)}
        {!items.length&&<EmptyRow columns={6}>{loading?'지정 목록을 불러오는 중입니다…':'목록을 불러오지 못했습니다.'}</EmptyRow>}
      </tbody></table></div>
      <p className="catalog-status" role="status">{message}</p><div className="catalog-footer"><span className="muted">수집 후 제목·본문 임베딩을 생성합니다.</span><button className="primary-button" disabled={loading||submitting||pending||!items.length} onClick={collect}>{submitting?'수집 요청 중…':pending?'일괄 수집 진행 중…':'26개 조회·수집 시작'}</button></div>
    </div>
  </Modal>;
}
