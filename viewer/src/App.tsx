import { useCallback, useEffect, useState } from "react";
import Viewer from "./Viewer";
import { createJob, createUpload, getJob, putToUploadUrl, subscribeToJobEvents, type JobStatus } from "./api";
import "./App.css";

const STAGE_LABELS: Record<string, string> = {
  segment_crop_fov: "Segmenting & estimating camera",
  structure_coarse_mesh: "Generating coarse structure",
  shape_upsample: "Upsampling shape",
  texture_sample: "Sampling texture",
  remesh_paint_final: "Remeshing & painting final mesh",
};

function App() {
  const [job, setJob] = useState<JobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshJob = useCallback(async (jobId: string) => {
    try {
      const status = await getJob(jobId);
      setJob(status);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    if (!job || job.status === "succeeded" || job.status === "failed") return;
    return subscribeToJobEvents(job.job_id, () => refreshJob(job.job_id));
  }, [job?.job_id, job?.status, refreshJob]);

  const onFileChosen = async (file: File) => {
    setError(null);
    setBusy(true);
    setJob(null);
    try {
      const upload = await createUpload(file);
      await putToUploadUrl(upload.upload_url, file);
      const created = await createJob(upload.object_key);
      await refreshJob(created.job_id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  // Prefer the compressed final GLB once it's ready (smaller download, same
  // geometry -- PLAN.md sec.7.3), fall back to the coarse Stage-2 preview
  // while the rest of the pipeline is still running (sec.6).
  const modelUrl = job?.final_glb_compressed_url ?? job?.final_glb_url ?? job?.coarse_glb_url ?? null;

  return (
    <div className="app">
      <header>
        <h1>image23D</h1>
        <p>Athlete photo &rarr; textured 3D model</p>
      </header>

      <div className="controls">
        <input
          type="file"
          accept="image/*"
          disabled={busy}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onFileChosen(file);
          }}
        />
        {error && <p className="error">{error}</p>}
      </div>

      {job && (
        <div className="progress">
          <p>
            Status: <strong>{job.status}</strong>
            {job.status === "running" && job.stage && ` — ${STAGE_LABELS[job.stage] ?? job.stage}`}
          </p>
          {job.status === "failed" && <p className="error">{job.error}</p>}
          {job.status === "running" && !job.final_glb_url && job.coarse_glb_url && (
            <p className="hint">Showing coarse preview while the final mesh finishes...</p>
          )}
        </div>
      )}

      <div className="viewer-container">
        {modelUrl ? <Viewer url={modelUrl} /> : <div className="placeholder">Upload a photo to begin</div>}
      </div>
    </div>
  );
}

export default App;
