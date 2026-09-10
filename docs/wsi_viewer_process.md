# Whole Slide Image (WSI) Digital Pathology Pipeline: Upload to Visualisation

This document provides a comprehensive, step-by-step breakdown of how Whole Slide Images (WSI) are ingested, processed, cached, served, and visualised in Yggdrasil's Urology domain, including the role and technical boundaries of **CornerstoneJS**.

---

## 1. High-Level Architecture Overview

Digital pathology whole slide images are gigapixel scans (often exceeding 10,000 × 10,000 pixels, up to several gigabytes uncompressed) captured at 20× or 40× optical magnification. Because browsers cannot load multi-gigabyte files into memory, WSI rendering relies on a **pyramidal multi-resolution tiling strategy**:

```
 [User Upload] ────▶ [Garage S3 Object Storage]
                            │
                            ▼
               [FileRegistry Metadata Record]
                            │
                            ▼
                [/urology/patient/<id>/]
              ┌─────────────┴─────────────┐
              ▼                           ▼
    [Metadata & Thumbnail API]     [WSI Viewport Mount]
              │                           │
              └─────────────┬─────────────┘
                            ▼
                 [Tile Pyramid Streaming]
           (Memory Cache ──▶ Disk Cache ──▶ Crop-First Extractor)
                            │
                            ▼
          [Canvas 2D Rendering + SVG Measurement Layer]
```

---

## 2. Role of CornerstoneJS in the Pipeline

A central design requirement is maximizing the use of **CornerstoneJS** while respecting the physical and technical constraints separating volumetric radiology (MRI/CT) from digital pathology (WSI).

### A. Where CornerstoneJS is Used 100% Natively
1. **Prostate mpMRI Volume Viewer**:
   - Uses `@cornerstonejs/core` and `@cornerstonejs/tools` directly.
   - Streams 3D NIfTI volumes via Cornerstone's volume loader and WebGL volume viewports.
   - Manages window/level, zoom, pan, camera rotation, crosshairs, and multi-planar reformatting (MPR).
2. **Cornerstone Image Loader Registration (`wsi:`)**:
   - In `frontend/imaging/wsi/wsiLoader.js`, the `wsi:` scheme is registered directly into Cornerstone's global image loader registry:
     ```javascript
     imageLoader.registerImageLoader("wsi", createWsiImageLoader({
         voxelManagerFactory: utilities?.VoxelManager?.createImageVoxelManager,
     }));
     ```
   - Each tile is wrapped in Cornerstone's `IImage` interface with its `voxelManager`, scalar pixel arrays, `slope`, `intercept`, `windowCenter`, `windowWidth`, and `voiLUTFunction`.
3. **Cornerstone Annotations & Measurements Schema**:
   - In `frontend/imaging/wsi/wsiMeasurements.js`, all tools (`Length`, `RectangleROI`, `CircleROI`, `SplineROI`, `Label`) serialize their data structures to exact Cornerstone3D annotation objects (`metadata: { toolName, ... }, data: { handles: { points } }`).
   - These payloads pass directly into `annotations/adapters/cornerstone.py` using the standard Cornerstone `image_pixel` coordinate system, fully interoperable with the backend revision store.

### B. Why Digital Pathology Cannot Use Cornerstone's Built-in WebGL Viewports Alone
Cornerstone3D was architected primarily for **radiology slices and 3D volumes** (CT, MRI, PET):

| Feature | Radiology (mpMRI / CT) | Pathology (WSI / Histology) |
|---|---|---|
| **Image Dimensions** | 512 × 512 × 100 slices | 10,000 × 10,000 to 100,000 × 100,000 pixels (1 single 2D plane) |
| **GPU Texture Limit** | Easily fits in GPU RAM (~50 MB) | Exceeds WebGL `MAX_TEXTURE_SIZE` (4096px – 8192px). Uploading a 10k×10k uncompressed image directly crashes WebGL textures. |
| **Data Structure** | 3D Voxel Array | Multi-resolution Level-of-Detail (LOD) Pyramid of thousands of 256×256 tiles |
| **Cornerstone Native Support** | Native `VolumeViewport` & `StackViewport` | **No native pyramidal TIFF slide viewport** exists in Cornerstone3D core |

