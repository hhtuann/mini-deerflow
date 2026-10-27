/** Bounded desktop Inspector width. Leave the conversation usable at every viewport. */
export const INSPECTOR_MIN_WIDTH = 340;
export const INSPECTOR_DEFAULT_WIDTH = 400;
export const CHAT_MIN_WIDTH = 380;
export const NAVIGATION_WIDTH = 250;

export function inspectorMaxWidth(viewportWidth: number, sidebarOpen: boolean): number {
  return Math.max(INSPECTOR_MIN_WIDTH, Math.floor(viewportWidth) - CHAT_MIN_WIDTH - (sidebarOpen ? NAVIGATION_WIDTH : 0));
}

export function clampInspectorWidth(width: number, viewportWidth: number, sidebarOpen: boolean): number {
  return Math.min(inspectorMaxWidth(viewportWidth, sidebarOpen), Math.max(INSPECTOR_MIN_WIDTH, Math.round(width)));
}
