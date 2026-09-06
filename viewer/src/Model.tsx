import { useEffect } from "react";
import { useGLTF } from "@react-three/drei";
import * as THREE from "three";

// PLAN.md sec.7's vertex-color gotchas:
//  1. three.js renders flat grey unless material.vertexColors is explicitly
//     true -- GLTFLoader sets this when it sees COLOR_0, but we force it here
//     too since it's the single most common "my colors vanished" cause and
//     costs nothing to double up on.
//  2. COLOR_0 is defined as linear by the glTF spec; three.js's vertex-color
//     path does not re-encode it, so as long as the renderer's own
//     outputColorSpace is set (see Viewer.tsx) no further conversion is
//     needed here.
export default function Model({ url }: { url: string }) {
  const { scene } = useGLTF(url);

  useEffect(() => {
    scene.traverse((obj) => {
      if (obj instanceof THREE.Mesh) {
        const materials = Array.isArray(obj.material) ? obj.material : [obj.material];
        for (const material of materials) {
          if ("vertexColors" in material && !material.vertexColors) {
            material.vertexColors = true;
            material.needsUpdate = true;
          }
        }
      }
    });
  }, [scene]);

  // Release the previous model when swapping coarse -> final, or on unmount.
  // Neither happens automatically: drei caches the parsed glTF by URL, and
  // three.js never frees GPU buffers on its own. Without this a job leaves its
  // coarse mesh's geometry resident for the life of the page.
  useEffect(() => {
    return () => {
      scene.traverse((obj) => {
        if (obj instanceof THREE.Mesh) {
          obj.geometry?.dispose();
          const materials = Array.isArray(obj.material) ? obj.material : [obj.material];
          for (const material of materials) material?.dispose();
        }
      });
      useGLTF.clear(url);
    };
  }, [scene, url]);

  return <primitive object={scene} />;
}
