// Cropper.js Integration for Lab Inventory
// Replace custom canvas-based editor with battle-tested library

let cropper = null;
let _originalImageSrcForBg = null;

// Initialize Cropper.js when image is loaded
function initCropper(imageUrl) {
  const img = document.getElementById('crop-image');
  
  // Destroy existing cropper if any
  if (cropper) {
    cropper.destroy();
    cropper = null;
  }
  
  // Load image (proxy external URLs)
  const isExternal = (
    imageUrl.startsWith('http://') || imageUrl.startsWith('https://')
  ) && !imageUrl.includes(window.location.hostname) && !imageUrl.startsWith('data:');
  
  if (isExternal) {
    // Proxy through server for CORS
    fetch('/api/images/proxy', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({image_url: imageUrl})
    })
    .then(r => r.json())
    .then(data => {
      img.src = data.data_url;
    })
    .catch(err => {
      console.error('Failed to proxy image:', err);
      alert('Failed to load image: ' + err.message);
    });
  } else {
    img.src = imageUrl;
  }
  
  // Initialize cropper after image loads
  img.onload = () => {
    cropper = new Cropper(img, {
      viewMode: 1,
      dragMode: 'move',
      aspectRatio: NaN,
      autoCropArea: 0.8,
      restore: false,
      guides: true,
      center: true,
      highlight: false,
      cropBoxMovable: true,
      cropBoxResizable: true,
      toggleDragModeOnDblclick: false,
      background: false,
      responsive: true,
      checkCrossOrigin: false,
    });
    
    _originalImageSrcForBg = img.src;
    console.log('Cropper initialized');
  };
}

function _initCropperInstance() {
  const img = document.getElementById('crop-image');
  
  img.onload = () => {
    cropper = new Cropper(img, {
      viewMode: 1, // Restrict crop box to canvas
      dragMode: 'move', // Drag to pan image
      aspectRatio: NaN, // Free aspect ratio
      autoCropArea: 0.8, // Initial crop area
      restore: false,
      guides: true,
      center: true,
      highlight: false,
      cropBoxMovable: true,
      cropBoxResizable: true,
      toggleDragModeOnDblclick: false,
      background: false, // Use CSS checkerboard instead
      responsive: true,
      checkCrossOrigin: false, // We handle CORS via proxy
    });
    
    _originalImageSrcForBg = img.src;
    console.log('Cropper initialized');
  };
}

// Rotation controls
function cropperRotate(degrees) {
  if (cropper) {
    cropper.rotate(degrees);
  }
}

// Zoom controls  
function cropperZoom(ratio) {
  if (cropper) {
    cropper.zoom(ratio);
  }
}

// Reset
function cropperReset() {
  if (cropper) {
    cropper.reset();
  }
}

// Background removal - apply to cropper image
function applyMechanicalBg() {
  if (!cropper) return;
  
  const canvas = cropper.getCroppedCanvas();
  const ctx = canvas.getContext('2d');
  const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const data = imageData.data;
  
  // Remove white/light pixels (threshold: RGB > 200)
  for (let i = 0; i < data.length; i += 4) {
    const r = data[i], g = data[i+1], b = data[i+2];
    if (r > 200 && g > 200 && b > 200) {
      data[i+3] = 0; // Transparent
    }
  }
  
  ctx.putImageData(imageData, 0, 0);
  
  // Replace cropper image with processed version
  const newDataUrl = canvas.toDataURL();
  document.getElementById('crop-image').src = newDataUrl;
  cropper.replace(newDataUrl);
  
  // Update UI
  document.getElementById('bg-not-applied').style.display = 'none';
  document.getElementById('bg-applied').style.display = 'block';
  document.getElementById('bg-method').textContent = 'mechanical';
}

function revertBackgroundRemoval() {
  if (!cropper || !_originalImageSrcForBg) return;
  
  document.getElementById('crop-image').src = _originalImageSrcForBg;
  cropper.replace(_originalImageSrcForBg);
  
  document.getElementById('bg-not-applied').style.display = 'block';
  document.getElementById('bg-applied').style.display = 'none';
}

