// Image Editor & Search - Lab Inventory Tracker
// Handles: image search, crop, rotate, background removal, color picker, auto-adjust

// Global state
let _selectedImageUrl = null;
let _selectedImageSource = null;
let _currentImageUrl = null;
let _currentImageData = null;
let _removeBgChoice = null;  // null, 'mechanical', 'ai', 'skip'
let _selectedColor = null;
let _rotationDegrees = 0;
let _canvas = null;
let _ctx = null;
let _img = null;
let _isDragging = false;
let _dragStart = {x: 0, y: 0};
let _imgOffset = {x: 0, y: 0};
let _sliderDragType = null;

// ========== IMAGE SEARCH ==========

function openImageSearch() {
  document.getElementById('img-modal').style.display = 'block';
  _selectedImageUrl = null;
  _selectedImageSource = null;
  document.getElementById('selected-preview-bottom').style.display = 'none';
  document.getElementById('floating-continue-btn').style.display = 'none';
  
  // Load cached search if exists
  const cached = localStorage.getItem('img-search-query');
  const cacheTime = localStorage.getItem('img-search-time');
  if (cached && cacheTime) {
    const age = Date.now() - parseInt(cacheTime);
    if (age < 300000) {  // 5 minutes
      document.getElementById('img-search-input').value = cached;
    }
  }
  
  // Setup scroll handler for floating button
  const modal = document.getElementById('img-modal');
  modal.onscroll = () => {
    const bottomBanner = document.getElementById('selected-preview-bottom');
    const floatingBtn = document.getElementById('floating-continue-btn');
    
    if (_selectedImageUrl && bottomBanner.style.display !== 'none') {
      const rect = bottomBanner.getBoundingClientRect();
      floatingBtn.style.display = (rect.top > window.innerHeight) ? 'block' : 'none';
    }
  };
  
  // Setup enter key handler
  document.getElementById('img-search-input').onkeydown = (e) => {
    if (e.key === 'Enter') {
      doImageSearch();
    }
  };
  
  // Don't auto-search on open - let user refine query
}

function closeImageSearch() {
  document.getElementById('img-modal').style.display = 'none';
}

function resetImageSearch() {
  document.getElementById('img-search-input').value = '';
  document.getElementById('img-results').innerHTML = '';
  document.getElementById('img-status').textContent = '';
  document.getElementById('selected-preview-bottom').style.display = 'none';
  _selectedImageUrl = null;
  _selectedImageSource = null;
  localStorage.removeItem('img-search-query');
  localStorage.removeItem('img-search-time');
}

async function doImageSearch() {
  const q = document.getElementById('img-search-input').value.trim();
  if (!q) return;
  
  // Cache search query
  localStorage.setItem('img-search-query', q);
  localStorage.setItem('img-search-time', Date.now().toString());
  
  const status = document.getElementById('img-status');
  const results = document.getElementById('img-results');
  status.textContent = 'Searching...';
  results.innerHTML = '';
  
  try {
    const data = await fetch('/api/images/search?q=' + encodeURIComponent(q) + '&limit=24').then(r => r.json());
    status.textContent = data.length ? `${data.length} results` : 'No results';
    
    data.forEach(img => {
      const div = document.createElement('div');
      div.style.cssText = `cursor:pointer;border:2px solid var(--border);border-radius:2px;overflow:hidden;background:var(--bg3);position:relative;`;
      div.innerHTML = `
        <img src="${img.thumb || img.url}" 
             style="width:100%;height:100%;object-fit:cover;display:block;" 
             loading="lazy" 
             onerror="this.parentElement.style.display='none'">
        <div style="position:absolute;bottom:2px;right:2px;background:rgba(0,0,0,0.7);padding:2px 4px;border-radius:2px;font-size:9px;color:var(--text3);">${img.source}</div>
      `;
      div.onclick = () => selectImageCandidate(img.url, img.source, div);
      results.appendChild(div);
    });
  } catch (e) {
    status.textContent = 'Search failed: ' + e.message;
  }
}

function selectImageCandidate(url, source, element) {
  _selectedImageUrl = url;
  _selectedImageSource = source;
  
  // Remove previous selection styling
  document.querySelectorAll('#img-results > div').forEach(el => {
    el.style.border = '2px solid var(--border)';
  });
  
  // Highlight selected
  element.style.border = '2px solid var(--green)';
  
  // Show bottom preview bar
  document.getElementById('selected-thumb-bottom').src = url;
  document.getElementById('selected-source-bottom').textContent = `Source: ${source}`;
  document.getElementById('selected-preview-bottom').style.display = 'block';
  
  // Trigger scroll check for floating button
  document.getElementById('img-modal').dispatchEvent(new Event('scroll'));
}

