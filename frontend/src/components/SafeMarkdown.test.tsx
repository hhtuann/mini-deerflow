import { render, screen, within } from '@testing-library/react';
import { expect, it } from 'vitest';
import SafeMarkdown from './SafeMarkdown';
import { acceptedUrl } from '../links';

it('permits exact accepted HTTP(S) citations and denies HTML, images, unknown URLs and active schemes', () => {
  const allowed = ['https://example.org/source', 'javascript:alert(1)'];
  const { container } = render(<SafeMarkdown allowed={allowed} text={'[good](https://example.org/source) [unknown](https://bad.org) [active](javascript:alert(1)) ![remote](https://example.org/image)\n<script>alert(1)</script><img src=x onerror=alert(1) />'} />);
  expect(screen.getByRole('link', { name: 'good' })).toHaveAttribute('rel', 'noopener noreferrer');
  expect(screen.getAllByRole('link')).toHaveLength(1);
  expect(container.querySelector('script,img,iframe')).toBeNull();
  expect(acceptedUrl('javascript:alert(1)', allowed)).toBe(false);
  expect(acceptedUrl('https://example.org/source?changed', allowed)).toBe(false);
});

it('renders semantic GFM comparison tables inside a focusable scroll region without relaxing URL or media safety', () => {
  const { container } = render(<SafeMarkdown allowed={['https://example.org/source', 'javascript:alert(1)']} text={[
    '| Criterion | Option A | Option B |',
    '| --- | --- | --- |',
    '| Evidence | [accepted](https://example.org/source) | [unaccepted](https://bad.org) |',
    '| Safety | ![remote](https://example.org/image) | <img src=x onerror=alert(1)> |',
    '| Protocol | [active](javascript:alert(1)) | https://bad.org |',
  ].join('\n')} />);
  const region = screen.getByRole('region', { name: 'Comparison table' });
  const table = within(region).getByRole('table');
  expect(region).toHaveAttribute('tabindex', '0');
  expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual(['Criterion', 'Option A', 'Option B']);
  expect(within(table).getAllByRole('row')).toHaveLength(4);
  expect(within(table).getByRole('cell', { name: 'Evidence' })).toBeInTheDocument();
  expect(within(table).getAllByRole('link')).toHaveLength(1);
  expect(within(table).getByRole('link', { name: 'accepted' })).toHaveAttribute('href', 'https://example.org/source');
  expect(container.querySelector('script,img,iframe')).toBeNull();
});
