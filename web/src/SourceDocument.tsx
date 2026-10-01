import { createElement, Fragment } from 'react';
import type { ReactNode } from 'react';
import type { SourceNode } from './types';

const tags = new Set([
  'div',
  'p',
  'strong',
  'em',
  'ul',
  'ol',
  'li',
  'br',
  'table',
  'thead',
  'tbody',
  'tr',
  'th',
  'td',
  'blockquote',
  'sup',
  'sub',
]);

function renderNodes(nodes: SourceNode[], depth = 0): ReactNode {
  if (depth > 60) return null;
  return nodes.map((node, index) => {
    if (node.type === 'text') return node.text ?? '';
    const children = renderNodes(node.children ?? [], depth + 1);
    if (/^h[1-6]$/.test(node.type)) return <h3 key={index}>{children}</h3>;
    if (node.type === 'a') {
      let href: string | undefined;
      try {
        const url = new URL(node.href ?? '');
        if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password)
          href = url.href;
      } catch {
        /* Keep label, without an unsafe/broken link. */
      }
      return href ? (
        <a key={index} href={href} target="_blank" rel="noopener noreferrer">
          {children}
        </a>
      ) : (
        <Fragment key={index}>{children}</Fragment>
      );
    }
    if (node.type === 'br') return <br key={index} />;
    if (tags.has(node.type))
      return createElement(
        node.type,
        {
          key: index,
          ...(node.type === 'ol' ? { start: node.start } : {}),
          ...(node.type === 'li' ? { value: node.value } : {}),
          ...(['td', 'th'].includes(node.type)
            ? { colSpan: node.colspan, rowSpan: node.rowspan }
            : {}),
        },
        children,
      );
    return <Fragment key={index}>{children}</Fragment>;
  });
}

export default function SourceDocument({
  nodes,
  fallback,
}: {
  nodes?: SourceNode[];
  fallback: string;
}) {
  return (
    <div className="source-document">
      {nodes?.length ? renderNodes(nodes) : <div className="source-plain">{fallback}</div>}
    </div>
  );
}