async function proceedWithSelectedImage() {
  if (!_selectedImageUrl) return;
  
  const status = document.getElementById('img-status');
  status.textContent = 'Loading preview...';
  _currentImageUrl = _selectedImageUrl;
  _rotationDegrees = 0;
  _removeBgChoice = null;
  
  try {
    const fd = new FormData();
    fd.append('component_id', COMP_ID);
    fd.append('image_url', _selectedImageUrl);
    const r = await fetch('/api/images/preview', {method:'POST', body: fd});
    if (r.ok) {
      const data = await r.json();
      _currentImageData = data;
      // Skip bg-removal dialog, go straight to crop
      document.getElementById('img-modal').style.display = 'none';
      showCropModal(data);
      status.textContent = '';
    } else {
      status.textContent = 'Preview failed: ' + r.status;
    }
  } catch(e) {
    status.textContent = 'Error: ' + e.message;
  }
}

// ========== BACKGROUND REMOVAL ==========

async function tryMechanicalBgRemoval() {
  const statusEl = document.getElementById('bg-dialog-status');
  statusEl.textContent = 'Applying mechanical removal...';
  _removeBgChoice = 'mechanical';
  
  setTimeout(() => {
    document.getElementById('bg-removal-dialog').style.display = 'none';
    showCropModal(_currentImageData);
  }, 300);
}

function skipBackgroundRemoval() {
  _removeBgChoice = 'skip';
  document.getElementById('bg-removal-dialog').style.display = 'none';
  showCropModal(_currentImageData);
}

function applyMechanicalBg() {
  _removeBgChoice = 'mechanical';
  document.getElementById('bg-not-applied').style.display = 'none';
  document.getElementById('bg-applied').style.display = 'block';
  document.getElementById('bg-method').textContent = 'mechanical';
  // Reload image with bg removal
  _img.src = _currentImageData.preview;
}

async function applyAiBg() {
  alert('AI background removal coming soon. Requires ML model integration.');
}

function revertBackgroundRemoval() {
  _removeBgChoice = 'skip';
  document.getElementById('bg-not-applied').style.display = 'block';
  document.getElementById('bg-applied').style.display = 'none';
  _img.src = _currentImageData.preview;
}

function showColorPicker() {
  const dialog = document.getElementById('color-picker-dialog');
  const canvas = document.getElementById('color-picker-canvas');
  const ctx = canvas.getContext('2d');
  const cropImg = document.getElementById('crop-image');
  const src = (_currentImageData && _currentImageData.preview)
    ? _currentImageData.preview
    : (cropImg ? cropImg.src : null);

  if (!src) {
    alert('No image available for color selection yet.');
    return;
  }
  
  // Draw image to canvas
  const tempImg = new Image();
  tempImg.crossOrigin = 'anonymous';
  tempImg.onload = () => {
    canvas.width = Math.min(tempImg.width, 400);
    canvas.height = Math.min(tempImg.height, 400);
    ctx.drawImage(tempImg, 0, 0, canvas.width, canvas.height);
    dialog.style.display = 'flex';
  };
  tempImg.src = src;
  
  // Setup click handler
  canvas.onclick = (e) => {
    const rect = canvas.getBoundingClientRect();
    const x = Math.floor((e.clientX - rect.left) * (canvas.width / rect.width));
    const y = Math.floor((e.clientY - rect.top) * (canvas.height / rect.height));
    const pixel = ctx.getImageData(x, y, 1, 1).data;
    
    _selectedColor = {r: pixel[0], g: pixel[1], b: pixel[2]};
    document.getElementById('color-preview').style.background = `rgb(${pixel[0]},${pixel[1]},${pixel[2]})`;
    document.getElementById('color-rgb').textContent = `RGB: ${pixel[0]}, ${pixel[1]}, ${pixel[2]}`;
    document.getElementById('apply-color-btn').disabled = false;
  };
}

function closeColorPicker() {
  document.getElementById('color-picker-dialog').style.display = 'none';
  _selectedColor = null;
}

