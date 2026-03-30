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
      document.getElementById('img-modal').style.display = 'none';
      document.getElementById('bg-removal-dialog').style.display = 'flex';
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
  
  // Draw image to canvas
  const tempImg = new Image();
  tempImg.crossOrigin = 'anonymous';
  tempImg.onload = () => {
    canvas.width = Math.min(tempImg.width, 400);
    canvas.height = Math.min(tempImg.height, 400);
    ctx.drawImage(tempImg, 0, 0, canvas.width, canvas.height);
    dialog.style.display = 'flex';
  };
  tempImg.src = _currentImageData.preview;
  
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
  document.getElementById('crop-modal').style.display = 'block';
  
  // Show/hide bg removal UI
  const bgApplied = document.getElementById('bg-applied');
  const bgNotApplied = document.getElementById('bg-not-applied');
  if (_removeBgChoice === 'mechanical' || _removeBgChoice === 'ai') {
    bgNotApplied.style.display = 'none';
    bgApplied.style.display = 'block';
    document.getElementById('bg-method').textContent = _removeBgChoice;
  } else {
    bgNotApplied.style.display = 'block';
    bgApplied.style.display = 'none';
  }
  
  _canvas = document.getElementById('crop-canvas');
  _ctx = _canvas.getContext('2d');
  _img = new Image();
  _img.crossOrigin = 'anonymous';
  
  _img.onload = () => {
    _canvas.width = _img.width;
    _canvas.height = _img.height;
    
    const tightBounds = findTightBounds(_img);
    
    document.getElementById('crop-left').value = (tightBounds.left / _img.width * 100).toFixed(1);
    document.getElementById('crop-top').value = (tightBounds.top / _img.height * 100).toFixed(1);
    document.getElementById('crop-right').value = (tightBounds.right / _img.width * 100).toFixed(1);
    document.getElementById('crop-bottom').value = (tightBounds.bottom / _img.height * 100).toFixed(1);
    
    _imgOffset = {x: 0, y: 0};
    _rotationDegrees = 0;
    document.getElementById('rotation-degrees').value = 0;
    redrawCanvas();
    
    _canvas.onmousedown = startDrag;
    _canvas.onmousemove = doDrag;
    _canvas.onmouseup = endDrag;
    _canvas.onmouseleave = endDrag;
  };
  
  _img.src = data.preview;
}

function closeCropModal() {
  document.getElementById('crop-modal').style.display = 'none';
}

function autoAdjust() {
  alert('Auto-rotate & crop algorithm coming soon. Will detect edges and straighten image.');
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
  return {left: 0, top: 0, right: img.width, bottom: img.height};
}

function redrawCanvas() {
  if (!_canvas || !_ctx || !_img) return;
  _ctx.clearRect(0, 0, _canvas.width, _canvas.height);
  _ctx.drawImage(_img, 0, 0);
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

function startDrag(e) {
  _isDragging = true;
  _dragStart = {x: e.offsetX, y: e.offsetY};
}

function doDrag(e) {
  if (!_isDragging) return;
  _imgOffset.x += e.offsetX - _dragStart.x;
  _imgOffset.y += e.offsetY - _dragStart.y;
  _dragStart = {x: e.offsetX, y: e.offsetY};
  redrawCanvas();
}

function endDrag() {
  _isDragging = false;
}

async function confirmCrop() {
  document.getElementById('crop-status').textContent = 'Saving...';
  // TODO: Send crop parameters to server
  alert('Crop/save implementation pending');
}
