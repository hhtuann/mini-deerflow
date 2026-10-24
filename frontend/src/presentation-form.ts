/** Presentation request boundaries shared by the workspace UI and tests. */
import type { PresentationLanguage, PresentationTheme } from './types';
import { conversationPath, segment } from './api';

export const PRESENTATION_TOPICS = { min: 10, max: 1000 } as const;
export const PRESENTATION_AUDIENCE = { min: 3, max: 120 } as const;
export const PRESENTATION_INSTRUCTIONS_MAX = 500;

export interface PresentationFormValues {
  topic: string; audience: string; language: PresentationLanguage; slideCount: number;
  theme: PresentationTheme; instructions: string;
}

/** Derive an honest initial topic from the turn's real user question (no silent inference of other fields). */
export function deriveTopic(userMessage: string): string {
  return userMessage.trim().slice(0, PRESENTATION_TOPICS.max);
}

export function validatePresentationForm(values: PresentationFormValues): Partial<Record<keyof PresentationFormValues, string>> {
  const errors: Partial<Record<keyof PresentationFormValues, string>> = {};
  const topic = values.topic.trim();
  if (topic.length < PRESENTATION_TOPICS.min || topic.length > PRESENTATION_TOPICS.max) errors.topic = `Topic must be ${PRESENTATION_TOPICS.min}–${PRESENTATION_TOPICS.max} characters.`;
  const audience = values.audience.trim();
  if (audience.length < PRESENTATION_AUDIENCE.min || audience.length > PRESENTATION_AUDIENCE.max) errors.audience = `Audience must be ${PRESENTATION_AUDIENCE.min}–${PRESENTATION_AUDIENCE.max} characters.`;
  if (!Number.isInteger(values.slideCount) || values.slideCount < 5 || values.slideCount > 12) errors.slideCount = 'Slide count must be 5–12.';
  if (values.instructions.length > PRESENTATION_INSTRUCTIONS_MAX) errors.instructions = `Instructions allow at most ${PRESENTATION_INSTRUCTIONS_MAX} characters.`;
  if (values.language !== 'vi' && values.language !== 'en') errors.language = 'Choose Vietnamese or English.';
  if (!['light', 'dark', 'academic'].includes(values.theme)) errors.theme = 'Choose a theme.';
  return errors;
}

export function isPresentationMetadata(value: unknown): value is import('./types').PresentationMetadata {
  const candidate = value as import('./types').PresentationMetadata;
  return !!candidate && typeof candidate.title === 'string' && typeof candidate.slide_count === 'number'
    && Array.isArray(candidate.outline) && Array.isArray(candidate.layouts)
    && typeof candidate.byte_size === 'number' && typeof candidate.sha256 === 'string';
}

export const presentationDownloadUrl = (mode: import('./types').Mode, thread: string, turn: string): string =>
  `/api${conversationPath(mode, thread)}/turns/${segment(turn)}/presentation`;
