import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MAX_FILE_BYTES, UploadForm, validateFile } from './UploadForm';

const pdf = () => new File(['pdf'], 'sample.pdf', { type: 'application/pdf' });
describe('upload form', () => {
  it('validates extension, matching MIME and the 50 MiB boundary', () => {
    expect(validateFile(pdf())).toBeNull();
    expect(validateFile(new File(['docx'], 'sample.DOCX', { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }))).toBeNull();
    expect(validateFile(new File(['pdf'], 'sample.txt', { type: 'application/pdf' }))).toContain('PDF, DOCX, or Markdown');
    expect(validateFile(new File(['pdf'], 'sample.pdf', { type: 'application/zip' }))).toContain('match');
    expect(validateFile(new File(['pdf'], 'sample.pdf'))).toContain('match');
    expect(validateFile(new File(['markdown'], 'notes.md', { type: 'text/markdown' }))).toBeNull();
    expect(validateFile(new File(['markdown'], 'notes.MD', { type: 'text/plain' }))).toBeNull();
    expect(validateFile(new File(['markdown'], 'notes.md'))).toBeNull();
    expect(validateFile(new File(['markdown'], 'notes.md', { type: 'application/octet-stream' }))).toContain('match');
    const file = pdf();
    Object.defineProperty(file, 'size', { value: MAX_FILE_BYTES, configurable: true });
    expect(validateFile(file)).toBeNull();
    Object.defineProperty(file, 'size', { value: MAX_FILE_BYTES + 1 });
    expect(validateFile(file)).toContain('50 MiB');
  });

  it('requires a file and at least one language, then submits selected languages', () => {
    const onSubmit = vi.fn();
    render(<UploadForm busy={false} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByRole('button', { name: 'Translate document' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a PDF, DOCX, or Markdown');
    const file = pdf();
    fireEvent.change(screen.getByLabelText('PDF, DOCX, or Markdown document'), { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: 'Translate document' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Select at least one');
    fireEvent.click(screen.getByLabelText('German'));
    fireEvent.click(screen.getByLabelText('French'));
    fireEvent.click(screen.getByRole('button', { name: 'Translate document' }));
    expect(onSubmit).toHaveBeenCalledWith(file, ['de', 'fr']);
  });

  it('supports dropping one file and reports multiple-file drops', () => {
    render(<UploadForm busy={false} onSubmit={vi.fn()} />);
    const zone = screen.getByText('Drop one document here, or choose a file. Maximum 50 MiB.').parentElement!;
    fireEvent.drop(zone, { dataTransfer: { files: [pdf()] } });
    expect(screen.getByText('Selected: sample.pdf')).toBeInTheDocument();
    fireEvent.drop(zone, { dataTransfer: { files: [pdf(), pdf()] } });
    expect(screen.getByRole('alert')).toHaveTextContent('one document');
  });

  it('disables input and submission while working', () => {
    render(<UploadForm busy onSubmit={vi.fn()} />);
    expect(screen.getByLabelText('PDF, DOCX, or Markdown document')).toBeDisabled();
    expect(screen.getByLabelText('German')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Translate document' })).toBeDisabled();
  });
});
