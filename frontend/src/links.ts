/** Exact source-membership check plus an independent browser scheme boundary. */
export function acceptedUrl(url: string, allowed: readonly string[]): boolean {
  if (!allowed.includes(url)) return false;
  try { return ['https:', 'http:'].includes(new URL(url).protocol); } catch { return false; }
}
