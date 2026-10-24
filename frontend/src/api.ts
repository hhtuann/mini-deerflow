import type { Mode } from './types';

export class ApiError extends Error {
  constructor(public status: number) {
    super(status === 409 ? 'The server is busy or this turn needs recovery. Refresh before trying again.'
      : status === 404 ? 'This saved item is unavailable. Refresh the conversation list.'
      : status === 422 ? 'Check your message (10–1,000 characters) and skill selection.'
      : status === 503 ? 'The service or skill catalog is unavailable. Please refresh and try again.'
      : 'Could not connect to the research service. Check the server and refresh.');
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    const response = await fetch(`/api${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers }, signal: init?.signal ?? AbortSignal.timeout(15_000) });
    if (!response.ok) throw new ApiError(response.status);
    return await response.json() as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(0);
  }
}
export const segment = (id: string): string => encodeURIComponent(id);
export const conversationPath = (mode: Mode, thread?: string): string => `/${mode}/conversations${thread ? `/${segment(thread)}` : ''}`;
export const activeJob = (job: { status: string }): boolean => ['queued', 'running'].includes(job.status);
