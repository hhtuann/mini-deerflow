import { useEffect, useRef, useState, useSyncExternalStore, type CSSProperties, type KeyboardEvent, type PointerEvent } from 'react';
import { onlineManager, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowUp, FlaskConical, Menu, MessageSquare, PanelLeftClose, PanelRight, Plus, Presentation as PresentationIcon, RefreshCw, Sprout } from 'lucide-react';
import { activeJob, ApiError, conversationPath, request } from './api';
import type { ConversationSummary, JobDTO, Mode, SkillsDTO } from './types';
import { jobVersion, loadSessionSnapshot } from './session';
import SafeMarkdown from './components/SafeMarkdown';
import Inspector from './components/Inspector';
import { clampInspectorWidth, inspectorMaxWidth, INSPECTOR_DEFAULT_WIDTH, INSPECTOR_MIN_WIDTH } from './inspectorSizing';
import PresentationWorkspace from './components/Presentation';
import { Drawer, Status } from './components/UI';

const subscribeOnline = (notify: () => void) => onlineManager.subscribe(notify);
const isOnline = () => onlineManager.isOnline();

function locationState(): { mode: Mode; thread?: string } {
  const query = new URLSearchParams(window.location.search);
  return { mode: query.get('mode') === 'live' ? 'live' : 'offline', thread: query.get('thread') || undefined };
}

export default function App() {
  const [location, setLocation] = useState(locationState);
  useEffect(() => { const update = () => setLocation(locationState()); window.addEventListener('popstate', update); return () => window.removeEventListener('popstate', update); }, []);
  function navigate(mode: Mode, thread?: string) {
    const query = new URLSearchParams({ mode }); if (thread) query.set('thread', thread);
    window.history.pushState(null, '', `?${query}`); setLocation({ mode, thread });
  }
  return <Workspace key={location.mode} mode={location.mode} thread={location.thread} navigate={navigate} />;
}