Even in the **OHIF Viewer** (the official clinical workstation maintained by the CornerstoneJS core team), digital pathology is rendered either through specialized tiling engines (like OpenSeadragon) or custom pyramidal canvas viewports, rather than Cornerstone's WebGL `StackViewport`.

### C. Best-of-Both-Worlds Dual Architecture
1. **Pyramidal 2D Viewport Engine (`wsiViewport.js`)**: Renders only the visible 256×256 tiles at the active zoom level (LOD culling), maintaining 60 FPS navigation across 90-megapixel slides with minimal memory footprint.
2. **Unified Multimodal Integration**: Both MRI (Cornerstone3D volume) and WSI (Cornerstone-integrated pyramidal viewport) live side-by-side on `/urology/patient/<id>/`. Users can correlate radiological lesions with histopathological tissue biopsies in **Split Correlation View** while persisting annotations into a single unified schema.

---

## 3. Step 1: Upload & Storage Ingestion

### A. User Interaction on `/urology/upload/`
1. The user navigates to `/urology/upload/` and opens the **Histopathology (WSI)** upload section.
2. The UI renders the dropzone configured in `templates/common/upload/modalities/urology-wsi.html` with:
   - Modality slug: `urology-wsi`
   - Accepted file extensions: `.tif`, `.tiff`, `.svs`, `.ndpi`
   - Input name: `urology-wsi`

### B. Upload Submission & Server Processing
1. When the user clicks **Upload and Process**, the browser submits a `multipart/form-data` POST request to `/urology/upload/`.
2. In `urology/views.py` (`upload_patient`):
   - The patient record (`urology.models.Patient`) is created or matched.
   - For each uploaded slide file, a SHA-256 hash is computed across the file stream.
   - The file payload is saved into the S3-compatible object storage (Garage) via `common.file_access.save_binary`:
     - Path pattern: `urology/patients/<patient_id>/wsi/<filename>`
   - A `FileRegistry` entry is created with:
     - `domain = "urology"`
     - `file_type = "urology_wsi_raw"`
     - `file_path = <garage_storage_path>`
     - `file_hash = <sha256_hash>`
     - `urology_patient = patient`
     - `metadata = {"original_filename": ..., "size_bytes": ...}`
3. The server responds with JSON `{ "ok": true, "redirect": "/urology/patients/" }` or redirects directly to the patient's record.

---

## 4. Step 2: Slide Metadata Extraction & Pyramidal Discovery

When opening a patient view (`/urology/patient/<id>/`), the application loads slide metadata to configure the viewport coordinate space:

### A. Endpoint: `GET /urology/api/wsi/<file_id>/metadata/`
Handled by `wsi_metadata_api` in `urology/wsi_views.py`:
1. **Access Control**: Validates user permissions against the patient's project using `authorize_file_read`.
2. **Local Slide Disk Cache**:
   - To avoid re-downloading multi-hundred-megabyte TIFF files from Garage S3 on every request, `_get_local_slide_path()` checks `/tmp/ygg_wsi_cache/<file_hash>.tif`.
   - If missing, it streams the file from object storage to disk atomically (`.tmp` $\to$ `.tif`).
3. **Pillow & BigTIFF Parser (`urology/wsi_reader.py`)**:
   - Registers missing BigTIFF pixel modes in Pillow (`TiffImagePlugin.OPEN_INFO`).
   - Sets `Image.MAX_IMAGE_PIXELS = None` to disable Pillow's decompression bomb limit for clinical gigapixel files.
   - Inspects IFD (Image File Directory) tags:
     - **Multi-Frame Pyramids**: If `getattr(img, "n_frames", 1) > 1`, iterates through all frames, reading each level's `width`, `height`, tile dimensions (e.g. 256×256), downsample factor, and grid columns/rows.
     - **Virtual Pyramids**: If the slide is a single high-resolution TIFF, virtual power-of-two downsample levels (e.g. Level 0 through Level 6) are generated automatically so the viewer can zoom smoothly from cellular (1:1) to macroscopic overview.
   - **Physical Calibration (MPP)**:
     - Extracts Microns-Per-Pixel (MPP) from TIFF tag 270 (e.g. Aperio `MPP = 0.2520`) or tags 282 (XResolution) and 296 (ResolutionUnit).
     - Sets `spacing_mm = [mpp / 1000.0, mpp / 1000.0]`.
