import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

vi.mock('./features/upload/UploadPage', () => ({ default: () => <h1>Translate a document</h1> }));
vi.mock('./features/history/HistoryPage', () => ({ default: () => <h1>Translation history</h1> }));

describe('application navigation', () => {
  it('provides a skip target and a local branded home link', () => {
    render(<MemoryRouter><App /></MemoryRouter>);
    const main = screen.getByRole('main');
    expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveAttribute('href', `#${main.id}`);
    expect(main).toHaveAttribute('tabindex', '-1');
    expect(within(main).getByRole('heading', { name: 'Translate a document' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Stark Future' })).toHaveAttribute('src', '/branding/starkfuture-wordmark.svg');
    expect(screen.getByRole('link', { name: 'Stark Future Document Translator home' })).toHaveAttribute('href', '/');
  });

  it('announces the active route to assistive technology', () => {
    render(<MemoryRouter initialEntries={['/history']}><App /></MemoryRouter>);
    const navigation = screen.getByRole('navigation', { name: 'Main navigation' });
    expect(within(navigation).getByRole('link', { name: 'History' })).toHaveAttribute('aria-current', 'page');
    expect(within(navigation).getByRole('link', { name: 'Translate' })).not.toHaveAttribute('aria-current');
    expect(within(screen.getByRole('main')).getByRole('heading', { name: 'Translation history' })).toBeInTheDocument();
  });
});
