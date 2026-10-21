import React, { createContext, useContext } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import remarkCjkFriendly from 'remark-cjk-friendly';

// Only plain-text citations with an actual source become controls. Leave code,
// existing links and unknown numbers alone; never rewrite the Markdown source.
function remarkCitations({ evidence }) {
  return tree => {
    function walk(node) {
      if (!node.children || ['link', 'linkReference', 'code', 'inlineCode', 'html'].includes(node.type)) return;
      node.children = node.children.flatMap(child => {
        if (child.type !== 'text') { walk(child); return [child]; }
        const parts = [];
        let end = 0;
        for (const match of child.value.matchAll(/\[([1-9]\d*)\]/g)) {
          const number = Number(match[1]);
          const source = evidence[number - 1];
          if (!source) continue;
          if (match.index > end) parts.push({ type: 'text', value: child.value.slice(end, match.index) });
          // Historical answers may have cited linked-only context. Hide those
          // markers without renumbering other citations in the saved answer.
          if (!source.relation) parts.push({ type: 'link', url: `#evidence-${number}`,
            data: { hProperties: { 'data-citation': number } },
            children: [{ type: 'text', value: match[0] }] });
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
  const { evidence, onCitationClick } = useContext(CitationContext);
  const number = Number(props['data-citation']);
  const source = evidence[number - 1];
  if (source) {
    const label = `${source.law_name} ${source.number}${source.title ? `(${source.title})` : ''}`;
    return <button type="button" className="citation-link" title={label}
      aria-label={`근거 ${number}: ${label} 보기`}
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

export default function AnswerMarkdown({ content, evidence = [], onCitationClick }) {
  return <CitationContext.Provider value={{ evidence, onCitationClick }}>
    <div className="answer-text markdown-body">
      <Markdown skipHtml disallowedElements={['img']} components={components}
        remarkPlugins={[remarkGfm, remarkCjkFriendly, remarkBreaks, [remarkCitations, { evidence }]]}
      >{content}</Markdown>
    </div>
  </CitationContext.Provider>;
}