function Workspace({ mode, thread, navigate }: { mode: Mode; thread?: string; navigate: (mode: Mode, thread?: string) => void }) {
  const client = useQueryClient();
  const online = useSyncExternalStore(subscribeOnline, isOnline);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [drawer, setDrawer] = useState<'navigation' | 'inspector' | 'presentation' | null>(null);
  const [presentationTurn, setPresentationTurn] = useState<string>();
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(INSPECTOR_DEFAULT_WIDTH);
  const resizeStart = useRef<{ x: number; width: number } | null>(null);
  useEffect(() => {
    const clampOnResize = () => setInspectorWidth((value) => clampInspectorWidth(value, window.innerWidth, sidebarOpen));
    window.addEventListener('resize', clampOnResize);
    clampOnResize();
    return () => window.removeEventListener('resize', clampOnResize);
  }, [sidebarOpen]);
  function inspectorPointerDown(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    resizeStart.current = { x: event.clientX, width: inspectorWidth };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  }
  function inspectorPointerMove(event: PointerEvent<HTMLDivElement>) {
    if (!resizeStart.current) return;
    setInspectorWidth(clampInspectorWidth(
      resizeStart.current.width + resizeStart.current.x - event.clientX,
      window.innerWidth, sidebarOpen,
    ));
  }
  function inspectorPointerEnd(event: PointerEvent<HTMLDivElement>) {
    resizeStart.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }
  function inspectorResizeKey(event: KeyboardEvent<HTMLDivElement>) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === 'Home' ? INSPECTOR_MIN_WIDTH
      : event.key === 'End' ? inspectorMaxWidth(window.innerWidth, sidebarOpen)
      : inspectorWidth + (event.key === 'ArrowLeft' ? 24 : -24);
    setInspectorWidth(clampInspectorWidth(target, window.innerWidth, sidebarOpen));
  }
  const [draft, setDraft] = useState('');
  const [skill, setSkill] = useState('');
  const [selectedTurn, setSelectedTurn] = useState<string>();
  const [submitted, setSubmitted] = useState<{ thread: string; turn: string | null; message: string }>();
  const submissionLock = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const transcript = useRef<HTMLDivElement>(null);
  useEffect(() => { setSelectedTurn(undefined); setDraft(''); setSkill(''); setPresentationTurn(undefined); setDrawer(null); }, [thread]);
  const health = useQuery({ queryKey: ['health'], queryFn: () => request<{ status: 'ok'; modes: Mode[] }>('/health'), refetchInterval: 30_000 });
  const skills = useQuery({ queryKey: ['skills'], queryFn: async () => {
    const catalog = await request<SkillsDTO>('/skills');
    if (!catalog || !Array.isArray(catalog.skills) || typeof catalog.available !== 'boolean' || catalog.skills.some((item) => typeof item.id !== 'string' || typeof item.description !== 'string' || typeof item.applicability !== 'string' || typeof item.version !== 'string')) throw new ApiError(503);
    return catalog;
  } });
  const jobs = useQuery({ queryKey: [mode, 'jobs'], queryFn: () => request<JobDTO[]>(`/${mode}/jobs`), refetchInterval: 1500 });
  const busy = jobs.data?.some(activeJob) ?? false;
  const durableReadsEnabled = jobs.isSuccess && !busy;
  const conversations = useQuery({ queryKey: [mode, 'conversations'], queryFn: () => request<ConversationSummary[]>(conversationPath(mode)), enabled: durableReadsEnabled });
  const session = useQuery({ queryKey: [mode, 'session', thread], queryFn: ({ signal }) => loadSessionSnapshot(client, mode, thread!, signal), enabled: !!thread && durableReadsEnabled, refetchInterval: durableReadsEnabled ? 2500 : false });
  const savedSession = session.data?.view;
  const currentJobs = jobs.data?.filter((job) => job.mode === mode && job.thread_id === thread) ?? [];
  const lastJob = currentJobs.at(-1);
  const currentActiveJob = [...currentJobs].reverse().find(activeJob);
  const needsRefresh = (job: JobDTO) => !activeJob(job) && !session.data?.terminalJobs.includes(jobVersion(job));
  const terminalRefreshPending = currentJobs.some(needsRefresh);
  const turns = savedSession?.turns ?? [];
  const lastTurnStatus = turns.at(-1)?.status;
  useEffect(() => {
    if (transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
  }, [thread, turns.length, lastTurnStatus, submitted?.turn]);
  const turnOptions = turns.map((item) => {
    const latest = [...currentJobs].reverse().find((job) => job.turn_id === item.turn_id);
    const status = latest && (activeJob(latest) || needsRefresh(latest)) ? `job ${latest.status}` : item.status;
    return { id: item.turn_id, label: `Turn ${item.sequence} · ${status}` };
  });
  for (const job of currentJobs) {
    if (job.turn_id && !turnOptions.some((option) => option.id === job.turn_id)) turnOptions.push({ id: job.turn_id, label: `Current job · ${job.status} · ${job.turn_id}` });
  }
  const selectedTurnId = (selectedTurn && turnOptions.some((option) => option.id === selectedTurn) ? selectedTurn : undefined)
    ?? currentActiveJob?.turn_id ?? (lastJob && needsRefresh(lastJob) ? lastJob.turn_id : undefined) ?? turns.at(-1)?.turn_id ?? lastJob?.turn_id ?? undefined;
  const turn = turns.find((item) => item.turn_id === selectedTurnId);
  const selectedJob = [...currentJobs].reverse().find((job) => job.turn_id === selectedTurnId);
  const presentationTurnView = turns.find((item) => item.turn_id === presentationTurn);
  const presentationJob = presentationTurn ? [...currentJobs].reverse().find((job) => job.operation === 'presentation' && job.turn_id === presentationTurn) : undefined;
  const historyPending = !!selectedJob && needsRefresh(selectedJob);
  const historyState = historyPending ? session.isError && !session.isFetching ? 'unavailable' : 'refreshing' : 'current';
  const unresolved = turns.some((item) => item.status === 'running' || item.status === 'interrupted');
  const blocked = !online || busy || jobs.isPending || jobs.isError || terminalRefreshPending || (!!thread && (session.isPending || session.isError)) || unresolved;
  const statusSignature = jobs.data?.map((job) => `${job.job_id}:${job.status}`).join('|');
  useEffect(() => {
    if (!durableReadsEnabled) return;
    void client.invalidateQueries({ queryKey: [mode, 'conversations'] });
  }, [statusSignature, durableReadsEnabled, client, mode]);
  const refetchSession = session.refetch;
  useEffect(() => {
    // A pre-terminal in-flight read may finish after the job changes status.
    // Its captured identities cannot mark it fresh; start a subsequent read.
    if (thread && durableReadsEnabled && terminalRefreshPending && !session.isFetching && !session.isError) void refetchSession();
  }, [thread, durableReadsEnabled, terminalRefreshPending, session.isFetching, session.isError, refetchSession]);
  const mutation = useMutation({
    networkMode: 'always',
    mutationFn: ({ resume, message, selectedSkill, targetThread }: { resume: boolean; message: string; selectedSkill: string; targetThread?: string }) => request<JobDTO>(`${conversationPath(mode, targetThread)}${resume ? '/resume' : targetThread ? '/turns' : ''}`, { method: 'POST', body: JSON.stringify(resume ? {} : { message, skill_id: selectedSkill || null }) }),
    onSuccess: (job, variables) => {
      client.setQueryData<JobDTO[]>([mode, 'jobs'], (previous) => [...(previous ?? []).filter((item) => item.job_id !== job.job_id), job]);
      const current = locationState();
      if (!mounted.current || current.mode !== mode || current.thread !== variables.targetThread) return;
      if (!variables.resume) { setDraft(''); setSkill(''); setSubmitted({ thread: job.thread_id, turn: job.turn_id, message: variables.message }); }
      setSelectedTurn(job.turn_id ?? undefined); navigate(mode, job.thread_id);
    },
    onSettled: () => { submissionLock.current = false; },
  });
  function send(resume = false) {
    if (!online || submissionLock.current || mutation.isPending || busy || jobs.isPending || jobs.isError || terminalRefreshPending || (!resume && (blocked || draft.trim().length < 10 || draft.trim().length > 1000))) return;
    submissionLock.current = true;
    const validSkill = skills.data?.available && skills.data.skills.some((item) => item.id === skill) ? skill : '';
    mutation.mutate({ resume, message: draft.trim(), selectedSkill: validSkill, targetThread: thread });
  }
  function refresh() { mutation.reset(); void Promise.all([health.refetch(), jobs.refetch(), skills.refetch(), ...(durableReadsEnabled ? [conversations.refetch(), ...(thread ? [session.refetch()] : [])] : [])]); }
  function openInspector() { if (window.matchMedia('(min-width: 1280px)').matches) setInspectorOpen((value) => !value); else setDrawer('inspector'); }
  const error = !online ? new ApiError(0) : mutation.error ?? (thread ? session.error : null) ?? jobs.error ?? conversations.error ?? health.error;
  const pendingMessage = submitted && submitted.thread === thread && !turns.some((item) => item.turn_id === submitted.turn) ? submitted.message : undefined;
  const navigation = <nav className="navigation" aria-label="Conversations">
    <div className="brand"><span className="brand-icon"><Sprout aria-hidden="true" /></span><span>Mini DeerFlow<small>RESEARCH WORKSPACE</small></span></div>
    <button className="primary new-chat" onClick={() => { navigate(mode); setSelectedTurn(undefined); setDraft(''); setSubmitted(undefined); mutation.reset(); setDrawer(null); }} disabled={mutation.isPending}><Plus size={18} aria-hidden="true" /> New chat</button>
    <label className="mode-select">Execution mode<select value={mode} onChange={(event) => navigate(event.target.value as Mode)} disabled={mutation.isPending}><option value="offline">Offline walkthrough</option><option value="live">Live</option></select></label>
    <p className="mode-note">{mode === 'offline' ? 'Deterministic scripted model and fixture evidence. No network research; answers demonstrate a fixed scenario.' : 'Live model and bounded web tools. Readiness is confirmed by actual runtime outcomes.'}</p>
    <div className="nav-heading"><h2>Saved conversations</h2><button aria-label="Refresh conversations" onClick={refresh}><RefreshCw size={16} aria-hidden="true" /></button></div>
    {conversations.isPending && <p className="caption">Loading conversations…</p>}
    {conversations.isError && <p role="alert">Conversation list unavailable. Use Refresh to retry.</p>}
    {conversations.data?.length === 0 && <p className="caption">Your research conversations will appear here.</p>}
    <div className="conversation-list">{conversations.data?.map((item) => <button className={`conversation-row ${thread === item.thread_id ? 'selected' : ''}`} aria-current={thread === item.thread_id ? 'page' : undefined} key={item.thread_id} disabled={mutation.isPending} onClick={() => { navigate(mode, item.thread_id); setSelectedTurn(undefined); setSubmitted(undefined); setDraft(''); mutation.reset(); setDrawer(null); }}><MessageSquare size={17} aria-hidden="true" /><span>{item.title}<small>{item.status} · {item.session_tool_calls}/{item.session_tool_call_limit} calls</small></span></button>)}</div>
    <div className="nav-footer"><FlaskConical size={16} aria-hidden="true" /><span>Local learning workspace<br />Phase 0 / 1 · bounded execution</span></div>
  </nav>;
  const inspector = <><label className="turn-picker">Inspect turn<select value={selectedTurnId ?? ''} onChange={(event) => setSelectedTurn(event.target.value)} disabled={!turnOptions.length}>{turnOptions.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}{!turnOptions.length && <option value="">No turn selected</option>}</select></label><Inspector key={`${thread ?? 'new'}:${selectedTurnId ?? 'none'}`} run={turn?.run ?? null} job={selectedJob} mode={mode} thread={thread} turn={selectedTurnId} historyState={historyState} onRetryHistory={() => void refetchSession()} /></>;
  return <div className={`app-shell ${sidebarOpen ? '' : 'sidebar-collapsed'} ${inspectorOpen ? 'inspector-open' : ''}`} style={{ '--inspector-width': `${inspectorWidth}px` } as CSSProperties}>
    <a className="skip-link" href="#composer">Skip to message</a>
    {sidebarOpen && <aside className="desktop-sidebar">{navigation}</aside>}
    <main className="workspace">
      <header className="workspace-header"><button className="desktop-toggle" onClick={() => setSidebarOpen((value) => !value)} aria-label={sidebarOpen ? 'Collapse sidebar' : 'Expand sidebar'} aria-expanded={sidebarOpen}><PanelLeftClose size={20} aria-hidden="true" /></button><button className="mobile-toggle" aria-label="Open navigation" onClick={() => setDrawer('navigation')}><Menu size={20} aria-hidden="true" /></button><div className="header-title"><span className="eyebrow">{mode === 'offline' ? 'OFFLINE WALKTHROUGH' : 'LIVE RESEARCH'}</span><h1>{savedSession?.conversation.title ?? (thread ? 'Saved conversation' : 'New research chat')}</h1></div><button className="refresh-button" aria-label="Refresh workspace" onClick={refresh}><RefreshCw size={18} aria-hidden="true" /></button><button className="inspect-button" aria-label="Open execution inspector" onClick={openInspector}><PanelRight size={18} aria-hidden="true" /><span>Inspect</span></button></header>
      {error && <div className="error-banner" role="alert"><span>{error instanceof ApiError ? error.message : 'The service is unavailable. Refresh to retry.'} {mutation.isError && 'If submission may have reached the server, check saved conversations before sending again.'}</span><button onClick={refresh}>Refresh & retry</button></div>}
      <div className="job-strip" role="status" aria-live="polite">{lastJob ? <><Status value={lastJob.status} /><span>{lastJob.operation} · {lastJob.turn_id}</span>{lastJob.error_message && <span>{lastJob.error_message}</span>}</> : <><span className="connection-dot" aria-hidden="true" /><span>{health.isSuccess ? 'Connected to local runtime' : 'Connecting to local runtime…'}</span></>}{savedSession && <span className="session-budget">{terminalRefreshPending ? 'Cached session tools' : 'Session tools'} {savedSession.conversation.session_tool_calls}/{savedSession.conversation.session_tool_call_limit}</span>}</div>
      {terminalRefreshPending && <div className="notice" role="status">{session.isError && !session.isFetching ? 'Saved conversation refresh failed.' : 'Refreshing saved conversation after the latest job.'} Cached messages are historical until a new read succeeds. Submission and recovery are paused.</div>}
      <div ref={transcript} className="transcript" aria-label="Conversation transcript" tabIndex={0}>
        {!thread && <section className="welcome"><span className="welcome-icon"><Sprout size={30} aria-hidden="true" /></span><span className="eyebrow">A LITTLE CURIOSITY. A CLEARER ANSWER.</span><h2>What would you like<br />to understand?</h2><p>Ask a focused question. Follow the evidence, explore the execution, and keep the conversation going.</p>{mode === 'offline' && <p className="notice">Offline walkthrough uses a fixed learning scenario, regardless of your question.</p>}</section>}
        {thread && session.isPending && <p className="empty-detail">Loading saved turns…</p>}
        {turns.map((item) => <article className="turn" key={item.turn_id} aria-label={`Turn ${item.sequence}`}>
          <div className="user-message"><span className="eyebrow">YOU · TURN {item.sequence}</span><p>{item.user_message}</p></div>
          <div className="assistant-message"><div className="answer-heading"><span className="answer-avatar"><Sprout size={18} aria-hidden="true" /></span><strong>Mini DeerFlow</strong><Status value={item.status} /></div><p className="caption">Skill: <strong>{item.run?.active_skill_id ?? 'None'}</strong></p>
            {item.assistant_message ? <SafeMarkdown text={item.assistant_message} allowed={item.run?.accepted_citations ?? []} /> : <p>{item.safe_error ?? (item.status === 'interrupted' ? 'This turn was interrupted. Resume it below to continue.' : item.status === 'completed' ? 'Completed without a displayable answer.' : item.status === 'failed' ? 'This turn failed safely.' : 'Waiting for the server to finish this turn…')}</p>}
            <button className="text-button" onClick={() => { setSelectedTurn(item.turn_id); if (window.matchMedia('(min-width: 1280px)').matches) setInspectorOpen(true); else setDrawer('inspector'); }}><PanelRight size={16} aria-hidden="true" /> Inspect turn {item.sequence}</button>
            {item.status === 'completed' && <button className="text-button" aria-haspopup="dialog" disabled={busy || !online} onClick={() => { setPresentationTurn(item.turn_id); setDrawer('presentation'); }}><PresentationIcon size={16} aria-hidden="true" /> Presentation for turn {item.sequence}</button>}
          </div>
        </article>)}
        {pendingMessage && <div className="user-message"><span className="eyebrow">SUBMITTED</span><p>{pendingMessage}</p></div>}
      </div>
      <div className="composer-wrap">
        {unresolved && !terminalRefreshPending && !busy && !jobs.isError && !jobs.isPending && <div className="recovery"><p>This turn is unresolved. Resume it before adding a follow-up.</p><button className="primary" disabled={!online || mutation.isPending || session.isError} onClick={() => send(true)}>Resume active turn</button></div>}
        {busy && <p className="caption" role="status">An operation is active in {mode} mode. Submission is disabled while it runs.</p>}
        <form className="composer" onSubmit={(event) => { event.preventDefault(); send(); }}>
          <label htmlFor="composer">{thread ? 'Ask a follow-up' : 'Your research question'}</label>
          <textarea id="composer" value={draft} maxLength={1000} rows={3} placeholder="Ask a specific, verifiable question…" disabled={blocked || mutation.isPending} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }} aria-describedby="composer-help" />
          <div className="composer-actions"><label className="skill-picker">Skill for next turn<select value={skills.data?.available && skills.data.skills.some((item) => item.id === skill) ? skill : ''} onChange={(event) => setSkill(event.target.value)} disabled={blocked || mutation.isPending || !skills.data?.available}><option value="">None</option>{skills.data?.skills.map((item) => <option key={item.id} value={item.id}>{item.id}</option>)}</select></label><span className="char-count">{draft.length}/1,000</span><button className="primary send" type="submit" disabled={blocked || mutation.isPending || draft.trim().length < 10} aria-label="Send message"><ArrowUp size={20} aria-hidden="true" /><span>Send</span></button></div>
          {skills.data?.skills.filter((item) => item.id === skill).map((item) => <p className="skill-description" key={item.id}><strong>{item.id} · v{item.version}</strong> — {item.description}<br />Applicability: {item.applicability}</p>)}
          {(skills.isError || skills.data?.available === false) && <p className="notice">Skill catalog unavailable. You can send without a skill. <button type="button" onClick={() => void skills.refetch()}>Retry skills</button></p>}
        </form>
        <p id="composer-help" className="composer-help">10–1,000 characters · Enter to send, Shift+Enter for a new line. Skill resets after send.</p>
      </div>
    </main>
    {inspectorOpen && <aside className="desktop-inspector">
      <div className="inspector-resize-handle" role="separator" aria-label="Resize execution inspector" aria-orientation="vertical"
        aria-valuemin={INSPECTOR_MIN_WIDTH} aria-valuemax={inspectorMaxWidth(window.innerWidth, sidebarOpen)} aria-valuenow={inspectorWidth}
        aria-valuetext={inspectorWidth + ' pixels wide'} tabIndex={0}
        onPointerDown={inspectorPointerDown} onPointerMove={inspectorPointerMove}
        onPointerUp={inspectorPointerEnd} onPointerCancel={inspectorPointerEnd}
        onKeyDown={inspectorResizeKey} title="Drag left/right to resize; arrow keys also work" />
      {inspector}
    </aside>}
    {drawer === 'presentation' && presentationTurnView && <Drawer title="Presentation workspace" close={() => setDrawer(null)}><PresentationWorkspace key={`${mode}:${thread}:${presentationTurn}`} mode={mode} thread={thread!} turn={presentationTurnView} job={presentationJob} busy={busy} /></Drawer>}
    {drawer && drawer !== 'presentation' && <Drawer title={drawer === 'navigation' ? 'Navigation' : 'Execution inspector'} close={() => setDrawer(null)}>{drawer === 'navigation' ? navigation : inspector}</Drawer>}
  </div>;
}
