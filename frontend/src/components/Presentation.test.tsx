import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PresentationWorkspace from './Presentation';
import {
  deriveTopic, PRESENTATION_AUDIENCE, PRESENTATION_INSTRUCTIONS_MAX, PRESENTATION_TOPICS,
  validatePresentationForm,
} from '../presentation-form';
import { job, session } from '../test/fixtures';
import type { JobDTO, PresentationMetadata } from '../types';

const metadata: PresentationMetadata = {
  artifact_type: 'pptx', title: 'Tổng quan minh họa', theme: 'academic', slide_count: 8,
  layouts: ['bullets', 'chart', 'conclusion', 'comparison', 'metric', 'steps', 'table', 'title'],
  outline: Array.from({ length: 8 }, (_, index) => ({ index: index + 1, slide_id: `slide-${index + 1}`, layout: 'bullets', title: `Slide ${index + 1}` })),
  byte_size: 46157, sha256: 'a'.repeat(64),
};

describe('form validation boundaries', () => {
  const valid = { topic: 'A solid topic', audience: 'students', language: 'vi' as const, slideCount: 8, theme: 'light' as const, instructions: '' };
  it('accepts boundary values', () => {
    expect(validatePresentationForm({ ...valid, topic: 'x'.repeat(PRESENTATION_TOPICS.min), audience: 'y'.repeat(PRESENTATION_AUDIENCE.min) })).toEqual({});
    expect(validatePresentationForm({ ...valid, slideCount: 5 })).toEqual({});
    expect(validatePresentationForm({ ...valid, slideCount: 12 })).toEqual({});
    expect(validatePresentationForm({ ...valid, instructions: 'z'.repeat(PRESENTATION_INSTRUCTIONS_MAX) })).toEqual({});
  });
  it('rejects out-of-bound values with field errors', () => {
    expect(validatePresentationForm({ ...valid, topic: 'x'.repeat(9) }).topic).toBeTruthy();
    expect(validatePresentationForm({ ...valid, topic: 'x'.repeat(1001) }).topic).toBeTruthy();
    expect(validatePresentationForm({ ...valid, audience: 'ab' }).audience).toBeTruthy();
    expect(validatePresentationForm({ ...valid, audience: 'a'.repeat(121) }).audience).toBeTruthy();
    expect(validatePresentationForm({ ...valid, slideCount: 4 }).slideCount).toBeTruthy();
    expect(validatePresentationForm({ ...valid, slideCount: 13 }).slideCount).toBeTruthy();
    expect(validatePresentationForm({ ...valid, slideCount: 7.5 }).slideCount).toBeTruthy();
    expect(validatePresentationForm({ ...valid, instructions: 'i'.repeat(501) }).instructions).toBeTruthy();
    expect(validatePresentationForm({ ...valid, language: 'fr' as never }).language).toBeTruthy();
    expect(validatePresentationForm({ ...valid, theme: 'navy' as never }).theme).toBeTruthy();
  });
  it('derives topic from the real user question only', () => {
    expect(deriveTopic('  Tìm bằng chứng về AI  ')).toBe('Tìm bằng chứng về AI');
    expect(deriveTopic('x'.repeat(1200))).toHaveLength(PRESENTATION_TOPICS.max);
  });
});

