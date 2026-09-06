import { useCallback, useEffect, useState } from "react";
import Viewer from "./Viewer";
import {
  createJob,
  createUpload,
  getApiKey,
  getJob,
  preferredModel,
  putToUploadUrl,
  setApiKey,
  subscribeToJobEvents,
  type JobStatus,
} from "./api";
import "./App.css";

const STAGE_LABELS: Record<string, string> = {
  segment_crop_fov: "Segmenting & estimating camera",
  structure_coarse_mesh: "Generating coarse structure",
  shape_upsample: "Upsampling shape",
  texture_sample: "Sampling texture",
  remesh_paint_final: "Remeshing & painting final mesh",
};

/** The model currently in the viewer, identified by its stable object key. */
interface LoadedModel {
  key: string;
  url: string;
}

function App() {
  const [job, setJob] = useState<JobStatus | null>(null);
  const [model, setModel] = useState<LoadedModel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [hasApiKey, setHasApiKey] = useState(() => Boolean(getApiKey()));

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
    setModel(null);
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
  //
  // Swap only when the *key* changes. The API re-signs presigned URLs on every
  // read, so the url string differs on almost every poll even when the object
  // is unchanged; keying on it made useGLTF treat each poll as a new asset and
  // re-download and re-parse the mesh several times per job.
  useEffect(() => {
    const next = job ? preferredModel(job) : null;
    if (!next) return;
    setModel((current) => (current && current.key === next.key ? current : next));
  }, [job]);

  // Presigned URLs expire (PRESIGNED_URL_TTL_SECONDS, 1h by default), so a page
  // left open long enough will fail to load the model. Re-poll once for a
  // freshly signed URL rather than showing a dead viewer.
  const refreshModelUrl = useCallback(async () => {
    if (!job) return;
    try {
      const fresh = await getJob(job.job_id);
      setJob(fresh);
      const next = preferredModel(fresh);
      if (next) setModel(next);
    } catch (e) {
      setError(String(e));
    }
  }, [job?.job_id]);

  return (
    <div className="app">
      <header>
        <h1>image23D</h1>
        <p>Athlete photo &rarr; textured 3D model</p>
      </header>

      {!hasApiKey ? (
        <div className="controls">
          <p>Enter an API key to continue (ask whoever ran <code>scripts/create_api_key.py</code>).</p>
          <input
            type="password"
            placeholder="i23d_..."
            value={apiKeyInput}
            onChange={(e) => setApiKeyInput(e.target.value)}
          />
          <button
            disabled={!apiKeyInput}
            onClick={() => {
              setApiKey(apiKeyInput);
              setHasApiKey(true);
            }}
          >
            Save key
          </button>
        </div>
      ) : (
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
      )}

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
        {model ? (
          <Viewer url={model.url} onLoadError={refreshModelUrl} />
        ) : (
          <div className="placeholder">Upload a photo to begin</div>
        )}
      </div>
    </div>
  );
}

export default App;