// Auto-crop function
function cropperAutoCrop() {
  if (!cropper) return;

  // Build a working canvas that applies current rotate/flip, so auto-crop reflects edits.
  const imageData = cropper.getImageData();
  const rotate = imageData.rotate || 0;
  const rad = (rotate * Math.PI) / 180;
  const absCos = Math.abs(Math.cos(rad));
  const absSin = Math.abs(Math.sin(rad));
  const srcW = Math.max(1, Math.round(imageData.naturalWidth || 1));
  const srcH = Math.max(1, Math.round(imageData.naturalHeight || 1));
  const workW = Math.max(1, Math.ceil(srcW * absCos + srcH * absSin));
  const workH = Math.max(1, Math.ceil(srcW * absSin + srcH * absCos));

  const tempCanvas = document.createElement('canvas');
  tempCanvas.width = workW;
  tempCanvas.height = workH;
  const tempCtx = tempCanvas.getContext('2d');

  const img = document.getElementById('crop-image');
  tempCtx.translate(workW / 2, workH / 2);
  tempCtx.rotate(rad);
  tempCtx.scale(imageData.scaleX || 1, imageData.scaleY || 1);
  tempCtx.drawImage(img, -srcW / 2, -srcH / 2, srcW, srcH);

  // Get pixel data from transformed canvas.
  const pixels = tempCtx.getImageData(0, 0, tempCanvas.width, tempCanvas.height);
  const data = pixels.data;
  
  // Find bounds (non-white pixels)
  let minX = tempCanvas.width, minY = tempCanvas.height, maxX = 0, maxY = 0;
  let found = false;
  
  for (let y = 0; y < tempCanvas.height; y++) {
    for (let x = 0; x < tempCanvas.width; x++) {
      const i = (y * tempCanvas.width + x) * 4;
      const r = data[i], g = data[i+1], b = data[i+2], a = data[i+3];
      
      // Check if not white/transparent (threshold for slight grays too)
      if (a > 25 && (r < 240 || g < 240 || b < 240)) {
        found = true;
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  
  if (!found) {
    alert('No content found to crop');
    return;
  }
  
  // Add small padding (2%)
  const paddingX = (maxX - minX) * 0.02;
  const paddingY = (maxY - minY) * 0.02;
  
  minX = Math.max(0, minX - paddingX);
  minY = Math.max(0, minY - paddingY);
  maxX = Math.min(tempCanvas.width, maxX + paddingX);
  maxY = Math.min(tempCanvas.height, maxY + paddingY);
  
  // Map working-canvas bounds into current cropper canvas coordinates.
  const canvasData = cropper.getCanvasData();
  const left = canvasData.left + (minX / tempCanvas.width) * canvasData.width;
  const top = canvasData.top + (minY / tempCanvas.height) * canvasData.height;
  const width = ((maxX - minX) / tempCanvas.width) * canvasData.width;
  const height = ((maxY - minY) / tempCanvas.height) * canvasData.height;

  cropper.setCropBoxData({ left, top, width, height });
  
  console.log('Auto-cropped to:', {minX, minY, maxX, maxY});
}

// Get final cropped/rotated image
async function getCroppedImage() {
  if (!cropper) {
    alert('Cropper not initialized');
    return null;
  }
  
  const canvas = cropper.getCroppedCanvas({
    maxWidth: 2048,
    maxHeight: 2048,
    imageSmoothingEnabled: true,
    imageSmoothingQuality: 'high',
  });
  
  return canvas.toDataURL('image/png');
}

// Save cropped image
async function saveCroppedImage() {
  if (!cropper) {
    alert('No image loaded');
    return;
  }
  
  const dataUrl = await getCroppedImage();
  if (!dataUrl) return;
  
  // Prefer UUID component id emitted by template; fallback to path segment.
  const compId = (typeof COMP_ID !== 'undefined' && COMP_ID)
    ? COMP_ID
    : window.location.pathname.split('/').pop();
  
  // Convert data URL to blob
  const response = await fetch(dataUrl);
  const blob = await response.blob();
  
  // Create form data
  const formData = new FormData();
  formData.append('file', blob, 'cropped.png');
  
  try {
    // Upload
    const uploadResponse = await fetch(`/api/images/upload/${compId}`, {
      method: 'POST',
      body: formData
    });
    
    if (uploadResponse.ok) {
      alert('Image saved!');
      location.reload();
    } else {
      const error = await uploadResponse.text();
      console.error('Upload failed:', error);
      alert('Failed to save image: ' + uploadResponse.status + ' ' + error);
    }
  } catch (err) {
    console.error('Save error:', err);
    alert('Failed to save image: ' + err.message);
  }
}

