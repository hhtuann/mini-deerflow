import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { acceptedUrl } from '../links';

export default function SafeMarkdown({ text, allowed }: { text: string; allowed: readonly string[] }) {
  return <div className="markdown"><Markdown remarkPlugins={[remarkGfm]} skipHtml disallowedElements={['img', 'iframe', 'script', 'style']} urlTransform={(url) => acceptedUrl(url, allowed) ? url : ''}
    components={{ table: ({ children }) => <div className="markdown-table" role="region" aria-label="Comparison table" tabIndex={0}><table>{children}</table></div>, h1: ({ children }) => <h2>{children}</h2>, h2: ({ children }) => <h3>{children}</h3>, h3: ({ children }) => <h4>{children}</h4>, a: ({ href, children }) => href && acceptedUrl(href, allowed) ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span> }}
  >{text}</Markdown></div>;
}