describe('PresentationWorkspace', () => {
  let posts: { path: string; body: Record<string, unknown> }[] = [];
  let metadataStatus = 404;
  let metadataPayload: PresentationMetadata | null = null;
  let metadataFetches: string[] = [];
  const completedTurn = session.turns[0];
  beforeEach(() => {
    posts = []; metadataStatus = 404; metadataPayload = null; metadataFetches = [];
    vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === 'POST') {
        posts.push({ path, body: JSON.parse(String(init.body)) as Record<string, unknown> });
        return new Response(JSON.stringify({ ...job, operation: 'presentation', status: 'queued' }), { status: 202 });
      }
      if (path.endsWith('/presentation/metadata')) {
        metadataFetches.push(path);
        return metadataPayload && metadataStatus === 200
          ? new Response(JSON.stringify(metadataPayload))
          : new Response(JSON.stringify({ detail: 'Presentation was not found.' }), { status: metadataStatus });
      }
      throw new Error(`unexpected fetch ${path}`);
    }));
  });
  afterEach(() => vi.unstubAllGlobals());
  function mount(props: Partial<Parameters<typeof PresentationWorkspace>[0]> = {}) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const view = render(<QueryClientProvider client={client}>
      <PresentationWorkspace mode="offline" thread="chat-one" turn={completedTurn} job={undefined} busy={false} {...props} />
    </QueryClientProvider>);
    return { ...view, client };
  }
  const user = userEvent.setup();
  const turnOneKey = ['offline', 'presentation-metadata', 'chat-one', 'turn-one'] as const;

  it('shows an honest empty state when no artifact exists and blocks invalid submission', async () => {
    mount();
    expect(await screen.findByText(/No saved presentation for this turn yet/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Generate PowerPoint' })).toBeDisabled();
    await user.type(screen.getByLabelText('Audience'), 'sinh viên');
    expect(screen.getByRole('button', { name: 'Generate PowerPoint' })).toBeEnabled();
  });
  it('submits the exact DTO shape and injects the returned job', async () => {
    mount();
    await screen.findByText(/No saved presentation for this turn yet/);
    await user.clear(screen.getByLabelText(/Topic/));
    await user.type(screen.getByLabelText(/Topic/), 'Bằng chứng về AI trong giáo dục');
    await user.type(screen.getByLabelText('Audience'), 'giảng viên');
    await user.selectOptions(screen.getByLabelText('Language'), 'en');
    await user.selectOptions(screen.getByLabelText('Theme'), 'dark');
    await user.click(screen.getByRole('button', { name: 'Generate PowerPoint' }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].path).toBe('/api/offline/conversations/chat-one/turns/turn-one/presentation');
    expect(posts[0].body).toEqual({ topic: 'Bằng chứng về AI trong giáo dục', audience: 'giảng viên', language: 'en', slide_count: 8, theme: 'dark', instructions: '' });
  });
  it('renders verified metadata, outline and a server-controlled download link', async () => {
    metadataStatus = 200; metadataPayload = metadata;
    mount();
    const region = await screen.findByRole('region', { name: /Saved presentation/ });
    expect(within(region).getByText(metadata.title)).toBeVisible();
    expect(within(region).getByText(metadata.sha256)).toBeVisible();
    expect(within(region).getAllByRole('listitem')).toHaveLength(8);
    const link = within(region).getByRole('link', { name: 'Download .pptx' });
    expect(link).toHaveAttribute('href', '/api/offline/conversations/chat-one/turns/turn-one/presentation');
    expect(link).toHaveAttribute('download');
    expect(within(region).getByText(/not a rendered preview/)).toBeVisible();
  });
  it('distinguishes corrupt storage (409) from absent (404) and never offers download', async () => {
    metadataStatus = 409;
    mount();
    expect(await screen.findByText(/failed verification/)).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Download .pptx' })).toBeNull();
    expect(screen.queryByText(/No saved presentation for this turn yet/)).toBeNull();
  });
  it('treats a busy 409 as transient, never as corruption', async () => {
    metadataStatus = 409;
    const running: JobDTO = { ...job, operation: 'presentation', status: 'running' };
    mount({ job: running, busy: true });
    expect(await screen.findByText(/job running/)).toBeVisible();
    expect(screen.queryByText(/failed verification/)).toBeNull();
    expect(screen.queryByText(/No saved presentation for this turn yet/)).toBeNull();
    expect(screen.getByText(/waits until the active operation finishes/)).toBeVisible();
  });
  it('treats transport failures as unknown, not absence, and offers a retry', async () => {
    metadataStatus = 500;
    mount();
    expect(await screen.findByText(/status is unavailable right now/)).toBeVisible();
    expect(screen.queryByText(/No saved presentation for this turn yet/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Retry status check' })).toBeEnabled();
  });
  it('surfaces a failed generation job with its safe backend message', async () => {
    metadataStatus = 200; metadataPayload = metadata;
    const failed: JobDTO = { ...job, operation: 'presentation', status: 'failed', error_message: 'A different presentation already belongs to this turn.' };
    mount({ job: failed });
    const alerts = await screen.findAllByRole('alert');
    expect(alerts.some((alert) => /different presentation/.test(alert.textContent ?? ''))).toBe(true);
    expect(await screen.findByRole('region', { name: /Saved presentation/ })).toBeVisible();
  });
  it('shows truthful running state and blocks resubmission while the job is active', async () => {
    const running: JobDTO = { ...job, operation: 'presentation', status: 'running' };
    const first = mount({ job: running, busy: true });
    expect(await screen.findByText(/job running/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Generate PowerPoint' })).toBeDisabled();
    first.unmount();
    // Busy from an unrelated operation also blocks submission truthfully.
    mount({ job: undefined, busy: true });
    expect(await screen.findByText('Another operation is active. Wait for it to finish.')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Generate PowerPoint' })).toBeDisabled();
  });
  it('explains ineligible turns instead of offering generation', () => {
    mount({ turn: { ...completedTurn, status: 'interrupted' } });
    expect(screen.getByText(/This turn is interrupted\./)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Generate PowerPoint' })).toBeDisabled();
  });
  it('prefills the topic from this turn only (no cross-turn inference)', () => {
    mount({ turn: { ...completedTurn, user_message: 'Câu hỏi riêng của lượt này về dữ liệu' } });
    expect(screen.getByLabelText(/Topic/)).toHaveValue('Câu hỏi riêng của lượt này về dữ liệu');
  });

  // Transition regressions: ONE mounted component and ONE QueryClient each;
  // only the mocked network changes between verifications.
  it('A: 200 -> 409 drops the stale deck and never re-generates', async () => {
    metadataStatus = 200; metadataPayload = metadata;
    const view = mount();
    expect(await screen.findByRole('region', { name: /Saved presentation/ })).toBeVisible();
    expect(screen.getByRole('link', { name: 'Download .pptx' })).toBeVisible();
    metadataStatus = 409; metadataPayload = null;
    await act(() => view.client.invalidateQueries({ queryKey: [...turnOneKey] }));
    expect(await screen.findByText(/failed verification/)).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Download .pptx' })).toBeNull();
    expect(screen.queryByRole('region', { name: /Saved presentation/ })).toBeNull();
    expect(screen.queryByText(metadata.title)).toBeNull();
    expect(screen.queryByText(metadata.sha256)).toBeNull();
    expect(posts).toHaveLength(0);
  });
  it('B: 200 -> 404 replaces the deck with the honest absent state', async () => {
    metadataStatus = 200; metadataPayload = metadata;
    const view = mount();
    expect(await screen.findByRole('region', { name: /Saved presentation/ })).toBeVisible();
    metadataStatus = 404; metadataPayload = null;
    await act(() => view.client.invalidateQueries({ queryKey: [...turnOneKey] }));
    expect(await screen.findByText(/No saved presentation for this turn yet/)).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Download .pptx' })).toBeNull();
    expect(screen.queryByText(metadata.title)).toBeNull();
    expect(screen.queryByText(metadata.sha256)).toBeNull();
  });
  it('C: 200 -> 500 shows unknown-with-retry, then a 200 retry restores the deck', async () => {
    metadataStatus = 200; metadataPayload = metadata;
    const view = mount();
    expect(await screen.findByRole('region', { name: /Saved presentation/ })).toBeVisible();
    metadataStatus = 500; metadataPayload = null;
    await act(() => view.client.invalidateQueries({ queryKey: [...turnOneKey] }));
    expect(await screen.findByText(/status is unavailable right now/)).toBeVisible();
    expect(screen.queryByRole('link', { name: 'Download .pptx' })).toBeNull();
    expect(screen.queryByText(metadata.title)).toBeNull();
    expect(screen.queryByText(/failed verification/)).toBeNull();
    expect(screen.queryByText(/No saved presentation for this turn yet/)).toBeNull();
    metadataStatus = 200; metadataPayload = metadata;
    await user.click(screen.getByRole('button', { name: 'Retry status check' }));
    expect(await screen.findByRole('region', { name: /Saved presentation/ })).toBeVisible();
    expect(screen.getByRole('link', { name: 'Download .pptx' })).toBeVisible();
    expect(screen.getByText(metadata.title)).toBeVisible();
  });
  it('D: a 409 while an operation is active stays transient, not corruption', async () => {
    metadataStatus = 409;
    const running: JobDTO = { ...job, operation: 'presentation', status: 'running' };
    mount({ job: running, busy: true });
    expect(await screen.findByText(/waits until the active operation finishes/)).toBeVisible();
    expect(screen.queryByText(/failed verification/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Generate PowerPoint' })).toBeDisabled();
  });
  it('keeps query scope isolated per mode/thread/turn', async () => {
    metadataStatus = 404; metadataPayload = null;
    const first = mount();
    await screen.findByText(/No saved presentation for this turn yet/);
    expect(metadataFetches.at(-1)).toBe('/api/offline/conversations/chat-one/turns/turn-one/presentation/metadata');
    first.unmount();
    const secondTurn = { ...completedTurn, turn_id: 'turn-two', user_message: 'Another turn question entirely' };
    const second = mount({ turn: secondTurn });
    expect(second.getByLabelText(/Topic/)).toHaveValue('Another turn question entirely');
    await waitFor(() => expect(metadataFetches.at(-1)).toBe('/api/offline/conversations/chat-one/turns/turn-two/presentation/metadata'));
    expect(second.queryByText(metadata.title)).toBeNull();
    second.unmount();
  });
});
