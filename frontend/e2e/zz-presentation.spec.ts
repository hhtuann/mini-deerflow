import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import type { ChatSession, JobDTO, PresentationMetadata } from '../src/types';

const AUDIENCE = 'sinh viên năm cuối và giảng viên';

async function jobStatus(request: import('@playwright/test').APIRequestContext, jobId: string): Promise<JobDTO> {
  return await (await request.get(`/api/offline/jobs/${jobId}`)).json() as JobDTO;
}

test('presentation workspace: real generation, download, persistence, isolation, conflict', async ({ page, request }, testInfo) => {
  const browserErrors: string[] = [];
  page.on('pageerror', (error) => { browserErrors.push(error.message); console.error(error); });
  await page.setViewportSize({ width: 1440, height: 900 });
  // Real offline research turn with the skill explicitly selected BEFORE creation.
  await page.goto('/?mode=offline');
  await page.getByLabel('Skill for next turn').selectOption('slide-creation');
  await page.getByLabel('Your research question').fill('Tổng hợp bằng chứng về trí tuệ nhân tạo trong giáo dục đại học.');
  const submitted = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/api/offline/conversations'));
  await page.getByRole('button', { name: 'Send message' }).click();
  const researchJob = await (await submitted).json() as JobDTO;
  await expect.poll(async () => (await jobStatus(request, researchJob.job_id)).status).toBe('completed');
  const thread = researchJob.thread_id!;
  const turn1 = researchJob.turn_id!;
  const basePath = `/api/offline/conversations/${thread}`;

  // Open the Presentation workspace for the completed turn.
  await page.getByRole('button', { name: 'Presentation for turn 1', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Presentation workspace' });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(turn1);
  // Topic derives from the turn's real question; other fields stay user-owned.
  await expect(dialog.getByLabel(/Topic/)).toHaveValue(/trí tuệ nhân tạo trong giáo dục/);
  await expect(dialog.getByLabel('Audience')).toHaveValue('');
  await expect(dialog.getByText('No saved presentation for this turn yet.')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('presentation-form-empty.png'), fullPage: true });

  // Validation is honest: empty audience blocks submission with a message.
  await expect(dialog.getByRole('button', { name: 'Generate PowerPoint' })).toBeDisabled();
  await dialog.getByLabel('Audience').fill('ab');
  await expect(dialog.getByText('Audience must be 3–120 characters.')).toBeVisible();
  await dialog.getByLabel('Audience').fill(AUDIENCE);
  await dialog.getByLabel('Theme').selectOption('academic');
  await expect(dialog.getByRole('button', { name: 'Generate PowerPoint' })).toBeEnabled();

  // Real 202 presentation job through the injected production SlideAgentService.
  const generationPosted = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith(`/api/offline/conversations/${thread}/turns/${turn1}/presentation`));
  await dialog.getByRole('button', { name: 'Generate PowerPoint' }).click();
  const generationResponse = await generationPosted;
  expect(generationResponse.status()).toBe(202);
  const generationJob = await generationResponse.json() as JobDTO;
  expect(generationJob.operation).toBe('presentation');
  await expect(dialog.getByRole('button', { name: 'Generate PowerPoint' })).toBeDisabled();
  await expect.poll(async () => (await jobStatus(request, generationJob.job_id)).status).toBe('completed');
  await expect(dialog.getByRole('region', { name: /Saved presentation/ })).toBeVisible();

  // Verified metadata: exact facts from the persisted, validated deck.
  const metadata = await (await request.get(`${basePath}/turns/${turn1}/presentation/metadata`)).json() as PresentationMetadata;
  expect(metadata.slide_count).toBe(8);
  expect(new Set(metadata.layouts).size).toBeGreaterThanOrEqual(4);
  await expect(dialog.getByText(metadata.title)).toBeVisible();
  await expect(dialog.getByText(metadata.sha256)).toBeVisible();
  await expect(dialog.locator('.presentation-outline li')).toHaveCount(8);
  for (const slide of metadata.outline) await expect(dialog.locator('.presentation-outline')).toContainText(slide.title);
  await expect(dialog.getByText(/not a rendered preview/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('presentation-saved.png'), fullPage: true });

  // Genuine .pptx download matching server bytes and MIME.
  const serverDownload = await request.get(`${basePath}/turns/${turn1}/presentation`);
  expect(serverDownload.status()).toBe(200);
  expect(serverDownload.headers()['content-type']).toBe('application/vnd.openxmlformats-officedocument.presentationml.presentation');
  const serverBytes = new Uint8Array(await serverDownload.body());
  expect(serverBytes.length).toBe(metadata.byte_size);
  const downloadPromise = page.waitForEvent('download');
  await dialog.getByRole('link', { name: 'Download .pptx' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('presentation.pptx');
  const uiBytes = new Uint8Array(await readFile((await download.path())!));
  expect(uiBytes.length).toBe(serverBytes.length);
  expect(uiBytes).toEqual(serverBytes);

  // Reload: persisted metadata returns without any new generation job.
  await page.reload();
  await page.getByRole('button', { name: 'Presentation for turn 1', exact: true }).click();
  const restored = page.getByRole('dialog', { name: 'Presentation workspace' });
  await expect(restored.getByText(metadata.title)).toBeVisible();
  await expect(restored.getByText(metadata.sha256)).toBeVisible();
  const jobsAfterReload = await (await request.get('/api/offline/jobs')).json() as JobDTO[];
  expect(jobsAfterReload.filter((job) => job.operation === 'presentation')).toHaveLength(1);
  await page.screenshot({ path: testInfo.outputPath('presentation-restored.png'), fullPage: true });

  // Follow-up turn: no saved presentation there; generate then hit immutability conflict.
  await page.keyboard.press('Escape');
  await page.getByLabel('Ask a follow-up').fill('What are the limitations of the collected evidence?');
  const followupPosted = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith(`${basePath}/turns`));
  await page.getByRole('button', { name: 'Send message' }).click();
  const followupJob = await (await (await followupPosted).json() as JobDTO);
  await expect.poll(async () => (await jobStatus(request, followupJob.job_id)).status).toBe('completed');
  const turn2 = followupJob.turn_id!;
  await expect.poll(async () => ((await (await request.get(basePath)).json() as ChatSession).turns.length)).toBe(2);
  await page.getByRole('button', { name: 'Presentation for turn 2', exact: true }).click();
  const second = page.getByRole('dialog', { name: 'Presentation workspace' });
  await expect(second.getByText('No saved presentation for this turn yet.')).toBeVisible();
  await second.getByLabel('Audience').fill(AUDIENCE);
  await second.getByRole('button', { name: 'Generate PowerPoint' }).click();
  await expect.poll(async () => {
    const jobs = await (await request.get('/api/offline/jobs')).json() as JobDTO[];
    const target = jobs.filter((job) => job.operation === 'presentation' && job.turn_id === turn2).at(-1);
    return target?.status;
  }, { timeout: 60_000 }).toBe('completed');
  await expect(second.getByText(/Saved presentation/)).toBeVisible();
  // Different slide count on the same turn: immutable publication conflict.
  await second.getByLabel('Slides').fill('6');
  await second.getByRole('button', { name: 'Generate PowerPoint' }).click();
  await expect.poll(async () => {
    const jobs = await (await request.get('/api/offline/jobs')).json() as JobDTO[];
    const target = jobs.filter((job) => job.operation === 'presentation' && job.turn_id === turn2).at(-1);
    return target?.status === 'failed' ? target.error_message : target?.status;
  }, { timeout: 60_000 }).toContain('different presentation');
  await expect(second.getByRole('alert')).toContainText('different presentation');
  await expect(second.getByText(/Saved presentation/)).toBeVisible();

  // Mode isolation: the offline artifact is invisible in live mode.
  const liveMetadata = await request.get(`/api/live/conversations/${thread}/turns/${turn1}/presentation/metadata`);
  expect(liveMetadata.status()).toBe(404);
  expect(browserErrors).toEqual([]);
});

test('presentation workspace on 375×812: keyboard, focus return, no horizontal overflow', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto('/?mode=offline');
  await page.getByLabel('Your research question').fill('Nghiên cứu bằng chứng về học máy trong y tế dự phòng.');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(page.getByRole('button', { name: 'Presentation for turn 1', exact: true })).toBeVisible({ timeout: 60_000 });
  const opener = page.getByRole('button', { name: 'Presentation for turn 1', exact: true });
  await opener.click();
  const dialog = page.getByRole('dialog', { name: 'Presentation workspace' });
  await expect(dialog).toBeVisible();
  const audience = dialog.getByLabel('Audience');
  await audience.fill('bác sĩ và điều dưỡng');
  await audience.press('Tab');
  await expect(dialog.getByLabel('Language')).toBeFocused();
  expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('presentation-phone.png'), fullPage: true });
  await page.keyboard.press('Escape');
  await expect(opener).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
