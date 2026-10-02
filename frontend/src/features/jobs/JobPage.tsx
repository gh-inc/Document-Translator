import { useParams } from 'react-router-dom';
import JobCard from './JobCard';

export default function JobPage() {
  const { jobId } = useParams();
  return <section><h1 className="mb-8 text-3xl font-semibold">Translation progress</h1>{jobId ? <JobCard jobId={jobId} /> : <p role="alert">Translation ID is missing.</p>}</section>;
}
