import React, { useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import Chat from './Chat';
import LawManagement from './LawManagement';
import { api, Modal } from './shared';
import 'pretendard/dist/web/variable/pretendardvariable.css';
import { ChatIcon, LawIcon, HomeIcon } from './Icons';
import './style.css';

function sourceLoaded(event) {
  const frame = event.currentTarget;
  const win = frame.contentWindow;
  function syncToc() {
    const current = win.location.hash;
    frame.contentDocument.querySelectorAll('.toc a').forEach(link => {
      if (link.hash === current) {
        link.setAttribute('aria-current', 'location');
        link.scrollIntoView({block:'nearest'});
      } else link.removeAttribute('aria-current');
    });
  }
  syncToc();
  win.addEventListener('hashchange', syncToc);
}

function App() {
  const [page,setPage]=useState(location.hash==='#laws'?'laws':'chat');
  const [visitedManagement,setVisitedManagement]=useState(location.hash==='#laws');
  const [status,setStatus]=useState(null);
  const [source,setSource]=useState(null);
  const [error,setError]=useState('');
  const refreshStatus=useCallback(async()=>{try{setStatus(await api('/api/status'));setError('');}catch(e){setError(e.message);}},[]);
  useEffect(()=>{refreshStatus();},[refreshStatus]);
  useEffect(()=>{const change=()=>{const next=location.hash==='#laws'?'laws':'chat';setPage(next);if(next==='laws')setVisitedManagement(true);};window.addEventListener('hashchange',change);return()=>window.removeEventListener('hashchange',change);},[]);
  return <>
    <nav className="app-nav" aria-label="주 메뉴"><a className="app-logo" href="#chat" aria-label="LawRAG 홈">L<span>R</span></a><a className={`app-menu ${page==='laws'?'selected':''}`} href="#laws" aria-current={page==='laws'?'page':undefined}><LawIcon/>법령관리</a><a className={`app-menu ${page==='chat'?'selected':''}`} href="#chat" aria-current={page==='chat'?'page':undefined}><ChatIcon/>채팅</a></nav>
    <div className="app-shell"><div className="app-topbar"><HomeIcon/><span className="breadcrumb-divider">/</span><strong>LawRAG</strong><span className="breadcrumb-divider">/</span><span>{page==='laws'?'법령 API 수집 관리':'채팅'}</span>{error&&<span role="status">{error}</span>}</div>
      <Chat visible={page==='chat'} status={status} onSource={setSource}/>
      {visitedManagement&&<LawManagement visible={page==='laws'} onSource={setSource} onStatusChange={refreshStatus}/>}
    </div>
    {source&&<Modal title={`${source.law_name} ${source.number} ${source.title||''}`} id="source-dialog" onClose={()=>setSource(null)}><iframe onLoad={sourceLoaded} title="법령 원문 조문" src={source.html_url} sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"/></Modal>}
  </>;
}
createRoot(document.getElementById('root')).render(<App/>);