function applyColorPick() {
  if (!_selectedColor) return;
  document.getElementById('color-picker-dialog').style.display = 'none';
  document.getElementById('bg-color-display').textContent = 
    `RGB(${_selectedColor.r}, ${_selectedColor.g}, ${_selectedColor.b}) ±20`;
  // TODO: Actually apply this threshold
}

// ========== CROP/ROTATE MODAL ==========

function showCropModal(data) {
  // Close image search modal if it's open
  const imgModal = document.getElementById('img-modal');
  if (imgModal) imgModal.style.display = 'none';
  
  const cropModal = document.getElementById('crop-modal');
  if (!cropModal) {
    console.error('crop-modal element not found in DOM');
    return;
  }
  cropModal.style.display = 'block';
  
  // Get image URL
  const imageUrl = typeof data === 'string' ? data : (data.preview || data);
  if (!imageUrl) {
    console.error('No image URL provided to showCropModal');
    alert('No image URL provided');
    return;
  }
  
  // Initialize Cropper.js with this image
  initCropper(imageUrl);
}

function closeCropModal() {
  document.getElementById('crop-modal').style.display = 'none';
}

function autoAdjust() {
  if (!_img) return;
  
  // Step 1: Find tight bounds
  const bounds = findTightBounds(_img);
  
  // Step 2: Set crop sliders to tight bounds
  const cropLeft = document.getElementById('crop-left');
  const cropTop = document.getElementById('crop-top');
  const cropRight = document.getElementById('crop-right');
  const cropBottom = document.getElementById('crop-bottom');
  const rotationDegrees = document.getElementById('rotation-degrees');
  
  if (cropLeft) cropLeft.value = (bounds.left / _img.width * 100).toFixed(1);
  if (cropTop) cropTop.value = (bounds.top / _img.height * 100).toFixed(1);
  if (cropRight) cropRight.value = (bounds.right / _img.width * 100).toFixed(1);
  if (cropBottom) cropBottom.value = (bounds.bottom / _img.height * 100).toFixed(1);
  
  // Step 3: Keep current rotation, only reset offset
  // Don't reset rotation - user may have manually rotated
  _imgOffset = {x: 0, y: 0};
  
  // Step 4: Redraw
  redrawCanvas();
  
  const status = document.getElementById('crop-status');
  if (status) {
    status.textContent = '✓ Auto-cropped (rotation preserved)';
    setTimeout(() => {
      status.textContent = '';
    }, 2000);
  }
}

// ========== ROTATION ==========

function rotatePreview(deg) {
  _rotationDegrees = (_rotationDegrees + deg) % 360;
  if (_rotationDegrees < 0) _rotationDegrees += 360;
  document.getElementById('rotation-degrees').value = _rotationDegrees.toFixed(1);
  redrawCanvas();
}

function setRotationDegrees(deg) {
  _rotationDegrees = parseFloat(deg) % 360;
  if (_rotationDegrees < 0) _rotationDegrees += 360;
  redrawCanvas();
}

// ========== SLIDER DRAG LOUPE ==========

function startSliderDrag(e, type) {
  _sliderDragType = type;
  // TODO: Show loupe with magnified view of crop edge
}

function endSliderDrag() {
  _sliderDragType = null;
  document.getElementById('slider-loupe').style.display = 'none';
}

// Placeholder functions - these exist but need full implementation
function findTightBounds(img) {
  // Create temp canvas to analyze pixels
  const tempCanvas = document.createElement('canvas');
  tempCanvas.width = img.width;
  tempCanvas.height = img.height;
  const tempCtx = tempCanvas.getContext('2d');
  tempCtx.drawImage(img, 0, 0);
  const imageData = tempCtx.getImageData(0, 0, img.width, img.height);
  const pixels = imageData.data;
  
  let left = img.width, right = 0, top = img.height, bottom = 0;
  
  // Scan for non-transparent pixels (alpha > 10)
  for (let y = 0; y < img.height; y++) {
    for (let x = 0; x < img.width; x++) {
      const i = (y * img.width + x) * 4;
      const alpha = pixels[i + 3];
      if (alpha > 10) {  // non-transparent
        left = Math.min(left, x);
        right = Math.max(right, x + 1);
        top = Math.min(top, y);
        bottom = Math.max(bottom, y + 1);
      }
    }
  }
  
  // If no non-transparent pixels found, return full image
  if (left >= right || top >= bottom) {
    return {left: 0, top: 0, right: img.width, bottom: img.height};
  }
  
  return {left, top, right, bottom};
}

