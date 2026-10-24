import type { QueryClient } from '@tanstack/react-query';
import { activeJob, conversationPath, request } from './api';
import type { ChatSession, JobDTO, Mode } from './types';

/** A read can reconcile only job states already known when that read STARTED. */
export interface SessionSnapshot { view: ChatSession; terminalJobs: string[] }

export const jobVersion = (job: JobDTO): string => `${job.job_id}:${job.status}`;

export async function loadSessionSnapshot(client: QueryClient, mode: Mode, thread: string, signal: AbortSignal): Promise<SessionSnapshot> {
  const terminalJobs = (client.getQueryData<JobDTO[]>([mode, 'jobs']) ?? [])
    .filter((job) => job.mode === mode && job.thread_id === thread && !activeJob(job))
    .map(jobVersion);
  const view = await request<ChatSession>(conversationPath(mode, thread), { signal });
  return { view, terminalJobs };
}
