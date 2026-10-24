import { useEffect, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';

export function Status({ value }: { value: string }) {
  return <span className={`status status-${value}`}><span aria-hidden="true">{['failed', 'error'].includes(value) ? '×' : ['completed', 'success'].includes(value) ? '✓' : '•'}</span> {value.replaceAll('_', ' ')}</span>;
}
export function Empty({ children }: { children: ReactNode }) { return <p className="empty-detail">{children}</p>; }
export function Fields({ values }: { values: Record<string, string | number | boolean | null | undefined> }) {
  return <dl className="fields">{Object.entries(values).filter(([, value]) => value != null).map(([label, value]) => <div key={label}><dt>{label.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>)}</dl>;
}
export function Drawer({ title, children, close }: { title: string; children: ReactNode; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    const dialog = ref.current;
    dialog?.showModal();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { dialog?.close(); document.body.style.overflow = previousOverflow; before?.focus(); };
  }, []);
  return <dialog ref={ref} className="drawer" aria-label={title} onCancel={(event) => { event.preventDefault(); close(); }} onClick={(event) => { if (event.target === event.currentTarget) close(); }}>
    <div className="drawer-header"><h2>{title}</h2><button onClick={close} aria-label={`Close ${title}`} autoFocus><X aria-hidden="true" /></button></div>{children}
  </dialog>;
}