4. **Metadata Cache**: Parsed metadata is cached in-process in `_METADATA_CACHE[cache_key]` for instant subsequent requests.
5. Returns JSON:
   ```json
   {
     "width": 10760,
     "height": 8351,
     "tileSize": 256,
     "mpp": 0.25,
     "levels": [
       {"level": 0, "width": 10760, "height": 8351, "downsample": 1.0, "cols": 43, "rows": 33},
       {"level": 1, "width": 5380, "height": 4175, "downsample": 2.0, "cols": 22, "rows": 17},
       ...
       {"level": 6, "width": 168, "height": 130, "downsample": 64.0, "cols": 1, "rows": 1}
     ]
   }
   ```

---

## 5. Step 3: Frontend Viewport Mount & Layout Initialization

In `templates/urology/patient_detail_content.html`, the client bootstraps the viewer via `frontend/entries/wsi-viewer.js` and `frontend/imaging/wsi/bootstrap.js`:

1. **DOM Structure**:
   - Primary HTML5 `<canvas>` for hardware-accelerated tile blitting.
   - SVG vector overlay for measurements and region-of-interest (ROI) outlines.
   - Minimap navigator box in the bottom-right corner with a high-level thumbnail (`/urology/api/wsi/<id>/thumbnail/`) and a real-time blue FOV (Field-of-View) indicator.
   - Micron/millimeter calibrated scale bar and optical magnification readout (e.g., `1.25x`, `10x`, `40x`).
2. **Hidden Container Handling (`display: none`)**:
   - The WSI container starts inside `#urologyWsiStagePanel.is-hidden` when viewing the MRI tab.
   - A `ResizeObserver` monitors the viewport element.
   - When the user switches to **WSI (Histopathology)** or **Split Correlation View**, `setViewMode()` reveals the panel and fires a resize event.
   - `wsiViewport.js` detects the first valid dimension measurement (`!hasFitted`) and automatically executes `fitToScreen()`.
3. **Camera Calibration**:
   - `fitToScreen()` computes `zoom = Math.min(width / slideWidth, height / slideHeight) * 0.95`.
   - Centers `centerX = slideWidth / 2`, `centerY = slideHeight / 2`.
   - Sets `minZoom = zoom * 0.2` and `maxZoom = Math.max(4.0, zoom * 80.0)`.

---

## 6. Step 4: High-Speed Tile Serving Backend

When the viewport requests tiles (`GET /urology/api/wsi/<file_id>/tile/<level>/<col>_<row>.jpg`):

1. **Authorization Check**: `_get_authorized_file(request, file_id)` ensures user has valid read permission in the `urology` project.
2. **HTTP 304 Not Modified**: Compares the request `If-None-Match` against `"<file_hash>_<level>_<col>_<row>"`. If matched, returns `304 Not Modified` immediately.
3. **In-Memory LRU Tile Cache**:
   - Checks `_TILE_CACHE` (thread-safe LRU dictionary holding up to 2,048 decoded JPEG tiles).
   - Cache hit latency is under **0.5 milliseconds**.
4. **Crop-First Tile Extraction (`urology/wsi_reader.py`)**:
   - When extracting tiles at virtual pyramid levels (`level > 0`):
     - Instead of resizing the entire 90-megapixel image (which took ~5.8 seconds per tile), the reader calculates the exact source coordinates in Level 0 pixels:
       ```python
       sx0 = col * tile_size * factor
       sy0 = row * tile_size * factor
       sx1 = min(base_w, (col + 1) * tile_size * factor)
       sy1 = min(base_h, (row + 1) * tile_size * factor)
       ```
     - It crops **only** that localized box from disk (`img.crop(...)`) and resizes only the small crop to the tile dimension (`256×256`).
     - This reduced extraction time from **5.8s down to < 0.01s** (over 500× faster).
5. **Tile Encoding & Headers**:
   - Compresses tile to JPEG format (quality 85).
   - Stores in `_TILE_CACHE`.
   - Returns with `Content-Type: image/jpeg` and `Cache-Control: public, max-age=604800, immutable`.

---

## 7. Step 5: Client-Side Tile Fetching, Caching & Smooth Rendering

In `frontend/imaging/wsi/wsiViewport.js`:

