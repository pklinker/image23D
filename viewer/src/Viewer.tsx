import { Component, Suspense, type ReactNode } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Stage } from "@react-three/drei";
import * as THREE from "three";
import Model from "./Model";

/** Catches a failed glTF load so an expired presigned URL can be retried.
 *
 * useGLTF throws to the nearest error boundary; without one, a 403 from an
 * expired URL takes down the whole canvas with no way back. Resets whenever the
 * url changes so a retry gets a fresh attempt.
 */
class ModelErrorBoundary extends Component<
  { url: string; onError: () => void; children: ReactNode },
  { failedUrl: string | null }
> {
  state: { failedUrl: string | null } = { failedUrl: null };

  componentDidCatch() {
    this.setState({ failedUrl: this.props.url });
    this.props.onError();
  }

  render() {
    // Only suppress the children for the url that actually failed -- once a
    // freshly signed one arrives, render again.
    if (this.state.failedUrl === this.props.url) return null;
    return this.props.children;
  }
}

export default function Viewer({ url, onLoadError }: { url: string; onLoadError?: () => void }) {
  // No key={url} here: the API re-signs presigned URLs (fresh signature +
  // timestamp) on every poll even when the underlying object hasn't changed,
  // so keying the Canvas on the raw url string was tearing down and
  // recreating the WebGL context on almost every SSE tick, not just on real
  // coarse->final swaps (visible as repeated "Context Lost" in devtools).
  // Model's useGLTF(url) suspends and swaps the loaded scene on its own.
  return (
    <Canvas
      camera={{ position: [0, 1, 3], fov: 45 }}
      gl={{ outputColorSpace: THREE.SRGBColorSpace }}
    >
      <Suspense fallback={null}>
        <ModelErrorBoundary url={url} onError={() => onLoadError?.()}>
          <Stage environment="city" intensity={0.5} adjustCamera={1.2}>
            <Model url={url} />
          </Stage>
        </ModelErrorBoundary>
      </Suspense>
      <OrbitControls makeDefault />
    </Canvas>
  );
}
