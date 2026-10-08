/** Read-only setup illustration. It never sends motion or connects a state socket. */
import * as THREE from 'three';
import { RobotScene } from './robot-scene.js?v=diagnostic-deck1';

const COVER_COLOR = 0xff963d;

function positive(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : fallback;
}

function definitionMatches(detail, error) {
    const id = error.labware_definition_id || error.labware_id;
    if (id) return [detail.definition_id, detail.labware_id, detail.id].includes(id);
    return !error.labware_name || detail.name === error.labware_name;
}

/** Resolve saved setup metadata without changing the editor's deck configuration. */
export function prepareDiagnosticDeckDetails(deckDetails, error) {
    const location = Number(error?.location);
    if (error?.kind !== 'covered_labware' || !Number.isInteger(location) || location < 1 || location > 9) {
        throw new Error('This diagnostic does not include a covered deck position.');
    }
    const sourceStack = deckDetails?.[String(location)];
    if (!Array.isArray(sourceStack) || !sourceStack.length) {
        throw new Error('The affected labware is missing from this setup snapshot.');
    }
    const stackIndex = Number.isInteger(error.stack_index)
        ? error.stack_index
        : sourceStack.findIndex(detail => definitionMatches(detail, error));
    const source = sourceStack[stackIndex];
    if (!source || !definitionMatches(source, error)) {
        throw new Error('The affected labware no longer matches this setup snapshot.');
    }
    const result = {};
    for (const [loc, stack] of Object.entries(deckDetails)) {
        if (!Array.isArray(stack)) continue;
        result[loc] = stack.map((original, index) => {
            const detail = { ...original };
            const selected = Number(loc) === location && index === stackIndex;
            if (selected) {
                if (typeof error.is_lidded === 'boolean') detail.is_lidded = error.is_lidded;
                if (typeof error.is_sealed === 'boolean') detail.is_sealed = error.is_sealed;
            }
            const baseHeight = positive(detail.base_height_mm, positive(detail.height_mm, positive(detail.height)));
            let height = detail.is_lidded
                ? positive(detail.lidded_height_mm, positive(detail.total_height_mm, baseHeight))
                : detail.is_sealed ? positive(detail.sealed_height_mm, baseHeight) : baseHeight;
            // The reported envelope may establish the active exterior height.
            // It is not rendered as a collision/contact shape or rejected pose.
            if (selected && Array.isArray(error.lower_mm) && Array.isArray(error.upper_mm)
                && error.lower_mm.length === 3 && error.upper_mm.length === 3
                && error.lower_mm.every(Number.isFinite) && error.upper_mm.every(Number.isFinite)
                && error.upper_mm.every((value, axis) => value > error.lower_mm[axis])) {
                height = error.upper_mm[2] - error.lower_mm[2];
            }
            detail.base_height_mm = baseHeight;
            detail.total_height_mm = height;
            detail.height_mm = height;
            if (detail.is_lidded) {
                detail.stack_height_mm = positive(detail.lidded_stack_height_mm, height);
                // Mirrors generated_lid_metadata / lid_thickness_mm in
                // pybravo/deck/labware.py, then uses RobotScene's existing lid.
                const lidded = positive(detail.lidded_height_mm);
                const resting = positive(detail.lid_resting_height_mm);
                const thickness = lidded > resting && resting > 0 ? lidded - resting
                    : lidded > baseHeight && baseHeight > 0 ? lidded - baseHeight
                    : resting || 0.1;
                detail.generated_lid = detail.generated_lid ? { ...detail.generated_lid } : {
                    name: `${detail.name || 'Labware'} Lid`,
                    kind: 'lid', base_class: 'lid', render_mode: 'generated_lid',
                    length_mm: positive(detail.length_mm, positive(detail.length)),
                    width_mm: positive(detail.width_mm, positive(detail.width)),
                    height_mm: Math.max(0.1, thickness),
                };
            } else {
                delete detail.generated_lid;
                if (detail.is_sealed) detail.stack_height_mm = positive(detail.sealed_stacking_height_mm, height);
            }
            return detail;
        });
    }
    const detail = result[String(location)][stackIndex];
    if (!detail.is_lidded && !detail.is_sealed) throw new Error('No lid or seal is recorded for this labware.');
    if (!positive(detail.length_mm, positive(detail.length))
        || !positive(detail.width_mm, positive(detail.width)) || !detail.base_height_mm) {
        throw new Error('The catalog is missing dimensions for this labware preview.');
    }
    return { deckDetails: result, location, stackIndex, detail };
}

function tintMeshes(root, color, retiredMaterials, { opacity = 1, emissive = 0x000000 } = {}) {
    root.traverse(object => {
        if (!object.isMesh) return;
        const tint = material => {
            retiredMaterials.add(material);
            const copy = material.clone();
            copy.color?.setHex(color);
            copy.emissive?.setHex(emissive);
            copy.opacity = opacity;
            copy.transparent = opacity < 1;
            copy.depthWrite = opacity >= 1;
            return copy;
        };
        object.material = Array.isArray(object.material) ? object.material.map(tint) : tint(object.material);
    });
}

