import * as THREE from 'three';

// Color lives in the URDF, so the control panel, designer and external URDF
// viewers share the same skin. These add surface response for the web viewers.
const FINISHES = {
    bravo_housing: { roughness: 0.66, metalness: 0.04 },
    bravo_frame: { roughness: 0.72, metalness: 0.18 },
    bravo_deck: { roughness: 0.78, metalness: 0.16 },
    bravo_tooling: { roughness: 0.34, metalness: 0.4 },
    bravo_peek: { roughness: 0.58, metalness: 0.0 },
    bravo_gasket: { roughness: 0.92, metalness: 0.0 },
    bravo_pad: { roughness: 0.76, metalness: 0.06 },
};

export function createRobotMaterial(visual) {
    const material = visual.querySelector('material');
    const name = material?.getAttribute('name') || '';
    const rgba = material?.querySelector('color')?.getAttribute('rgba')?.trim().split(/\s+/).map(Number);
    const color = rgba
        ? new THREE.Color().setRGB(rgba[0], rgba[1], rgba[2], THREE.SRGBColorSpace)
        : new THREE.Color(0xd4d5d8);
    const finish = Object.entries(FINISHES).find(([prefix]) => name.startsWith(prefix))?.[1];
    return new THREE.MeshStandardMaterial({
        name,
        color,
        roughness: 0.65,
        metalness: 0.1,
        ...finish,
        opacity: rgba?.[3] ?? 1,
        transparent: (rgba?.[3] ?? 1) < 1,
    });
}
