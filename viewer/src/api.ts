const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";
const API_KEY_STORAGE_KEY = "image23d_api_key";

export interface JobStatus {
  job_id: string;
  status: "pending" | "running" | "succeeded" | "failed";
  stage: string | null;
  error: string | null;
  stage_timings: { stage: string; seconds: number }[];
  total_seconds: number | null;
  gpu_peak_mb: number | null;
  coarse_glb_url: string | null;
  final_glb_url: string | null;
  final_glb_compressed_url: string | null;
  created_at: string;
  updated_at: string;
}

export function getApiKey(): string | null {
  return localStorage.getItem(API_KEY_STORAGE_KEY);
}

export function setApiKey(key: string): void {
  localStorage.setItem(API_KEY_STORAGE_KEY, key);
}

function authHeaders(): Record<string, string> {
  const key = getApiKey();
  return key ? { Authorization: `Bearer ${key}` } : {};
}

export async function createUpload(file: File): Promise<{ object_key: string; upload_url: string }> {
  const res = await fetch(`${API_BASE}/v1/uploads`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ filename: file.name, content_type: file.type || "image/png" }),
  });
  if (!res.ok) throw new Error(`upload request failed: ${res.status}`);
  return res.json();
}

export async function putToUploadUrl(uploadUrl: string, file: File): Promise<void> {
  // Goes straight to MinIO with a presigned URL, not through the API -- no
  // API key needed or accepted here.
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
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ object_key: objectKey }),
  });
  if (!res.ok) throw new Error(`job creation failed: ${res.status}`);
  return res.json();
}

export async function getJob(jobId: string): Promise<JobStatus> {
  const res = await fetch(`${API_BASE}/v1/jobs/${jobId}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`job fetch failed: ${res.status}`);
  return res.json();
}

export function subscribeToJobEvents(jobId: string, onEvent: () => void): () => void {
  // EventSource can't set custom headers, so the API also accepts the key as
  // a query param on this one route.
  const key = getApiKey();
  const url = new URL(`${API_BASE}/v1/jobs/${jobId}/events`);
  if (key) url.searchParams.set("api_key", key);
  const source = new EventSource(url);
  source.onmessage = () => onEvent();
  return () => source.close();
}
