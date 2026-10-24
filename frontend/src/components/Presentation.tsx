import { useEffect, useId, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, Presentation } from 'lucide-react';
import { ApiError, conversationPath, request, segment } from '../api';
import type { ChatTurn, JobDTO, Mode, PresentationLanguage, PresentationTheme } from '../types';
import { deriveTopic, isPresentationMetadata, PRESENTATION_AUDIENCE, PRESENTATION_INSTRUCTIONS_MAX, PRESENTATION_TOPICS, presentationDownloadUrl, PresentationFormValues, validatePresentationForm } from '../presentation-form';
import { Empty, Fields, Status } from './UI';

/**
 * Presentation workspace for one selected completed turn.
 * Truthful lifecycle only: real job polling via the shared jobs query, verified
 * server metadata (never job-history inference), server-controlled download URL.
 */
export default function PresentationWorkspace({ mode, thread, turn, job, busy }: {
  mode: Mode; thread: string; turn: ChatTurn; job: JobDTO | undefined; busy: boolean;
}) {
  const client = useQueryClient();
  const id = useId();
  const [values, setValues] = useState<PresentationFormValues>(() => ({
    topic: deriveTopic(turn.user_message), audience: '', language: 'vi', slideCount: 8, theme: 'light', instructions: '',
  }));
  const errors = useMemo(() => validatePresentationForm(values), [values]);
  const eligible = turn.status === 'completed';
  const generationJob = job && job.operation === 'presentation' ? job : undefined;
  const generating = !!generationJob && (generationJob.status === 'queued' || generationJob.status === 'running');
  // Verified persisted metadata only for the selected completed turn while the
  // server is idle: availability is never inferred from ephemeral job history.
  const metadata = useQuery({
    queryKey: [mode, 'presentation-metadata', thread, turn.turn_id],
    queryFn: async ({ signal }) => {
      const value = await request<unknown>(`${conversationPath(mode, thread)}/turns/${segment(turn.turn_id)}/presentation/metadata`, { signal });
      if (!isPresentationMetadata(value)) throw new ApiError(0);
      return value;
    },
    enabled: eligible && !busy,
    staleTime: 30_000, retry: false,
  });
  useEffect(() => {
    if (generationJob?.status === 'completed') void client.invalidateQueries({ queryKey: [mode, 'presentation-metadata', thread, turn.turn_id] });
  }, [generationJob?.status, client, mode, thread, turn.turn_id]);
  const mutation = useMutation({
    networkMode: 'always',
    mutationFn: (form: PresentationFormValues) => request<JobDTO>(`${conversationPath(mode, thread)}/turns/${segment(turn.turn_id)}/presentation`, {
      method: 'POST',
      body: JSON.stringify({ topic: form.topic.trim(), audience: form.audience.trim(), language: form.language, slide_count: form.slideCount, theme: form.theme, instructions: form.instructions }),
    }),
    onSuccess: (newJob) => {
      client.setQueryData<JobDTO[]>([mode, 'jobs'], (previous) => [...(previous ?? []).filter((item) => item.job_id !== newJob.job_id), newJob]);
    },
  });
  const submitBlocked = busy || generating || mutation.isPending || !eligible || Object.keys(errors).length > 0;
  // TanStack Query RETAINS the last successful data when a background refetch
  // fails, so a cached `metadata.data` alone proves nothing about the CURRENT
  // verification. Derive one trustworthy value from the current query state:
  // any newer failed verification discards retained data, and error states
  // take explicit rendering precedence over it. A 409 while the server is busy
  // is a transient refusal (this route shares the download lock), never
  // corruption; transport/5xx failures mean "unknown", not "absent".
  const errorStatus = metadata.isError && metadata.error instanceof ApiError
    ? metadata.error.status : null;
  const verifiedMetadata = !metadata.isError && metadata.isSuccess ? metadata.data ?? null : null;
  const absent = !verifiedMetadata && errorStatus === 404;
  const corrupt = !verifiedMetadata && errorStatus === 409 && !busy && !generating;
  const busyRefused = !verifiedMetadata && errorStatus === 409 && (busy || generating);
  const unavailable = !verifiedMetadata && metadata.isError && errorStatus !== 404 && errorStatus !== 409;
  const checking = !verifiedMetadata && !metadata.isError && metadata.isFetching;
  const rechecking = !!verifiedMetadata && metadata.isFetching;
  return <section className="presentation" aria-label={`Presentation workspace for turn ${turn.turn_id}`}>
    <div className="presentation-heading">
      <span className="eyebrow">PRESENTATION WORKSPACE</span>
      <h3>Edit-ready PowerPoint from this turn</h3>
      <p className="caption">Requests an editable deck from the completed research turn <strong>{turn.turn_id}</strong>. Generation runs the real slide-planner pipeline; a repeated request with different settings can conflict with the saved deck.</p>
    </div>
    <form className="presentation-form" onSubmit={(event) => { event.preventDefault(); if (!submitBlocked) mutation.mutate(values); }}>
      <label htmlFor={`${id}-topic`}>Topic <span className="char-count">{values.topic.trim().length}/{PRESENTATION_TOPICS.max}</span></label>
      <textarea id={`${id}-topic`} rows={2} value={values.topic} maxLength={PRESENTATION_TOPICS.max} aria-describedby={`${id}-topic-help`} aria-invalid={!!errors.topic} disabled={mutation.isPending || generating} onChange={(event) => setValues((current) => ({ ...current, topic: event.target.value }))} />
      <p className="caption" id={`${id}-topic-help`}>{errors.topic ?? `Derived from this turn's question. ${PRESENTATION_TOPICS.min}–${PRESENTATION_TOPICS.max} characters.`}</p>
      <label htmlFor={`${id}-audience`}>Audience</label>
      <input id={`${id}-audience`} type="text" value={values.audience} maxLength={PRESENTATION_AUDIENCE.max} placeholder="Who will read this deck?" aria-invalid={!!errors.audience} disabled={mutation.isPending || generating} onChange={(event) => setValues((current) => ({ ...current, audience: event.target.value }))} />
      {errors.audience && <p className="caption" role="alert">{errors.audience}</p>}
      <div className="presentation-grid">
        <label htmlFor={`${id}-language`}>Language<select id={`${id}-language`} value={values.language} disabled={mutation.isPending || generating} onChange={(event) => setValues((current) => ({ ...current, language: event.target.value as PresentationLanguage }))}><option value="vi">Vietnamese</option><option value="en">English</option></select></label>
        <label htmlFor={`${id}-count`}>Slides<input id={`${id}-count`} type="number" min={5} max={12} step={1} value={values.slideCount} aria-invalid={!!errors.slideCount} disabled={mutation.isPending || generating} onChange={(event) => setValues((current) => ({ ...current, slideCount: Number(event.target.value) }))} /></label>
        <label htmlFor={`${id}-theme`}>Theme<select id={`${id}-theme`} value={values.theme} disabled={mutation.isPending || generating} onChange={(event) => setValues((current) => ({ ...current, theme: event.target.value as PresentationTheme }))}><option value="light">Light</option><option value="dark">Dark</option><option value="academic">Academic</option></select></label>
      </div>
      {errors.slideCount && <p className="caption" role="alert">{errors.slideCount}</p>}
      <label htmlFor={`${id}-instructions`}>Optional instructions</label>
      <input id={`${id}-instructions`} type="text" value={values.instructions} maxLength={PRESENTATION_INSTRUCTIONS_MAX} aria-invalid={!!errors.instructions} disabled={mutation.isPending || generating} onChange={(event) => setValues((current) => ({ ...current, instructions: event.target.value }))} />
      {errors.instructions && <p className="caption" role="alert">{errors.instructions}</p>}
      <div className="presentation-actions">
        <button className="primary" type="submit" disabled={submitBlocked}><Presentation size={18} aria-hidden="true" /><span>Generate PowerPoint</span></button>
        {busy && !generating && <p className="caption">Another operation is active. Wait for it to finish.</p>}
        {(generating || mutation.isPending) && <p className="caption" role="status" aria-live="polite">{mutation.isPending ? 'Submitting presentation request…' : `Generating presentation · job ${generationJob!.status}`}</p>}
      </div>
    </form>
    {!eligible && <div className="notice" role="status">This turn is {turn.status}. Complete or resume the research turn before requesting slides.</div>}
    {generationJob?.status === 'failed' && generationJob.error_message && (
      <div className="notice" role="alert"><strong>Generation failed.</strong> {generationJob.error_message} Saved work on this turn is unchanged; adjust the request or reuse the saved deck below.</div>
    )}
    {verifiedMetadata && <>
      <section className="presentation-artifact" aria-label="Saved presentation">
        <div className="presentation-artifact-heading">
          <h4>Saved presentation</h4>
          <Status value="completed" />
          <a className="button primary" href={presentationDownloadUrl(mode, thread, turn.turn_id)} download><Download size={16} aria-hidden="true" /> Download .pptx</a>
        </div>
        <Fields values={{ Title: verifiedMetadata.title, Theme: verifiedMetadata.theme, Slides: verifiedMetadata.slide_count, Layouts: verifiedMetadata.layouts.join(', '), Size: `${verifiedMetadata.byte_size} bytes`, 'SHA-256': verifiedMetadata.sha256 }} />
        <h5 className="outline-heading">Read-only slide outline <span className="caption">(verified persisted structure — not a rendered preview)</span></h5>
        <ol className="presentation-outline">{verifiedMetadata.outline.map((slide) => <li key={slide.slide_id}><span className="outline-index">{slide.index}</span><span className={`outline-layout layout-${slide.layout}`}>{slide.layout}</span><span className="outline-title">{slide.title}</span></li>)}</ol>
        <p className="caption">Numbers inside charts, tables and metrics are labeled illustrative in the deck itself. Open the downloaded file in PowerPoint or LibreOffice to edit; in-app slide rendering is not provided.</p>
        {rechecking && <p className="caption" role="status">Re-verifying the saved presentation…</p>}
      </section>
    </>}
    {!verifiedMetadata && corrupt && (
      <div className="notice" role="alert"><strong>Saved presentation failed verification.</strong> The stored file did not match its recorded integrity. Download is disabled; nothing was overwritten.</div>
    )}
    {!verifiedMetadata && absent && <Empty>No saved presentation for this turn yet{eligible ? '. Generate one above.' : ''}</Empty>}
    {!verifiedMetadata && busyRefused && eligible && (
      <Empty>Checking the saved presentation waits until the active operation finishes.</Empty>
    )}
    {!verifiedMetadata && unavailable && eligible && (
      <Empty>Saved presentation status is unavailable right now. {busy || generating ? 'It will refresh when the active operation finishes.' : <button type="button" onClick={() => void metadata.refetch()}>Retry status check</button>}</Empty>
    )}
    {!verifiedMetadata && checking && eligible && <Empty>Checking the saved presentation…</Empty>}
    {!verifiedMetadata && !checking && !busyRefused && (busy || generating) && eligible && (
      <Empty>Checking the saved presentation waits until the active operation finishes.</Empty>
    )}
  </section>;
}
