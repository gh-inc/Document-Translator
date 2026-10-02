import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { ApiError } from '../../api/errors';
import UploadPage from './UploadPage';

vi.mock('../../api/client', () => ({ api: { uploadDocument: vi.fn(), getDocument: vi.fn(), retryTriage: vi.fn(), createJobs: vi.fn() } }));
function Destination() { return <p>Batch: {useParams().batchId}</p>; }
function setup() {
  render(<MemoryRouter><Routes><Route path="/" element={<UploadPage />} /><Route path="/batches/:batchId" element={<Destination />} /></Routes></MemoryRouter>);
  fireEvent.change(screen.getByLabelText('PDF or DOCX document'), { target: { files: [new File(['pdf'], 'file.pdf', { type: 'application/pdf' })] } });
  fireEvent.click(screen.getByLabelText('German'));
  fireEvent.click(screen.getByRole('button', { name: 'Translate document' }));
}

describe('upload page', () => {
  beforeEach(() => {
    vi.mocked(api.uploadDocument).mockReset().mockResolvedValue({ id: 'doc', filename: 'file.pdf', format: 'pdf', status: 'extracted', block_count: 1 });
    vi.mocked(api.createJobs).mockReset().mockResolvedValue({ batch_id: 'batch-123', jobs: [] });
  });

  it('navigates to the created batch', async () => {
    setup();
    expect(await screen.findByText('Batch: batch-123')).toBeInTheDocument();
  });

  it('shows safe failures and concrete submission retry and document recovery actions', async () => {
    vi.mocked(api.createJobs).mockRejectedValueOnce(new ApiError('provider_connection'));
    setup();
    const retry = await screen.findByRole('button', { name: 'Retry translation submission' });
    expect(screen.getByRole('status')).toHaveTextContent('Translation provider is unreachable');
    expect(screen.getByRole('button', { name: 'Choose another document' })).toBeInTheDocument();
    fireEvent.click(retry);
    expect(await screen.findByText('Batch: batch-123')).toBeInTheDocument();
  });

  it('lets users cancel analysis and choose another document', async () => {
    vi.mocked(api.uploadDocument).mockResolvedValue({ id: 'doc', filename: 'file.pdf', format: 'pdf', status: 'analyzing', block_count: 0 });
    setup();
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Analyzing'));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('status')).toHaveTextContent('Request cancelled');
    fireEvent.click(screen.getByRole('button', { name: 'Choose another document' }));
    expect(screen.queryByText('Selected: file.pdf')).not.toBeInTheDocument();
    expect(screen.getByLabelText('German')).not.toBeChecked();
    expect(api.createJobs).not.toHaveBeenCalled();
  });
});
