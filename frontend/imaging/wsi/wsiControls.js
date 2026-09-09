/**
 * Toolbar bindings, magnification buttons, and measurement save handling for WSI viewer.
 */

import { buildWsiSaveRequest, interpretSaveResponse } from './wsiMeasurements.js';

export function wireWsiControls({
    viewport,
    metadata,
    fileId,
    patientId,
    initialRevision = 0,
    csrfToken,
    namespace = 'urology',
}) {
    let currentRevision = initialRevision;
    let isDirty = false;
    let isVisible = true;

    // Tool switching
    const toolButtons = {
        Pan: document.getElementById('urologyWsiToolPan'),
        Length: document.getElementById('urologyWsiToolLength'),
        RectangleROI: document.getElementById('urologyWsiToolRect'),
        CircleROI: document.getElementById('urologyWsiToolCircle'),
        SplineROI: document.getElementById('urologyWsiToolSpline'),
        Label: document.getElementById('urologyWsiToolLabel'),
    };

    function setActiveTool(toolName) {
        viewport.setTool(toolName);
        for (const [name, btn] of Object.entries(toolButtons)) {
            if (!btn) continue;
            if (name === toolName) {
                btn.classList.add('active');
                btn.setAttribute('aria-pressed', 'true');
            } else {
                btn.classList.remove('active');
                btn.setAttribute('aria-pressed', 'false');
            }
        }
    }

    for (const [name, btn] of Object.entries(toolButtons)) {
        if (!btn) continue;
        btn.addEventListener('click', () => setActiveTool(name));
    }

    // Magnification buttons
    const magButtons = [
        { id: 'urologyWsiMag1', mag: 1.25 },
        { id: 'urologyWsiMag2', mag: 2.5 },
        { id: 'urologyWsiMag5', mag: 5.0 },
        { id: 'urologyWsiMag10', mag: 10.0 },
        { id: 'urologyWsiMag20', mag: 20.0 },
        { id: 'urologyWsiMag40', mag: 40.0 },
    ];

    for (const item of magButtons) {
        const btn = document.getElementById(item.id);
        if (!btn) continue;
        btn.addEventListener('click', () => {
            viewport.setZoomLevel(item.mag);
            for (const b of magButtons) {
                document.getElementById(b.id)?.classList.remove('active');
            }
            btn.classList.add('active');
        });
    }

    const fitBtn = document.getElementById('urologyWsiFit');
    if (fitBtn) {
        fitBtn.addEventListener('click', () => {
            viewport.fitToScreen();
            for (const b of magButtons) {
                document.getElementById(b.id)?.classList.remove('active');
            }
        });
    }

    // Actions
    const saveBtn = document.getElementById('urologyWsiSaveBtn');
    const clearBtn = document.getElementById('urologyWsiClearBtn');
    const toggleBtn = document.getElementById('urologyWsiToggleVisibilityBtn');
    const statusEl = document.getElementById('urologyWsiStatus');

    function updateStatus(msg, isError = false) {
        if (!statusEl) return;
        statusEl.textContent = msg;
        statusEl.style.color = isError ? '#ef4444' : '#10b981';
        setTimeout(() => {
            if (statusEl.textContent === msg) {
                statusEl.textContent = isDirty ? 'Unsaved changes' : '';
                statusEl.style.color = '#94a3b8';
            }
        }, 4000);
    }

    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            if (window.confirm('Clear all WSI annotations?')) {
                viewport.clearAnnotations();
                isDirty = true;
                updateStatus('Annotations cleared (unsaved)');
            }
        });
    }

    if (toggleBtn) {
        toggleBtn.addEventListener('click', () => {
            isVisible = !isVisible;
            viewport.setAnnotationsVisible(isVisible);
            toggleBtn.classList.toggle('active', isVisible);
            toggleBtn.setAttribute('aria-pressed', isVisible ? 'true' : 'false');
            toggleBtn.title = isVisible ? 'Hide Annotations' : 'Show Annotations';
        });
    }

    if (saveBtn) {
        saveBtn.addEventListener('click', async () => {
            const annots = viewport.getAnnotations();
            try {
                saveBtn.disabled = true;
                saveBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving...';

                const savePayload = buildWsiSaveRequest({
                    fileId,
                    annotations: annots,
                    metadata,
                    expectedRevision: currentRevision,
                });

                const response = await fetch(`/${namespace}/api/patients/${patientId}/measurements/`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrfToken,
                    },
                    body: JSON.stringify(savePayload),
                });

                const body = await response.json().catch(() => ({}));
                const result = interpretSaveResponse(response, body);

                if (result.saved) {
                    currentRevision = result.revision;
                    isDirty = false;
                    updateStatus('Saved successfully!');
                } else if (result.reload) {
                    alert(result.message);
                    window.location.reload();
                } else {
                    alert(`Save failed: ${result.message}`);
                    updateStatus('Save failed', true);
                }
            } catch (err) {
                alert(`Save error: ${err.message}`);
                updateStatus('Save error', true);
            } finally {
                saveBtn.disabled = false;
                saveBtn.innerHTML = '<i class="fas fa-floppy-disk"></i> Save';
            }
        });
    }

    // Set initial tool
    setActiveTool('Pan');
}
