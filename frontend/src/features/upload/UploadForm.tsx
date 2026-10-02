import { useId, useState, type FormEvent } from 'react';

export const MAX_FILE_BYTES = 50 * 1024 * 1024;
const fileTypes: Record<string, string> = {
  pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
};
export function validateFile(file: File): string | null {
  const extension = file.name.split('.').pop()?.toLowerCase() ?? '';
  if (!Object.hasOwn(fileTypes, extension)) return 'Choose a PDF or DOCX document.';
  if (file.type !== fileTypes[extension]) return 'The file type must match its PDF or DOCX extension.';
  if (file.size > MAX_FILE_BYTES) return 'Document exceeds the 50 MiB upload limit.';
  return null;
}

const languages = [
  ['de', 'German'], ['fr', 'French'], ['es', 'Spanish'], ['en', 'English'],
  ['it', 'Italian'], ['pt', 'Portuguese'], ['uk', 'Ukrainian'], ['sv', 'Swedish'],
];
interface Props { busy: boolean; onSubmit: (file: File, languages: string[]) => void }

export function UploadForm({ busy, onSubmit }: Props) {
  const id = useId();
  const [file, setFile] = useState<File | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  function choose(files: FileList | null) {
    if (busy || !files?.length) return;
    if (files.length !== 1) {
      setFile(null);
      setError('Choose one document at a time.');
      return;
    }
    const next = files[0];
    const issue = validateFile(next);
    setFile(issue ? null : next);
    setError(issue);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!file) { setError('Choose a PDF or DOCX document.'); return; }
    const issue = validateFile(file);
    if (issue) { setError(issue); return; }
    if (!selected.length) { setError('Select at least one target language.'); return; }
    setError(null);
    onSubmit(file, selected);
  }

  return (
    <form onSubmit={submit} className="upload-panel space-y-6" aria-busy={busy}>
      <div
        className={`upload-dropzone border-2 border-dashed text-center ${dragging ? 'border-stark-red' : 'border-neutral-600'}`}
        onDragOver={(event) => { event.preventDefault(); if (!busy) setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => { event.preventDefault(); setDragging(false); choose(event.dataTransfer.files); }}
      >
        <label htmlFor={`${id}-file`} className="mb-3 block font-semibold">PDF or DOCX document</label>
        <p id={`${id}-help`} className="mb-4 text-neutral-300">Drop one document here, or choose a file. Maximum 50 MiB.</p>
        <input
          id={`${id}-file`}
          type="file"
          accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          disabled={busy}
          aria-describedby={`${id}-help${error ? ` ${id}-error` : ''}`}
          aria-invalid={!!error}
          onChange={(event) => choose(event.currentTarget.files)}
          className="max-w-full"
        />
        {file && <p className="mt-4 break-words">Selected: {file.name}</p>}
      </div>
      <fieldset disabled={busy} className="rounded border border-neutral-600 p-4">
        <legend className="px-2 font-semibold">Target languages</legend>
        <p className="mb-4 text-sm text-neutral-300">Select one or more languages.</p>
        <div className="grid grid-cols-1 gap-2 min-[380px]:grid-cols-2 sm:grid-cols-4">
          {languages.map(([code, label]) => (
            <label key={code} className="language-choice flex items-center gap-2">
              <input type="checkbox" value={code} checked={selected.includes(code)} onChange={(event) => {
                setSelected((previous) => event.target.checked ? [...previous, code] : previous.filter((language) => language !== code));
              }} />
              {label}
            </label>
          ))}
        </div>
      </fieldset>
      {error && <p id={`${id}-error`} role="alert" className="text-red-300">{error}</p>}
      <button type="submit" disabled={busy} className="button-primary rounded px-6 py-3 disabled:opacity-50">Translate document</button>
    </form>
  );
}
