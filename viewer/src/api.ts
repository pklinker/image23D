const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export interface JobStatus {
  job_id: string;
  status: "pending" | "running" | "succeeded" | "failed";
  stage: string | null;
  error: string | null;
  stage_timings: { stage: string; seconds: number }[];
  coarse_glb_url: string | null;
  final_glb_url: string | null;
  final_glb_compressed_url: string | null;
  created_at: string;
  updated_at: string;
}

export async function createUpload(file: File): Promise<{ object_key: string; upload_url: string }> {
  const res = await fetch(`${API_BASE}/v1/uploads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: file.name, content_type: file.type || "image/png" }),
  });
  if (!res.ok) throw new Error(`upload request failed: ${res.status}`);
  return res.json();
}

export async function putToUploadUrl(uploadUrl: string, file: File): Promise<void> {
  const res = await fetch(uploadUrl, {
    method: "PUT",
    headers: { "Content-Type": file.type || "image/png" },
    body: file,
  });
  if (!res.ok) throw new Error(`upload to storage failed: ${res.status}`);
}

export async function createJob(objectKey: string): Promise<{ job_id: string }> {
  const res = await fetch(`${API_BASE}/v1/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ object_key: objectKey }),
  });
  if (!res.ok) throw new Error(`job creation failed: ${res.status}`);
  return res.json();
}

export async function getJob(jobId: string): Promise<JobStatus> {
  const res = await fetch(`${API_BASE}/v1/jobs/${jobId}`);
  if (!res.ok) throw new Error(`job fetch failed: ${res.status}`);
  return res.json();
}

export function subscribeToJobEvents(jobId: string, onEvent: () => void): () => void {
  const source = new EventSource(`${API_BASE}/v1/jobs/${jobId}/events`);
  source.onmessage = () => onEvent();
  return () => source.close();
}