function highlightCover(group, retiredMaterials) {
    tintMeshes(group, COVER_COLOR, retiredMaterials, { opacity: 0.9, emissive: 0x70240a });
    const meshes = [];
    group.traverse(object => { if (object.isMesh) meshes.push(object); });
    for (const mesh of meshes) {
        // Trace the rendered lid, not a fabricated collision box.
        mesh.add(new THREE.LineSegments(
            new THREE.EdgesGeometry(mesh.geometry),
            new THREE.LineBasicMaterial({ color: 0xffc070, transparent: true, opacity: 0.95 }),
        ));
    }
}

function addSealHighlight(group, detail, retiredMaterials) {
    // A flat surface annotation makes a recorded seal visible; there is no
    // catalog seal collision mesh and this preview does not invent one.
    const seal = new THREE.Mesh(
        new THREE.PlaneGeometry(
            positive(detail.length_mm, positive(detail.length)) / 1000,
            positive(detail.width_mm, positive(detail.width)) / 1000,
        ),
        new THREE.MeshStandardMaterial({
            color: COVER_COLOR, emissive: 0x70240a, side: THREE.DoubleSide,
            transparent: true, opacity: 0.82, roughness: 0.36,
        }),
    );
    seal.name = 'recorded-seal-highlight';
    seal.userData.labwarePart = 'seal-annotation';
    seal.position.z = detail.total_height_mm / 1000 + 0.00015;
    group.add(seal);
    highlightCover(seal, retiredMaterials);
}

/**
 * Returns immediately so the caller can dispose while assets are loading.
 * `ready` resolves when the setup view is focused, or rejects with a readable
 * asset/setup error. Closing the popup makes any late loading work inert.
 */
export function createPhysicalDiagnosticView(container, { error, deckDetails } = {}) {
    let disposed = false;
    let scene = null;
    let focus = null;
    const retiredMaterials = new Set();

    const dispose = () => {
        if (disposed) return;
        disposed = true;
        scene?.dispose();
        retiredMaterials.forEach(material => material.dispose());
        retiredMaterials.clear();
    };

    const resetView = () => {
        if (disposed || !scene || !focus) return;
        scene.resize();
        const radius = Math.max(focus.radius, 0.07);
        const verticalFov = THREE.MathUtils.degToRad(scene.camera.fov);
        const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * scene.camera.aspect);
        const distance = radius / Math.sin(Math.min(verticalFov, horizontalFov) / 2) * 1.45;
        scene.camera.position.copy(focus.center).addScaledVector(
            new THREE.Vector3(0.8, 1.35, 1.2).normalize(), distance,
        );
        scene.controls.target.copy(focus.center);
        scene.controls.minDistance = radius * 0.65;
        scene.controls.maxDistance = Math.max(distance * 4, 1);
        scene.controls.maxPolarAngle = Math.PI * 0.49;
        scene.controls.saveState();
        scene.controls.update();
    };

    const ready = Promise.resolve().then(async () => {
        if (disposed) return;
        const setup = prepareDiagnosticDeckDetails(deckDetails, error);
        scene = new RobotScene(container, { autoConnect: false, showGizmo: false, deckOnly: true });
        await scene.init();
        if (disposed) return;
        if (!scene.deckSlotAnchors.has(setup.location)) {
            throw new Error('The deck asset could not be loaded for this preview.');
        }
        await scene.setDeckDetails(setup.deckDetails);
        if (disposed) return;
        const target = scene.labwareRoot.children.find(group =>
            group.userData.deckLocation === setup.location && group.userData.stackIndex === setup.stackIndex);
        if (!target) throw new Error('The affected labware could not be drawn.');

        // Keep neighboring positions in view while reducing their contrast.
        for (const group of scene.labwareRoot.children) {
            if (group !== target) tintMeshes(group, 0x566074, retiredMaterials, { opacity: 0.55 });
        }
        for (const linkName of scene.deckSlotLinkNames.values()) {
            const deck = scene.urdfRobot._links[linkName];
            if (deck) tintMeshes(deck, 0x465064, retiredMaterials);
        }
        if (setup.detail.is_lidded) {
            const lids = [];
            target.traverse(object => { if (object.userData.labwarePart === 'lid') lids.push(object); });
            if (!lids.length) throw new Error('The configured lid could not be drawn.');
            lids.forEach(lid => highlightCover(lid, retiredMaterials));
        }
        if (setup.detail.is_sealed) addSealHighlight(target, setup.detail, retiredMaterials);

        scene.scene.updateMatrixWorld(true);
        const bounds = new THREE.Box3().setFromObject(target);
        focus = bounds.getBoundingSphere(new THREE.Sphere());
        scene.setBackground(getComputedStyle(container).getPropertyValue('--bg-viewport').trim() || '#0d0d14');
        scene.renderer.domElement.setAttribute('role', 'img');
        scene.renderer.domElement.setAttribute('aria-label',
            `Setup preview, position ${setup.location}: ${setup.detail.name || 'labware'}, `
            + `${setup.detail.is_lidded ? 'lid' : 'seal'} highlighted. Drag to rotate, scroll to zoom.`);
        resetView();
    }).catch(error => {
        dispose();
        throw error;
    });

    return { ready, dispose, resetView };
}
