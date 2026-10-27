import { describe, expect, it } from 'vitest';
import { clampInspectorWidth, inspectorMaxWidth, INSPECTOR_DEFAULT_WIDTH, INSPECTOR_MIN_WIDTH } from './inspectorSizing';

describe('resizable execution inspector boundaries', () => {
  it('starts at the existing desktop width', () => {
    expect(INSPECTOR_DEFAULT_WIDTH).toBe(400);
  });
  it('clamps to a usable chat width with navigation expanded', () => {
    expect(inspectorMaxWidth(1280, true)).toBe(650);
    expect(clampInspectorWidth(200, 1280, true)).toBe(INSPECTOR_MIN_WIDTH);
    expect(clampInspectorWidth(9999, 1280, true)).toBe(650);
    expect(clampInspectorWidth(560, 1280, true)).toBe(560);
  });
  it('allows a wider graph when the navigation sidebar is collapsed', () => {
    expect(inspectorMaxWidth(1280, false)).toBe(900);
    expect(clampInspectorWidth(780, 1280, false)).toBe(780);
  });
  it('keeps a valid minimum for small viewports and shrinks after resize', () => {
    expect(clampInspectorWidth(850, 1280, true)).toBe(650);
    expect(clampInspectorWidth(850, 880, true)).toBe(INSPECTOR_MIN_WIDTH);
    expect(clampInspectorWidth(439.7, 1440, true)).toBe(440);
  });
});
