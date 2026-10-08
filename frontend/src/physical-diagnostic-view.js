/** Read-only obstruction evidence. It never sends motion or connects a state socket. */
import * as THREE from 'three';
import { RobotScene } from './robot-scene.js?v=cellvis-lid1';

const COVER_COLOR = 0xff963d;

function positive(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : fallback;
}

function validBounds(error) {
    return error.coordinate_frame === 'machine_xyz_z_up_mm'
        && [error.lower_mm, error.upper_mm].every(values => Array.isArray(values)
            && values.length === 3 && values.every(Number.isFinite))
        && error.upper_mm.every((value, axis) => value > error.lower_mm[axis]);
}

function validLidGeometry(geometry, detail = null) {
    if (geometry?.model !== 'manufacturer_exterior_envelope'
        || typeof geometry.source !== 'string' || !geometry.source.trim()
        || !['length_mm', 'width_mm', 'height_mm'].every(key =>
            typeof geometry[key] === 'number' && Number.isFinite(geometry[key]) && geometry[key] > 0)
        || !Number.isFinite(geometry.seated_bottom_mm) || geometry.seated_bottom_mm < 0) return false;
    const top = geometry.seated_bottom_mm + geometry.height_mm;
    if (!Number.isFinite(top)) return false;
    if (detail?.lidded_height_mm != null
        && (!Number.isFinite(detail.lidded_height_mm) || Math.abs(detail.lidded_height_mm - top) > 1e-6)) return false;
    const plateHeight = detail?.base_height_mm ?? detail?.height_mm;
    return plateHeight == null || (Number.isFinite(plateHeight)
        && geometry.seated_bottom_mm < plateHeight && plateHeight <= top);
}

function dimensionedLid(error) {
    return error?.geometry === 'manufacturer_exterior_envelope' && validLidGeometry(error.lid_geometry);
}

function definitionMatches(detail, error) {
    const id = error.labware_definition_id || error.labware_id;
    if (id) return [detail.definition_id, detail.labware_id, detail.id].includes(id);
    return !error.labware_name || detail.name === error.labware_name;
}