let _cropInImageCoords = null; // Store crop in image pixel coordinates

function redrawCanvas() {
  if (!_canvas || !_ctx || !_img) return;
  
  // Calculate canvas size to fit rotated image
  const angle = (_rotationDegrees * Math.PI) / 180;
  const cos = Math.abs(Math.cos(angle));
  const sin = Math.abs(Math.sin(angle));
  const rotatedWidth = _img.width * cos + _img.height * sin;
  const rotatedHeight = _img.width * sin + _img.height * cos;
  
  // Set canvas to fit the rotated image (with small padding)
  const canvasWidth = Math.ceil(rotatedWidth * 1.1);
  const canvasHeight = Math.ceil(rotatedHeight * 1.1);
  
  const canvasSizeChanged = (_canvas.width !== canvasWidth || _canvas.height !== canvasHeight);
  
  if (canvasSizeChanged) {
    // Before resizing, save crop in image coordinates
    if (_cropInImageCoords === null) {
      // First time - initialize from sliders
      const cropLeft = document.getElementById('crop-left');
      const cropTop = document.getElementById('crop-top');
      const cropRight = document.getElementById('crop-right');
      const cropBottom = document.getElementById('crop-bottom');
      
      if (cropLeft && cropTop && cropRight && cropBottom) {
        _cropInImageCoords = {
          left: parseFloat(cropLeft.value),
          top: parseFloat(cropTop.value),
          right: parseFloat(cropRight.value),
          bottom: parseFloat(cropBottom.value)
        };
      }
    }
    
    _canvas.width = canvasWidth;
    _canvas.height = canvasHeight;
    
    // Update sliders to maintain same visual crop
    if (_cropInImageCoords) {
      const cropLeft = document.getElementById('crop-left');
      const cropTop = document.getElementById('crop-top');
      const cropRight = document.getElementById('crop-right');
      const cropBottom = document.getElementById('crop-bottom');
      
      if (cropLeft && cropTop && cropRight && cropBottom) {
        // Crop stays as same percentage (they're already in %)
        cropLeft.value = _cropInImageCoords.left.toFixed(1);
        cropTop.value = _cropInImageCoords.top.toFixed(1);
        cropRight.value = _cropInImageCoords.right.toFixed(1);
        cropBottom.value = _cropInImageCoords.bottom.toFixed(1);
      }
    }
  }
  
  // Clear canvas
  _ctx.clearRect(0, 0, _canvas.width, _canvas.height);
  
  // Draw pink/green checkerboard background (20px grid)
  const gridSize = 20;
  for (let y = 0; y < _canvas.height; y += gridSize) {
    for (let x = 0; x < _canvas.width; x += gridSize) {
      const isEvenSquare = (Math.floor(x / gridSize) + Math.floor(y / gridSize)) % 2 === 0;
      _ctx.fillStyle = isEvenSquare ? '#f0f' : '#9f9';
      _ctx.fillRect(x, y, gridSize, gridSize);
    }
  }
  
  // Save context
  _ctx.save();
  
  // Translate to center
  _ctx.translate(_canvas.width / 2, _canvas.height / 2);
  
  // Apply rotation
  _ctx.rotate(angle);
  
  // Apply offset (for dragging)
  _ctx.translate(_imgOffset.x, _imgOffset.y);
  
  // Draw image centered
  _ctx.drawImage(_img, -_img.width / 2, -_img.height / 2);
  
  // Restore context
  _ctx.restore();
  
  // Draw crop overlay AFTER restoring (in canvas space, not rotated)
  // This keeps crop lines axis-aligned with canvas
  const cropLeft = document.getElementById('crop-left');
  const cropTop = document.getElementById('crop-top');
  const cropRight = document.getElementById('crop-right');
  const cropBottom = document.getElementById('crop-bottom');
  
  if (cropLeft && cropTop && cropRight && cropBottom) {
    const left = parseFloat(cropLeft.value) / 100 * _canvas.width;
    const top = parseFloat(cropTop.value) / 100 * _canvas.height;
    const right = parseFloat(cropRight.value) / 100 * _canvas.width;
    const bottom = parseFloat(cropBottom.value) / 100 * _canvas.height;
    
    // Crop rectangle (always axis-aligned)
    _ctx.strokeStyle = '#ff0000';
    _ctx.lineWidth = 2;
    _ctx.setLineDash([5, 5]);
    _ctx.strokeRect(left, top, right - left, bottom - top);
    _ctx.setLineDash([]);
    
    // Draw corner handles for resizing
    const handleSize = 10;
    _ctx.fillStyle = '#ff0000';
    // Top-left
    _ctx.fillRect(left - handleSize/2, top - handleSize/2, handleSize, handleSize);
    // Top-right  
    _ctx.fillRect(right - handleSize/2, top - handleSize/2, handleSize, handleSize);
    // Bottom-left
    _ctx.fillRect(left - handleSize/2, bottom - handleSize/2, handleSize, handleSize);
    // Bottom-right
    _ctx.fillRect(right - handleSize/2, bottom - handleSize/2, handleSize, handleSize);
    
    // Draw rotation handle (outside top-right corner)
    const rotateHandleX = right + 40;
    const rotateHandleY = top - 40;
    
    // Draw line from corner to handle
    _ctx.strokeStyle = '#00ff00';
    _ctx.lineWidth = 1;
    _ctx.beginPath();
    _ctx.moveTo(right, top);
    _ctx.lineTo(rotateHandleX, rotateHandleY);
    _ctx.stroke();
    
    // Draw rotation handle circle
    _ctx.beginPath();
    _ctx.arc(rotateHandleX, rotateHandleY, 12, 0, 2 * Math.PI);
    _ctx.fillStyle = '#00ff00';
    _ctx.fill();
    _ctx.strokeStyle = '#000';
    _ctx.lineWidth = 2;
    _ctx.stroke();
    
    // Draw rotation icon (curved arrow)
    _ctx.strokeStyle = '#000';
    _ctx.lineWidth = 2;
    _ctx.beginPath();
    _ctx.arc(rotateHandleX, rotateHandleY, 6, -Math.PI/4, Math.PI, false);
    _ctx.stroke();
    
    // Store handle position for mouse detection
    _rotationHandle = {x: rotateHandleX, y: rotateHandleY, radius: 12};
  }
}