### A. Dual-Layer Rendering Strategy (No White/Blank Flashes)
1. **Base Overview Layer**:
   - The lowest resolution pyramid level (e.g. Level 6, 168 × 130 pixels) is requested on startup and permanently stored in `globalWsiTileCache`.
   - In `drawScene()`, this base overview image is **always drawn scaled underneath**. When zooming rapidly across magnifications, the user never sees a blank white canvas while higher-resolution tiles load.
2. **Active Detail Level**:
   - `choosePyramidLevel()` evaluates `targetDownsample = 1.0 / zoom` and picks the closest matching pyramid level.
   - Tile culling: transforms screen corners to slide space via `screenToSlide(0, 0)` and `screenToSlide(width, height)`.
   - Only tiles intersecting the visible viewport rectangle are scheduled for rendering.

### B. Debouncing & Request Lifecycle
1. Fast wheel zooming updates camera coordinates at 60 FPS.
2. New tile fetches are debounced by 40ms (`scheduleFetchTile`).
3. `TileCache.has(key)` checks client memory:
   - If cached, tile is blitted directly to the canvas using `ctx.drawImage()`.
   - If missing, queued for fetch with an `AbortController`.
4. Stale in-flight requests from bypassed intermediate zoom levels are aborted via `controller.abort()`, keeping the network pipe clear for visible tiles.
5. Bitmaps are decoded off-thread via `createImageBitmap(blob)` (with fallback to `HTMLImageElement`).

---

## 8. Step 6: Navigation, Minimap & Calibrated Measurements

### A. Panning and Zooming
- **Pan**: Left-click drag (or middle-click drag) translates `centerX` and `centerY`.
- **Zoom**: Mouse wheel scales `zoom`, keeping the point under the cursor stationary on screen (`screenToSlide` anchoring).
- **Preset Buttons**: `1.25x`, `2.5x`, `5x`, `10x`, `20x`, `40x`, and `Fit` buttons set exact optical magnification equivalents.

### B. Interactive Minimap
- Renders the complete slide overview thumbnail.
- Computes visible slide boundaries and projects a blue viewport box over the thumbnail:
  ```javascript
  rx0 = (visX0 / slideWidth) * minimapWidth;
  ry0 = (visY0 / slideHeight) * minimapHeight;
  ```
- Clicking or dragging inside the minimap moves `centerX, centerY` directly to that location on the slide.

### C. Physical Scale Bar
- Microns per screen pixel is calculated as `umPerPx = mppX / zoom`.
- Dynamically displays calibrated physical length (`µm` or `mm`) corresponding to a 100-pixel reference line.

### D. Measurements & Annotations
- Vector tools:
  - **Ruler / Length**: Calibrated distance in µm or mm.
  - **Rectangle ROI**: Area in µm² or mm².
  - **Circle ROI**: Circular regions and radii.
  - **Spline ROI**: Freehand polygon boundaries (e.g., Gleason pattern or tumor margins).
  - **Label**: Named landmark point markers.
- Coordinates are grounded in Level 0 slide pixels (`image_pixel`).
- Clicking **Save** serializes annotations into Cornerstone-compatible format and persists them via `POST /urology/api/patients/<id>/measurements/`.

---

## 9. Summary Checklist of File Roles

| Path | Purpose |
|---|---|
| `templates/common/upload/modalities/urology-wsi.html` | WSI upload dropzone template |
| `urology/views.py` | Ingestion, SHA-256 computation, and `FileRegistry` persistence |
| `urology/wsi_reader.py` | IFD parsing, virtual downsampling, MPP calibration, and crop-first tile extraction |
| `urology/wsi_views.py` | Tile & metadata API endpoints, permission checks, in-memory LRU cache |
| `urology/app_urls.py` | URL routing for metadata, tiles (`.jpg`/`.png`), and overview thumbnails |
| `frontend/imaging/wsi/wsiLoader.js` | Client-side `TileCache` (LRU) and image loader utilities |
| `frontend/imaging/wsi/wsiViewport.js` | Canvas 2D scene renderer, tile culler, minimap tracker, and camera controls |
| `frontend/imaging/wsi/wsiControls.js` | UI toolbar event bindings (Pan, Ruler, Magnifications, Save) |
| `frontend/imaging/wsi/wsiMeasurements.js` | Annotation payload formatting and persistence adapter |
| `templates/urology/patient_detail_content.html` | Stage layout and modality switcher (`mri`, `wsi`, `split`) |