/** Resolve saved setup metadata without changing the editor's deck configuration. */
export function prepareDiagnosticDeckDetails(deckDetails, error) {
    const location = Number(error?.location);
    if (!['covered_labware', 'lid_access_blocked', 'lid_collision'].includes(error?.kind)
        || !Number.isInteger(location) || location < 1 || location > 9) {
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
            if (!validLidGeometry(detail.lid_geometry, detail)) {
                delete detail.lid_geometry;
                // Never present a backend-rejected geometry record as checked.
                if (detail.generated_lid?.lid_geometry) delete detail.generated_lid;
            }
            const selected = Number(loc) === location && index === stackIndex;
            if (selected) {
                if (typeof error.is_lidded === 'boolean') detail.is_lidded = error.is_lidded;
                if (typeof error.is_sealed === 'boolean') detail.is_sealed = error.is_sealed;
            }
            const baseHeight = positive(detail.base_height_mm, positive(detail.height_mm, positive(detail.height)));
            let height = detail.is_lidded
                ? positive(detail.lidded_height_mm, positive(detail.total_height_mm, baseHeight))
                : detail.is_sealed ? positive(detail.sealed_height_mm, baseHeight) : baseHeight;
            if (selected && dimensionedLid(error)) {
                // These bounds describe the lid alone, not the complete plate.
                // Keep the plate floor at its real height and seat the lid above it.
                detail.lid_geometry = { ...error.lid_geometry };
                height = detail.lid_geometry.seated_bottom_mm + detail.lid_geometry.height_mm;
                detail.lidded_height_mm = height;
                detail.lid_resting_height_mm = detail.lid_geometry.seated_bottom_mm;
            } else if (selected && error.geometry !== 'manufacturer_exterior_envelope'
                && Array.isArray(error.lower_mm) && Array.isArray(error.upper_mm)
                && error.lower_mm.length === 3 && error.upper_mm.length === 3
                && error.lower_mm.every(Number.isFinite) && error.upper_mm.every(Number.isFinite)
                && error.upper_mm.every((value, axis) => value > error.lower_mm[axis])) {
                // Older setup failures carry the full plate exterior bounds.
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
                if (validLidGeometry(detail.lid_geometry, detail)) {
                    const lid = detail.lid_geometry;
                    detail.generated_lid = { ...detail.generated_lid, lid_geometry: { ...lid },
                        length_mm: lid.length_mm, width_mm: lid.width_mm, height_mm: lid.height_mm,
                        lower_local_mm: [-lid.length_mm / 2, -lid.width_mm / 2, lid.seated_bottom_mm],
                        upper_local_mm: [lid.length_mm / 2, lid.width_mm / 2, lid.seated_bottom_mm + lid.height_mm] };
                }
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

function highlightCover(group, retiredMaterials, opacity = 0.9) {
    tintMeshes(group, COVER_COLOR, retiredMaterials, { opacity, emissive: 0x70240a });
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

/** The sampled line is collision evidence, not a detailed tip taper. */
function addSampledTipAxis(target, error) {
    if (!dimensionedLid(error) || !validBounds(error)
        || !Array.isArray(error.tip_segment_mm) || error.tip_segment_mm.length !== 2
        || !error.tip_segment_mm.every(point => Array.isArray(point)
            && point.length === 3 && point.every(Number.isFinite))) return null;
    const centerX = (error.lower_mm[0] + error.upper_mm[0]) / 2;
    const centerY = (error.lower_mm[1] + error.upper_mm[1]) / 2;
    const lidTop = error.lid_geometry.seated_bottom_mm + error.lid_geometry.height_mm;
    const points = error.tip_segment_mm.map(point => new THREE.Vector3(
        (point[0] - centerX) / 1000, (point[1] - centerY) / 1000,
        (point[2] - error.upper_mm[2] + lidTop) / 1000,
    ));
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),
        new THREE.LineBasicMaterial({ color: 0xff414b, depthTest: false, linewidth: 2 }));
    line.name = 'sampled-tip-axis';
    line.renderOrder = 10;
    target.add(line);
    return line;
}

function sampledRobotState(error) {
    const state = error?.runtime_state;
    if (error?.kind !== 'lid_collision' || !state || !positive(state.teach_tip_length_mm)
        || !['X', 'Y', 'Z', 'Zg', 'G', 'W'].every(axis => Number.isFinite(error.pose?.[axis]))
        || !state.head_type || typeof state.tips_on_head !== 'boolean') return null;
    if (state.tips_on_head && (!positive(state.attached_tip_length_mm)
        || !state.tips_on_head_mode || !state.tips_on_head_selection)) return null;
    return state;
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
        const runtime = sampledRobotState(error);
        scene = new RobotScene(container, { autoConnect: false, showGizmo: false, deckOnly: !runtime,
            teachTipLengthMm: runtime?.teach_tip_length_mm });
        if (runtime) {
            scene.setPositions(error.pose);
            scene.snapRenderPositions(error.pose);
        }
        await scene.init();
        if (disposed) return;
        if (!scene.deckSlotAnchors.has(setup.location)) {
            throw new Error('The deck asset could not be loaded for this preview.');
        }
        if (runtime) {
            const teachpoints = Object.fromEntries(Object.entries(runtime.teachpoints || {}).map(([loc, point]) =>
                [loc, { x: point.x ?? point.X, y: point.y ?? point.Y, z: point.z ?? point.Z }]));
            scene.setTeachpoints(teachpoints);
            await scene.setHeadTipState(runtime);
            if (disposed) return;
            scene._updateURDFJoints(error.pose);
        }
        await scene.setDeckDetails(setup.deckDetails);
        if (disposed) return;
        const target = scene.labwareRoot.children.find(group =>
            group.userData.deckLocation === setup.location && group.userData.stackIndex === setup.stackIndex);
        if (!target) throw new Error('The affected labware could not be drawn.');
        if (error.carried_lid) {
            if (error.coordinate_frame !== 'machine_xyz_z_up_mm'
                || !Array.isArray(error.carry_offset_mm) || error.carry_offset_mm.length !== 3
                || !error.carry_offset_mm.every(Number.isFinite)) {
                throw new Error('The carried lid position is missing from the sampled evidence.');
            }
            const offset = new THREE.Vector3(...error.carry_offset_mm).multiplyScalar(0.001);
            target.position.add(offset);
            const entry = scene.deckLabwareMeshes.get(setup.location);
            if (entry?.group === target) entry.anchor.add(offset);
        }

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
            lids.forEach(lid => highlightCover(lid, retiredMaterials, dimensionedLid(error) ? 0.28 : 0.9));
        }
        if (setup.detail.is_sealed) addSealHighlight(target, setup.detail, retiredMaterials);
        const tipAxis = error.kind === 'lid_collision' ? addSampledTipAxis(target, error) : null;

        scene.scene.updateMatrixWorld(true);
        const bounds = new THREE.Box3().setFromObject(target);
        focus = bounds.getBoundingSphere(new THREE.Sphere());
        scene.setBackground(getComputedStyle(container).getPropertyValue('--bg-viewport').trim() || '#0d0d14');
        scene.renderer.domElement.setAttribute('role', 'img');
        scene.renderer.domElement.setAttribute('aria-label',
            `${error.kind === 'lid_collision' ? 'Sampled interference' : 'Setup preview'}, position ${setup.location}: ${setup.detail.name || 'labware'}, `
            + `${setup.detail.is_lidded ? 'lid' : 'seal'} highlighted. Drag to rotate, scroll to zoom.`);
        resetView();
        return { sampledPoseShown: Boolean(runtime), tipAxisShown: Boolean(tipAxis), dimensioned: dimensionedLid(error) };
    }).catch(error => {
        dispose();
        throw error;
    });

    return { ready, dispose, resetView };
}