let _rotationHandle = null;
let _isDraggingRotate = false;
let _dragStartAngle = 0;

function getCursorForPosition(x, y) {
  // Check if near rotation handle
  if (_rotationHandle) {
    const dx = x - _rotationHandle.x;
    const dy = y - _rotationHandle.y;
    if (Math.sqrt(dx*dx + dy*dy) <= _rotationHandle.radius + 10) {
      return 'crosshair'; // rotation cursor
    }
  }
  
  // Check if on crop edges
  const cropLeft = parseFloat(document.getElementById('crop-left').value) / 100 * _canvas.width;
  const cropTop = parseFloat(document.getElementById('crop-top').value) / 100 * _canvas.height;
  const cropRight = parseFloat(document.getElementById('crop-right').value) / 100 * _canvas.width;
  const cropBottom = parseFloat(document.getElementById('crop-bottom').value) / 100 * _canvas.height;
  
  const threshold = 5;
  
  // Left edge
  if (Math.abs(x - cropLeft) < threshold && y >= cropTop && y <= cropBottom) {
    return 'ew-resize';
  }
  // Right edge
  if (Math.abs(x - cropRight) < threshold && y >= cropTop && y <= cropBottom) {
    return 'ew-resize';
  }
  // Top edge
  if (Math.abs(y - cropTop) < threshold && x >= cropLeft && x <= cropRight) {
    return 'ns-resize';
  }
  // Bottom edge
  if (Math.abs(y - cropBottom) < threshold && x >= cropLeft && x <= cropRight) {
    return 'ns-resize';
  }
  
  return 'move'; // default for panning
}

function updateCropPreview() {
  redrawCanvas();
}

function resetCrop() {
  if (!_img) return;
  const bounds = findTightBounds(_img);
  document.getElementById('crop-left').value = (bounds.left / _img.width * 100).toFixed(1);
  document.getElementById('crop-top').value = (bounds.top / _img.height * 100).toFixed(1);
  document.getElementById('crop-right').value = (bounds.right / _img.width * 100).toFixed(1);
  document.getElementById('crop-bottom').value = (bounds.bottom / _img.height * 100).toFixed(1);
  updateCropPreview();
}

let _isDraggingCrop = false;
let _cropDragEdge = null;

