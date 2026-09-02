/**
 * gt-annotator — Frontend Application Logic
 * Single-page canvas bounding box annotation tool.
 * Zero external dependencies / 100% offline.
 */

(function () {
  'use strict';

  // --- Constants & Storage Keys ---
  const STORAGE_KEY_TOKEN = 'gt_annotator_auth_token';
  const STORAGE_KEY_ANNOTATOR = 'gt_annotator_name';
  const MIN_BOX_SIZE_PX = 4; // Minimum drag size in natural image pixels to avoid zero-area boxes
  const HANDLE_RADIUS_SCREEN = 7; // Radius of resize handles in canvas screen pixels
  const IMAGE_CACHE_CAPACITY = 50; // Maximum cached images in LRU cache to prevent memory explosion

  // --- LRU Image Cache with Blob ObjectURL Management ---
  class ImageLRUCache {
    constructor(maxSize = IMAGE_CACHE_CAPACITY) {
      this.maxSize = maxSize;
      this.cache = new Map(); // key (url) -> { img: HTMLImageElement, objectUrl: string|null }
    }

    get(url) {
      if (!this.cache.has(url)) return null;
      const entry = this.cache.get(url);
      // Re-insert to refresh LRU order
      this.cache.delete(url);
      this.cache.set(url, entry);
      return entry.img;
    }

    set(url, img, objectUrl = null) {
      if (this.cache.has(url)) {
        const existing = this.cache.get(url);
        if (existing.objectUrl && existing.objectUrl !== objectUrl) {
          URL.revokeObjectURL(existing.objectUrl);
        }
        this.cache.delete(url);
      } else if (this.cache.size >= this.maxSize) {
        // Evict oldest (least recently used)
        const oldestKey = this.cache.keys().next().value;
        const oldestEntry = this.cache.get(oldestKey);
        if (oldestEntry && oldestEntry.objectUrl) {
          URL.revokeObjectURL(oldestEntry.objectUrl);
        }
        this.cache.delete(oldestKey);
      }
      this.cache.set(url, { img, objectUrl });
    }

    has(url) {
      return this.cache.has(url);
    }
  }

  // --- Application State ---
  const state = {
    manifest: '',
    totalItems: 0,
    items: [],
    currentIndex: 0,
    annotator: localStorage.getItem(STORAGE_KEY_ANNOTATOR) || '',
    token: localStorage.getItem(STORAGE_KEY_TOKEN) || '',
    
    // Cached Images with LRU eviction
    imageCache: new ImageLRUCache(IMAGE_CACHE_CAPACITY),
    currentImage: null,
    isImageLoading: false,
    imageLoadError: null,

    // Active Box on current item: [x1, y1, x2, y2] in normalized 0-1 XYXY
    activeBbox: null,

    // Canvas Viewport Transform
    transform: {
      scale: 1.0,
      offsetX: 0.0,
      offsetY: 0.0
    },

    // Mouse Interaction State
    interaction: {
      mode: 'idle', // 'idle' | 'drawing' | 'moving' | 'resizing' | 'panning'
      activeHandle: null, // 'nw'|'ne'|'se'|'sw'|'n'|'s'|'e'|'w'
      startMouseX: 0,
      startMouseY: 0,
      startImgX: 0,
      startImgY: 0,
      startBbox: null, // [x1, y1, x2, y2] in natural pixels
      startTransform: { scale: 1, offsetX: 0, offsetY: 0 }
    }
  };

  // --- DOM Elements ---
  const dom = {
    // Header
    manifestBadge: document.getElementById('manifest-badge'),
    itemIndexDisplay: document.getElementById('item-index-display'),
    progressText: document.getElementById('progress-text'),
    imageProgressText: document.getElementById('image-progress-text'),
    progressBarFill: document.getElementById('progress-bar-fill'),
    seekSlider: document.getElementById('seek-slider'),
    seekPreview: document.getElementById('seek-preview'),
    jumpInput: document.getElementById('jump-input'),
    jumpBtn: document.getElementById('jump-btn'),
    annotatorInput: document.getElementById('annotator-input'),
    tokenBtn: document.getElementById('token-btn'),
    helpBtn: document.getElementById('help-btn'),

    // Query Bar
    currentIdBadge: document.getElementById('current-id-badge'),
    annotationStatusBadge: document.getElementById('annotation-status-badge'),
    queryEnText: document.getElementById('query-en-text'),
    queryZhWrap: document.getElementById('query-zh-wrap'),
    queryZhText: document.getElementById('query-zh-text'),
    prevBtn: document.getElementById('prev-btn'),
    nextBtn: document.getElementById('next-btn'),
    prevUnannotatedBtn: document.getElementById('prev-unannotated-btn'),
    nextUnannotatedBtn: document.getElementById('next-unannotated-btn'),
    clearBtn: document.getElementById('clear-btn'),
    saveBtn: document.getElementById('save-btn'),

    // Canvas
    canvasContainer: document.getElementById('canvas-container'),
    canvas: document.getElementById('annotation-canvas'),
    hudCoords: document.getElementById('hud-coords'),
    hudImgSize: document.getElementById('hud-img-size'),
    zoomLevelText: document.getElementById('zoom-level-text'),
    zoomOutBtn: document.getElementById('zoom-out-btn'),
    zoomInBtn: document.getElementById('zoom-in-btn'),
    zoomFitBtn: document.getElementById('zoom-fit-btn'),
    zoom100Btn: document.getElementById('zoom-100-btn'),
    canvasLoading: document.getElementById('canvas-loading'),
    canvasLoadingText: document.getElementById('canvas-loading-text'),
    canvasError: document.getElementById('canvas-error'),
    canvasErrorText: document.getElementById('canvas-error-text'),
    retryImageBtn: document.getElementById('retry-image-btn'),

    // Footer
    footerItemInfo: document.getElementById('footer-item-info'),
    footerAnnotatorInfo: document.getElementById('footer-annotator-info'),
    footerBboxInfo: document.getElementById('footer-bbox-info'),

    // Modals
    tokenModal: document.getElementById('token-modal'),
    tokenModalInput: document.getElementById('token-modal-input'),
    tokenModalAlert: document.getElementById('token-modal-alert'),
    tokenModalClose: document.getElementById('token-modal-close'),
    tokenModalCancel: document.getElementById('token-modal-cancel'),
    tokenModalSave: document.getElementById('token-modal-save'),

    helpModal: document.getElementById('help-modal'),
    helpModalClose: document.getElementById('help-modal-close'),
    helpModalOk: document.getElementById('help-modal-ok'),

    toastContainer: document.getElementById('toast-container')
  };

  const ctx = dom.canvas.getContext('2d');

  // --- API Client with Auth ---
  async function apiFetch(url, options = {}) {
    const headers = options.headers || {};
    if (state.token) {
      headers['X-Auth-Token'] = state.token;
    }
    const fetchOptions = {
      ...options,
      headers: {
        ...headers,
        ...(options.body ? { 'Content-Type': 'application/json' } : {})
      }
    };

    try {
      const resp = await fetch(url, fetchOptions);
      if (resp.status === 401) {
        showTokenModal('鉴权失败 (401 Unauthorized)：请输入有效 Token');
        throw new Error('401 Unauthorized');
      }
      return resp;
    } catch (err) {
      if (err.message !== '401 Unauthorized') {
        showToast('网络请求失败: ' + err.message, 'error');
      }
      throw err;
    }
  }

  // --- Toast Notifications ---
  function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    dom.toastContainer.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(-10px)';
      setTimeout(() => toast.remove(), 250);
    }, 2800);
  }

  // --- Session Data & Loading ---
  async function loadSession() {
    try {
      const resp = await apiFetch('/api/session');
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = await resp.json();
      state.manifest = data.manifest || 'unknown';
      state.totalItems = data.total_items || data.items.length;
      state.items = data.items || [];
      
      dom.manifestBadge.textContent = state.manifest;
      
      // Auto-position to first unannotated item
      const firstUnannotatedIdx = state.items.findIndex(it => it.bbox === null);
      state.currentIndex = firstUnannotatedIdx >= 0 ? firstUnannotatedIdx : 0;
      
      updateOverallProgress();
      renderCurrentItem();
    } catch (err) {
      // Mute console noise on expected initial 401
      if (err.message !== '401 Unauthorized') {
        console.error('Failed to load session:', err);
      }
    }
  }

  // --- Progress Updates ---
  function updateOverallProgress() {
    const total = state.items.length;
    let annotatedCount = 0;
    const imageSet = new Set();
    const annotatedImageSet = new Set();

    state.items.forEach(it => {
      imageSet.add(it.image_url);
      if (it.bbox !== null) {
        annotatedCount++;
        annotatedImageSet.add(it.image_url);
      }
    });

    const pct = total > 0 ? ((annotatedCount / total) * 100).toFixed(1) : '0.0';
    dom.progressText.textContent = `已标: ${annotatedCount} / ${total} (${pct}%)`;
    dom.imageProgressText.textContent = `图片: ${annotatedImageSet.size} / ${imageSet.size}`;
    dom.progressBarFill.style.width = `${pct}%`;
  }

  // --- Item Navigation ---
  function getCurrentItem() {
    return state.items[state.currentIndex] || null;
  }

  function goToIndex(index) {
    if (index < 0 || index >= state.items.length) return;
    if (state.currentIndex === index && state.currentImage) return;
    state.currentIndex = index;
    renderCurrentItem();
  }

  function nextItem() {
    if (state.currentIndex < state.items.length - 1) {
      goToIndex(state.currentIndex + 1);
    } else {
      showToast('已是最后一条', 'info');
    }
  }

  function prevItem() {
    if (state.currentIndex > 0) {
      goToIndex(state.currentIndex - 1);
    } else {
      showToast('已是第一条', 'info');
    }
  }

  function findNextUnannotated(fromIndex = state.currentIndex) {
    for (let i = fromIndex + 1; i < state.items.length; i++) {
      if (state.items[i].bbox === null) return i;
    }
    // wrap around
    for (let i = 0; i <= fromIndex; i++) {
      if (state.items[i].bbox === null) return i;
    }
    return -1;
  }

  function findPrevUnannotated(fromIndex = state.currentIndex) {
    for (let i = fromIndex - 1; i >= 0; i--) {
      if (state.items[i].bbox === null) return i;
    }
    // wrap around
    for (let i = state.items.length - 1; i >= fromIndex; i--) {
      if (state.items[i].bbox === null) return i;
    }
    return -1;
  }

  function goToNextUnannotated() {
    const nextIdx = findNextUnannotated();
    if (nextIdx !== -1 && nextIdx !== state.currentIndex) {
      goToIndex(nextIdx);
    } else if (state.items.every(it => it.bbox !== null)) {
      showToast('🎉 所有条目已标注完毕！', 'success');
    } else {
      showToast('已是最后一条未标注', 'info');
    }
  }

  function goToPrevUnannotated() {
    const prevIdx = findPrevUnannotated();
    if (prevIdx !== -1 && prevIdx !== state.currentIndex) {
      goToIndex(prevIdx);
    } else {
      showToast('没有更多未标注条目', 'info');
    }
  }

  // --- Image Jump matching ---
  function jumpToImage(query) {
    if (!query || !query.trim()) return;
    const q = query.trim().toLowerCase();

    // 0. "#N" → jump to ordinal item N (1-based)
    if (q.startsWith('#')) {
      const n = parseInt(q.slice(1), 10);
      if (Number.isInteger(n) && n >= 1 && n <= state.items.length) {
        goToIndex(n - 1);
        showToast(`已跳转到第 ${n} 条 (${state.items[n - 1].id})`, 'info');
        dom.jumpInput.value = '';
      } else {
        showToast(`序数超出范围: 1 ~ ${state.items.length}`, 'error');
      }
      return;
    }

    // 1. Match item ID exact or prefix
    let targetIdx = state.items.findIndex(it => it.id.toLowerCase() === q || it.id.toLowerCase().startsWith(q));

    // 2. Match image filename
    if (targetIdx === -1) {
      targetIdx = state.items.findIndex(it => {
        try {
          const urlObj = new URL(it.image_url, window.location.origin);
          const srcParam = urlObj.searchParams.get('src') || it.image_url;
          const fileName = srcParam.split('/').pop().replace(/\.[^/.]+$/, "").toLowerCase();
          return fileName === q || fileName.includes(q) || srcParam.toLowerCase().includes(q);
        } catch {
          return it.image_url.toLowerCase().includes(q);
        }
      });
    }

    // 3. Pure digits that matched nothing → treat as ordinal
    if (targetIdx === -1 && /^\d+$/.test(q)) {
      const n = parseInt(q, 10);
      if (n >= 1 && n <= state.items.length) {
        goToIndex(n - 1);
        showToast(`未匹配到 id/图号，已按序数跳到第 ${n} 条 (${state.items[n - 1].id})`, 'info');
        dom.jumpInput.value = '';
      } else {
        showToast(`未找到匹配，且序数超出范围: 1 ~ ${state.items.length}`, 'error');
      }
      return;
    }

    if (targetIdx !== -1) {
      goToIndex(targetIdx);
      showToast(`已跳转到条目 #${targetIdx + 1} (${state.items[targetIdx].id})`, 'info');
      dom.jumpInput.value = '';
    } else {
      showToast(`未找到匹配的图号或编号: "${query}"`, 'error');
    }
  }

  // --- Rendering UI & Image ---
  function renderCurrentItem() {
    const item = getCurrentItem();
    if (!item) return;

    // Active bbox copy
    state.activeBbox = item.bbox ? [...item.bbox] : null;

    // Header & Meta Info
    dom.itemIndexDisplay.textContent = `Item #${state.currentIndex + 1} / ${state.items.length}`;
    dom.currentIdBadge.textContent = `ID: ${item.id}`;

    // Seek slider position sync
    if (dom.seekSlider) {
      dom.seekSlider.max = String(state.items.length);
      dom.seekSlider.value = String(state.currentIndex + 1);
    }
    
    if (item.bbox) {
      dom.annotationStatusBadge.textContent = item.annotator ? `已标注 (${item.annotator})` : '已标注';
      dom.annotationStatusBadge.className = 'badge badge-annotated';
    } else {
      dom.annotationStatusBadge.textContent = '未标注';
      dom.annotationStatusBadge.className = 'badge badge-unannotated';
    }

    // Query text (EN / ZH)
    dom.queryEnText.textContent = item.query_en || '';
    if (item.query_zh) {
      dom.queryZhWrap.classList.remove('hidden');
      dom.queryZhText.textContent = item.query_zh;
    } else {
      dom.queryZhWrap.classList.add('hidden');
      dom.queryZhText.textContent = '';
    }

    // Recalculate canvas size immediately to account for any height shifts in the query panel
    resizeCanvas(false);

    // Footer Info
    dom.footerItemInfo.textContent = `Item: ${item.id} (${state.currentIndex + 1}/${state.items.length})`;
    dom.footerAnnotatorInfo.textContent = `Annotator: ${item.annotator || '-'}`;
    updateFooterBboxInfo();

    // Nav Button states
    dom.prevBtn.disabled = state.currentIndex === 0;
    dom.nextBtn.disabled = state.currentIndex === state.items.length - 1;

    // Load Image
    loadImage(item.image_url);
  }

  function updateFooterBboxInfo() {
    if (state.activeBbox) {
      const [x1, y1, x2, y2] = state.activeBbox;
      dom.footerBboxInfo.textContent = `BBox: [${x1.toFixed(3)}, ${y1.toFixed(3)}, ${x2.toFixed(3)}, ${y2.toFixed(3)}]`;
    } else {
      dom.footerBboxInfo.textContent = 'BBox: 未绘制';
    }
  }

  async function loadImage(url) {
    dom.canvasError.classList.add('hidden');
    
    if (state.imageCache.has(url)) {
      const cached = state.imageCache.get(url);
      state.currentImage = cached;
      state.isImageLoading = false;
      dom.canvasLoading.classList.add('hidden');
      fitImageToCanvas();
      redraw();
      return;
    }

    state.isImageLoading = true;
    dom.canvasLoading.classList.remove('hidden');
    dom.canvasLoadingText.textContent = '加载图片中...';

    const img = new Image();
    let objectUrl = null;

    if (state.token) {
      try {
        const resp = await apiFetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await resp.blob();
        objectUrl = URL.createObjectURL(blob);
        img.src = objectUrl;
      } catch (err) {
        state.isImageLoading = false;
        dom.canvasLoading.classList.add('hidden');
        dom.canvasError.classList.remove('hidden');
        dom.canvasErrorText.textContent = '图片下载失败: ' + err.message;
        return;
      }
    } else {
      img.src = url;
    }

    img.onload = () => {
      state.imageCache.set(url, img, objectUrl);
      if (getCurrentItem()?.image_url === url) {
        state.currentImage = img;
        state.isImageLoading = false;
        dom.canvasLoading.classList.add('hidden');
        fitImageToCanvas();
        redraw();
      }
    };

    img.onerror = () => {
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
      if (getCurrentItem()?.image_url === url) {
        state.isImageLoading = false;
        dom.canvasLoading.classList.add('hidden');
        dom.canvasError.classList.remove('hidden');
        dom.canvasErrorText.textContent = '无法加载图片资源';
      }
    };
  }

  // --- Coordinate Transformation Engine ---
  function getCanvasSize() {
    const rect = dom.canvas.getBoundingClientRect();
    return { width: rect.width, height: rect.height };
  }

  function resizeCanvas(fitImage = false) {
    if (!dom.canvas) return;
    const rect = dom.canvas.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;

    const dpr = window.devicePixelRatio || 1;
    const targetW = Math.round(rect.width * dpr);
    const targetH = Math.round(rect.height * dpr);

    const sizeChanged = (dom.canvas.width !== targetW || dom.canvas.height !== targetH);
    if (sizeChanged) {
      dom.canvas.width = targetW;
      dom.canvas.height = targetH;
    }

    // Always reset transform matrix directly to dpr to guarantee 1:1 crispness without accumulation
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    if (fitImage && state.currentImage && state.interaction.mode === 'none') {
      fitImageToCanvas();
    }
    redraw();
  }

  function fitImageToCanvas() {
    if (!state.currentImage) return;
    const { width: cw, height: ch } = getCanvasSize();
    const iw = state.currentImage.naturalWidth;
    const ih = state.currentImage.naturalHeight;
    if (iw === 0 || ih === 0 || cw === 0 || ch === 0) return;

    const padding = 24;
    const scaleX = (cw - padding * 2) / iw;
    const scaleY = (ch - padding * 2) / ih;
    const scale = Math.min(scaleX, scaleY, 1.0); // Don't over-scale small images initially

    state.transform.scale = scale;
    state.transform.offsetX = (cw - iw * scale) / 2;
    state.transform.offsetY = (ch - ih * scale) / 2;

    updateZoomDisplay();
  }

  function setActualSize() {
    if (!state.currentImage) return;
    const { width: cw, height: ch } = getCanvasSize();
    const iw = state.currentImage.naturalWidth;
    const ih = state.currentImage.naturalHeight;

    state.transform.scale = 1.0;
    state.transform.offsetX = (cw - iw) / 2;
    state.transform.offsetY = (ch - ih) / 2;

    updateZoomDisplay();
    redraw();
  }

  function updateZoomDisplay() {
    const pct = Math.round(state.transform.scale * 100);
    dom.zoomLevelText.textContent = `${pct}%`;
  }

  // Convert Screen (Canvas CSS) -> Image Natural Pixels
  function screenToImage(screenX, screenY) {
    const { scale, offsetX, offsetY } = state.transform;
    return {
      x: (screenX - offsetX) / scale,
      y: (screenY - offsetY) / scale
    };
  }

  // Convert Image Natural Pixels -> Screen (Canvas CSS)
  function imageToScreen(imgX, imgY) {
    const { scale, offsetX, offsetY } = state.transform;
    return {
      x: imgX * scale + offsetX,
      y: imgY * scale + offsetY
    };
  }

  // Convert Normalized [0, 1] -> Image Natural Pixels
  function normalizedToImageRect(normBbox) {
    if (!state.currentImage || !normBbox) return null;
    const iw = state.currentImage.naturalWidth;
    const ih = state.currentImage.naturalHeight;
    return {
      x1: normBbox[0] * iw,
      y1: normBbox[1] * ih,
      x2: normBbox[2] * iw,
      y2: normBbox[3] * ih
    };
  }

  // Convert Image Natural Pixels -> Normalized [0, 1]
  function imageRectToNormalized(x1, y1, x2, y2) {
    if (!state.currentImage) return null;
    const iw = state.currentImage.naturalWidth;
    const ih = state.currentImage.naturalHeight;
    if (iw === 0 || ih === 0) return null;

    const minX = Math.max(0, Math.min(x1, x2));
    const maxX = Math.min(iw, Math.max(x1, x2));
    const minY = Math.max(0, Math.min(y1, y2));
    const maxY = Math.min(ih, Math.max(y1, y2));

    return [
      Number((minX / iw).toFixed(6)),
      Number((minY / ih).toFixed(6)),
      Number((maxX / iw).toFixed(6)),
      Number((maxY / ih).toFixed(6))
    ];
  }

  // Get 8 Resize Handles in Screen Coordinates
  function getHandles(normBbox) {
    const imgRect = normalizedToImageRect(normBbox);
    if (!imgRect) return {};

    const p1 = imageToScreen(imgRect.x1, imgRect.y1);
    const p2 = imageToScreen(imgRect.x2, imgRect.y2);
    const midX = (p1.x + p2.x) / 2;
    const midY = (p1.y + p2.y) / 2;

    return {
      nw: { x: p1.x, y: p1.y, cursor: 'nwse-resize' },
      ne: { x: p2.x, y: p1.y, cursor: 'nesw-resize' },
      se: { x: p2.x, y: p2.y, cursor: 'nwse-resize' },
      sw: { x: p1.x, y: p2.y, cursor: 'nesw-resize' },
      n:  { x: midX, y: p1.y, cursor: 'ns-resize' },
      s:  { x: midX, y: p2.y, cursor: 'ns-resize' },
      w:  { x: p1.x, y: midY, cursor: 'ew-resize' },
      e:  { x: p2.x, y: midY, cursor: 'ew-resize' }
    };
  }

  // Hit Testing
  function hitTest(screenX, screenY) {
    if (!state.activeBbox || !state.currentImage) return { type: 'none' };

    // 1. Check handles first
    const handles = getHandles(state.activeBbox);
    for (const [key, h] of Object.entries(handles)) {
      const dist = Math.hypot(screenX - h.x, screenY - h.y);
      if (dist <= HANDLE_RADIUS_SCREEN + 3) {
        return { type: 'handle', handle: key, cursor: h.cursor };
      }
    }

    // 2. Check inside box
    const imgRect = normalizedToImageRect(state.activeBbox);
    const p1 = imageToScreen(imgRect.x1, imgRect.y1);
    const p2 = imageToScreen(imgRect.x2, imgRect.y2);
    const minX = Math.min(p1.x, p2.x);
    const maxX = Math.max(p1.x, p2.x);
    const minY = Math.min(p1.y, p2.y);
    const maxY = Math.max(p1.y, p2.y);

    if (screenX >= minX && screenX <= maxX && screenY >= minY && screenY <= maxY) {
      return { type: 'box', cursor: 'move' };
    }

    return { type: 'none' };
  }

  // --- Canvas Drawing ---
  function redraw() {
    // Thoroughly clear the entire physical canvas buffer to prevent ghosting/clipping
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, dom.canvas.width, dom.canvas.height);
    ctx.restore();

    if (!state.currentImage) return;

    const { scale, offsetX, offsetY } = state.transform;
    const iw = state.currentImage.naturalWidth;
    const ih = state.currentImage.naturalHeight;

    // 1. Draw Image with high quality
    ctx.imageSmoothingEnabled = scale < 2.0; // Sharp pixelated look if zoomed in very close
    ctx.drawImage(state.currentImage, offsetX, offsetY, iw * scale, ih * scale);

    // 2. Draw Image Border
    ctx.strokeStyle = '#334155';
    ctx.lineWidth = 1;
    ctx.strokeRect(offsetX, offsetY, iw * scale, ih * scale);

    // 3. Draw Active BBox
    if (state.activeBbox) {
      drawBbox(state.activeBbox);
    }

    // Update HUD items
    dom.hudImgSize.textContent = `尺寸: ${iw} × ${ih}`;
  }

  function drawBbox(normBbox) {
    const imgRect = normalizedToImageRect(normBbox);
    if (!imgRect) return;

    const p1 = imageToScreen(imgRect.x1, imgRect.y1);
    const p2 = imageToScreen(imgRect.x2, imgRect.y2);
    const x = Math.min(p1.x, p2.x);
    const y = Math.min(p1.y, p2.y);
    const w = Math.abs(p2.x - p1.x);
    const h = Math.abs(p2.y - p1.y);

    // Box semi-transparent fill
    ctx.fillStyle = 'rgba(6, 182, 212, 0.16)';
    ctx.fillRect(x, y, w, h);

    // Box outer dark outline (for contrast on light images)
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.7)';
    ctx.lineWidth = 3;
    ctx.strokeRect(x, y, w, h);

    // Box inner bright border
    ctx.strokeStyle = '#06b6d4';
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, w, h);

    // Draw 8 Handles
    const handles = getHandles(normBbox);
    for (const h of Object.values(handles)) {
      // Handle Shadow
      ctx.fillStyle = 'rgba(0, 0, 0, 0.4)';
      ctx.beginPath();
      ctx.arc(h.x + 1, h.y + 1, HANDLE_RADIUS_SCREEN, 0, Math.PI * 2);
      ctx.fill();

      // Handle Outer Stroke
      ctx.strokeStyle = '#0891b2';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(h.x, h.y, HANDLE_RADIUS_SCREEN, 0, Math.PI * 2);
      ctx.stroke();

      // Handle Inner Fill
      ctx.fillStyle = '#ffffff';
      ctx.beginPath();
      ctx.arc(h.x, h.y, HANDLE_RADIUS_SCREEN - 1, 0, Math.PI * 2);
      ctx.fill();
    }

    // Badge with coordinates above box
    const badgeText = `${Math.round(Math.abs(imgRect.x2 - imgRect.x1))}×${Math.round(Math.abs(imgRect.y2 - imgRect.y1))} px`;
    ctx.font = '11px ui-monospace, monospace';
    const textMetrics = ctx.measureText(badgeText);
    const badgeW = textMetrics.width + 10;
    const badgeH = 18;
    const badgeX = x;
    const badgeY = y - badgeH - 4 >= 0 ? y - badgeH - 4 : y + 4;

    ctx.fillStyle = 'rgba(15, 23, 42, 0.85)';
    ctx.fillRect(badgeX, badgeY, badgeW, badgeH);
    ctx.strokeStyle = '#06b6d4';
    ctx.lineWidth = 1;
    ctx.strokeRect(badgeX, badgeY, badgeW, badgeH);

    ctx.fillStyle = '#38bdf8';
    ctx.textBaseline = 'middle';
    ctx.fillText(badgeText, badgeX + 5, badgeY + badgeH / 2);
  }

  // --- Mouse & Zoom Interaction ---
  function getMousePos(e) {
    const rect = dom.canvas.getBoundingClientRect();
    return {
      x: e.clientX - rect.left,
      y: e.clientY - rect.top
    };
  }

  function handleWheel(e) {
    e.preventDefault();
    if (!state.currentImage) return;

    const { x: mouseX, y: mouseY } = getMousePos(e);
    const { scale, offsetX, offsetY } = state.transform;

    const zoomFactor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    const newScale = Math.min(50.0, Math.max(0.05, scale * zoomFactor));

    // Keep point under cursor stationary
    const imgX = (mouseX - offsetX) / scale;
    const imgY = (mouseY - offsetY) / scale;

    state.transform.scale = newScale;
    state.transform.offsetX = mouseX - imgX * newScale;
    state.transform.offsetY = mouseY - imgY * newScale;

    updateZoomDisplay();
    redraw();
  }

  function handleMouseDown(e) {
    if (!state.currentImage || state.isImageLoading) return;
    
    const { x: mouseX, y: mouseY } = getMousePos(e);
    const imgPos = screenToImage(mouseX, mouseY);
    const hit = hitTest(mouseX, mouseY);

    // Pan Mode: middle click (button 1) or right click (button 2)
    if (e.button === 1 || e.button === 2) {
      state.interaction.mode = 'panning';
      state.interaction.startMouseX = e.clientX;
      state.interaction.startMouseY = e.clientY;
      state.interaction.startTransform = { ...state.transform };
      dom.canvas.style.cursor = 'grabbing';
      return;
    }

    if (e.button !== 0) return; // Only process left click for drawing/resizing/moving

    if (hit.type === 'handle') {
      // Resize Handle
      state.interaction.mode = 'resizing';
      state.interaction.activeHandle = hit.handle;
      state.interaction.startMouseX = mouseX;
      state.interaction.startMouseY = mouseY;
      state.interaction.startBbox = normalizedToImageRect(state.activeBbox);
      dom.canvas.style.cursor = hit.cursor;
    } else if (hit.type === 'box') {
      // Move Existing Box
      state.interaction.mode = 'moving';
      state.interaction.startMouseX = mouseX;
      state.interaction.startMouseY = mouseY;
      state.interaction.startBbox = normalizedToImageRect(state.activeBbox);
      dom.canvas.style.cursor = 'move';
    } else {
      // Start Drawing New Box (or panning if clicked outside image bounds)
      const iw = state.currentImage.naturalWidth;
      const ih = state.currentImage.naturalHeight;
      const insideImage = imgPos.x >= 0 && imgPos.x <= iw && imgPos.y >= 0 && imgPos.y <= ih;

      if (!insideImage) {
        // Drag background to pan
        state.interaction.mode = 'panning';
        state.interaction.startMouseX = e.clientX;
        state.interaction.startMouseY = e.clientY;
        state.interaction.startTransform = { ...state.transform };
        dom.canvas.style.cursor = 'grabbing';
      } else {
        state.interaction.mode = 'drawing';
        state.interaction.startImgX = Math.max(0, Math.min(iw, imgPos.x));
        state.interaction.startImgY = Math.max(0, Math.min(ih, imgPos.y));
        state.activeBbox = imageRectToNormalized(
          state.interaction.startImgX,
          state.interaction.startImgY,
          state.interaction.startImgX,
          state.interaction.startImgY
        );
        dom.canvas.style.cursor = 'crosshair';
      }
    }
  }

  function handleMouseMove(e) {
    if (!state.currentImage) return;

    const { x: mouseX, y: mouseY } = getMousePos(e);
    const imgPos = screenToImage(mouseX, mouseY);
    const iw = state.currentImage.naturalWidth;
    const ih = state.currentImage.naturalHeight;

    // Update HUD Coordinates
    const curX = Math.round(Math.max(0, Math.min(iw, imgPos.x)));
    const curY = Math.round(Math.max(0, Math.min(ih, imgPos.y)));
    dom.hudCoords.textContent = `坐标: (${curX}, ${curY})`;

    const { mode } = state.interaction;

    if (mode === 'idle') {
      const hit = hitTest(mouseX, mouseY);
      dom.canvas.style.cursor = hit.cursor || 'crosshair';
      return;
    }

    if (mode === 'panning') {
      const dx = e.clientX - state.interaction.startMouseX;
      const dy = e.clientY - state.interaction.startMouseY;
      state.transform.offsetX = state.interaction.startTransform.offsetX + dx;
      state.transform.offsetY = state.interaction.startTransform.offsetY + dy;
      redraw();
      return;
    }

    if (mode === 'drawing') {
      const curImgX = Math.max(0, Math.min(iw, imgPos.x));
      const curImgY = Math.max(0, Math.min(ih, imgPos.y));
      state.activeBbox = imageRectToNormalized(
        state.interaction.startImgX,
        state.interaction.startImgY,
        curImgX,
        curImgY
      );
      updateFooterBboxInfo();
      redraw();
      return;
    }

    if (mode === 'moving') {
      const startBbox = state.interaction.startBbox;
      const startPos = screenToImage(state.interaction.startMouseX, state.interaction.startMouseY);
      const dx = imgPos.x - startPos.x;
      const dy = imgPos.y - startPos.y;

      const boxW = startBbox.x2 - startBbox.x1;
      const boxH = startBbox.y2 - startBbox.y1;

      let newX1 = startBbox.x1 + dx;
      let newY1 = startBbox.y1 + dy;

      // Clamp within image bounds
      if (newX1 < 0) newX1 = 0;
      if (newX1 + boxW > iw) newX1 = iw - boxW;
      if (newY1 < 0) newY1 = 0;
      if (newY1 + boxH > ih) newY1 = ih - boxH;

      state.activeBbox = imageRectToNormalized(newX1, newY1, newX1 + boxW, newY1 + boxH);
      updateFooterBboxInfo();
      redraw();
      return;
    }

    if (mode === 'resizing') {
      const start = state.interaction.startBbox;
      const curImgX = Math.max(0, Math.min(iw, imgPos.x));
      const curImgY = Math.max(0, Math.min(ih, imgPos.y));
      const handle = state.interaction.activeHandle;

      let x1 = start.x1;
      let y1 = start.y1;
      let x2 = start.x2;
      let y2 = start.y2;

      if (handle.includes('w')) x1 = curImgX;
      if (handle.includes('e')) x2 = curImgX;
      if (handle.includes('n')) y1 = curImgY;
      if (handle.includes('s')) y2 = curImgY;

      state.activeBbox = imageRectToNormalized(x1, y1, x2, y2);
      updateFooterBboxInfo();
      redraw();
      return;
    }
  }

  function handleMouseUp() {
    if (!state.currentImage) return;

    if (state.interaction.mode === 'drawing') {
      const rect = normalizedToImageRect(state.activeBbox);
      if (rect) {
        const w = Math.abs(rect.x2 - rect.x1);
        const h = Math.abs(rect.y2 - rect.y1);
        if (w < MIN_BOX_SIZE_PX || h < MIN_BOX_SIZE_PX) {
          // Box too small: cancel
          state.activeBbox = null;
          updateFooterBboxInfo();
          redraw();
        }
      }
    }

    state.interaction.mode = 'idle';
    state.interaction.activeHandle = null;
    dom.canvas.style.cursor = 'crosshair';
    redraw();
  }

  // --- API Mutations (Save & Delete) ---
  async function saveBbox() {
    const item = getCurrentItem();
    if (!item) return;

    if (!state.activeBbox) {
      showToast('请先在图片上绘制目标框', 'error');
      return;
    }

    const [x1, y1, x2, y2] = state.activeBbox;
    if (x1 >= x2 || y1 >= y2) {
      showToast('目标框尺寸非法', 'error');
      return;
    }

    const annotatorName = (dom.annotatorInput.value || '').trim();
    if (annotatorName) {
      state.annotator = annotatorName;
      localStorage.setItem(STORAGE_KEY_ANNOTATOR, annotatorName);
    }

    try {
      dom.saveBtn.disabled = true;
      const resp = await apiFetch(`/api/item/${encodeURIComponent(item.id)}/bbox`, {
        method: 'PUT',
        body: JSON.stringify({
          bbox: state.activeBbox,
          annotator: annotatorName || null
        })
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || `HTTP ${resp.status}`);
      }

      const result = await resp.json();
      
      // Update local item
      item.bbox = result.bbox;
      item.annotator = annotatorName || null;
      state.activeBbox = [...result.bbox];

      updateOverallProgress();
      showToast(`已保存 #${state.currentIndex + 1} (${item.id})`, 'success');

      // Auto-jump to next unannotated
      goToNextUnannotated();
    } catch (err) {
      showToast('保存失败: ' + err.message, 'error');
    } finally {
      dom.saveBtn.disabled = false;
    }
  }

  async function clearBbox() {
    const item = getCurrentItem();
    if (!item) return;

    if (!state.activeBbox && !item.bbox) {
      showToast('当前无已绘制的目标框', 'info');
      return;
    }

    // If item was already saved on server, call DELETE
    if (item.bbox) {
      try {
        dom.clearBtn.disabled = true;
        const resp = await apiFetch(`/api/item/${encodeURIComponent(item.id)}/bbox`, {
          method: 'DELETE'
        });

        if (!resp.ok) {
          const err = await resp.json();
          throw new Error(err.error || `HTTP ${resp.status}`);
        }

        item.bbox = null;
        item.annotator = null;
        state.activeBbox = null;
        updateOverallProgress();
        renderCurrentItem();
        showToast('已清除标注', 'info');
      } catch (err) {
        showToast('清除失败: ' + err.message, 'error');
      } finally {
        dom.clearBtn.disabled = false;
      }
    } else {
      // Just clear unsaved drawn box
      state.activeBbox = null;
      updateFooterBboxInfo();
      redraw();
      showToast('已清除画布草稿', 'info');
    }
  }

  // --- Token Dialog ---
  function showTokenModal(alertMsg = '') {
    dom.tokenModalInput.value = state.token;
    if (alertMsg) {
      dom.tokenModalAlert.textContent = alertMsg;
      dom.tokenModalAlert.classList.remove('hidden');
    } else {
      dom.tokenModalAlert.classList.add('hidden');
    }
    dom.tokenModal.classList.remove('hidden');
    dom.tokenModalInput.focus();
  }

  function hideTokenModal() {
    dom.tokenModal.classList.add('hidden');
  }

  function saveToken() {
    const token = (dom.tokenModalInput.value || '').trim();
    state.token = token;
    localStorage.setItem(STORAGE_KEY_TOKEN, token);
    hideTokenModal();
    showToast('Token 已保存，正在连接...', 'info');
    loadSession();
  }

  // --- Help Dialog ---
  function showHelpModal() {
    dom.helpModal.classList.remove('hidden');
  }

  function hideHelpModal() {
    dom.helpModal.classList.add('hidden');
  }

  // --- Keyboard Shortcuts ---
  function handleKeyDown(e) {
    // If typing inside an input/textarea, do not intercept navigation shortcuts
    const target = e.target;
    const isInput = target.tagName === 'INPUT' || target.tagName === 'TEXTAREA';

    if (isInput) {
      if (e.key === 'Enter' && target === dom.jumpInput) {
        jumpToImage(dom.jumpInput.value);
      }
      return;
    }

    if (e.key === 'Enter' || e.key === ' ' || e.code === 'Space') {
      e.preventDefault();
      if (!e.repeat) {
        saveBbox();
      }
      return;
    }

    if (e.key === 'Escape' || e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      clearBbox();
      return;
    }

    if (e.key === 'ArrowRight') {
      e.preventDefault();
      if (e.shiftKey) {
        goToNextUnannotated();
      } else {
        nextItem();
      }
      return;
    }

    if (e.key === 'ArrowLeft') {
      e.preventDefault();
      if (e.shiftKey) {
        goToPrevUnannotated();
      } else {
        prevItem();
      }
      return;
    }

    if (e.key === '0' || e.key === 'r' || e.key === 'R') {
      e.preventDefault();
      fitImageToCanvas();
      redraw();
      return;
    }

    if (e.key === '1') {
      e.preventDefault();
      setActualSize();
      return;
    }

    if (e.key === '+' || e.key === '=') {
      e.preventDefault();
      state.transform.scale = Math.min(50, state.transform.scale * 1.25);
      updateZoomDisplay();
      redraw();
      return;
    }

    if (e.key === '-' || e.key === '_') {
      e.preventDefault();
      state.transform.scale = Math.max(0.05, state.transform.scale / 1.25);
      updateZoomDisplay();
      redraw();
      return;
    }

    if (e.key === '?' || (e.key === '/' && e.shiftKey)) {
      e.preventDefault();
      showHelpModal();
      return;
    }
  }

  // --- Event Bindings ---
  function bindEvents() {
    // Window & Canvas Resize Observer
    window.addEventListener('resize', () => resizeCanvas(true));

    if (window.ResizeObserver && dom.canvasContainer) {
      const resizeObserver = new ResizeObserver(() => {
        // Sync canvas buffer with container size shifts (e.g. query panel expansion/collapse)
        resizeCanvas(true);
      });
      resizeObserver.observe(dom.canvasContainer);
    }

    // Canvas Mouse Events
    dom.canvas.addEventListener('wheel', handleWheel, { passive: false });
    dom.canvas.addEventListener('mousedown', handleMouseDown);
    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    dom.canvas.addEventListener('contextmenu', (e) => e.preventDefault());

    // Navigation & Action Buttons
    dom.prevBtn.addEventListener('click', prevItem);
    dom.nextBtn.addEventListener('click', nextItem);
    dom.prevUnannotatedBtn.addEventListener('click', goToPrevUnannotated);
    dom.nextUnannotatedBtn.addEventListener('click', goToNextUnannotated);
    dom.clearBtn.addEventListener('click', clearBbox);
    dom.saveBtn.addEventListener('click', saveBbox);

    // Jump Input
    dom.jumpBtn.addEventListener('click', () => jumpToImage(dom.jumpInput.value));

    // Seek Slider: floating preview while dragging (zero layout shift), jump on release
    dom.seekSlider.addEventListener('input', () => {
      const idx = parseInt(dom.seekSlider.value, 10) - 1;
      const item = state.items[idx];
      if (item) {
        dom.seekPreview.textContent = `第 ${idx + 1} / ${state.items.length} 条 · ${item.id}`;
        dom.seekPreview.classList.remove('hidden');
      }
    });
    dom.seekSlider.addEventListener('change', () => {
      dom.seekPreview.classList.add('hidden');
      goToIndex(parseInt(dom.seekSlider.value, 10) - 1);
    });

    // Annotator Input
    dom.annotatorInput.value = state.annotator;
    dom.annotatorInput.addEventListener('change', () => {
      state.annotator = dom.annotatorInput.value.trim();
      localStorage.setItem(STORAGE_KEY_ANNOTATOR, state.annotator);
    });

    // Zoom Floating Controls
    dom.zoomInBtn.addEventListener('click', () => {
      state.transform.scale = Math.min(50, state.transform.scale * 1.25);
      updateZoomDisplay();
      redraw();
    });
    dom.zoomOutBtn.addEventListener('click', () => {
      state.transform.scale = Math.max(0.05, state.transform.scale / 1.25);
      updateZoomDisplay();
      redraw();
    });
    dom.zoomFitBtn.addEventListener('click', () => {
      fitImageToCanvas();
      redraw();
    });
    dom.zoom100Btn.addEventListener('click', setActualSize);

    // Retry Image
    dom.retryImageBtn.addEventListener('click', () => {
      const item = getCurrentItem();
      if (item) loadImage(item.image_url);
    });

    // Modals
    dom.tokenBtn.addEventListener('click', () => showTokenModal());
    dom.tokenModalClose.addEventListener('click', hideTokenModal);
    dom.tokenModalCancel.addEventListener('click', hideTokenModal);
    dom.tokenModalSave.addEventListener('click', saveToken);

    dom.helpBtn.addEventListener('click', showHelpModal);
    dom.helpModalClose.addEventListener('click', hideHelpModal);
    dom.helpModalOk.addEventListener('click', hideHelpModal);

    // Keyboard Shortcuts
    window.addEventListener('keydown', handleKeyDown);
  }

  // --- App Initialization ---
  function init() {
    bindEvents();
    resizeCanvas();
    loadSession();
  }

  // Run on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
