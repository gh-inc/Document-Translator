import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../../App';
import { api } from '../../api/client';
import { ApiError } from '../../api/errors';
import type { JobSummaryResponse } from '../../api/types';
import HistoryPage from './HistoryPage';

vi.mock('../../api/client', () => ({ api: { listRecentJobs: vi.fn(), getJob: vi.fn(), retryJob: vi.fn(), download: vi.fn(), getDocument: vi.fn() } }));

const completed: JobSummaryResponse = { id: 'job-done', document_id: 'doc', batch_id: 'batch/1', target_language: 'German', status: 'done', total_chunks: 4, done_chunks: 4, cache_hit_blocks: 0, cache_miss_blocks: 0, cost_usd: 0.0123, analysis_cost_usd: 0.0014, error: null };
const failed: JobSummaryResponse = { ...completed, id: 'job-failed', target_language: 'Swedish', status: 'failed', done_chunks: 2, error: { error_code: 'provider_timeout', message: 'unsafe server message', retryable: true } };

function renderPage() { return render(<MemoryRouter><HistoryPage /></MemoryRouter>); }
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((fulfill) => { resolve = fulfill; });
  return { promise, resolve };
}

describe('translation history', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listRecentJobs).mockReset().mockResolvedValue([]);
    vi.stubGlobal('EventSource', vi.fn());
  });

  it('loads recent jobs with cancellation and shows a loading state', async () => {
    const pending = deferred<JobSummaryResponse[]>();
    vi.mocked(api.listRecentJobs).mockReturnValue(pending.promise);
    renderPage();
    expect(screen.getByRole('status')).toHaveTextContent('Loading translation history');
    expect(screen.getByRole('button', { name: 'Reload history' })).toBeDisabled();
    expect(api.listRecentJobs).toHaveBeenCalledWith(10, expect.any(AbortSignal));
    await act(async () => pending.resolve([]));
    expect(screen.queryByText('Loading translation history…')).not.toBeInTheDocument();
  });

  it('offers a new translation when history is empty', async () => {
    renderPage();
    expect(await screen.findByText('No translations yet.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Translate a document' })).toHaveAttribute('href', '/');
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
  });

  it('shows a safe load failure and reloads successfully', async () => {
    vi.mocked(api.listRecentJobs).mockRejectedValueOnce(new Error('private diagnostic')).mockResolvedValueOnce([completed]);
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('Internal server error');
    expect(screen.queryByText('private diagnostic')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reload history' }));
    expect(await screen.findByRole('article', { name: 'Translation to German' })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(api.listRecentJobs).toHaveBeenCalledTimes(2);
  });

  it('renders status, language, progress, cost, actions and detail links from snapshots', async () => {
    vi.mocked(api.listRecentJobs).mockResolvedValue([completed, failed]);
    renderPage();
    const german = await screen.findByRole('article', { name: 'Translation to German' });
    expect(within(german).getByText('Completed')).toBeInTheDocument();
    expect(within(german).getByText('4 / 4 chunks')).toBeInTheDocument();
    expect(within(german).getByText('$0.0123')).toBeInTheDocument();
    expect(within(german).getByRole('progressbar')).toHaveAttribute('value', '4');
    expect(within(german).getByRole('button', { name: 'Download translation' })).toBeInTheDocument();
    expect(within(german).getByRole('link', { name: 'View translation' })).toHaveAttribute('href', '/jobs/job-done');
    const swedish = screen.getByRole('article', { name: 'Translation to Swedish' });
    expect(within(swedish).getByRole('button', { name: 'Retry translation' })).toBeInTheDocument();
    expect(within(swedish).getByRole('alert')).toHaveTextContent('Translation provider timed out');
    expect(screen.queryByText('unsafe server message')).not.toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'View batch' })[0]).toHaveAttribute('href', '/batches/batch%2F1');
    expect(api.getJob).not.toHaveBeenCalled();
    expect(EventSource).not.toHaveBeenCalled();
  });

  it('filters recent jobs by status and restores all jobs', async () => {
    vi.mocked(api.listRecentJobs).mockResolvedValue([completed, failed]);
    renderPage();
    await screen.findByRole('article', { name: 'Translation to German' });
    fireEvent.change(screen.getByRole('combobox', { name: 'Filter by status' }), { target: { value: 'failed' } });
    expect(screen.queryByRole('article', { name: 'Translation to German' })).not.toBeInTheDocument();
    expect(screen.getByRole('article', { name: 'Translation to Swedish' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'all' } });
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(api.listRecentJobs).toHaveBeenCalledTimes(1);
  });

  it('explains when a filter has no matches', async () => {
    vi.mocked(api.listRecentJobs).mockResolvedValue([completed]);
    renderPage();
    await screen.findByRole('article');
    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'queued' } });
    expect(screen.getByRole('status')).toHaveTextContent('No translations match this status');
    expect(screen.queryByText('No translations yet.')).not.toBeInTheDocument();
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
  });

  it('moves a retried job out of the failed filter and preserves its new status across filter changes', async () => {
    const queued = { ...failed, status: 'queued' as const, error: null };
    vi.mocked(api.listRecentJobs).mockResolvedValue([completed, failed]);
    vi.mocked(api.retryJob).mockResolvedValue(queued);
    vi.mocked(api.getJob).mockResolvedValue(queued);
    renderPage();
    await screen.findByRole('article', { name: 'Translation to Swedish' });
    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'failed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Retry translation' }));
    expect(await screen.findByText(/No translations match this status/)).toBeInTheDocument();
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
    expect(api.retryJob).toHaveBeenCalledWith('job-failed', expect.any(AbortSignal));

    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'queued' } });
    const card = await screen.findByRole('article', { name: 'Translation to Swedish' });
    expect(within(card).getByText('Queued')).toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: 'Retry translation' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'all' } });
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(within(screen.getByRole('article', { name: 'Translation to Swedish' })).getByText('Queued')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'failed' } });
    expect(screen.getByRole('status')).toHaveTextContent('No translations match this status');
    expect(api.listRecentJobs).toHaveBeenCalledTimes(1);
    expect(EventSource).not.toHaveBeenCalled();
  });

  it('shows the analysis cost once per document and marks it as shared', async () => {
    vi.mocked(api.listRecentJobs).mockResolvedValue([
      { ...completed, id: 'job-de', document_id: 'doc-a', target_language: 'German' },
      { ...completed, id: 'job-fr', document_id: 'doc-a', target_language: 'French' },
      { ...completed, id: 'job-es', document_id: 'doc-b', target_language: 'Spanish' },
    ]);
    renderPage();
    await screen.findByRole('article', { name: 'Translation to Spanish' });
    const lines = screen.getAllByText(/Document analysis/);
    expect(lines[0]).toHaveTextContent('$0.0014');
    // Two documents, so two lines: one shared by two languages, one by one.
    expect(lines).toHaveLength(2);
    expect(screen.getByText('shared by 2 translations')).toBeInTheDocument();
    expect(screen.getByText('shared by 1 translation')).toBeInTheDocument();
    // The French card repeats the document but not the figure.
    const french = screen.getByRole('article', { name: 'Translation to French' });
    expect(within(french).queryByText(/Document analysis/)).not.toBeInTheDocument();
  });

  it('hides the analysis line when the document analysis cost is zero', async () => {
    vi.mocked(api.listRecentJobs).mockResolvedValue([
      { ...completed, analysis_cost_usd: 0 },
    ]);
    renderPage();
    await screen.findByRole('article', { name: 'Translation to German' });
    expect(screen.queryByText(/Document analysis/)).not.toBeInTheDocument();
    expect(screen.getByText('$0.0123')).toBeInTheDocument();
  });

  it('keeps filters synchronized with a retry follow-up job refresh', async () => {
    const queued = { ...failed, status: 'queued' as const, error: null };
    const running = { ...queued, status: 'running' as const };
    vi.mocked(api.listRecentJobs).mockResolvedValue([failed]);
    vi.mocked(api.retryJob).mockResolvedValue(queued);
    vi.mocked(api.getJob).mockResolvedValue(running);
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Retry translation' }));
    const card = await screen.findByRole('article', { name: 'Translation to Swedish' });
    await waitFor(() => expect(within(card).getByText('Translating')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'running' } });
    expect(screen.getByRole('article', { name: 'Translation to Swedish' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'queued' } });
    expect(screen.getByRole('status')).toHaveTextContent('No translations match this status');
    fireEvent.change(screen.getByLabelText('Filter by status'), { target: { value: 'running' } });
    expect(within(screen.getByRole('article')).getByText('Translating')).toBeInTheDocument();
    expect(EventSource).not.toHaveBeenCalled();
  });

  it('aborts on unmount and ignores a delayed result', async () => {
    const pending = deferred<JobSummaryResponse[]>();
    vi.mocked(api.listRecentJobs).mockReturnValue(pending.promise);
    const page = renderPage();
    const signal = vi.mocked(api.listRecentJobs).mock.calls[0][1];
    page.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve([completed]));
    expect(api.getJob).not.toHaveBeenCalled();
    expect(EventSource).not.toHaveBeenCalled();
  });

  it('ignores the cancelled StrictMode request if it resolves after the current request', async () => {
    const obsolete = deferred<JobSummaryResponse[]>();
    vi.mocked(api.listRecentJobs).mockReturnValueOnce(obsolete.promise).mockResolvedValueOnce([completed]);
    render(<StrictMode><MemoryRouter><HistoryPage /></MemoryRouter></StrictMode>);
    await screen.findByRole('article', { name: 'Translation to German' });
    expect(vi.mocked(api.listRecentJobs).mock.calls[0][1]?.aborted).toBe(true);
    await act(async () => obsolete.resolve([failed]));
    expect(screen.getByRole('article', { name: 'Translation to German' })).toBeInTheDocument();
    expect(screen.queryByRole('article', { name: 'Translation to Swedish' })).not.toBeInTheDocument();
  });

  it('handles catalogued errors and permits a history refresh', async () => {
    vi.mocked(api.listRecentJobs).mockRejectedValueOnce(new ApiError('not_ready')).mockResolvedValueOnce([]);
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('Service dependencies are unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Reload history' }));
    await waitFor(() => expect(screen.getByText('No translations yet.')).toBeInTheDocument());
  });

  it('renders the history route from the app', async () => {
    render(<MemoryRouter initialEntries={['/history']}><App /></MemoryRouter>);
    expect(screen.getByRole('heading', { name: 'Translation history' })).toBeInTheDocument();
    expect(await screen.findByText('No translations yet.')).toBeInTheDocument();
  });
});