function startDrag(e) {
  const rect = _canvas.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const y = e.clientY - rect.top;
  
  // Check rotation handle first
  if (_rotationHandle) {
    const dx = x - _rotationHandle.x;
    const dy = y - _rotationHandle.y;
    if (Math.sqrt(dx*dx + dy*dy) <= _rotationHandle.radius + 10) {
      _isDraggingRotate = true;
      const centerX = _canvas.width / 2;
      const centerY = _canvas.height / 2;
      _dragStartAngle = Math.atan2(y - centerY, x - centerX) * 180 / Math.PI - _rotationDegrees;
      e.preventDefault();
      return;
    }
  }
  
  // Check crop edges
  const cropLeft = parseFloat(document.getElementById('crop-left').value) / 100 * _canvas.width;
  const cropTop = parseFloat(document.getElementById('crop-top').value) / 100 * _canvas.height;
  const cropRight = parseFloat(document.getElementById('crop-right').value) / 100 * _canvas.width;
  const cropBottom = parseFloat(document.getElementById('crop-bottom').value) / 100 * _canvas.height;
  
  const threshold = 5;
  
  if (Math.abs(x - cropLeft) < threshold && y >= cropTop - threshold && y <= cropBottom + threshold) {
    _isDraggingCrop = true;
    _cropDragEdge = 'left';
    e.preventDefault();
    return;
  }
  if (Math.abs(x - cropRight) < threshold && y >= cropTop - threshold && y <= cropBottom + threshold) {
    _isDraggingCrop = true;
    _cropDragEdge = 'right';
    e.preventDefault();
    return;
  }
  if (Math.abs(y - cropTop) < threshold && x >= cropLeft - threshold && x <= cropRight + threshold) {
    _isDraggingCrop = true;
    _cropDragEdge = 'top';
    e.preventDefault();
    return;
  }
  if (Math.abs(y - cropBottom) < threshold && x >= cropLeft - threshold && x <= cropRight + threshold) {
    _isDraggingCrop = true;
    _cropDragEdge = 'bottom';
    e.preventDefault();
    return;
  }
  
  // Default: pan image
  _isDragging = true;
  _dragStart = {x: e.offsetX, y: e.offsetY};
}

function doDrag(e) {
  const rect = _canvas.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const y = e.clientY - rect.top;
  
  // Update cursor
  _canvas.style.cursor = getCursorForPosition(x, y);
  
  if (_isDraggingRotate) {
    // Rotate around center
    const centerX = _canvas.width / 2;
    const centerY = _canvas.height / 2;
    const currentAngle = Math.atan2(y - centerY, x - centerX) * 180 / Math.PI;
    _rotationDegrees = (currentAngle - _dragStartAngle + 360) % 360;
    const degInput = document.getElementById('rotation-degrees');
    if (degInput) degInput.value = _rotationDegrees.toFixed(1);
    redrawCanvas();
    return;
  }
  
  if (_isDraggingCrop) {
    // Drag crop edges
    const pctX = (x / _canvas.width * 100);
    const pctY = (y / _canvas.height * 100);
    
    if (_cropDragEdge === 'left') {
      document.getElementById('crop-left').value = Math.max(0, Math.min(pctX, parseFloat(document.getElementById('crop-right').value) - 1)).toFixed(1);
    } else if (_cropDragEdge === 'right') {
      document.getElementById('crop-right').value = Math.min(100, Math.max(pctX, parseFloat(document.getElementById('crop-left').value) + 1)).toFixed(1);
    } else if (_cropDragEdge === 'top') {
      document.getElementById('crop-top').value = Math.max(0, Math.min(pctY, parseFloat(document.getElementById('crop-bottom').value) - 1)).toFixed(1);
    } else if (_cropDragEdge === 'bottom') {
      document.getElementById('crop-bottom').value = Math.min(100, Math.max(pctY, parseFloat(document.getElementById('crop-top').value) + 1)).toFixed(1);
    }
    redrawCanvas();
    return;
  }
  
  if (_isDragging) {
    // Pan image
    _imgOffset.x += e.offsetX - _dragStart.x;
    _imgOffset.y += e.offsetY - _dragStart.y;
    _dragStart = {x: e.offsetX, y: e.offsetY};
    redrawCanvas();
  }
}

function endDrag() {
  _isDragging = false;
  _isDraggingRotate = false;
  _isDraggingCrop = false;
  _cropDragEdge = null;
}

async function confirmCrop() {
  document.getElementById('crop-status').textContent = 'Saving...';
  // TODO: Send crop parameters to server
  alert('Crop/save implementation pending');
}
