import { Suspense } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Stage } from "@react-three/drei";
import * as THREE from "three";
import Model from "./Model";

export default function Viewer({ url }: { url: string }) {
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
        <Stage environment="city" intensity={0.5} adjustCamera={1.2}>
          <Model url={url} />
        </Stage>
      </Suspense>
      <OrbitControls makeDefault />
    </Canvas>
  );
}
