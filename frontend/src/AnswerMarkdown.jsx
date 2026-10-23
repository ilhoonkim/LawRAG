import React, { createContext, useContext } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import remarkCjkFriendly from 'remark-cjk-friendly';

// Only plain-text citations with an actual source become controls. Leave code,
// existing links and unknown numbers alone; never rewrite the Markdown source.
function remarkCitations({ evidence, linkedSources }) {
  return tree => {
    function walk(node) {
      if (!node.children || ['link', 'linkReference', 'code', 'inlineCode', 'html'].includes(node.type)) return;
      node.children = node.children.flatMap(child => {
        if (child.type !== 'text') { walk(child); return [child]; }
        const parts = [];
        let end = 0;
        for (const match of child.value.matchAll(/\[(C?[1-9]\d*)\]/g)) {
          const linked = match[1].startsWith('C');
          const number = linked ? match[1] : Number(match[1]);
          const source = linked ? linkedSources[number] : evidence[number - 1];
          if (!source) continue;
          if (linked && !source.html_url) continue;
          if (match.index > end) parts.push({ type: 'text', value: child.value.slice(end, match.index) });
          // Historical answers may have cited linked-only context. Hide those
          // markers without renumbering other citations in the saved answer.
          if (linked || !source.relation || source.include_in_sources) parts.push({ type: 'link', url: `#evidence-${number}`,
            data: { hProperties: { 'data-citation': number } },
            children: [{ type: 'text', value: linked ? source.number : match[0] }] });
          end = match.index + match[0].length;
        }
        if (end < child.value.length) parts.push({ type: 'text', value: child.value.slice(end) });
        return parts;
      });
    }
    walk(tree);
  };
}

const CitationContext = createContext({ evidence: [] });

function MarkdownLink({ node, children, href, title, ...props }) {
  const { evidence, linkedSources, onCitationClick } = useContext(CitationContext);
  const marker = String(props['data-citation']);
  const linked = /^C[1-9]\d*$/.test(marker);
  const number = linked ? marker : Number(marker);
  const source = linked ? linkedSources[number] : evidence[number - 1];
  if (source) {
    const label = `${source.law_name} ${source.number}${source.title ? `(${source.title})` : ''}`;
    return <button type="button" className="citation-link" title={label}
      aria-label={linked ? `참조 자료: ${label} 보기` : `근거 ${number}: ${label} 보기`}
      onClick={event => onCitationClick(number, event.currentTarget)}>{children}</button>;
  }
  return <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a>;
}

function MarkdownTable({ node, children, ...props }) {
  return <div className="markdown-table-wrap" tabIndex={0} role="region" aria-label="답변 표"><table {...props}>{children}</table></div>;
}

// Keep element types stable so opening/closing a source doesn't replace the
// citation button and lose the user's keyboard focus.
const components = { a: MarkdownLink, table: MarkdownTable };

export default function AnswerMarkdown({ content, evidence = [], linkedSources = {}, onCitationClick }) {
  return <CitationContext.Provider value={{ evidence, linkedSources, onCitationClick }}>
    <div className="answer-text markdown-body">
      <Markdown skipHtml disallowedElements={['img']} components={components}
        remarkPlugins={[remarkGfm, remarkCjkFriendly, remarkBreaks, [remarkCitations, { evidence, linkedSources }]]}
      >{content}</Markdown>
    </div>
  </CitationContext.Provider>;
}
