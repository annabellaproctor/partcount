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
      _initCropperInstance();
    })
    .catch(err => {
      console.error('Failed to proxy image:', err);
      alert('Failed to load image: ' + err.message);
    });
  } else {
    img.src = imageUrl;
    _initCropperInstance();
  }
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

// Get final cropped/rotated image
async function getCroppedImage() {
  if (!cropper) {
    alert('Cropper not initialized');
    return null;
  }
  
  const canvas = cropper.getCroppedCanvas({
    width: 1024, // Max width
    height: 1024, // Max height
    imageSmoothingEnabled: true,
    imageSmoothingQuality: 'high',
  });
  
  return canvas.toDataURL('image/png');
}

// Save cropped image
async function saveCroppedImage() {
  const dataUrl = await getCroppedImage();
  if (!dataUrl) return;
  
  const compId = window.location.pathname.split('/').pop();
  
  // Convert data URL to blob
  const response = await fetch(dataUrl);
  const blob = await response.blob();
  
  // Create form data
  const formData = new FormData();
  formData.append('file', blob, 'cropped.png');
  
  // Upload
  const uploadResponse = await fetch(`/api/images/upload/${compId}`, {
    method: 'POST',
    body: formData
  });
  
  if (uploadResponse.ok) {
    const result = await uploadResponse.json();
    alert('Image saved!');
    location.reload(); // Reload to show new image
  } else {
    alert('Failed to save image');
  }
}
